"""Windows editable Workflow Draft 的文件系统 CAS 边界。"""

from __future__ import annotations

import ctypes
import hashlib
import os
import stat
from contextlib import suppress
from pathlib import Path
from typing import Protocol
from uuid import uuid4


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
        if root.is_symlink() or not stat.S_ISDIR(root_stat.st_mode):
            raise WindowsDraftCasInvalidTarget
        relative = parent.relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            current_stat = current.lstat()
            if current.is_symlink() or not stat.S_ISDIR(current_stat.st_mode):
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
        if root.is_symlink() or not stat.S_ISDIR(root_before.st_mode):
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


def _windows_extended_path(path: Path) -> str:
    """返回供 Win32 Unicode API 使用的绝对长路径表示。"""

    absolute = str(path.absolute())
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return f"\\\\?\\UNC\\{absolute[2:]}"
    return f"\\\\?\\{absolute}"


def _native_replace_with_backup(
    target: Path,
    replacement: Path,
    backup: Path,
) -> None:
    """用 Win32 `ReplaceFileW` 原子替换并保存替换瞬间的原 Draft。"""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    replace_file = kernel32.ReplaceFileW
    replace_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    replace_file.restype = ctypes.c_int
    succeeded = replace_file(
        _windows_extended_path(target),
        _windows_extended_path(replacement),
        _windows_extended_path(backup),
        0,
        None,
        None,
    )
    if succeeded:
        return
    error_number = ctypes.get_last_error()
    raise OSError(error_number, ctypes.FormatError(error_number), str(target))


def _portable_replace_with_backup(
    target: Path,
    replacement: Path,
    backup: Path,
) -> None:
    """为非 Windows 合同测试模拟替换与备份；生产 Windows 不走该路径。"""

    os.replace(target, backup)
    try:
        os.replace(replacement, target)
    except OSError:
        with suppress(OSError):
            os.replace(backup, target)
        raise


def _replace_with_backup(
    target: Path,
    replacement: Path,
    backup: Path,
) -> None:
    """选择当前平台的替换原语；Windows 必须使用单次 `ReplaceFileW`。"""

    if os.name == "nt":
        _native_replace_with_backup(target, replacement, backup)
        return
    _portable_replace_with_backup(target, replacement, backup)


def _restore_missing_target(target: Path, backup: Path) -> None:
    """在 Win32 部分失败留下 canonical 缺口时尽力恢复原 Draft。"""

    if target.exists() or not backup.exists():
        return
    try:
        os.rename(backup, target)
    except OSError:
        return


def _restore_conflicting_backup(
    *,
    target: Path,
    backup: Path,
    replacement_hash: str,
    byte_limit: int,
) -> None:
    """把 CAS 竞争者保存到 canonical，并只清理可证明属于本次写入的文件。"""

    displaced = target.with_name(f".{target.name}.{uuid4().hex}.displaced")
    try:
        _replace_with_backup(target, backup, displaced)
    except OSError:
        # `backup` 或 `displaced` 可能是外部 Authority 的唯一完整副本；无法
        # 证明归属时保留 artifact，禁止用历史内容再次覆盖 canonical。
        return
    try:
        displaced_content = _read_regular_path(displaced, byte_limit=byte_limit)
    except (WindowsDraftCasConflict, WindowsDraftCasInvalidTarget):
        displaced_content = None
    if displaced_content is not None and _sha256(displaced_content) == replacement_hash:
        with suppress(OSError):
            displaced.unlink()
        return

    # rollback 窗口内若又有外部写入，`displaced` 才是更新的外部 Draft；把它
    # 放回 canonical，并把较早竞争者留作 artifact，禁止丢失任一外部版本。
    preserved = target.with_name(f".{target.name}.{uuid4().hex}.cas")
    with suppress(OSError):
        _replace_with_backup(target, displaced, preserved)


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

    `root` 与 `target` 来自已注册 editable package，`content` 是新 Draft 字节，
    `expected_hash` 是调用方观察到的旧 hash，`byte_limit` 同时约束新旧源码，
    `locking` 是当前解释器的 `msvcrt`。成功没有返回值；路径异常、CAS 冲突和
    不可恢复系统错误分别抛出对应异常。函数保证只有替换瞬间的旧 Draft hash
    仍匹配时才接受发布，否则恢复或保留竞争者字节并报告冲突。
    """

    if len(content) > byte_limit:
        raise WindowsDraftCasInvalidTarget
    directory_identity = _create_parent(root, target.parent)
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
        try:
            _replace_with_backup(target, temporary, backup)
        except OSError:
            _restore_missing_target(target, backup)
            raise WindowsDraftCasConflict from None
        try:
            backup_content = _read_regular_path(backup, byte_limit=byte_limit)
        except (WindowsDraftCasConflict, WindowsDraftCasInvalidTarget):
            _restore_conflicting_backup(
                target=target,
                backup=backup,
                replacement_hash=replacement_hash,
                byte_limit=byte_limit,
            )
            raise WindowsDraftCasConflict from None
        if _sha256(backup_content) != expected_hash:
            _restore_conflicting_backup(
                target=target,
                backup=backup,
                replacement_hash=replacement_hash,
                byte_limit=byte_limit,
            )
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
