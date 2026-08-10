"""设备软件包 CLI 云端上传、下载、缓存与源码导出合同。"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import shutil
import zipfile
from pathlib import Path
from typing import Any

import pytest

from tests.package_manager.test_package_build_audit import _prepare_buildable_package


def _build_artifact(workspace: Path, output: Path):
    """构建一个带开发工作区清单的真实测试 wheel。

    参数：``workspace`` 是源码根；``output`` 是产物目录。
    返回：完整 ``PackageBuildArtifact``。
    异常：测试工作区或标准 wheel 构建失败时传播真实异常。
    """

    from unilabos.package_manager.package_distribution import build_workspace_package
    from unilabos.package_manager.workspace_runtime import compile_package_source

    _prepare_buildable_package(workspace)
    return build_workspace_package(
        workspace,
        output,
        compile_catalog=compile_package_source,
    )


def _descriptor(artifact, *, digest: str | None = None):
    """从本地构建产物生成 Backend 无关测试发布描述。

    参数：``artifact`` 是已审计构建；``digest`` 可覆盖 Artifact digest。
    返回：覆盖完整设备源码身份的 ``PackageReleaseDescriptor``。
    异常：构建目录缺少设备时由描述模型抛出 ``ValueError``。
    """

    from unilabos.package_manager.package_distribution import PackageReleaseDescriptor

    catalog = artifact.catalog
    return PackageReleaseDescriptor(
        template_uuid="11e27cf5-3ec8-4cfb-bb17-db941426e94e",
        distribution=catalog.distribution.name,
        normalized_name=catalog.distribution.normalized_name,
        version=catalog.distribution.version,
        namespace=catalog.namespace,
        artifact_digest=digest or artifact.artifact_digest,
        catalog_digest=catalog.catalog_digest,
        content_digest=catalog.content_digest,
        source_fqids=tuple(
            f"{item.module}:{item.symbol}" for item in catalog.definitions.devices
        ),
    )


def _strip_workspace_manifest(wheel: Path) -> None:
    """删除测试 wheel 的工作区清单并重新生成合法 RECORD。

    参数：``wheel`` 是可原地改写的真实构建产物副本。
    返回：无。
    异常：wheel 缺少唯一 RECORD 或归档不可写时传播测试错误。
    """

    with zipfile.ZipFile(wheel) as archive:
        members = {
            item.filename: archive.read(item)
            for item in archive.infolist()
            if not item.is_dir()
            and not item.filename.endswith(
                ".dist-info/unilab_workspace/manifest.json"
            )
        }
    record_names = [name for name in members if name.endswith(".dist-info/RECORD")]
    assert len(record_names) == 1
    record_name = record_names[0]
    del members[record_name]
    rows: list[list[str]] = []
    for name, payload in sorted(members.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode(
            "ascii"
        ).rstrip("=")
        rows.append([name, f"sha256={digest}", str(len(payload))])
    rows.append([record_name, "", ""])
    record_stream = io.StringIO(newline="")
    csv.writer(record_stream, lineterminator="\n").writerows(rows)
    members[record_name] = record_stream.getvalue().encode("utf-8")

    temporary = wheel.with_suffix(".rewrite")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in sorted(members.items()):
            archive.writestr(name, payload)
    temporary.replace(wheel)


class _MemoryTransferPort:
    """以内存描述和本地 wheel 代替遗留 Backend/OSS 的测试 Adapter。"""

    def __init__(self, wheel: Path, descriptor: Any) -> None:
        """固定下载来源与远端描述。

        参数：``wheel`` 是测试 Artifact；``descriptor`` 是对账描述。
        返回：无。
        异常：无。
        """

        self.wheel = wheel
        self.descriptor = descriptor
        self.download_calls = 0
        self.upload_calls = 0
        self.publication_calls = 0
        self.existing = None

    def probe(self) -> str:
        """返回固定遗留能力名。

        参数：无。
        返回：``legacy-template-package/v1``。
        异常：无。
        """

        return "legacy-template-package/v1"

    def resolve(self, _request: Any):
        """返回固定可信发布描述。

        参数：``_request`` 是本测试不需要解释的选择器。
        返回：构造时描述。
        异常：无。
        """

        return self.descriptor

    def download_artifact(self, _template_uuid: str, target: Path) -> None:
        """把真实测试 wheel 复制到缓存临时文件。

        参数：``_template_uuid`` 是已解析模板；``target`` 是缓存候选。
        返回：无。
        异常：复制失败时传播原始 IO 异常。
        """

        self.download_calls += 1
        shutil.copyfile(self.wheel, target)

    def find_release(self, _distribution: str, _version: str):
        """返回可切换的同版本既有发布。

        参数：发行名和版本仅用于符合发布 Interface。
        返回：``existing`` 当前值。
        异常：无。
        """

        return self.existing

    def upload_release_artifact(
        self,
        _wheel: Path,
        *,
        normalized_name: str,
        version: str,
    ) -> tuple[str, str]:
        """记录上传并返回固定 OSS 兼容身份。

        参数：``_wheel`` 是同一已审计 wheel；包名和版本是对象分组。
        返回：公开兼容地址和对象键。
        异常：若分组身份为空则测试断言失败。
        """

        assert normalized_name and version
        self.upload_calls += 1
        return "https://objects.example/package.whl", "file/packages/package.whl"

    def publish_resources(
        self,
        resources: list[dict[str, Any]],
        package_info: dict[str, Any],
    ) -> None:
        """记录模板发布并验证源码身份证据重复保存。

        参数：``resources`` 是兼容 DTO；``package_info`` 是同一发布身份。
        返回：无。
        异常：必要字段丢失时测试断言失败。
        """

        assert resources
        assert all(item["package_info"] is package_info for item in resources)
        assert all(item["source_fqid"] for item in resources)
        assert all(item["source_registry"]["source_fqid"] for item in resources)
        assert all(item["source_registry"]["content_hash"] for item in resources)
        self.publication_calls += 1


def test_cached_archive_download_recompiles_and_exports_catalog_parity(
    tmp_path: Path,
) -> None:
    """下载必须经 CachedArchiveSource 重编译，导出后仍保持 Catalog parity。

    参数：``tmp_path`` 提供工作区、构建、缓存和导出隔离目录。
    返回：无；断言首次下载、缓存命中和派生工作区都使用同一目录事实。
    异常：归档来源、清单、缓存或导出不兼容时测试失败。
    """

    from unilabos.package_manager.package_catalog import (
        CachedArchiveSource,
        WorkspaceSource,
    )
    from unilabos.package_manager.package_distribution import (
        PackageCache,
        PackageDownloadRequest,
        acquire_package,
    )
    from unilabos.package_manager.workspace_runtime import compile_package_source

    artifact = _build_artifact(tmp_path / "workspace", tmp_path / "dist")
    descriptor = _descriptor(artifact)
    port = _MemoryTransferPort(artifact.wheel, descriptor)
    observed_source_types: list[type[Any]] = []

    def compile_observed(source: WorkspaceSource):
        """记录下载验证采用的来源类型并委托统一静态编译器。

        参数：``source`` 是归档临时工作区或最终派生工作区。
        返回：规范包目录。
        异常：来源无效时传播统一编译错误。
        """

        observed_source_types.append(type(source))
        return compile_package_source(source)

    request = PackageDownloadRequest(package_name=descriptor.distribution)
    first = acquire_package(
        request,
        port=port,
        cache=PackageCache(tmp_path / "cache"),
        environment="uat",
        compile_catalog=compile_observed,
        extract_source=tmp_path / "derived",
    )
    second = acquire_package(
        request,
        port=port,
        cache=PackageCache(tmp_path / "cache"),
        environment="uat",
        compile_catalog=compile_observed,
    )

    assert first["status"] == "package_cached_and_source_exported"
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert port.download_calls == 1
    assert CachedArchiveSource in observed_source_types
    assert compile_package_source(WorkspaceSource(tmp_path / "derived")).to_canonical_bytes() == (
        artifact.catalog.to_canonical_bytes()
    )
    assert tmp_path.joinpath("derived/.unilab-package-origin.json").is_file()


def test_cache_rejects_wrong_artifact_digest_without_publishing_object(
    tmp_path: Path,
) -> None:
    """远端 Artifact digest 错误时缓存不得出现正式可用对象。

    参数：``tmp_path`` 提供真实 wheel 和空缓存。
    返回：无；断言稳定错误码且 objects 下没有正式 ``.whl``。
    异常：若摘要错误被接受则测试失败。
    """

    from unilabos.package_manager.package_distribution import (
        PackageCache,
        PackageDownloadRequest,
        acquire_package,
    )
    from unilabos.package_manager.package_distribution.errors import (
        PackageTransferError,
    )
    from unilabos.package_manager.workspace_runtime import compile_package_source

    artifact = _build_artifact(tmp_path / "workspace", tmp_path / "dist")
    wrong_digest = "sha256:" + hashlib.sha256(b"different").hexdigest()
    port = _MemoryTransferPort(artifact.wheel, _descriptor(artifact, digest=wrong_digest))
    cache_root = tmp_path / "cache"

    with pytest.raises(PackageTransferError) as caught:
        acquire_package(
            PackageDownloadRequest(package_name=artifact.catalog.distribution.name),
            port=port,
            cache=PackageCache(cache_root),
            environment="uat",
            compile_catalog=compile_package_source,
        )

    assert caught.value.code == "remote_package_incompatible"
    assert not tuple(cache_root.glob("objects/sha256/*.whl"))


def test_legacy_wheel_without_manifest_stays_cached_when_source_export_fails(
    tmp_path: Path,
) -> None:
    """老 wheel 可被可信缓存，但请求源码导出必须稳定关闭式失败。

    参数：``tmp_path`` 提供构建副本、内容寻址缓存和不存在的导出目标。
    返回：无；断言 ``source_export_unavailable`` 且已验证 wheel 未被删除。
    异常：若老包被伪造为源码工作区或缓存被回滚则测试失败。
    """

    from unilabos.package_manager.package_distribution import (
        PackageCache,
        PackageDownloadRequest,
        acquire_package,
    )
    from unilabos.package_manager.package_distribution.errors import (
        PackageTransferError,
    )
    from unilabos.package_manager.workspace_runtime import compile_package_source

    artifact = _build_artifact(tmp_path / "workspace", tmp_path / "dist")
    legacy_wheel = tmp_path / "legacy.whl"
    shutil.copyfile(artifact.wheel, legacy_wheel)
    _strip_workspace_manifest(legacy_wheel)
    legacy_digest = "sha256:" + hashlib.sha256(legacy_wheel.read_bytes()).hexdigest()
    descriptor = _descriptor(artifact, digest=legacy_digest)
    cache_root = tmp_path / "cache"

    with pytest.raises(PackageTransferError) as caught:
        acquire_package(
            PackageDownloadRequest(package_name=descriptor.distribution),
            port=_MemoryTransferPort(legacy_wheel, descriptor),
            cache=PackageCache(cache_root),
            environment="uat",
            compile_catalog=compile_package_source,
            extract_source=tmp_path / "derived",
        )

    assert caught.value.code == "source_export_unavailable"
    assert tuple(cache_root.glob("objects/sha256/*.whl"))
    assert not (tmp_path / "derived").exists()


def test_publication_is_idempotent_and_rejects_same_version_different_artifact(
    tmp_path: Path,
) -> None:
    """发布必须后置广场对账，同摘要幂等而不同 Artifact 关闭式冲突。

    参数：``tmp_path`` 提供真实构建产物。
    返回：无；断言一次发布、幂等命中和 ``version_conflict`` 三种结果。
    异常：若重复上传或覆盖同版本则测试失败。
    """

    from unilabos.package_manager.package_distribution.errors import (
        PackageTransferError,
    )
    from unilabos.package_manager.package_distribution.publication import (
        publish_package_artifact,
    )

    artifact = _build_artifact(tmp_path / "workspace", tmp_path / "dist")
    descriptor = _descriptor(artifact)
    port = _MemoryTransferPort(artifact.wheel, descriptor)

    published = publish_package_artifact(artifact, port=port, environment="uat")
    port.existing = descriptor
    repeated = publish_package_artifact(artifact, port=port, environment="uat")
    port.existing = _descriptor(
        artifact,
        digest="sha256:" + hashlib.sha256(b"other-wheel").hexdigest(),
    )
    with pytest.raises(PackageTransferError) as caught:
        publish_package_artifact(artifact, port=port, environment="uat")

    assert published["status"] == "published"
    assert published["square_verified"] is True
    assert repeated["status"] == "already_published"
    assert port.upload_calls == 1
    assert port.publication_calls == 1
    assert caught.value.code == "version_conflict"


def test_package_catalog_strict_decoder_rejects_noncanonical_bytes(
    tmp_path: Path,
) -> None:
    """内嵌 PackageCatalog 解码必须拒绝语义相同但非规范的 JSON。

    参数：``tmp_path`` 提供真实目录文档。
    返回：无；断言规范字节可 round-trip，而尾随空白被拒绝。
    异常：若解码器宽松接受非规范编码则测试失败。
    """

    from unilabos.package_manager.package_catalog import PackageCatalog

    artifact = _build_artifact(tmp_path / "workspace", tmp_path / "dist")
    payload = artifact.catalog.to_canonical_bytes()

    assert PackageCatalog.from_canonical_bytes(payload).to_canonical_bytes() == payload
    with pytest.raises(ValueError, match="规范"):
        PackageCatalog.from_canonical_bytes(payload + b"\n")


@pytest.mark.parametrize(
    ("value", "name", "url"),
    [
        (None, "prod", "https://leap-lab.bohrium.com/api/v1"),
        ("prod", "prod", "https://leap-lab.bohrium.com/api/v1"),
        ("production", "prod", "https://leap-lab.bohrium.com/api/v1"),
        ("test", "test", "https://leap-lab.test.bohrium.com/api/v1"),
        ("uat", "uat", "https://leap-lab.uat.bohrium.com/api/v1"),
    ],
)
def test_package_environment_aliases_are_exact(
    value: str | None,
    name: str,
    url: str,
) -> None:
    """包命令环境别名必须精确映射且省略地址固定正式环境。

    参数：``value`` 是输入；``name`` 和 ``url`` 是冻结期望。
    返回：无；断言没有 session 或历史环境回退。
    异常：映射漂移时测试失败。
    """

    from unilabos.package_manager.package_distribution.environment import (
        resolve_package_environment,
    )

    resolved = resolve_package_environment(value)
    assert (resolved.name, resolved.base_url) == (name, url)


def test_workspace_manifest_is_record_protected_and_contains_project_identity(
    tmp_path: Path,
) -> None:
    """新 wheel 必须在 dist-info 内携带受 RECORD 保护的工作区清单。

    参数：``tmp_path`` 提供真实构建目录。
    返回：无；断言清单身份、项目元数据和文件闭包存在。
    异常：构建或清单验证失败时测试失败。
    """

    from unilabos.package_manager.package_distribution.wheel import (
        read_verified_wheel_members,
    )
    from unilabos.package_manager.package_distribution.workspace_manifest import (
        validate_workspace_manifest,
    )

    artifact = _build_artifact(tmp_path / "workspace", tmp_path / "dist")
    members = read_verified_wheel_members(
        artifact.wheel,
        expected_digest=artifact.artifact_digest,
    )
    manifest = validate_workspace_manifest(members, artifact.catalog)

    assert manifest is not None
    assert manifest["schema_version"] == "unilab-derived-workspace/v1"
    assert manifest["project"]["name"] == artifact.catalog.distribution.name
    assert manifest["files"]
