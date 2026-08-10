"""现有设备广场模板协议 ``legacy-template-package/v1`` 的 HTTP Adapter。"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
from uuid import UUID

import requests

from ..errors import PackageTransferError
from ..transfer_models import PackageDownloadRequest, PackageReleaseDescriptor
from ..wheel import MAX_ARCHIVE_BYTES
from .legacy_resolver import (
    descriptor_for_package,
    descriptor_for_template,
    find_release,
    package_summaries,
)

LEGACY_CAPABILITY = "legacy-template-package/v1"


class LegacyTemplateBackendAdapter:
    """把遗留模板 JSONB 和公开下载路由封装为包传输 Interface。"""

    def __init__(
        self,
        base_url: str,
        *,
        auth_secret: str = "",
        session: requests.Session | None = None,
    ) -> None:
        """固定一次命令的 Backend 根地址和短生命周期鉴权。

        参数：``base_url`` 是完整 ``/api/v1`` 根；``auth_secret`` 是短生命周期
        ``base64(ak:sk)``；``session`` 仅供测试或连接池注入。
        返回：无。
        异常：地址含 userinfo、query、fragment、错误 scheme 或缺少 ``/api/v1`` 时
        抛出 ``PackageTransferError``。
        """

        self.base_url, self.allow_http = _validate_base_url(base_url)
        self._auth_secret = auth_secret
        self._session = session or requests.Session()
        self._owns_session = session is None

    def close(self) -> None:
        """关闭由 Adapter 自己创建的短生命周期 HTTP 会话。

        参数：无。
        返回：无。
        异常：会话关闭异常由 requests 传播。
        """

        if self._owns_session:
            self._session.close()

    def probe(self) -> str:
        """只读证明设备广场包列表路由与信封兼容。

        参数：无。
        返回：固定 ``legacy-template-package/v1`` 能力名。
        异常：路由、HTTP、JSON、业务码或字段不兼容时抛出
        ``backend_incompatible``。
        """

        try:
            package_summaries(self._request_envelope)
        except PackageTransferError as error:
            raise PackageTransferError(
                "backend_incompatible",
                "目标 Backend 不支持 legacy-template-package/v1 包协议",
                retryable=False,
            ) from error
        return LEGACY_CAPABILITY

    def resolve(
        self,
        request: PackageDownloadRequest,
    ) -> PackageReleaseDescriptor:
        """解析模板 UUID 或包名为唯一远端发布描述。

        参数：``request`` 是互斥选择请求。
        返回：不暴露模板 JSONB 的 Backend 无关发布描述。
        异常：模板不存在、包发布混杂或必要字段缺失时抛出稳定
        ``PackageTransferError``。
        """

        if request.template_uuid:
            return descriptor_for_template(self._request_envelope, request.template_uuid)
        assert request.package_name is not None
        return descriptor_for_package(
            self._request_envelope,
            request.package_name,
            request.version,
        )

    def find_release(
        self,
        distribution: str,
        version: str,
    ) -> PackageReleaseDescriptor | None:
        """只读查找同名同版本发布，供上传不可变预检。

        参数：``distribution`` 是发行名；``version`` 是待发布版本。
        返回：不存在包名或版本时返回 ``None``，否则返回唯一描述。
        异常：已存在包的模板字段混杂或缺失时关闭式失败。
        """

        return find_release(self._request_envelope, distribution, version)

    def upload_release_artifact(
        self,
        wheel: str | Path,
        *,
        normalized_name: str,
        version: str,
    ) -> tuple[str, str]:
        """申请 ``scene=file`` token 并把同一 wheel 流式直传 OSS。

        参数：``wheel`` 是已审计 Artifact；``normalized_name`` 和 ``version`` 用于
        可读对象子路径。
        返回：遗留公开地址和对象键；对象键必须存在。
        异常：鉴权、token 信封、签名字段或 PUT 失败时抛出稳定传输错误；OSS
        请求不携带 Lab Authorization 且禁止重定向。
        """

        self._require_auth()
        wheel_path = Path(wheel)
        data = self._request_envelope(
            "GET",
            "/lab/storage/token",
            authenticated=True,
            params={
                "scene": "file",
                "filename": wheel_path.name,
                "content_type": "application/octet-stream",
                "sub_path": f"packages/{normalized_name}/{version}",
            },
            timeout=(5, 30),
            incompatible_on_shape=True,
        )
        if not isinstance(data, dict):
            raise _backend_incompatible("storage token data 必须是对象")
        put_url = data.get("url")
        object_key = data.get("path")
        public_url = data.get("public_url") or ""
        content_type = data.get("content_type") or "application/octet-stream"
        if not isinstance(put_url, str) or not put_url:
            raise _backend_incompatible("storage token 缺少 url")
        if not isinstance(object_key, str) or not object_key:
            raise _backend_incompatible("storage token 缺少 path")
        if content_type != "application/octet-stream":
            raise _backend_incompatible("storage token Content-Type 不兼容")
        try:
            with wheel_path.open("rb") as stream:
                response = requests.put(
                    put_url,
                    data=stream,
                    headers={"Content-Type": content_type},
                    allow_redirects=False,
                    timeout=(5, 120),
                )
        except requests.RequestException as error:
            raise PackageTransferError(
                "artifact_upload_failed",
                "设备软件包 Artifact 直传失败",
                retryable=True,
                details={"artifact_uploaded": False},
            ) from error
        if response.status_code not in (200, 201):
            raise PackageTransferError(
                "artifact_upload_failed",
                f"设备软件包 Artifact 直传失败（HTTP {response.status_code}）",
                retryable=response.status_code >= 500,
                details={"artifact_uploaded": False},
            )
        return str(public_url), object_key

    def publish_resources(
        self,
        resources: list[dict[str, Any]],
        package_info: dict[str, Any],
    ) -> None:
        """通过现有 ``/lab/resource`` 发布模板兼容投影。

        参数：``resources`` 是设备与资源模板；``package_info`` 是同一 wheel 身份。
        返回：无；同时满足 HTTP 与 ``code == 0`` 才成功。
        异常：传输、非法 JSON 或业务码失败时抛出稳定错误。
        """

        self._require_auth()
        body = gzip.compress(
            json.dumps(
                {"package_info": package_info, "resources": resources},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        self._request_envelope(
            "POST",
            "/lab/resource",
            authenticated=True,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            },
            timeout=(5, 60),
        )

    def download_artifact(self, template_uuid: str, target: Path) -> None:
        """经 Backend 公开 route 和最多一次安全 302 流式下载 wheel。

        参数：``template_uuid`` 是 Backend 详情重新取得的模板 UUID；``target`` 是
        缓存拥有的空临时文件。
        返回：无；完整下载写入目标。
        异常：UUID、302、scheme、大小或网络失败时抛出稳定错误；跳转请求不携带
        Authorization、Cookie 或追踪头。
        """

        try:
            normalized_uuid = str(UUID(template_uuid))
        except (ValueError, TypeError) as error:
            raise PackageTransferError(
                "remote_package_incompatible",
                "Backend 返回了无效模板 UUID",
                retryable=False,
            ) from error
        route = f"/lab/square/packages/releases/{normalized_uuid}/download"
        try:
            response = self._session.get(
                self.base_url + route,
                allow_redirects=False,
                timeout=(5, 30),
            )
        except requests.RequestException as error:
            raise PackageTransferError(
                "package_download_failed",
                "设备软件包下载路由不可达",
                retryable=True,
            ) from error
        if response.status_code != 302:
            raise PackageTransferError(
                "backend_incompatible",
                f"设备软件包下载路由未返回 302（HTTP {response.status_code}）",
                retryable=False,
            )
        location = response.headers.get("Location", "")
        redirect_url = urljoin(self.base_url + route, location)
        parsed = urlsplit(redirect_url)
        allowed_schemes = {"https", "http"} if self.allow_http else {"https"}
        if parsed.scheme not in allowed_schemes or not parsed.netloc:
            raise PackageTransferError(
                "remote_package_incompatible",
                "设备软件包下载跳转地址不安全",
                retryable=False,
            )
        bare_session = requests.Session()
        try:
            with bare_session.get(
                redirect_url,
                stream=True,
                allow_redirects=False,
                headers={"Accept": "application/octet-stream"},
                timeout=(5, 120),
            ) as artifact_response:
                if artifact_response.status_code != 200:
                    raise PackageTransferError(
                        "package_download_failed",
                        f"Artifact 下载失败（HTTP {artifact_response.status_code}）",
                        retryable=artifact_response.status_code >= 500,
                    )
                declared_size = artifact_response.headers.get("Content-Length")
                if declared_size and int(declared_size) > MAX_ARCHIVE_BYTES:
                    raise PackageTransferError(
                        "remote_package_incompatible",
                        "远端 wheel 超过归档大小上限",
                        retryable=False,
                    )
                total = 0
                with target.open("wb") as stream:
                    for chunk in artifact_response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_ARCHIVE_BYTES:
                            raise PackageTransferError(
                                "remote_package_incompatible",
                                "远端 wheel 超过归档大小上限",
                                retryable=False,
                            )
                        stream.write(chunk)
        except PackageTransferError:
            raise
        except (requests.RequestException, OSError, ValueError) as error:
            raise PackageTransferError(
                "package_download_failed",
                "设备软件包 Artifact 流式下载失败",
                retryable=True,
            ) from error
        finally:
            bare_session.close()

    def _request_envelope(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = False,
        incompatible_on_shape: bool = False,
        **kwargs: Any,
    ) -> Any:
        """发送同环境请求并严格解开 ``{code,data}`` 信封。

        参数：``method`` 和 ``path`` 标识 Backend 调用；``authenticated`` 控制
        Lab 头；``incompatible_on_shape`` 把协议形状失败归为能力不兼容；``kwargs``
        是 requests 参数。
        返回：信封 ``data``。
        异常：HTTP、JSON 或业务码失败时抛出不含响应正文的稳定错误。
        """

        headers = dict(kwargs.pop("headers", {}) or {})
        if authenticated:
            self._require_auth()
            headers["Authorization"] = f"Lab {self._auth_secret}"
        try:
            response = self._session.request(
                method,
                self.base_url + path,
                headers=headers,
                allow_redirects=False,
                **kwargs,
            )
        except requests.RequestException as error:
            raise PackageTransferError(
                "remote_request_failed",
                "设备包 Backend 请求失败",
                retryable=True,
            ) from error
        if response.status_code not in (200, 201):
            code = "backend_incompatible" if incompatible_on_shape else "remote_request_failed"
            raise PackageTransferError(
                code,
                f"设备包 Backend 请求失败（HTTP {response.status_code}）",
                retryable=response.status_code >= 500,
            )
        try:
            envelope = response.json()
        except (ValueError, json.JSONDecodeError) as error:
            raise _backend_incompatible("Backend 响应不是 JSON 信封") from error
        if not isinstance(envelope, dict) or "code" not in envelope:
            raise _backend_incompatible("Backend 响应缺少 code")
        if envelope["code"] != 0:
            raise PackageTransferError(
                "remote_business_error",
                f"设备包 Backend 返回业务错误码 {envelope['code']}",
                retryable=False,
            )
        return envelope.get("data")

    def _require_auth(self) -> None:
        """要求上传阶段已经提供完整短生命周期鉴权。

        参数：无。
        返回：无。
        异常：缺失凭据时抛出 ``authentication_required``。
        """

        if not self._auth_secret:
            raise PackageTransferError(
                "authentication_required",
                "package upload 需要 login session、--auth-stdin 或显式 AK/SK",
                retryable=False,
            )


def _validate_base_url(value: str) -> tuple[str, bool]:
    """验证并规范化本次命令唯一 Backend API 根。

    参数：``value`` 是环境解析器给出的完整地址。
    返回：去尾斜杠地址，以及是否允许本地 HTTP 下载跳转。
    异常：地址不安全或缺少 ``/api/v1`` 时抛出稳定错误。
    """

    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.rstrip("/").endswith("/api/v1")
    ):
        raise PackageTransferError(
            "invalid_environment",
            "设备包 Backend 地址必须是无 userinfo/query/fragment 的完整 /api/v1 根",
            retryable=False,
        )
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise PackageTransferError(
            "invalid_environment",
            "只有显式本地测试地址允许使用 HTTP",
            retryable=False,
        )
    return value.rstrip("/"), parsed.scheme == "http"


def _backend_incompatible(reason: str) -> PackageTransferError:
    """创建一个不泄漏响应正文的协议不兼容错误。

    参数：``reason`` 是本地字段检查说明。
    返回：稳定 ``backend_incompatible`` 错误。
    异常：无。
    """

    return PackageTransferError(
        "backend_incompatible",
        f"目标 Backend 与 {LEGACY_CAPABILITY} 不兼容：{reason}",
        retryable=False,
    )


__all__ = ["LEGACY_CAPABILITY", "LegacyTemplateBackendAdapter"]
