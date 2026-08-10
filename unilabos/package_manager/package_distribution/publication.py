"""已审计设备软件包到远端设备广场的不可变发布编排。"""

from __future__ import annotations

from typing import Any, Protocol

from ..package_catalog import PackageCatalog
from .build import PackageBuildArtifact
from .errors import PackageTransferError
from .transfer_models import PackageReleaseDescriptor, command_success_document


class PackagePublisherPort(Protocol):
    """上传编排需要的最小远端发布 Interface。"""

    def probe(self) -> str:
        """只读证明目标 Backend 的包协议能力。"""

        ...

    def find_release(
        self,
        distribution: str,
        version: str,
    ) -> PackageReleaseDescriptor | None:
        """查找同名同版本远端发布。"""

        ...

    def upload_release_artifact(
        self,
        wheel: Any,
        *,
        normalized_name: str,
        version: str,
    ) -> tuple[str, str]:
        """流式上传同一已审计 wheel 并返回遗留地址和对象键。"""

        ...

    def publish_resources(
        self,
        resources: list[dict[str, Any]],
        package_info: dict[str, Any],
    ) -> None:
        """发布同一目录代的模板兼容投影。"""

        ...

    def resolve(self, request: Any) -> PackageReleaseDescriptor:
        """发布后重新从公开广场解析同一包版本。"""

        ...


def publish_package_artifact(
    artifact: PackageBuildArtifact,
    *,
    port: PackagePublisherPort,
    environment: str,
) -> dict[str, Any]:
    """预检版本不可变性、上传 wheel、发布模板并从广场对账。

    参数：``artifact`` 是唯一已审计构建结果；``port`` 是传输 Adapter；
    ``environment`` 是一次命令固定环境。
    返回：稳定 ``package.upload`` 成功 JSON 字典。
    异常：无设备定义、版本冲突、上传、业务码或广场不可见时抛出稳定错误；不写
    物料（Material）、库存（Inventory）或 Graph。
    """

    catalog = artifact.catalog
    if not catalog.definitions.devices:
        raise PackageTransferError(
            "device_definition_required",
            "设备广场上传至少需要一个设备定义",
            retryable=False,
        )
    port.probe()
    existing = port.find_release(
        catalog.distribution.name,
        catalog.distribution.version,
    )
    if existing is not None:
        if _descriptor_matches_artifact(existing, artifact):
            return _upload_result(
                existing,
                catalog,
                environment=environment,
                status="already_published",
                square_verified=True,
            )
        raise PackageTransferError(
            "version_conflict",
            "设备广场已存在同名同版本但不同 Artifact；请提升版本后重新发布",
            retryable=False,
        )

    publication = artifact.publication_input()
    package_info = dict(publication["package_info"])
    resources = [dict(item) for item in publication["resources"]]
    try:
        public_url, object_key = port.upload_release_artifact(
            artifact.wheel,
            normalized_name=catalog.distribution.normalized_name,
            version=catalog.distribution.version,
        )
    except PackageTransferError:
        raise
    except Exception as error:
        raise PackageTransferError(
            "artifact_upload_failed",
            "设备软件包 Artifact 上传失败",
            retryable=True,
            details={"artifact_uploaded": False},
        ) from error
    package_info["download_url"] = public_url
    package_info["oss_object_key"] = object_key
    for resource in resources:
        resource["package_info"] = package_info
    try:
        port.publish_resources(resources, package_info)
    except PackageTransferError as error:
        error.details.setdefault("artifact_uploaded", True)
        raise
    except Exception as error:
        raise PackageTransferError(
            "template_publication_failed",
            "Artifact 已上传，但设备模板发布失败",
            retryable=True,
            details={
                "artifact_uploaded": True,
                "orphan_object_recorded": bool(object_key),
            },
        ) from error

    from .transfer_models import PackageDownloadRequest

    try:
        published = port.resolve(
            PackageDownloadRequest(
                package_name=catalog.distribution.name,
                version=catalog.distribution.version,
            )
        )
        published.assert_catalog_parity(catalog, exact_source_set=True)
        if published.artifact_digest != artifact.artifact_digest:
            raise ValueError("广场 Artifact digest 与上传 wheel 不一致")
    except Exception as error:
        raise PackageTransferError(
            "uploaded_not_in_square",
            "模板接口已接受上传，但同一发布未在权威设备广场通过对账",
            retryable=False,
            details={
                "artifact_uploaded": True,
                "orphan_object_recorded": bool(object_key),
            },
        ) from error
    return _upload_result(
        published,
        catalog,
        environment=environment,
        status="published",
        square_verified=True,
    )


def _descriptor_matches_artifact(
    descriptor: PackageReleaseDescriptor,
    artifact: PackageBuildArtifact,
) -> bool:
    """判断既有发布是否与本地已审计 Artifact 完全相同。

    参数：``descriptor`` 是远端发布；``artifact`` 是本地构建。
    返回：三摘要、发行身份和设备源码集合都相同时为 ``True``。
    异常：无；任何 parity 失败返回 ``False``。
    """

    if descriptor.artifact_digest != artifact.artifact_digest:
        return False
    try:
        descriptor.assert_catalog_parity(artifact.catalog, exact_source_set=True)
    except ValueError:
        return False
    return True


def _upload_result(
    descriptor: PackageReleaseDescriptor,
    catalog: PackageCatalog,
    *,
    environment: str,
    status: str,
    square_verified: bool,
) -> dict[str, Any]:
    """生成发布成功或幂等命中的命令结果。

    参数：``descriptor`` 是广场对账描述；``catalog`` 是本地目录；其余字段描述
    命令环境和状态。
    返回：不含对象键、签名 URL 或绝对路径的稳定字典。
    异常：无。
    """

    return command_success_document(
        command="package.upload",
        environment=environment,
        status=status,
        descriptor=descriptor,
        extra={
            "definition_fqids": [
                item.fqid
                for item in (
                    *catalog.definitions.devices,
                    *catalog.definitions.resources,
                    *catalog.definitions.workflows,
                )
            ],
            "square_verified": square_verified,
        },
    )


__all__ = ["PackagePublisherPort", "publish_package_artifact"]
