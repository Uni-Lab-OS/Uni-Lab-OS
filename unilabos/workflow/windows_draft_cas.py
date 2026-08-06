"""Windows editable Workflow Draft 的文件系统 CAS 边界。"""

from __future__ import annotations

import ctypes
import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from unilabos.workflow.windows_file_api import (
    WindowsFileConflict,
    WindowsFileInternalError,
    WindowsFileInvalidTarget,
    hold_directory_chain,
    portable_replace_file_with_backup,
    replace_file_with_backup,
)


class WindowsDraftCasConflict(Exception):
    """Windows Draft 已变化或无法取得排他写入证据。"""


class WindowsDraftCasInvalidTarget(Exception):
    """Windows Draft 路径不再指向注册的普通文件。"""


class WindowsDraftCasInternalError(Exception):
    """Windows Draft CAS 遇到不可归类为并发冲突的系统错误。"""


class WindowsLocking(Protocol):
    """`msvcrt` 暴露给 Draft CAS 的最小字节区间锁 Interface。"""

    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, descriptor: int, mode: int, size: int) -> None:
        """锁定或解锁 `descriptor` 从当前位置开始的 `size` 字节。"""


def _is_reparse_point(stat_result: os.stat_result) -> bool:
    """返回 Windows stat 是否声明当前路径是 reparse point。"""

    return bool(getattr(stat_result, "st_file_attributes", 0) & 0x400)


def _sha256(content: bytes) -> str:
    """返回 `content` 的 Workflow Draft hash；输入字节保持不变。"""

    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _identity(stat_result: os.stat_result) -> tuple[int, int]:
    """返回可比较的文件系统身份；参数只读，结果不含易变时间戳。"""

    return stat_result.st_dev, stat_result.st_ino


def _read_regular_descriptor(descriptor: int, *, byte_limit: int) -> bytes:
    """从头有界读取普通文件描述符，超过 `byte_limit` 时拒绝。"""

    stat_result = os.fstat(descriptor)
    if not stat.S_ISREG(stat_result.st_mode) or stat_result.st_size > byte_limit:
        raise WindowsDraftCasConflict
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = byte_limit + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    content = b"".join(chunks)
    if len(content) > byte_limit:
        raise WindowsDraftCasConflict
    return content


def _read_regular_path(path: Path, *, byte_limit: int) -> bytes:
    """按路径有界读取普通文件，并验证打开前后的文件身份未变化。"""

    descriptor = -1
    try:
        before = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(before.st_mode):
            raise WindowsDraftCasInvalidTarget
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(before):
            raise WindowsDraftCasConflict
        content = _read_regular_descriptor(descriptor, byte_limit=byte_limit)
        after = path.lstat()
        if path.is_symlink() or _identity(after) != _identity(opened):
            raise WindowsDraftCasConflict
        return content
    except (WindowsDraftCasConflict, WindowsDraftCasInvalidTarget):
        raise
    except (OSError, OverflowError, TypeError, ValueError):
        raise WindowsDraftCasConflict from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_directory_chain(
    root: Path,
    parent: Path,
) -> tuple[tuple[int, int], tuple[int, int]]:
    """验证 `root` 到 `parent` 无符号链接并返回两端稳定身份。"""

    try:
        root_stat = root.lstat()
        if (
            root.is_symlink()
            or _is_reparse_point(root_stat)
            or not stat.S_ISDIR(root_stat.st_mode)
        ):
            raise WindowsDraftCasInvalidTarget
        relative = parent.relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            current_stat = current.lstat()
            if (
                current.is_symlink()
                or _is_reparse_point(current_stat)
                or not stat.S_ISDIR(current_stat.st_mode)
            ):
                raise WindowsDraftCasInvalidTarget
        parent_stat = parent.lstat()
        return _identity(root_stat), _identity(parent_stat)
    except WindowsDraftCasInvalidTarget:
        raise
    except (OSError, TypeError, ValueError):
        raise WindowsDraftCasInvalidTarget from None


def _create_parent(
    root: Path,
    parent: Path,
) -> tuple[tuple[int, int], tuple[int, int]]:
    """在注册根目录内创建 Draft 父目录并返回校验后的目录身份。"""

    try:
        root_before = root.lstat()
        if (
            root.is_symlink()
            or _is_reparse_point(root_before)
            or not stat.S_ISDIR(root_before.st_mode)
        ):
            raise WindowsDraftCasInvalidTarget
        parent.relative_to(root)
        parent.mkdir(parents=True, exist_ok=True)
        root_after, parent_after = _validate_directory_chain(root, parent)
        if _identity(root_before) != root_after:
            raise WindowsDraftCasInvalidTarget
        return root_after, parent_after
    except WindowsDraftCasInvalidTarget:
        raise
    except (OSError, TypeError, ValueError):
        raise WindowsDraftCasInvalidTarget from None


def _assert_directory_identity(
    root: Path,
    parent: Path,
    expected: tuple[tuple[int, int], tuple[int, int]],
) -> None:
    """确认 Draft 根目录与父目录仍是 `expected` 指向的同一目录。"""

    if _validate_directory_chain(root, parent) != expected:
        raise WindowsDraftCasConflict


