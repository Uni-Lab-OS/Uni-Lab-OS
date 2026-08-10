"""遗留设备模板 JSONB 到可信包发布描述的纯解析。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import quote
from uuid import UUID

from ..errors import PackageTransferError
from ..transfer_models import PackageReleaseDescriptor

EnvelopeFetcher = Callable[..., Any]


def package_summaries(fetch: EnvelopeFetcher) -> list[dict[str, Any]]:
    """读取并验证公开设备包列表的遗留双层 data 形状。

    参数：``fetch`` 是 Adapter 的同环境信封请求函数。
    返回：包摘要对象列表。
    异常：信封或字段形状无效时抛出 ``backend_incompatible``。
    """

    data = fetch(
        "GET",
        "/lab/square/packages",
        timeout=(5, 30),
        incompatible_on_shape=True,
    )
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise _incompatible("package list 缺少 data 数组")
    if any(not isinstance(item, dict) for item in data["data"]):
        raise _incompatible("package list 成员必须是对象")
    return data["data"]


def descriptor_for_template(
    fetch: EnvelopeFetcher,
    template_uuid: str,
) -> PackageReleaseDescriptor:
    """从公开模板详情严格投影一个远端发布描述。

    参数：``fetch`` 是信封请求函数；``template_uuid`` 是模板 UUID。
    返回：单设备源码身份的发布描述。
    异常：UUID、详情信封或必要包字段无效时抛出稳定错误。
    """

    try:
        normalized_uuid = str(UUID(template_uuid))
    except (ValueError, TypeError) as error:
        raise PackageTransferError(
            "invalid_selector",
            "--template-uuid 不是有效 UUID",
            retryable=False,
        ) from error
    data = fetch(
        "GET",
        f"/lab/square/detail/{normalized_uuid}",
        timeout=(5, 30),
    )
    if not isinstance(data, dict):
        raise _incompatible("template detail data 必须是对象")
    package_info = data.get("package_info")
    source_registry = data.get("source_registry")
    if not isinstance(package_info, dict) or not isinstance(source_registry, dict):
        raise _incompatible("template detail 缺少包元数据")
    artifact_digest = package_info.get("artifact_digest") or package_info.get("sha256")
    if package_info.get("sha256") not in (None, artifact_digest):
        raise _incompatible("package_info sha256 与 artifact_digest 不一致")
    try:
        return PackageReleaseDescriptor(
            template_uuid=normalized_uuid,
            distribution=package_info["name"],
            normalized_name=package_info["normalized_name"],
            version=package_info["version"],
            namespace=package_info["class_namespace"],
            artifact_digest=artifact_digest,
            catalog_digest=package_info["catalog_digest"],
            content_digest=package_info["content_digest"],
            source_fqids=(source_registry["source_fqid"],),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise _incompatible("template detail 包身份或摘要字段无效") from error


def descriptor_for_package(
    fetch: EnvelopeFetcher,
    distribution: str,
    version: str | None,
) -> PackageReleaseDescriptor:
    """逐页读取包内设备详情并收敛为一个发布。

    参数：``fetch`` 是信封请求函数；``distribution`` 是包名；``version`` 是可选
    精确版本。
    返回：候选模板全部同代的合并描述。
    异常：无设备、版本未命中或同包混有多个发布时抛出稳定错误。
    """

    devices: list[dict[str, Any]] = []
    page = 1
    total = None
    while total is None or len(devices) < total:
        data = fetch(
            "GET",
            f"/lab/square/packages/{quote(distribution, safe='')}",
            params={"page": page, "page_size": 100},
            timeout=(5, 30),
        )
        if not isinstance(data, dict) or not isinstance(data.get("devices"), list):
            raise _incompatible("package detail 缺少 devices")
        if total is None:
            total_value = data.get("device_count")
            if not isinstance(total_value, int) or total_value < 1:
                raise PackageTransferError(
                    "remote_package_not_found",
                    "设备广场中的包没有设备模板",
                    retryable=False,
                )
            total = total_value
        devices.extend(data["devices"])
        if not data["devices"]:
            break
        page += 1
    descriptors = []
    for device in devices:
        if not isinstance(device, dict) or not isinstance(device.get("uuid"), str):
            raise _incompatible("package detail 设备缺少 uuid")
        descriptor = descriptor_for_template(fetch, device["uuid"])
        if version is None or descriptor.version == version:
            descriptors.append(descriptor)
    if not descriptors:
        raise PackageTransferError(
            "remote_package_not_found",
            "设备广场中没有请求的包版本",
            retryable=False,
        )
    identities = {
        (
            item.distribution,
            item.normalized_name,
            item.version,
            item.namespace,
            item.artifact_digest,
            item.catalog_digest,
            item.content_digest,
        )
        for item in descriptors
    }
    if len(identities) != 1:
        raise PackageTransferError(
            "remote_package_ambiguous",
            "同一设备包的模板指向多个发布，请改用精确模板 UUID 或重新发布",
            retryable=False,
        )
    first = min(descriptors, key=lambda item: item.template_uuid)
    return PackageReleaseDescriptor(
        template_uuid=first.template_uuid,
        distribution=first.distribution,
        normalized_name=first.normalized_name,
        version=first.version,
        namespace=first.namespace,
        artifact_digest=first.artifact_digest,
        catalog_digest=first.catalog_digest,
        content_digest=first.content_digest,
        source_fqids=tuple(
            sorted({fqid for item in descriptors for fqid in item.source_fqids})
        ),
    )


def find_release(
    fetch: EnvelopeFetcher,
    distribution: str,
    version: str,
) -> PackageReleaseDescriptor | None:
    """只读查找同名同版本遗留发布。

    参数：``fetch`` 是信封请求函数；``distribution`` 和 ``version`` 是待发布身份。
    返回：包名或版本不存在时返回 ``None``，否则返回唯一描述。
    异常：既有包模板混杂或缺失时关闭式失败。
    """

    summaries = package_summaries(fetch)
    if distribution not in {
        str(item.get("name")) for item in summaries if isinstance(item, dict)
    }:
        return None
    try:
        return descriptor_for_package(fetch, distribution, version)
    except PackageTransferError as error:
        if error.code == "remote_package_not_found":
            return None
        raise


def _incompatible(reason: str) -> PackageTransferError:
    """创建遗留模板字段不兼容错误。

    参数：``reason`` 是本地字段检查说明。
    返回：稳定 ``backend_incompatible`` 错误。
    异常：无。
    """

    return PackageTransferError(
        "backend_incompatible",
        "目标 Backend 与 legacy-template-package/v1 不兼容：" + reason,
        retryable=False,
    )


__all__ = [
    "descriptor_for_package",
    "descriptor_for_template",
    "find_release",
    "package_summaries",
]
