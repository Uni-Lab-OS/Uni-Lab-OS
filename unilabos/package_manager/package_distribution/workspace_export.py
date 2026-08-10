"""已验证 wheel 到派生 Package Workspace 的安全原子导出。"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from ..package_catalog import PackageCatalog, WorkspaceSource
from .errors import PackageBuildError, PackageTransferError
from .transfer_models import PackageReleaseDescriptor
from .wheel import read_verified_wheel_members
from .workspace_manifest import _decode_inline_record, validate_workspace_manifest


def export_derived_workspace(
    wheel: str | Path,
    target: str | Path,
    *,
    descriptor: PackageReleaseDescriptor,
    catalog: PackageCatalog,
    environment: str,
    compile_catalog: Callable[[WorkspaceSource], PackageCatalog],
) -> Path:
    """从经过缓存验证的 wheel 原子导出可重建工作区。

    参数：``wheel`` 是缓存对象；``target`` 是必须尚不存在的目录；``descriptor``
    与 ``catalog`` 是已完成三摘要对账的事实；``environment`` 是来源环境标签；
    ``compile_catalog`` 是统一静态编译器。
    返回：成功提交的规范目标路径。
    异常：老 wheel、目标冲突、清单或导出 parity 无效时抛出稳定
    ``PackageTransferError``；不覆盖已有目录。
    """

    target_path = Path(target).expanduser().absolute()
    if target_path.exists() or target_path.is_symlink():
        raise PackageTransferError(
            "source_output_exists",
            "源码导出目标已经存在，拒绝覆盖或合并",
            retryable=False,
        )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        members = read_verified_wheel_members(
            wheel,
            expected_digest=descriptor.artifact_digest,
        )
        manifest = validate_workspace_manifest(members, catalog)
    except PackageBuildError as error:
        raise PackageTransferError(
            "source_export_incompatible",
            "wheel 开发工作区清单无效",
            retryable=False,
        ) from error
    if manifest is None:
        raise PackageTransferError(
            "source_export_unavailable",
            "该设备软件包没有开发工作区导出清单；已验证 wheel 仍保留在缓存",
            retryable=False,
        )

    temporary_path = Path(
        tempfile.mkdtemp(prefix=f".{target_path.name}.", dir=target_path.parent)
    )
    try:
        for record in manifest["files"]:
            source_name = str(record["source"])
            target_name = PurePosixPath(str(record["target"]))
            output = temporary_path.joinpath(*target_name.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(members[source_name])
        temporary_path.joinpath("pyproject.toml").write_bytes(
            _decode_inline_record(manifest["pyproject"], "pyproject")
        )
        if "package_yaml" in manifest:
            temporary_path.joinpath("package.yaml").write_bytes(
                _decode_inline_record(manifest["package_yaml"], "package_yaml")
            )
        _write_origin_record(
            temporary_path / ".unilab-package-origin.json",
            environment=environment,
            descriptor=descriptor,
        )
        exported_catalog = compile_catalog(WorkspaceSource(temporary_path))
        if exported_catalog.to_canonical_bytes() != catalog.to_canonical_bytes():
            raise ValueError("派生工作区包目录与缓存 wheel 不一致")
        temporary_path.replace(target_path)
    except PackageTransferError:
        shutil.rmtree(temporary_path, ignore_errors=True)
        raise
    except Exception as error:
        shutil.rmtree(temporary_path, ignore_errors=True)
        raise PackageTransferError(
            "source_export_incompatible",
            "派生工作区重新检查失败，未提交目标目录",
            retryable=False,
        ) from error
    return target_path


def _write_origin_record(
    target: Path,
    *,
    environment: str,
    descriptor: PackageReleaseDescriptor,
) -> None:
    """写入不含签名地址和本地缓存路径的来源记录。

    参数：``target`` 是临时工作区根文件；``environment`` 是环境标签；
    ``descriptor`` 是发布身份。
    返回：无。
    异常：文件不可写时传播原始 IO 异常。
    """

    document: dict[str, Any] = {
        "schema_version": "unilab-package-origin/v1",
        "environment": environment,
        "distribution": descriptor.distribution,
        "version": descriptor.version,
        "namespace": descriptor.namespace,
        "artifact_digest": descriptor.artifact_digest,
        "catalog_digest": descriptor.catalog_digest,
        "content_digest": descriptor.content_digest,
    }
    target.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = ["export_derived_workspace"]