def _directory_paths(root: Path, parent: Path) -> tuple[Path, ...]:
    """返回从 registered `root` 到 Draft `parent` 的有序目录链。"""

    relative = parent.relative_to(root)
    paths = [root]
    current = root
    for part in relative.parts:
        current = current / part
        paths.append(current)
    return tuple(paths)


@contextmanager
def _publication_directory_guard(
    root: Path,
    parent: Path,
    expected: tuple[tuple[int, int], tuple[int, int]],
) -> Iterator[None]:
    """固定 Draft 目录链，覆盖临时写入、发布、backup 校验与恢复窗口。

    参数来自同一次注册路径校验，上下文不产出值。Windows 逐级持有不共享
    delete/rename 的目录句柄；任一身份变化都失败关闭，禁止发布到注册根外。
    """

    try:
        if os.name == "nt":
            with hold_directory_chain(
                _directory_paths(root, parent),
                ctypes_api=ctypes,
            ):
                _assert_directory_identity(root, parent, expected)
                yield
                _assert_directory_identity(root, parent, expected)
            return
        _assert_directory_identity(root, parent, expected)
        yield
        _assert_directory_identity(root, parent, expected)
    except WindowsFileConflict:
        raise WindowsDraftCasConflict from None
    except WindowsFileInternalError:
        raise WindowsDraftCasInternalError from None


def _native_replace_with_backup(
    target: Path,
    replacement: Path,
    backup: Path,
) -> None:
    """用 Win32 `ReplaceFileW` 替换，并稳定分类 CAS 与基础设施错误。"""

    try:
        replace_file_with_backup(
            target,
            replacement,
            backup,
            ctypes_api=ctypes,
        )
    except WindowsFileConflict:
        raise WindowsDraftCasConflict from None
    except WindowsFileInternalError:
        raise WindowsDraftCasInternalError from None


def _replace_with_backup(
    target: Path,
    replacement: Path,
    backup: Path,
) -> None:
    """选择当前平台的替换原语；Windows 必须使用单次 `ReplaceFileW`。"""

    if os.name == "nt":
        _native_replace_with_backup(target, replacement, backup)
        return
    try:
        portable_replace_file_with_backup(target, replacement, backup)
    except WindowsFileInvalidTarget:
        raise WindowsDraftCasInvalidTarget from None


def _restore_missing_target(target: Path, backup: Path) -> bool:
    """恢复 Win32 部分失败留下的 canonical 缺口，并返回是否存在目标。"""

    if target.exists():
        return True
    if not backup.exists():
        return False
    try:
        os.rename(backup, target)
    except OSError:
        return False
    return True


def _restore_conflicting_backup(
    *,
    target: Path,
    backup: Path,
    replacement_hash: str,
    byte_limit: int,
) -> bool:
    """把 CAS 竞争者恢复到 canonical，并返回是否完成可证明的恢复。"""

    displaced = target.with_name(f".{target.name}.{uuid4().hex}.displaced")
    restored = False
    for _attempt in range(2):
        try:
            _replace_with_backup(target, backup, displaced)
            restored = True
            break
        except (
            OSError,
            WindowsDraftCasConflict,
            WindowsDraftCasInternalError,
        ):
            continue
    if not restored:
        # `backup` 可能是外部 Authority 的唯一完整副本；无法恢复时保留
        # artifact，并让调用方报告内部不确定性，禁止伪装成已回滚的 409。
        return False
    try:
        displaced_content = _read_regular_path(displaced, byte_limit=byte_limit)
    except (WindowsDraftCasConflict, WindowsDraftCasInvalidTarget):
        displaced_content = None
    if displaced_content is not None and _sha256(displaced_content) == replacement_hash:
        with suppress(OSError):
            displaced.unlink()
        return True

    # rollback 窗口内若又有外部写入，`displaced` 才是更新的外部 Draft；把它
    # 放回 canonical，并把较早竞争者留作 artifact，禁止丢失任一外部版本。
    preserved = target.with_name(f".{target.name}.{uuid4().hex}.cas")
    try:
        _replace_with_backup(target, displaced, preserved)
    except (
        OSError,
        WindowsDraftCasConflict,
        WindowsDraftCasInternalError,
    ):
        return False
    return True


def write_windows_draft_cas(
    *,
    root: Path,
    target: Path,
    content: bytes,
    expected_hash: str | None,
    byte_limit: int,
    locking: WindowsLocking,
) -> None:
    """在 Windows 保存一个 Draft，并以 hash CAS 保护外部编辑。

    `root`/`target` 标识注册源码，`content`/`expected_hash` 是新旧 Draft，
    `byte_limit` 约束源码，`locking` 提供 `msvcrt`。成功没有返回值；只有目录、
    文件身份与旧 hash 都稳定时才发布，否则恢复外部字节并分类抛出异常。
    """

    if len(content) > byte_limit:
        raise WindowsDraftCasInvalidTarget
    directory_identity = _create_parent(root, target.parent)
    with _publication_directory_guard(root, target.parent, directory_identity):
        _write_windows_draft_cas_guarded(
            root=root,
            target=target,
            content=content,
            expected_hash=expected_hash,
            byte_limit=byte_limit,
            locking=locking,
            directory_identity=directory_identity,
        )


