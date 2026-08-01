"""Wheel staging, embedded Catalog generation, and source-parity audit."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .catalog import PackageCatalog
from .compiler import compile_package_source
from .sources import CachedArchiveSource, WorkspaceSource

_STAGING_EXCLUDES = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "unilabos_data",
        "venv",
    }
)


class PackageDistributionError(RuntimeError):
    """A wheel could not be built or did not preserve its PackageCatalog."""


@dataclass(frozen=True)
class BuildArtifact:
    wheel: Path
    artifact_digest: str
    catalog: PackageCatalog


def build_workspace_wheel(
    workspace: str | Path,
    output_dir: str | Path,
) -> BuildArtifact:
    """Build in temporary staging, inject generated facts, then self-audit."""

    source = WorkspaceSource(workspace)
    catalog = compile_package_source(source)
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="unilab-package-build-") as temporary:
        temporary_root = Path(temporary)
        staging = temporary_root / "workspace"
        wheel_output = temporary_root / "wheel"
        _copy_workspace(source.root.resolve(), staging)
        generated = staging / catalog.import_package / "_generated"
        generated.mkdir(parents=True, exist_ok=True)
        (generated / "package.catalog.json").write_bytes(catalog.to_canonical_bytes())
        (generated / "pyproject.toml").write_bytes(source.read_bytes("pyproject.toml"))
        wheel_output.mkdir()
        command = [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_output),
            str(staging),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise PackageDistributionError(f"wheel build 失败: {detail}")
        wheels = sorted(wheel_output.glob("*.whl"))
        if len(wheels) != 1:
            raise PackageDistributionError(
                f"wheel build 必须产生一个 wheel，实际 {len(wheels)}"
            )
        built_wheel = wheels[0]
        injected = {
            f"{catalog.import_package}/_generated/package.catalog.json": (
                catalog.to_canonical_bytes()
            ),
            f"{catalog.import_package}/_generated/pyproject.toml": (
                source.read_bytes("pyproject.toml")
            ),
            **{
                asset.logical_path: source.read_bytes(asset.logical_path)
                for asset in catalog.assets
            },
        }
        if (source.root / "package.yaml").is_file():
            injected[f"{catalog.import_package}/_generated/package.yaml"] = (
                source.read_bytes("package.yaml")
            )
        _inject_wheel_members(built_wheel, injected)
        target = output / built_wheel.name
        shutil.copy2(built_wheel, target)

    artifact_digest = _artifact_digest(target)
    audited = compile_package_source(
        CachedArchiveSource(target, expected_digest=artifact_digest)
    )
    if audited.to_canonical_bytes() != catalog.to_canonical_bytes():
        raise PackageDistributionError(
            "wheel Catalog 与 workspace Catalog canonical bytes 不一致"
        )
    audit_wheel(target, catalog, expected_digest=artifact_digest)
    return BuildArtifact(
        wheel=target,
        artifact_digest=artifact_digest,
        catalog=catalog,
    )


def audit_wheel(
    wheel: str | Path,
    catalog: PackageCatalog,
    *,
    expected_digest: str,
) -> None:
    """Reject extra import roots and any missing definition/asset closure."""

    source = CachedArchiveSource(wheel, expected_digest=expected_digest)
    members = set(source.members())
    payload_roots = {
        PurePosixPath(name).parts[0]
        for name in members
        if name
        and not PurePosixPath(name).parts[0].endswith(".dist-info")
        and not PurePosixPath(name).parts[0].endswith(".data")
    }
    if payload_roots != {catalog.import_package}:
        raise PackageDistributionError(
            "wheel 必须只有领域顶层 import package；发现: "
            + ", ".join(sorted(payload_roots))
        )
    required = {
        *(record.declaring_file for record in catalog.definitions.devices),
        *(record.declaring_file for record in catalog.definitions.resources),
        *(record.declaring_file for record in catalog.definitions.workflows),
        *(asset.logical_path for asset in catalog.assets),
        f"{catalog.import_package}/_generated/package.catalog.json",
        f"{catalog.import_package}/_generated/pyproject.toml",
    }
    missing = sorted(required - members)
    if missing:
        raise PackageDistributionError(
            "wheel 缺失 Catalog closure: " + ", ".join(missing)
        )


def _copy_workspace(source: Path, target: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in _STAGING_EXCLUDES}

    shutil.copytree(source, target, ignore=ignore, symlinks=True)


def _inject_wheel_members(wheel: Path, injected: dict[str, bytes]) -> None:
    with zipfile.ZipFile(wheel) as archive:
        members = {
            item.filename: archive.read(item)
            for item in archive.infolist()
            if not item.is_dir()
            and not item.filename.endswith(("/RECORD.jws", "/RECORD.p7s"))
        }
    members.update(injected)
    record_names = [name for name in members if name.endswith(".dist-info/RECORD")]
    if len(record_names) != 1:
        raise PackageDistributionError(f"wheel RECORD 数量不是 1: {len(record_names)}")
    record_name = record_names[0]
    members[record_name] = _wheel_record(members, record_name)
    replacement = wheel.with_suffix(".rewrite.whl")
    with zipfile.ZipFile(
        replacement,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for name, payload in sorted(members.items()):
            archive.writestr(name, payload)
    replacement.replace(wheel)


def _wheel_record(members: dict[str, bytes], record_name: str) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    for name, payload in sorted(members.items()):
        if name == record_name:
            writer.writerow((name, "", ""))
            continue
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode()
        writer.writerow((name, f"sha256={digest.rstrip('=')}", len(payload)))
    return stream.getvalue().encode("utf-8")


def _artifact_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


__all__ = [
    "BuildArtifact",
    "PackageDistributionError",
    "audit_wheel",
    "build_workspace_wheel",
]
