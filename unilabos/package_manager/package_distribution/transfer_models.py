"""云端设备软件包传输的 Backend 无关领域对象。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from ..package_catalog import PackageCatalog


@dataclass(frozen=True, slots=True)
class PackageDownloadRequest:
    """一次只下载和缓存、不安装的软件包选择请求。"""

    template_uuid: str | None = None
    package_name: str | None = None
    version: str | None = None

    def __post_init__(self) -> None:
        """验证两个选择器精确互斥。

        参数：无；读取构造字段。
        返回：无。
        异常：模板 UUID 与包名同时存在或同时缺失时抛出 ``ValueError``。
        """

        if bool(self.template_uuid) == bool(self.package_name):
            raise ValueError("--template-uuid 与 --package 必须且只能选择一个")
        if self.version and not self.package_name:
            raise ValueError("--version 只能与 --package 一起使用")
        if self.template_uuid:
            try:
                UUID(self.template_uuid)
            except (ValueError, TypeError) as error:
                raise ValueError("--template-uuid 不是有效 UUID") from error

    @property
    def selector_kind(self) -> Literal["template", "package"]:
        """返回本次远端选择器类型。

        参数：无。
        返回：精确模板或包名选择的稳定 wire value。
        异常：无；构造阶段已经保证互斥。
        """

        return "template" if self.template_uuid else "package"


@dataclass(frozen=True, slots=True)
class PackageReleaseDescriptor:
    """由 Backend Adapter 解析出的可信远端发布描述。"""

    template_uuid: str
    distribution: str
    normalized_name: str
    version: str
    namespace: str
    artifact_digest: str
    catalog_digest: str
    content_digest: str
    source_fqids: tuple[str, ...]

    def __post_init__(self) -> None:
        """验证发布身份和三摘要的封闭形状。

        参数：无；读取构造字段。
        返回：无；稳定排序源码身份。
        异常：任一身份缺失、摘要无效或源码身份重复时抛出 ``ValueError``。
        """

        for field_name in (
            "template_uuid",
            "distribution",
            "normalized_name",
            "version",
            "namespace",
        ):
            if not isinstance(getattr(self, field_name), str) or not getattr(
                self, field_name
            ).strip():
                raise ValueError(f"远端发布缺少 {field_name}")
        for field_name in (
            "artifact_digest",
            "catalog_digest",
            "content_digest",
        ):
            _validate_digest(getattr(self, field_name), field_name)
        if not self.source_fqids or any(
            not isinstance(item, str) or not item.strip() for item in self.source_fqids
        ):
            raise ValueError("远端发布缺少设备 source_fqid")
        if len(set(self.source_fqids)) != len(self.source_fqids):
            raise ValueError("远端发布包含重复设备 source_fqid")
        object.__setattr__(self, "source_fqids", tuple(sorted(self.source_fqids)))

    @property
    def cache_key(self) -> str:
        """返回人类可读但以 Artifact digest 为权威的缓存键。

        参数：无。
        返回：``namespace@version#sha256:...``。
        异常：无。
        """

        return f"{self.namespace}@{self.version}#{self.artifact_digest}"

    def assert_catalog_parity(
        self,
        catalog: PackageCatalog,
        *,
        exact_source_set: bool,
    ) -> None:
        """验证远端描述与归档重编译目录完全一致。

        参数：``catalog`` 是下载 wheel 重编译目录；``exact_source_set`` 表示按包
        选择时设备源码集合也必须完全相等，精确模板选择时只要求包含。
        返回：无。
        异常：身份、三摘要或设备源码身份不一致时抛出 ``ValueError``。
        """

        comparisons = {
            "distribution": (self.distribution, catalog.distribution.name),
            "normalized_name": (
                self.normalized_name,
                catalog.distribution.normalized_name,
            ),
            "version": (self.version, catalog.distribution.version),
            "namespace": (self.namespace, catalog.namespace),
            "catalog_digest": (self.catalog_digest, catalog.catalog_digest),
            "content_digest": (self.content_digest, catalog.content_digest),
        }
        mismatched = [
            name for name, (remote, local) in comparisons.items() if remote != local
        ]
        if mismatched:
            raise ValueError("远端发布与 wheel 包目录身份不一致：" + ", ".join(mismatched))
        catalog_sources = {
            f"{item.module}:{item.symbol}" for item in catalog.definitions.devices
        }
        selected_sources = set(self.source_fqids)
        sources_match = (
            selected_sources == catalog_sources
            if exact_source_set
            else selected_sources.issubset(catalog_sources)
        )
        if not sources_match:
            raise ValueError("远端设备 source_fqid 与 wheel 包目录不一致")


@dataclass(frozen=True, slots=True)
class PackageCacheEntry:
    """一次已验证内容寻址缓存提交结果。"""

    wheel: Path
    catalog: PackageCatalog
    cache_hit: bool
    cache_key: str


def command_success_document(
    *,
    command: str,
    environment: str,
    status: str,
    descriptor: PackageReleaseDescriptor,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成上传或下载成功的稳定 CLI JSON 文档。

    参数：``command``、``environment`` 和 ``status`` 是命令身份；``descriptor``
    是经过对账的发布描述；``extra`` 是不含秘密的命令扩展字段。
    返回：新的可序列化字典。
    异常：无；发布描述构造阶段已完成字段验证。
    """

    document: dict[str, Any] = {
        "schema_version": "unilab-package-command/v1",
        "command": command,
        "environment": environment,
        "status": status,
        "distribution": descriptor.distribution,
        "version": descriptor.version,
        "namespace": descriptor.namespace,
        "artifact_digest": descriptor.artifact_digest,
        "catalog_digest": descriptor.catalog_digest,
        "content_digest": descriptor.content_digest,
    }
    document.update(extra or {})
    return document


def _validate_digest(value: str, field_name: str) -> None:
    """验证一个发布 SHA-256 字段。

    参数：``value`` 是摘要；``field_name`` 是诊断字段名。
    返回：无。
    异常：摘要格式无效时抛出 ``ValueError``。
    """

    if not isinstance(value, str) or len(value) != 71 or not value.startswith(
        "sha256:"
    ):
        raise ValueError(f"远端发布 {field_name} 不是 sha256 摘要")
    try:
        int(value[7:], 16)
    except ValueError as error:
        raise ValueError(f"远端发布 {field_name} 不是 sha256 摘要") from error


__all__ = [
    "PackageCacheEntry",
    "PackageDownloadRequest",
    "PackageReleaseDescriptor",
    "command_success_document",
]
