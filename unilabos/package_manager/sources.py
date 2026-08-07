"""显式工作区（Workspace）的受限本地文件来源。"""

from __future__ import annotations

import hashlib
import os
import stat
import zipfile
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, runtime_checkable


@runtime_checkable
class PackageSource(Protocol):
    """由调用方显式授权的一次领域包观测。"""

    @property
    def source_kind(self) -> str: ...

    def read_bytes(self, logical_path: str) -> bytes: ...


@dataclass(frozen=True)
class WorkspaceSource:
    """由公共命令行（CLI）显式授权的一次工作区文件来源。"""

    # ``root`` 是不经过符号链接的规范工作区根目录，也是全部文件读取的授权边界。
    root: Path

    def __init__(self, root: str | Path):
        """固定不经过符号链接的工作区根目录。

        参数：``root`` 是调用者显式选择的工作区目录。
        返回：无；构造后的 ``root`` 是规范绝对路径。
        异常：目录缺失、不是目录或任一路径段是符号链接时抛出 ``ValueError``。
        """

        selected_root = Path(os.path.abspath(Path(root).expanduser()))
        try:
            resolved_root = selected_root.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError("工作区（Workspace）根目录不存在或不可访问") from error
        if (
            selected_root.is_symlink()
            or not resolved_root.is_dir()
            or resolved_root != selected_root
        ):
            raise ValueError("工作区（Workspace）根目录必须是无符号链接的目录")
        object.__setattr__(self, "root", resolved_root)

    @property
    def source_kind(self) -> Literal["workspace"]:
        """返回包来源的稳定类型。

        参数：无。
        返回：固定 wire value ``workspace``。
        异常：无。
        """

        return "workspace"

    def read_bytes(self, logical_path: str) -> bytes:
        """读取工作区根目录内的一个普通文件。

        参数：``logical_path`` 是使用 POSIX 分隔符的工作区相对文件路径。
        返回：文件的原始字节。
        异常：路径非法、缺失、越界、包含符号链接或不是普通文件时抛出
        ``ValueError``。
        """

        resolved_file = self._resolve_regular_file(logical_path, required=True)
        assert resolved_file is not None
        try:
            return resolved_file.read_bytes()
        except OSError as error:
            raise ValueError(f"工作区文件不可读: {logical_path}") from error

    def has_file(self, logical_path: str) -> bool:
        """判断工作区内是否存在一个安全普通文件。

        参数：``logical_path`` 是使用 POSIX 分隔符的工作区相对文件路径。
        返回：文件安全存在时为 ``True``，缺失时为 ``False``。
        异常：路径非法、越界、包含符号链接或目标不是普通文件时抛出
        ``ValueError``，避免把不安全对象解释成缺失。
        """

        return self._resolve_regular_file(logical_path, required=False) is not None

    def _resolve_regular_file(
        self,
        logical_path: str,
        *,
        required: bool,
    ) -> Path | None:
        """解析并验证一个工作区相对普通文件。

        参数：``logical_path`` 是待解析相对路径；``required`` 决定缺失时是否失败。
        返回：安全文件的规范路径；仅可选且缺失时返回 ``None``。
        异常：非法路径、符号链接、目录逃逸或非普通文件抛出 ``ValueError``。
        """

        logical_file = _safe_logical_path(logical_path)
        selected_file = self.root.joinpath(*logical_file.parts)
        if not selected_file.exists():
            if required:
                raise ValueError(f"工作区文件不存在: {logical_path}")
            return None
        if selected_file.is_symlink() or any(
            parent.is_symlink()
            for parent in selected_file.parents
            if parent != self.root and parent.is_relative_to(self.root)
        ):
            raise ValueError(f"工作区文件不得经过符号链接: {logical_path}")
        try:
            resolved_file = selected_file.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError(f"工作区文件不可访问: {logical_path}") from error
        if not resolved_file.is_relative_to(self.root) or not resolved_file.is_file():
            raise ValueError(f"工作区文件路径越界或不是普通文件: {logical_path}")
        return resolved_file


@dataclass(frozen=True)
class CachedArchiveSource:
    """已缓存且受摘要约束的 wheel 领域包来源。"""

    wheel: Path
    expected_digest: str

    def __init__(self, wheel: str | Path, expected_digest: str):
        object.__setattr__(self, "wheel", Path(wheel))
        object.__setattr__(self, "expected_digest", expected_digest)

    @property
    def source_kind(self) -> Literal["cached_archive"]:
        return "cached_archive"

    def verify_artifact(self) -> None:
        if self.wheel.is_symlink() or not self.wheel.is_file():
            raise ValueError(f"wheel 不存在: {self.wheel}")
        if self.wheel.stat().st_size > _MAX_ARCHIVE_BYTES:
            raise ValueError(f"wheel 超过 artifact 大小上限: {self.wheel}")
        digest = hashlib.sha256()
        with self.wheel.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        actual = "sha256:" + digest.hexdigest()
        if not self.expected_digest or actual != self.expected_digest:
            raise ValueError(
                f"artifact digest mismatch: {actual} != "
                f"{self.expected_digest or '-'}"
            )

    def embedded_catalog_bytes(self) -> bytes:
        self.verify_artifact()
        return self.read_bytes(self._single_catalog_member())

    def read_bytes(self, logical_path: str) -> bytes:
        logical = _safe_logical_path(logical_path).as_posix()
        try:
            with zipfile.ZipFile(self.wheel) as archive:
                infos = _validated_archive_infos(archive)
                matches = [item for item in infos if item.filename == logical]
                if len(matches) != 1:
                    raise ValueError(
                        f"wheel 中 Package source 数量不是 1: {logical_path}"
                    )
                item = matches[0]
                mode = item.external_attr >> 16
                if item.is_dir() or stat.S_ISLNK(mode):
                    raise ValueError(
                        f"wheel Package source 非普通文件: {logical_path}"
                    )
                return archive.read(item)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"wheel 格式无效: {self.wheel}") from exc

    def members(self) -> tuple[str, ...]:
        self.verify_artifact()
        try:
            with zipfile.ZipFile(self.wheel) as archive:
                return tuple(
                    sorted(item.filename for item in _validated_archive_infos(archive))
                )
        except zipfile.BadZipFile as exc:
            raise ValueError(f"wheel 格式无效: {self.wheel}") from exc

    def _single_catalog_member(self) -> str:
        candidates = [
            name
            for name in self.members()
            if PurePosixPath(name).parts[-2:]
            == ("_generated", "package.catalog.json")
        ]
        if len(candidates) != 1:
            raise ValueError(
                f"wheel embedded PackageCatalog 数量不是 1: {len(candidates)}"
            )
        return candidates[0]


