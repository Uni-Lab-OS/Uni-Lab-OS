"""规范检查产物到既有云端广场 HTTP 合同的发布 Adapter。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from unilabos.utils.banner_print import print_status

from ...errors import PackageCLIError


class PublicationPort(Protocol):
    """发布已经检查的软件包产物所需的最小传输 Interface。"""

    def upload_artifact(self, path: str) -> tuple[str, str]:
        """上传归档并返回公开地址和可选对象键。

        参数：``path`` 是本地已经检查的软件包归档路径。
        返回：公开下载地址与存储对象键；至少一项必须非空。
        异常：传输失败时由 Adapter 抛出原始异常，编排层负责归一化。
        """

        ...

    def publish_resources(
        self,
        resources: list[dict[str, Any]],
        package_info: dict[str, Any],
    ) -> Any:
        """发布资源模板投影及其软件包身份。

        参数：``resources`` 是检查产物生成的兼容资源 DTO；``package_info`` 是
        同一归档的软件包身份和下载信息。
        返回：携带 HTTP 状态与响应文本的传输结果对象。
        异常：网络或鉴权失败时由具体 Adapter 传播。
        """

        ...


class HttpClientPublicationAdapter:
    """把现有通用 HTTP 客户端适配为软件包发布传输 Interface。"""

    def __init__(self, http_client: Any) -> None:
        """固定已鉴权的现有 HTTP 客户端。

        参数：``http_client`` 提供 OSS 直传和资源发布两个既有操作。
        返回：无。
        异常：无；客户端能力错误会在实际调用时传播并由发布编排归一化。
        """

        # ``_http_client`` 是云端广场调用的唯一传输 Adapter，不进入目录编译。
        self._http_client = http_client

    def upload_artifact(self, path: str) -> tuple[str, str]:
        """通过现有预签名接口上传软件包归档。

        参数：``path`` 是本地归档路径。
        返回：公开下载地址与对象键。
        异常：网络、鉴权和服务端异常原样传播，由发布编排转为 ``PackageCLIError``。
        """

        return self._http_client.upload_file_to_oss(path, scene="models")

    def publish_resources(
        self,
        resources: list[dict[str, Any]],
        package_info: dict[str, Any],
    ) -> Any:
        """通过现有资源上传接口发布兼容投影。

        参数：``resources`` 是资源 DTO；``package_info`` 是同一软件包发布信息。
        返回：现有 HTTP 客户端的响应对象。
        异常：网络、鉴权和服务端异常原样传播。
        """

        return self._http_client.upload_package_resources(resources, package_info)


def publish_inspection(
    inspection: Mapping[str, Any],
    port: PublicationPort,
    *,
    download_url: str = "",
) -> dict[str, Any]:
    """发布一次已经完成目录编译和归档构建的检查产物。

    参数：``inspection`` 是 ``inspect_package`` 的冻结结果；``port`` 是注入的
    传输 Adapter；``download_url`` 是可选的调用者指定归档地址。
    返回：发布后的软件包信息、资源 DTO、下载地址和 HTTP 状态。
    异常：归档上传没有身份或资源接口返回非成功状态时抛出
    ``PackageCLIError``；``port.publish_resources`` 的传输异常保持原对象和类型
    传播。函数不会重新扫描工作区或重建包目录（PackageCatalog）。
    """

    # ``package_info`` 是检查产物生成的兼容软件包投影，本发布独占一份容器。
    package_info = dict(inspection["package_info"])
    # ``archive_path`` 是已检查且与当前包目录对应的本地归档路径。
    archive_path = str(inspection["archive_path"])
    # ``final_url`` 与 ``object_key`` 共同标识本次发布可引用的同一归档产物。
    final_url, object_key = _resolve_download_target(
        port,
        archive_path,
        download_url,
    )
    package_info["download_url"] = final_url
    if object_key:
        package_info["oss_object_key"] = object_key

    # ``resources`` 是本次发布独占的兼容资源 DTO，不是新的目录权威。
    resources = [dict(item) for item in inspection["resources"]]
    for item in resources:
        item["package_info"] = package_info

    # ``response`` 是云端资源发布调用的原始传输结果；传输异常必须原样传播。
    response = port.publish_resources(resources, package_info)
    # ``status`` 与 ``response_text`` 维持历史 HTTP 成功和诊断合同。
    status = getattr(response, "status_code", None)
    response_text = getattr(response, "text", "")
    if status not in (200, 201):
        raise PackageCLIError(f"上传 /lab/resource 失败：{status} {response_text}")
    return {
        "package_info": package_info,
        "resources": resources,
        "download_url": final_url,
        "response_status": status,
    }


def upload_package(
    path: str,
    http_client: Any,
    namespace: str | None = None,
    out_dir: str | None = None,
    download_url: str = "",
) -> dict[str, Any]:
    """检查软件包并把兼容资源投影发布到远端。

    参数：``path`` 是软件包根；``http_client`` 是已鉴权的 HTTP Adapter；
    ``namespace`` 仅供遗留包使用；``out_dir`` 是可选产物目录；
    ``download_url`` 是可选显式归档地址。
    返回：发布后的 ``package_info``、资源 DTO、下载地址和 HTTP 状态。
    异常：鉴权客户端缺失、归档上传或资源发布失败时抛出
    ``PackageCLIError``。
    """

    if http_client is None:
        raise PackageCLIError("upload 需要有效的 http_client（请确认已传 --ak/--sk）")

    # ``inspection`` 是规范目录、归档与遗留上传 DTO 的唯一共同来源。
    # 延迟导入保留遗留“检查后发布”编排，同时避免包分发（Package Distribution）
    # 在 Module 加载时反向初始化工作区运行时（Workspace Runtime）。
    from ...inspection import inspect_package

    inspection = inspect_package(path, namespace=namespace, out_dir=out_dir)
    # ``publication`` 只消费已检查产物，不允许传输 Adapter 重新扫描来源。
    publication = publish_inspection(
        inspection,
        HttpClientPublicationAdapter(http_client),
        download_url=download_url,
    )
    print_status(
        "package upload 完成，设备模板已落库 package_info + source_registry",
        "info",
    )
    print_status(
        "  download_url : "
        f"{publication['download_url'] or '(空，请确认 OSS 或 --download-url)'}",
        "info",
    )
    # ``package_info`` 是状态输出引用的同一已发布软件包身份投影。
    package_info = publication["package_info"]
    print_status(f"  class_namespace : {package_info['class_namespace']}", "info")
    print_status(
        "  现在可用含 community.* 节点的 graph 启动 Edge 触发 resolve/下载",
        "info",
    )
    return publication


def _resolve_download_target(
    port: PublicationPort,
    archive_path: str,
    download_url: str,
) -> tuple[str, str]:
    """确定软件包归档的可达地址。

    参数：``port`` 提供产物上传 Interface；``archive_path`` 是本地归档；
    ``download_url`` 是可选的调用者指定地址。
    返回：公开下载地址与可选对象键。
    异常：直传失败或未返回任何可引用身份时抛出 ``PackageCLIError``。
    """

    if download_url:
        print_status(f"使用显式 download_url：{download_url}", "info")
        return download_url, ""

    print_status(f"上传归档到 OSS（预签名直传）：{archive_path}", "info")
    try:
        # ``public_url`` 与 ``object_key`` 共同标识云端可引用的归档产物。
        public_url, object_key = port.upload_artifact(archive_path)
    except Exception as error:
        raise PackageCLIError(
            f"归档预签名直传失败：{error}；可改用 --download-url 指向可达地址"
        ) from error
    if not public_url and not object_key:
        raise PackageCLIError(
            "OSS 直传未返回 public_url/object_key；可改用 --download-url"
        )
    return public_url, object_key


__all__ = [
    "HttpClientPublicationAdapter",
    "PublicationPort",
    "publish_inspection",
    "upload_package",
]