def _write_windows_draft_cas_guarded(
    *,
    root: Path,
    target: Path,
    content: bytes,
    expected_hash: str | None,
    byte_limit: int,
    locking: WindowsLocking,
    directory_identity: tuple[tuple[int, int], tuple[int, int]],
) -> None:
    """在已固定的目录链内完成临时写入、CAS 发布和冲突恢复。
    参数与公开 Interface 相同，`directory_identity` 是固定目录身份；成功没有
    返回值，且不得在 guard 外执行任何 canonical 路径写入。
    """

    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    backup = target.with_name(f".{target.name}.{uuid4().hex}.cas")
    descriptor = -1
    target_descriptor = -1
    locked = False
    lock_size = byte_limit + 1
    replacement_hash = _sha256(content)
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

        _assert_directory_identity(root, target.parent, directory_identity)
        if expected_hash is None:
            if target.exists() or target.is_symlink():
                raise WindowsDraftCasConflict
            _assert_directory_identity(root, target.parent, directory_identity)
            try:
                os.link(temporary, target, follow_symlinks=False)
            except OSError:
                if target.exists() or target.is_symlink():
                    raise WindowsDraftCasConflict from None
                raise WindowsDraftCasInternalError from None
            temporary.unlink()
            return

        try:
            target_before = target.lstat()
            if target.is_symlink() or not stat.S_ISREG(target_before.st_mode):
                raise WindowsDraftCasInvalidTarget
            target_descriptor = os.open(
                target,
                os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0),
            )
        except FileNotFoundError:
            raise WindowsDraftCasConflict from None
        opened = os.fstat(target_descriptor)
        if _identity(opened) != _identity(target_before):
            raise WindowsDraftCasConflict
        os.lseek(target_descriptor, 0, os.SEEK_SET)
        try:
            locking.locking(target_descriptor, locking.LK_NBLCK, lock_size)
            locked = True
        except (OSError, ValueError):
            raise WindowsDraftCasConflict from None
        original = _read_regular_descriptor(
            target_descriptor,
            byte_limit=byte_limit,
        )
        if _sha256(original) != expected_hash:
            raise WindowsDraftCasConflict
        target_after = target.lstat()
        if target.is_symlink() or _identity(target_after) != _identity(opened):
            raise WindowsDraftCasConflict
        _assert_directory_identity(root, target.parent, directory_identity)

        os.lseek(target_descriptor, 0, os.SEEK_SET)
        locking.locking(target_descriptor, locking.LK_UNLCK, lock_size)
        locked = False
        os.close(target_descriptor)
        target_descriptor = -1

        # 关闭禁止 delete 的 CRT handle 后存在一个极短窗口；ReplaceFileW 的
        # backup 是替换瞬间的原文件，以它再次核验 hash 才完成 CAS 证明。
        if _sha256(_read_regular_path(target, byte_limit=byte_limit)) != expected_hash:
            raise WindowsDraftCasConflict
        _assert_directory_identity(root, target.parent, directory_identity)
        try:
            _replace_with_backup(target, temporary, backup)
        except WindowsDraftCasConflict:
            _restore_missing_target(target, backup)
            raise
        except WindowsDraftCasInternalError:
            _restore_missing_target(target, backup)
            raise
        except OSError:
            _restore_missing_target(target, backup)
            raise WindowsDraftCasConflict from None
        try:
            backup_content = _read_regular_path(backup, byte_limit=byte_limit)
        except (WindowsDraftCasConflict, WindowsDraftCasInvalidTarget):
            restored = _restore_conflicting_backup(
                target=target,
                backup=backup,
                replacement_hash=replacement_hash,
                byte_limit=byte_limit,
            )
            if not restored:
                raise WindowsDraftCasInternalError from None
            raise WindowsDraftCasConflict from None
        if _sha256(backup_content) != expected_hash:
            restored = _restore_conflicting_backup(
                target=target,
                backup=backup,
                replacement_hash=replacement_hash,
                byte_limit=byte_limit,
            )
            if not restored:
                raise WindowsDraftCasInternalError
            raise WindowsDraftCasConflict
        with suppress(OSError):
            backup.unlink()
    except (WindowsDraftCasConflict, WindowsDraftCasInvalidTarget):
        raise
    except (OSError, OverflowError, TypeError, ValueError):
        raise WindowsDraftCasInternalError from None
    finally:
        if locked and target_descriptor >= 0:
            with suppress(OSError, ValueError):
                os.lseek(target_descriptor, 0, os.SEEK_SET)
                locking.locking(target_descriptor, locking.LK_UNLCK, lock_size)
        if target_descriptor >= 0:
            os.close(target_descriptor)
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(OSError):
            temporary.unlink()
