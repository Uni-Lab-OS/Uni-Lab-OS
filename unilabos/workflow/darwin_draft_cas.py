"""Darwin editable Workflow Draft 的原生文件系统 CAS 边界。"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
import sys
from contextlib import suppress
from typing import Callable
from uuid import uuid4

_RENAME_SWAP = 0x00000002
_RENAME_NOFOLLOW_ANY = 0x00000010
_RenameAtx = Callable[[int, bytes, int, bytes, int], int]


class DarwinDraftCasConflict(Exception):
    """Darwin Draft 已变化，或原子交换时无法证明旧版本匹配。"""


class DarwinDraftCasInvalidTarget(Exception):
    """Darwin Draft 路径不再指向已注册的普通文件。"""


class DarwinDraftCasInternalError(Exception):
    """Darwin Draft CAS 遇到无法安全归类的系统错误。"""


def _load_renameatx_np() -> _RenameAtx | None:
    """加载 Darwin 原生原子交换函数；当前平台不支持时返回 ``None``。"""

    if sys.platform != "darwin":
        return None
    try:
        function = ctypes.CDLL(None, use_errno=True).renameatx_np
    except (AttributeError, OSError):
        return None
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    return function


_RENAMEATX_NP = _load_renameatx_np()


def supports_darwin_draft_cas() -> bool:
    """返回当前进程是否具备 Darwin 同目录原子交换能力。"""

    return (
        sys.platform == "darwin"
        and _RENAMEATX_NP is not None
        and os.open in getattr(os, "supports_dir_fd", ())
        and os.stat in getattr(os, "supports_dir_fd", ())
        and os.rename in getattr(os, "supports_dir_fd", ())
    )


def _sha256(content: bytes) -> str:
    """返回 ``content`` 的 Workflow Draft hash。"""

    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _identity(stat_result: os.stat_result) -> tuple[int, int]:
    """返回不含易变时间戳的文件系统身份。"""

    return stat_result.st_dev, stat_result.st_ino


def _version(stat_result: os.stat_result) -> tuple[int, int, int, int, int]:
    """返回一次有界读取前后可比较的文件版本证据。"""

    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
    )


def _read_regular_descriptor(descriptor: int, *, byte_limit: int) -> bytes:
    """稳定、有界地读取普通文件描述符；并发变化归类为 CAS 冲突。"""

    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_size > byte_limit:
        raise DarwinDraftCasConflict
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
    after = os.fstat(descriptor)
    if len(content) > byte_limit or _version(after) != _version(before):
        raise DarwinDraftCasConflict
    return content


def _path_matches_descriptor(
    parent_fd: int,
    name: str,
    descriptor: int,
) -> bool:
    """确认目录项仍指向 ``descriptor`` 打开的普通文件。"""

    try:
        path_stat = os.stat(
            name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        opened_stat = os.fstat(descriptor)
    except OSError:
        return False
    return (
        stat.S_ISREG(path_stat.st_mode)
        and stat.S_ISREG(opened_stat.st_mode)
        and _identity(path_stat) == _identity(opened_stat)
    )


def _open_regular(parent_fd: int, name: str) -> int:
    """在固定目录中打开普通文件，拒绝符号链接与非普通目标。"""

    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        raise DarwinDraftCasConflict from None
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR, errno.EISDIR}:
            raise DarwinDraftCasInvalidTarget from None
        raise DarwinDraftCasInternalError from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise DarwinDraftCasInvalidTarget
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _swap_names(parent_fd: int, first_name: str, second_name: str) -> None:
    """用 ``renameatx_np(RENAME_SWAP)`` 原子交换同目录的两个名字。"""

    if _RENAMEATX_NP is None:
        raise DarwinDraftCasConflict
    result = _RENAMEATX_NP(
        parent_fd,
        os.fsencode(first_name),
        parent_fd,
        os.fsencode(second_name),
        _RENAME_SWAP | _RENAME_NOFOLLOW_ANY,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {
        errno.ENOENT,
        errno.ENOTDIR,
        errno.ELOOP,
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EINVAL),
    }:
        raise DarwinDraftCasConflict
    raise DarwinDraftCasInternalError from OSError(
        error_number,
        os.strerror(error_number),
    )


def _preserve_artifact(
    parent_fd: int,
    target_name: str,
    temporary_name: str,
) -> None:
    """把无法安全删除的交换文件保留为显式 ``.cas`` 恢复 artifact。"""

    artifact_name = f".{target_name}.{uuid4().hex}.cas"
    try:
        os.rename(
            temporary_name,
            artifact_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    except OSError:
        # 原 temporary 名仍在同一固定目录内；保留它比误删外部字节安全。
        return


def _restore_conflicting_swap(
    *,
    parent_fd: int,
    target_name: str,
    temporary_name: str,
    replacement_descriptor: int,
    replacement_hash: str,
    byte_limit: int,
) -> None:
    """恢复交换瞬间胜出的外部 Draft，并保留二次竞争字节。"""

    _swap_names(parent_fd, target_name, temporary_name)
    os.fsync(parent_fd)
    try:
        replacement_bytes = _read_regular_descriptor(
            replacement_descriptor,
            byte_limit=byte_limit,
        )
    except DarwinDraftCasConflict:
        replacement_bytes = b""
    if (
        _path_matches_descriptor(
            parent_fd,
            temporary_name,
            replacement_descriptor,
        )
        and _sha256(replacement_bytes) == replacement_hash
    ):
        os.unlink(temporary_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return

    # 发布之后 canonical 又被外部修改或替换：第一次恢复把这个更新放到了
    # temporary。再次交换使最新外部版本回到 canonical，并保留较早竞争者。
    newer_descriptor = _open_regular(parent_fd, temporary_name)
    try:
        _read_regular_descriptor(newer_descriptor, byte_limit=byte_limit)
        _swap_names(parent_fd, target_name, temporary_name)
        os.fsync(parent_fd)
        if not _path_matches_descriptor(
            parent_fd,
            target_name,
            newer_descriptor,
        ):
            raise DarwinDraftCasInternalError
    finally:
        os.close(newer_descriptor)
    _preserve_artifact(parent_fd, target_name, temporary_name)


def _descriptor_matches_named_hash(
    *,
    parent_fd: int,
    name: str,
    descriptor: int,
    expected_hash: str,
    byte_limit: int,
) -> bool:
    """返回描述符内容与目录项是否稳定匹配预期版本。"""

    try:
        content = _read_regular_descriptor(descriptor, byte_limit=byte_limit)
    except DarwinDraftCasConflict:
        return False
    return _sha256(content) == expected_hash and _path_matches_descriptor(
        parent_fd, name, descriptor
    )


def _restore_then_raise_conflict(
    *,
    parent_fd: int,
    target_name: str,
    temporary_name: str,
    replacement_descriptor: int,
    replacement_hash: str,
    byte_limit: int,
) -> None:
    """完成可证明的冲突恢复；恢复不确定时保留 artifact 并报告内部错误。"""

    try:
        _restore_conflicting_swap(
            parent_fd=parent_fd,
            target_name=target_name,
            temporary_name=temporary_name,
            replacement_descriptor=replacement_descriptor,
            replacement_hash=replacement_hash,
            byte_limit=byte_limit,
        )
    except (DarwinDraftCasConflict, DarwinDraftCasInternalError, OSError):
        _preserve_artifact(parent_fd, target_name, temporary_name)
        raise DarwinDraftCasInternalError from None
    raise DarwinDraftCasConflict


def write_darwin_draft_cas(
    *,
    parent_fd: int,
    target_name: str,
    content: bytes,
    expected_hash: str,
    byte_limit: int,
) -> None:
    """用 Darwin 原生交换保存已有 Draft，并保护外部文件 Authority。

    ``parent_fd`` 固定已验证的源码父目录，``target_name`` 是普通文件叶子名，
    ``content`` 是已做 UTF-8/大小校验的新源码，``expected_hash`` 是调用方观察到
    的旧 Draft hash。交换瞬间的旧文件会被再次核验；不匹配时恢复外部版本并抛出
    ``DarwinDraftCasConflict``，绝不把本次内容当作成功写入。
    """

    if not supports_darwin_draft_cas():
        raise DarwinDraftCasConflict
    if (
        not target_name
        or target_name in {".", ".."}
        or "/" in target_name
        or "\x00" in target_name
        or len(content) > byte_limit
    ):
        raise DarwinDraftCasInvalidTarget

    temporary_name = f".{target_name}.{uuid4().hex}.tmp"
    temporary_descriptor = -1
    replacement_descriptor = -1
    target_descriptor = -1
    swapped = False
    cleanup_temporary = True
    replacement_hash = _sha256(content)
    try:
        temporary_descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        with os.fdopen(temporary_descriptor, "wb") as stream:
            temporary_descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        replacement_descriptor = _open_regular(parent_fd, temporary_name)
        if (
            _sha256(
                _read_regular_descriptor(
                    replacement_descriptor,
                    byte_limit=byte_limit,
                )
            )
            != replacement_hash
        ):
            raise DarwinDraftCasInternalError

        target_descriptor = _open_regular(parent_fd, target_name)
        original = _read_regular_descriptor(
            target_descriptor,
            byte_limit=byte_limit,
        )
        if _sha256(original) != expected_hash or not _path_matches_descriptor(
            parent_fd,
            target_name,
            target_descriptor,
        ):
            raise DarwinDraftCasConflict

        _swap_names(parent_fd, target_name, temporary_name)
        swapped = True
        cleanup_temporary = False
        os.fsync(parent_fd)
        if not _descriptor_matches_named_hash(
            parent_fd=parent_fd,
            name=temporary_name,
            descriptor=target_descriptor,
            expected_hash=expected_hash,
            byte_limit=byte_limit,
        ):
            _restore_then_raise_conflict(
                parent_fd=parent_fd,
                target_name=target_name,
                temporary_name=temporary_name,
                replacement_descriptor=replacement_descriptor,
                replacement_hash=replacement_hash,
                byte_limit=byte_limit,
            )

        # 紧邻删除前再次核验，覆盖交换后仍持有旧 inode 的常见原地写入竞争。
        if not _descriptor_matches_named_hash(
            parent_fd=parent_fd,
            name=temporary_name,
            descriptor=target_descriptor,
            expected_hash=expected_hash,
            byte_limit=byte_limit,
        ):
            _restore_then_raise_conflict(
                parent_fd=parent_fd,
                target_name=target_name,
                temporary_name=temporary_name,
                replacement_descriptor=replacement_descriptor,
                replacement_hash=replacement_hash,
                byte_limit=byte_limit,
            )
        os.unlink(temporary_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except (DarwinDraftCasConflict, DarwinDraftCasInvalidTarget):
        raise
    except DarwinDraftCasInternalError:
        if swapped:
            _preserve_artifact(parent_fd, target_name, temporary_name)
        raise
    except (OSError, OverflowError, TypeError, ValueError):
        if swapped:
            _preserve_artifact(parent_fd, target_name, temporary_name)
        raise DarwinDraftCasInternalError from None
    finally:
        if target_descriptor >= 0:
            os.close(target_descriptor)
        if replacement_descriptor >= 0:
            os.close(replacement_descriptor)
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if cleanup_temporary:
            with suppress(OSError):
                os.unlink(temporary_name, dir_fd=parent_fd)
