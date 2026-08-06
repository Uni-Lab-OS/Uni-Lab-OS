"""设备包发布专用的最小 Lab/OSS HTTP 传输。"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests


class PackagePublicationHttpClient:
    """只实现设备包 Artifact 上传与 ResourceTemplate 发布的短生命周期客户端。"""

    def __init__(
        self,
        *,
        base_url: str,
        auth_secret: str,
        working_dir: str | Path,
        session: requests.Session | None = None,
    ) -> None:
        """创建不记录凭据的设备包发布客户端。

        参数 ``base_url`` 是已选择环境的 Backend API 根，``auth_secret`` 是只用于
        Authorization 头的 ``base64(ak:sk)``，``working_dir`` 保存不含凭据的上传
        请求/响应诊断，``session`` 允许测试注入。构造函数无返回值；非法地址或空
        鉴权载荷会抛出 :class:`ValueError`。
        """

        if not auth_secret:
            raise ValueError("设备包上传鉴权信息为空")
        self.base_url = _normalize_base_url(base_url)
        self.working_dir = Path(working_dir).expanduser().resolve()
        self.working_dir.mkdir(parents=True, exist_ok=True)
        self._session = session or requests.Session()
        self._session.headers.update({"Authorization": f"Lab {auth_secret}"})

    def upload_file_to_oss(
        self,
        file_path: str,
        scene: str = "models",
    ) -> tuple[str, str]:
        """取得现有 storage token 并把一个 wheel 直传 OSS。

        参数 ``file_path`` 是已审计 Artifact，``scene`` 是现有 token 场景。返回
        ``(public_url, object_key)``；token 或 PUT 失败、响应缺失 URL 时抛出
        :class:`RuntimeError`。OSS PUT 使用裸请求，不携带 Lab Authorization。
        """

        artifact = Path(file_path).resolve()
        content_type = "application/gzip"
        try:
            token_response = self._session.get(
                f"{self.base_url}/lab/storage/token",
                params={
                    "scene": scene,
                    "filename": artifact.name,
                    "content_type": content_type,
                },
                timeout=30,
            )
        except requests.RequestException as error:
            raise RuntimeError("获取存储 token 失败：网络异常") from error
        if token_response.status_code != 200:
            raise RuntimeError(
                f"获取存储 token 失败：{token_response.status_code} "
                f"{token_response.text}"
            )
        payload = token_response.json()
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            data = {}
        put_url = str(data.get("url") or "")
        object_key = str(data.get("path") or "")
        public_url = str(data.get("public_url") or "")
        signed_content_type = str(data.get("content_type") or content_type)
        if not put_url:
            raise RuntimeError("存储 token 响应缺少预签名 url")
        try:
            with artifact.open("rb") as artifact_stream:
                put_response = requests.put(
                    put_url,
                    data=artifact_stream,
                    headers={"Content-Type": signed_content_type},
                    timeout=120,
                )
        except requests.RequestException as error:
            raise RuntimeError("OSS 直传失败：网络异常") from error
        if put_response.status_code not in (200, 201):
            raise RuntimeError(
                f"OSS 直传失败：{put_response.status_code} {put_response.text}"
            )
        return public_url, object_key

    def upload_package_resources(
        self,
        resources: list[dict[str, Any]],
        package_info: dict[str, Any],
    ) -> requests.Response:
        """复用现有 ``POST /lab/resource`` 发布 PackageCatalog 派生资源。

        参数 ``resources`` 是 Catalog 派生的设备/资源模板，``package_info`` 是
        Artifact 与 Catalog 身份。返回原始 HTTP 响应供发布层判断；请求和响应会
        写入受管诊断文件，但 Authorization 不在文件正文中。
        """

        body = {"package_info": package_info, "resources": resources}
        request_path = self.working_dir / "req_package_upload.json"
        request_path.write_text(
            json.dumps(body, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            response = self._session.post(
                f"{self.base_url}/lab/resource",
                data=gzip.compress(
                    json.dumps(body, ensure_ascii=False, separators=(",", ":"))
                    .encode("utf-8")
                ),
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                },
                timeout=60,
            )
        except requests.RequestException as error:
            raise RuntimeError("发布设备包资源失败：网络异常") from error
        response_path = self.working_dir / "res_package_upload.json"
        response_path.write_text(
            f"{response.status_code}\n{response.text}",
            encoding="utf-8",
        )
        return response

    def close(self) -> None:
        """关闭当前上传进程持有的 HTTP 连接池；函数无参数和返回值。"""

        self._session.close()


def _normalize_base_url(value: str) -> str:
    """校验设备包发布 API 根地址并移除尾部斜杠。

    参数 ``value`` 是 CLI ``--addr`` 解析结果。返回不含凭据、query、fragment 的
    HTTP(S) URL；非法输入抛出 :class:`ValueError`。
    """

    candidate = value.strip()
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("设备包上传地址必须是无凭据、query 和 fragment 的 HTTP(S) URL")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


__all__ = ["PackagePublicationHttpClient"]