@dataclass(frozen=True)
class InstalledDistributionSource:
    """已安装分发包中的领域包来源。"""

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
        distribution = self._distribution()
        candidates = [
            entry
            for entry in distribution.files or ()
            if PurePosixPath(str(entry)).parts[-2:]
            == ("_generated", "package.catalog.json")
        ]
        if len(candidates) != 1:
            raise ValueError(
                "installed distribution embedded PackageCatalog 数量不是 1: "
                f"{len(candidates)}"
            )
        return _read_installed_file(distribution, str(candidates[0]))

    def read_bytes(self, logical_path: str) -> bytes:
        logical = _safe_logical_path(logical_path).as_posix()
        return _read_installed_file(self._distribution(), logical)

    def members(self) -> tuple[str, ...]:
        return tuple(
            sorted(str(entry) for entry in self._distribution().files or ())
        )


def _safe_logical_path(logical_path: str) -> PurePosixPath:
    """校验一个工作区逻辑路径不含逃逸语义。

    参数：``logical_path`` 是调用者提供的相对逻辑路径。
    返回：规范 ``PurePosixPath``。
    异常：绝对路径、空路径、反斜杠或父目录段抛出 ``ValueError``。
    """

    if not isinstance(logical_path, str) or not logical_path or "\\" in logical_path:
        raise ValueError("工作区逻辑路径必须是非空 POSIX 相对路径")
    logical_file = PurePosixPath(logical_path)
    if (
        logical_file.is_absolute()
        or not logical_file.parts
        or any(part in {"", ".", ".."} for part in logical_file.parts)
    ):
        raise ValueError(f"工作区逻辑路径非法: {logical_path}")
    return logical_file


def _read_installed_file(
    distribution: metadata.Distribution,
    logical_path: str,
) -> bytes:
    entries = [
        entry for entry in distribution.files or () if str(entry) == logical_path
    ]
    if len(entries) != 1:
        raise ValueError(
            f"installed distribution 中 Package source 数量不是 1: "
            f"{logical_path}"
        )
    distribution_root = Path(distribution.locate_file(".")).resolve()
    path = Path(distribution.locate_file(entries[0]))
    if (
        path.is_symlink()
        or any(
            parent.is_symlink()
            for parent in path.parents
            if parent != distribution_root
            and parent.is_relative_to(distribution_root)
        )
        or not path.resolve().is_relative_to(distribution_root)
        or not path.is_file()
    ):
        raise ValueError(
            f"installed distribution Package source 非普通文件: "
            f"{logical_path}"
        )
    return path.read_bytes()


def _validated_archive_infos(
    archive: zipfile.ZipFile,
) -> tuple[zipfile.ZipInfo, ...]:
    infos = tuple(archive.infolist())
    if len(infos) > _MAX_ARCHIVE_MEMBERS:
        raise ValueError("wheel 成员数量超过上限")
    names: set[str] = set()
    total = 0
    for item in infos:
        _safe_logical_path(item.filename.rstrip("/"))
        if item.filename in names:
            raise ValueError(f"wheel 包含重复成员: {item.filename}")
        names.add(item.filename)
        mode = item.external_attr >> 16
        if item.flag_bits & 0x1:
            raise ValueError(f"wheel 包含加密成员: {item.filename}")
        if stat.S_ISLNK(mode):
            raise ValueError(f"wheel 包含 symlink 成员: {item.filename}")
        if item.file_size > _MAX_MEMBER_BYTES:
            raise ValueError(f"wheel 成员超过大小上限: {item.filename}")
        total += item.file_size
        if total > _MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise ValueError("wheel 解压后总大小超过上限")
        if (item.file_size > 0 and item.compress_size == 0) or (
            item.compress_size > 0
            and item.file_size / item.compress_size > _MAX_COMPRESSION_RATIO
        ):
            raise ValueError(f"wheel 成员压缩比超过上限: {item.filename}")
    return infos


__all__ = [
    "CachedArchiveSource",
    "InstalledDistributionSource",
    "PackageSource",
    "WorkspaceSource",
]
