"""工作流源码（Workflow Source）的受限本地文件访问实现。"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from unilabos.workflow.source_publication import (
    NO_EXPECTED_HASH,
    SourcePublicationConflict,
    SourcePublicationError,
    atomic_publish_source,
)

MANIFEST_BYTE_LIMIT = 1024 * 1024
WORKFLOW_SOURCE_BYTE_LIMIT = 8 * 1024 * 1024


class SourceWorkspaceError(RuntimeError):
    """表示授权源码工作区无法被安全读取。"""

    def __init__(self, code: str):
        """保存稳定工作区错误码。

        参数：``code`` 表示无效包目录或无效声明文件。
        返回：无；错误不携带文件内容。
        """

        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PackageRootSnapshot:
    """一次显式授权目录读取时固定的目录身份。"""

    selected_root: Path
    identity: tuple[int, int]
    manifest_bytes: bytes


@dataclass(frozen=True)
class PackageSourceSnapshot:
    """经普通文件与 UTF-8 校验后的实际 Python 包目录身份。"""

    package_root: Path
    identity: tuple[int, int]


@dataclass(frozen=True)
class SourceDocument:
    """安全读取后的工作流源码（Workflow Source）内容与版本事实。"""

    python_source: str
    draft_hash: str
    update_time: str


class SourceWorkspaceConflict(RuntimeError):
    """表示工作流源码 CAS 条件与当前物理文件不一致。"""


def validate_source_registration(
    *,
    package_root: str | Path,
    relative_path: str,
) -> tuple[Path, str]:
    """验证单项来源注册指向受限的 ``workflows/*.py`` 路径。

    参数：``package_root`` 是实际 Python 包目录；``relative_path`` 是包内源码路径。
    返回：规范绝对包目录和规范 POSIX 相对路径。
    异常：目录、路径、符号链接或既有目标不安全时抛出 ``SourceWorkspaceError``。
    """

    registration = {
        "package_root": str(package_root),
        "relative_path": relative_path,
    }
    root, normalized_relative, root_identity = _source_location(registration)
    with _source_parent_descriptor(
        root,
        normalized_relative,
        expected_root_identity=root_identity,
        create=False,
    ) as source_parent:
        if source_parent is not None:
            parent_descriptor, filename = source_parent
            try:
                metadata = os.stat(
                    filename,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            except OSError:
                raise SourceWorkspaceError("invalid_input") from None
            else:
                if not stat.S_ISREG(metadata.st_mode):
                    raise SourceWorkspaceError("invalid_input")
    return root, normalized_relative.as_posix()


def read_registered_source(
    registration: Mapping[str, Any],
) -> SourceDocument | None:
    """读取一项已注册工作流源码并执行普通文件、UTF-8 与大小校验。

    参数：``registration`` 含持久化 ``package_root`` 与 ``relative_path``。
    返回：源码缺失时为 ``None``，否则返回内容、哈希和修改时间。
    异常：路径不安全、非普通文件、超限或非 UTF-8 时抛出
    ``SourceWorkspaceError``。
    """

    root, relative, root_identity = _source_location(registration)
    with _source_parent_descriptor(
        root,
        relative,
        expected_root_identity=root_identity,
        create=False,
    ) as source_parent:
        if source_parent is None:
            return None
        parent_descriptor, filename = source_parent
        descriptor = -1
        try:
            descriptor = os.open(filename, _file_flags(), dir_fd=parent_descriptor)
        except FileNotFoundError:
            return None
        except OSError:
            raise SourceWorkspaceError("invalid_input") from None
        try:
            metadata = os.fstat(descriptor)
            raw_source = _read_regular_descriptor(
                descriptor,
                byte_limit=WORKFLOW_SOURCE_BYTE_LIMIT,
                error_code="invalid_input",
            )
        finally:
            os.close(descriptor)
    try:
        python_source = raw_source.decode("utf-8")
    except UnicodeError:
        raise SourceWorkspaceError("invalid_input") from None
    return SourceDocument(
        python_source=python_source,
        draft_hash=_sha256(raw_source),
        update_time=_mtime_rfc3339(metadata.st_mtime),
    )


def registered_source_signature(
    registration: Mapping[str, Any],
) -> tuple[Any, ...]:
    """返回无需读取内容的安全文件签名，供源码监视器（Source Monitor）去抖。

    参数：``registration`` 是已持久化来源身份。
    返回：缺失标记，或普通文件的设备、索引节点、大小和时间签名。
    异常：路径不安全或目标不是普通文件时抛出 ``SourceWorkspaceError``。
    """

    root, relative, root_identity = _source_location(registration)
    with _source_parent_descriptor(
        root,
        relative,
        expected_root_identity=root_identity,
        create=False,
    ) as source_parent:
        if source_parent is None:
            return ("missing",)
        parent_descriptor, filename = source_parent
        try:
            metadata = os.stat(
                filename,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return ("missing",)
        except OSError:
            raise SourceWorkspaceError("invalid_input") from None
        if not stat.S_ISREG(metadata.st_mode):
            raise SourceWorkspaceError("invalid_input")
    return (
        "file",
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def write_registered_source(
    registration: Mapping[str, Any],
    content: bytes,
    *,
    expected_hash: object | str | None = NO_EXPECTED_HASH,
) -> None:
    """在注册来源的规范路径执行受限原子 CAS 写入。

    参数：``registration`` 是来源身份；``content`` 是 UTF-8 源码字节；
    ``expected_hash`` 是可选的当前草稿哈希条件。
    返回：无；成功时规范路径完整替换且目录已同步。
    异常：内容超限或路径不安全抛出 ``SourceWorkspaceError``；CAS 冲突抛出
    ``SourceWorkspaceConflict``。
    """

    if len(content) > WORKFLOW_SOURCE_BYTE_LIMIT:
        raise SourceWorkspaceError("invalid_input")
    root, relative, root_identity = _source_location(registration)
    # 首次保存允许创建固定的 workflows 目录，但不创建 manifest 声明本身。
    with _source_parent_descriptor(
        root,
        relative,
        expected_root_identity=root_identity,
        create=True,
    ):
        pass
    with _source_parent_descriptor(
        root,
        relative,
        expected_root_identity=root_identity,
        create=False,
    ) as source_parent:
        if source_parent is None:
            raise SourceWorkspaceError("invalid_input")
        parent_descriptor, filename = source_parent
        try:
            atomic_publish_source(
                parent_descriptor=parent_descriptor,
                target_name=filename,
                content=content,
                byte_limit=WORKFLOW_SOURCE_BYTE_LIMIT,
                expected_hash=expected_hash,
            )
        except SourcePublicationConflict:
            raise SourceWorkspaceConflict("draft_hash_conflict") from None
        except SourcePublicationError:
            raise SourceWorkspaceError("internal_error") from None


def read_package_root(selected_root: str | Path) -> PackageRootSnapshot:
    """安全读取显式授权目录中的 ``package.yaml``。

    参数：``selected_root`` 是启动配置明确列出的包选择目录。
    返回：目录路径、设备/索引节点身份和受限大小的 manifest 字节。
    异常：目录、符号链接或 manifest 不安全时抛出 ``SourceWorkspaceError``。
    """

    root = Path(os.path.abspath(selected_root))
    try:
        root_metadata = root.lstat()
    except (OSError, TypeError, ValueError):
        raise SourceWorkspaceError("invalid_package_root") from None
    if not stat.S_ISDIR(root_metadata.st_mode) or _contains_symlink(root):
        raise SourceWorkspaceError("invalid_package_root")

    directory_flags = _directory_flags()
    file_flags = _file_flags()
    root_descriptor = -1
    manifest_descriptor = -1
    try:
        root_descriptor = _open_directory_chain(root, flags=directory_flags)
        opened_metadata = os.fstat(root_descriptor)
        if (opened_metadata.st_dev, opened_metadata.st_ino) != (
            root_metadata.st_dev,
            root_metadata.st_ino,
        ):
            raise SourceWorkspaceError("invalid_package_root")
        manifest_descriptor = os.open(
            "package.yaml",
            file_flags,
            dir_fd=root_descriptor,
        )
        manifest_bytes = _read_regular_descriptor(
            manifest_descriptor,
            byte_limit=MANIFEST_BYTE_LIMIT,
            error_code="invalid_manifest",
        )
    except SourceWorkspaceError:
        raise
    except (OSError, TypeError, ValueError):
        raise SourceWorkspaceError("invalid_manifest") from None
    finally:
        if manifest_descriptor >= 0:
            os.close(manifest_descriptor)
        if root_descriptor >= 0:
            os.close(root_descriptor)
    return PackageRootSnapshot(
        selected_root=root,
        identity=(root_metadata.st_dev, root_metadata.st_ino),
        manifest_bytes=manifest_bytes,
    )


def validate_declared_sources(
    root_snapshot: PackageRootSnapshot,
    *,
    package_id: str,
    relative_paths: Iterable[str],
) -> PackageSourceSnapshot:
    """校验 manifest 指向的实际 Python 包目录和已有源码。

    参数：``root_snapshot`` 是 manifest 读取时固定的授权目录身份；
    ``package_id`` 是已验证的包目录名；``relative_paths`` 是规范
    ``workflows/*.py`` 路径集合。
    返回：实际 Python 包目录路径及其设备/索引节点身份。
    异常：目录身份变化、符号链接、非普通文件、超限或非 UTF-8 时抛出
    ``SourceWorkspaceError``；缺失源码保持合法且不会被创建。
    """

    selected_descriptor = -1
    package_descriptor = -1
    workflows_descriptor = -1
    try:
        selected_descriptor = _open_directory_chain(
            root_snapshot.selected_root,
            flags=_directory_flags(),
        )
        selected_metadata = os.fstat(selected_descriptor)
        if (
            selected_metadata.st_dev,
            selected_metadata.st_ino,
        ) != root_snapshot.identity:
            raise SourceWorkspaceError("invalid_package_root")
        package_descriptor = _open_child_directory(
            selected_descriptor,
            package_id,
            missing_ok=False,
        )
        assert package_descriptor is not None
        package_metadata = os.fstat(package_descriptor)
        workflows_descriptor = _open_child_directory(
            package_descriptor,
            "workflows",
            missing_ok=True,
        )
        if workflows_descriptor is not None:
            for relative_path in tuple(relative_paths):
                # 路径结构已由 manifest 模块验证；这里只用最终文件名做 dir_fd 读取。
                filename = PurePosixPath(relative_path).name
                source_bytes = _read_optional_regular_at(
                    workflows_descriptor,
                    filename,
                    byte_limit=WORKFLOW_SOURCE_BYTE_LIMIT,
                    error_code="invalid_workflow_source",
                )
                if source_bytes is not None:
                    try:
                        source_bytes.decode("utf-8")
                    except UnicodeError:
                        raise SourceWorkspaceError("invalid_workflow_source") from None
    except SourceWorkspaceError:
        raise
    except (OSError, TypeError, ValueError):
        raise SourceWorkspaceError("invalid_package_root") from None
    finally:
        if workflows_descriptor is not None and workflows_descriptor >= 0:
            os.close(workflows_descriptor)
        if package_descriptor >= 0:
            os.close(package_descriptor)
        if selected_descriptor >= 0:
            os.close(selected_descriptor)
    return PackageSourceSnapshot(
        package_root=root_snapshot.selected_root / package_id,
        identity=(package_metadata.st_dev, package_metadata.st_ino),
    )


def _directory_flags() -> int:
    """返回当前平台可用的安全目录打开标志。

    参数：无。
    返回：禁止跟随符号链接并要求目录类型的 ``os.open`` 标志。
    """

    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _file_flags() -> int:
    """返回当前平台可用的安全只读文件打开标志。

    参数：无。
    返回：禁止跟随符号链接且避免 FIFO 阻塞的 ``os.open`` 标志。
    """

    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _contains_symlink(path: Path) -> bool:
    """判断绝对路径链中是否含符号链接。

    参数：``path`` 是待核验的显式授权目录。
    返回：任一祖先或目录本身是符号链接时为 ``True``。
    """

    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
    return False


def _source_location(
    registration: Mapping[str, Any],
) -> tuple[Path, PurePosixPath, tuple[int, int]]:
    """规范并验证一项持久来源注册的目录与相对路径。

    参数：``registration`` 必须含 ``package_root`` 和 ``relative_path``。
    返回：无符号链接的绝对包目录、规范 ``workflows/*.py`` 路径和目录身份。
    异常：字段、目录或路径不合法时抛出 ``SourceWorkspaceError``。
    """

    try:
        stored_root = Path(registration["package_root"])
        raw_relative = registration["relative_path"]
    except (KeyError, TypeError, ValueError):
        raise SourceWorkspaceError("invalid_input") from None
    if (
        not isinstance(raw_relative, str)
        or "\\" in raw_relative
        or "\x00" in raw_relative
    ):
        raise SourceWorkspaceError("invalid_input")
    absolute_root = Path(os.path.abspath(stored_root))
    try:
        root_metadata = absolute_root.lstat()
    except OSError:
        raise SourceWorkspaceError("invalid_input") from None
    if not stat.S_ISDIR(root_metadata.st_mode) or _contains_symlink(absolute_root):
        raise SourceWorkspaceError("invalid_input")
    try:
        root = absolute_root.resolve(strict=True)
    except OSError:
        raise SourceWorkspaceError("invalid_input") from None
    if not root.is_dir():
        raise SourceWorkspaceError("invalid_input")
    relative = PurePosixPath(raw_relative)
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or relative.parts[0] != "workflows"
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.suffix != ".py"
        or not relative.stem
    ):
        raise SourceWorkspaceError("invalid_input")
    return root, relative, (root_metadata.st_dev, root_metadata.st_ino)


@contextmanager
def _source_parent_descriptor(
    root: Path,
    relative: PurePosixPath,
    *,
    expected_root_identity: tuple[int, int],
    create: bool,
) -> Iterator[tuple[int, str] | None]:
    """固定已注册源码的 ``workflows`` 父目录文件描述符。

    参数：``root`` 和 ``relative`` 是已规范来源路径；``expected_root_identity``
    固定本次操作开始时的包目录身份；``create`` 决定是否允许创建固定父目录。
    返回：上下文中产生父目录描述符和源码文件名；允许缺失时可产生 ``None``。
    异常：目录链含符号链接、类型错误或发生竞态时抛出 ``SourceWorkspaceError``。
    """

    root_descriptor = _open_directory_chain(root, flags=_directory_flags())
    parent_descriptor = -1
    try:
        opened_root_metadata = os.fstat(root_descriptor)
        if (
            opened_root_metadata.st_dev,
            opened_root_metadata.st_ino,
        ) != expected_root_identity:
            raise SourceWorkspaceError("invalid_input")
        try:
            parent_descriptor = os.open(
                relative.parts[0],
                _directory_flags(),
                dir_fd=root_descriptor,
            )
        except FileNotFoundError:
            if not create:
                yield None
                return
            with suppress(FileExistsError):
                os.mkdir(relative.parts[0], 0o755, dir_fd=root_descriptor)
            parent_descriptor = os.open(
                relative.parts[0],
                _directory_flags(),
                dir_fd=root_descriptor,
            )
        yield parent_descriptor, relative.parts[1]
    except SourceWorkspaceError:
        raise
    except OSError:
        raise SourceWorkspaceError("invalid_input") from None
    finally:
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
        os.close(root_descriptor)


def _open_directory_chain(path: Path, *, flags: int) -> int:
    """逐级以 ``O_NOFOLLOW`` 打开目录并返回最终文件描述符。

    参数：``path`` 是绝对目录；``flags`` 是平台可用的安全目录标志。
    返回：调用者负责关闭的最终目录文件描述符。
    异常：任一级目录不可安全打开时抛出 ``SourceWorkspaceError``。
    """

    current_descriptor = -1
    try:
        current_descriptor = os.open(path.anchor, flags)
        for part in path.parts[1:]:
            next_descriptor = os.open(part, flags, dir_fd=current_descriptor)
            os.close(current_descriptor)
            current_descriptor = next_descriptor
        return current_descriptor
    except (OSError, TypeError, ValueError):
        if current_descriptor >= 0:
            os.close(current_descriptor)
        raise SourceWorkspaceError("invalid_package_root") from None


def _open_child_directory(
    parent_descriptor: int,
    name: str,
    *,
    missing_ok: bool,
) -> int | None:
    """相对已固定目录安全打开一个直接子目录。

    参数：``parent_descriptor`` 是父目录文件描述符；``name`` 是单段目录名；
    ``missing_ok`` 决定缺失目录是否返回 ``None``。
    返回：调用者负责关闭的子目录文件描述符，或在允许缺失时返回 ``None``。
    异常：符号链接、非目录或不允许的缺失抛出 ``SourceWorkspaceError``。
    """

    try:
        return os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise SourceWorkspaceError("invalid_package_root") from None
    except (OSError, TypeError, ValueError):
        raise SourceWorkspaceError("invalid_package_root") from None


def _read_optional_regular_at(
    parent_descriptor: int,
    name: str,
    *,
    byte_limit: int,
    error_code: str,
) -> bytes | None:
    """相对固定目录读取一个允许缺失的受限普通文件。

    参数：``parent_descriptor`` 是父目录；``name`` 是单段文件名；
    ``byte_limit`` 是读取上限；``error_code`` 是失败时的稳定分类。
    返回：文件缺失时为 ``None``，否则返回不超过上限的字节。
    异常：符号链接、非普通文件、超限或读取失败抛出 ``SourceWorkspaceError``。
    """

    descriptor = -1
    try:
        descriptor = os.open(name, _file_flags(), dir_fd=parent_descriptor)
    except FileNotFoundError:
        return None
    except (OSError, TypeError, ValueError):
        raise SourceWorkspaceError(error_code) from None
    try:
        return _read_regular_descriptor(
            descriptor,
            byte_limit=byte_limit,
            error_code=error_code,
        )
    finally:
        os.close(descriptor)


def _read_regular_descriptor(
    descriptor: int,
    *,
    byte_limit: int,
    error_code: str,
) -> bytes:
    """在硬字节上限内读取普通文件。

    参数：``descriptor`` 是已安全打开的文件；``byte_limit`` 是最大字节数；
    ``error_code`` 是失败时稳定映射的错误码。
    返回：不超过上限的文件字节。
    异常：非普通文件、超限或读取失败时抛出 ``SourceWorkspaceError``。
    """

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > byte_limit:
            raise SourceWorkspaceError(error_code)
        chunks = bytearray()
        while len(chunks) <= byte_limit:
            chunk = os.read(
                descriptor,
                min(64 * 1024, byte_limit + 1 - len(chunks)),
            )
            if not chunk:
                break
            chunks.extend(chunk)
        if len(chunks) > byte_limit:
            raise SourceWorkspaceError(error_code)
        return bytes(chunks)
    except SourceWorkspaceError:
        raise
    except (OSError, OverflowError, ValueError):
        raise SourceWorkspaceError(error_code) from None


def _sha256(content: bytes) -> str:
    """计算工作流源码字节的稳定 SHA-256。

    参数：``content`` 是完整源码字节。
    返回：带 ``sha256:`` 前缀的小写十六进制摘要。
    """

    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _mtime_rfc3339(value: float) -> str:
    """把文件系统修改时间转换成 UTC RFC3339 文本。

    参数：``value`` 是文件系统返回的 Unix 秒时间。
    返回：以 ``Z`` 结尾的 UTC 时间文本。
    """

    return (
        datetime.fromtimestamp(value, tz=timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "MANIFEST_BYTE_LIMIT",
    "NO_EXPECTED_HASH",
    "WORKFLOW_SOURCE_BYTE_LIMIT",
    "PackageRootSnapshot",
    "PackageSourceSnapshot",
    "SourceDocument",
    "SourceWorkspaceConflict",
    "SourceWorkspaceError",
    "read_package_root",
    "read_registered_source",
    "registered_source_signature",
    "validate_declared_sources",
    "validate_source_registration",
    "write_registered_source",
]
