"""软件包发布（Package Publication）的遗留 HTTP 适配器。"""

from __future__ import annotations

from typing import Any

from unilabos.utils.banner_print import print_status

from .errors import PackageCLIError
from .inspection import inspect_package


def upload_package(
    path: str,
    http_client: Any,
    namespace: str | None = None,
    out_dir: str | None = None,
    download_url: str = "",
) -> dict[str, Any]:
    """检查软件包并把兼容资源投影发布到远端。

    参数：``path`` 是软件包根；``http_client`` 是已鉴权的 HTTP 适配器；
    ``namespace`` 仅供遗留包使用；``out_dir`` 是可选产物目录；
    ``download_url`` 是可选显式归档地址。
    返回：发布后的 ``package_info``、资源 DTO、下载地址和 HTTP 状态。
    异常：鉴权客户端缺失、归档上传或资源发布失败时抛出
    ``PackageCLIError``。
    """

    if http_client is None:
        raise PackageCLIError("upload 需要有效的 http_client（请确认已传 --ak/--sk）")

    # ``inspection`` 是规范目录与遗留上传 DTO 的唯一来源。
    inspection = inspect_package(path, namespace=namespace, out_dir=out_dir)
    package_info: dict[str, Any] = inspection["package_info"]
    archive_path = inspection["archive_path"]

    final_url, object_key = _resolve_download_target(
        http_client,
        archive_path,
        download_url,
    )
    package_info["download_url"] = final_url
    if object_key:
        package_info["oss_object_key"] = object_key

    # ``resources`` 是发布 API 的兼容资源 DTO，不是新的目录权威。
    resources = inspection["resources"]
    for item in resources:
        item["package_info"] = package_info

    response = http_client.upload_package_resources(resources, package_info)
    status = getattr(response, "status_code", None)
    response_text = getattr(response, "text", "")
    if status not in (200, 201):
        raise PackageCLIError(f"上传 /lab/resource 失败：{status} {response_text}")

    print_status(
        "package upload 完成，设备模板已落库 package_info + source_registry",
        "info",
    )
    print_status(
        f"  download_url : {final_url or '(空，请确认 OSS 或 --download-url)'}",
        "info",
    )
    print_status(f"  class_namespace : {package_info['class_namespace']}", "info")
    print_status(
        "  现在可用含 community.* 节点的 graph 启动 Edge 触发 resolve/下载",
        "info",
    )
    return {
        "package_info": package_info,
        "resources": resources,
        "download_url": final_url,
        "response_status": status,
    }


def _resolve_download_target(
    http_client: Any,
    archive_path: str,
    download_url: str,
) -> tuple[str, str]:
    """确定软件包归档的可达地址。

    参数：``http_client`` 提供预签名直传；``archive_path`` 是本地归档；
    ``download_url`` 是可选的调用者指定地址。
    返回：公开下载地址与可选对象键。
    异常：直传失败或未返回任何可引用身份时抛出 ``PackageCLIError``。
    """

    if download_url:
        print_status(f"使用显式 download_url：{download_url}", "info")
        return download_url, ""

    print_status(f"上传归档到 OSS（预签名直传）：{archive_path}", "info")
    try:
        public_url, object_key = http_client.upload_file_to_oss(
            archive_path,
            scene="models",
        )
    except Exception as error:
        raise PackageCLIError(
            f"归档预签名直传失败：{error}；可改用 --download-url 指向可达地址"
        ) from error
    if not public_url and not object_key:
        raise PackageCLIError(
            "OSS 直传未返回 public_url/object_key；可改用 --download-url"
        )
    return public_url, object_key


__all__ = ["upload_package"]
