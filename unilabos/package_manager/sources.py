"""显式 Package Source Adapter。"""

from __future__ import annotations

import hashlib
import stat
import zipfile
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, runtime_checkable


@runtime_checkable
class PackageSource(Protocol):
    """由调用方显式授权的一次 package observation。"""

    @property
    def source_kind(self) -> str: ...

    def read_bytes(self, logical_path: str) -> bytes: ...


@dataclass(frozen=True)
class WorkspaceSource:
    root: Path

    def __init__(self, root: str | Path):
        object.__setattr__(self, "root", Path(root))

    @property
    def source_kind(self) -> Literal["workspace"]:
        return "workspace"

    def read_bytes(self, logical_path: str) -> bytes:
        logical = _safe_logical_path(logical_path)
        root = self.root.resolve()
        path = root.joinpath(*logical.parts)
        if path.is_symlink() or any(
            parent.is_symlink()
            for parent in path.parents
            if parent != root and parent.is_relative_to(root)
        ):
            raise ValueError(f"Package source 不得通过 symlink 读取: {logical_path}")
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValueError(f"Package source 不存在: {logical_path}") from exc
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValueError(f"Package source 路径逃逸: {logical_path}")
        return resolved.read_bytes()


@dataclass(frozen=True)
class CachedArchiveSource:
    wheel: Path
    expected_digest: str

    def __init__(self, wheel: str | Path, expected_digest: str):
        object.__setattr__(self, "wheel", Path(wheel))
        object.__setattr__(self, "expected_digest", expected_digest)

    @property
    def source_kind(self) -> Literal["cached_archive"]:
        return "cached_archive"

    def verify_artifact(self) -> None:
        if not self.wheel.is_file():
            raise ValueError(f"wheel 不存在: {self.wheel}")
        actual = "sha256:" + hashlib.sha256(self.wheel.read_bytes()).hexdigest()
        if not self.expected_digest or actual != self.expected_digest:
            raise ValueError(
                f"artifact digest mismatch: {actual} != {self.expected_digest or '-'}"
            )

    def embedded_catalog_bytes(self) -> bytes:
        self.verify_artifact()
        member = self._single_catalog_member()
        return self.read_bytes(member)

    def read_bytes(self, logical_path: str) -> bytes:
        logical = _safe_logical_path(logical_path).as_posix()
        try:
            with zipfile.ZipFile(self.wheel) as archive:
                matches = [
                    item for item in archive.infolist() if item.filename == logical
                ]
                if len(matches) != 1:
                    raise ValueError(
                        f"wheel 中 Package source 数量不是 1: {logical_path}"
                    )
                item = matches[0]
                mode = item.external_attr >> 16
                if item.is_dir() or stat.S_ISLNK(mode):
                    raise ValueError(f"wheel Package source 非普通文件: {logical_path}")
                return archive.read(item)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"wheel 格式无效: {self.wheel}") from exc

    def members(self) -> tuple[str, ...]:
        self.verify_artifact()
        try:
            with zipfile.ZipFile(self.wheel) as archive:
                return tuple(sorted(item.filename for item in archive.infolist()))
        except zipfile.BadZipFile as exc:
            raise ValueError(f"wheel 格式无效: {self.wheel}") from exc

    def _single_catalog_member(self) -> str:
        candidates = [
            name
            for name in self.members()
            if PurePosixPath(name).parts[-2:]
            == (
                "_generated",
                "package.catalog.json",
            )
        ]
        if len(candidates) != 1:
            raise ValueError(
                f"wheel embedded PackageCatalog 数量不是 1: {len(candidates)}"
            )
        return candidates[0]


@dataclass(frozen=True)
class InstalledDistributionSource:
    distribution: str

    @property
    def source_kind(self) -> Literal["installed_distribution"]:
        return "installed_distribution"

    def _distribution(self) -> metadata.Distribution:
        try:
            return metadata.distribution(self.distribution)
        except metadata.PackageNotFoundError as exc:
            raise ValueError(
                f"installed distribution 不存在: {self.distribution}"
            ) from exc

    def embedded_catalog_bytes(self) -> bytes:
        dist = self._distribution()
        candidates = [
            entry
            for entry in dist.files or ()
            if PurePosixPath(str(entry)).parts[-2:]
            == ("_generated", "package.catalog.json")
        ]
        if len(candidates) != 1:
            raise ValueError(
                "installed distribution embedded PackageCatalog 数量不是 1: "
                f"{len(candidates)}"
            )
        return _read_installed_file(dist, str(candidates[0]))

    def read_bytes(self, logical_path: str) -> bytes:
        logical = _safe_logical_path(logical_path).as_posix()
        return _read_installed_file(self._distribution(), logical)

    def members(self) -> tuple[str, ...]:
        return tuple(sorted(str(entry) for entry in self._distribution().files or ()))


def _read_installed_file(dist: metadata.Distribution, logical_path: str) -> bytes:
    entries = [entry for entry in dist.files or () if str(entry) == logical_path]
    if len(entries) != 1:
        raise ValueError(
            f"installed distribution 中 Package source 数量不是 1: {logical_path}"
        )
    path = Path(dist.locate_file(entries[0]))
    if path.is_symlink() or not path.is_file():
        raise ValueError(
            f"installed distribution Package source 非普通文件: {logical_path}"
        )
    return path.read_bytes()


def _safe_logical_path(logical_path: str) -> PurePosixPath:
    logical = PurePosixPath(logical_path)
    if logical.is_absolute() or ".." in logical.parts or "\\" in logical_path:
        raise ValueError(f"Package source 路径非法: {logical_path}")
    return logical


__all__ = [
    "CachedArchiveSource",
    "InstalledDistributionSource",
    "PackageSource",
    "WorkspaceSource",
]
