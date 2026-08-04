"""工作流源码（Workflow Source）的原子 CAS 发布内部实现。"""

from __future__ import annotations

import fcntl
import hashlib
import os
import signal
import stat
import struct
import threading
from contextlib import suppress
from uuid import uuid4

NO_EXPECTED_HASH = object()
_F_SETOWN_EX = getattr(fcntl, "F_SETOWN_EX", 15)
_F_OWNER_TID = 0
_LEASE_BREAK_SIGNAL = signal.SIGRTMAX


class SourcePublicationConflict(RuntimeError):
    """表示规范源码在 CAS 发布期间被其他写入者改变。"""


class SourcePublicationError(RuntimeError):
    """表示原子发布无法安全完成的基础设施错误。"""


def atomic_publish_source(
    *,
    parent_descriptor: int,
    target_name: str,
    content: bytes,
    byte_limit: int,
    expected_hash: object | str | None = NO_EXPECTED_HASH,
) -> None:
    """在已固定目录中原子发布一份工作流源码。

    参数：``parent_descriptor`` 是规范源码父目录；``target_name`` 是单段文件名；
    ``content`` 是待发布字节；``byte_limit`` 是源码硬上限；``expected_hash``
    是可选的原稿 SHA-256 CAS 条件。
    返回：无；成功时规范路径指向完整新文件并完成目录 ``fsync``。
    异常：并发变化抛出 ``SourcePublicationConflict``；文件系统失败抛出
    ``SourcePublicationError``。
    """

    if len(content) > byte_limit:
        raise SourcePublicationError("source_too_large")
    temporary_name = f".{target_name}.{uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary_name,
            (os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW),
            0o600,
            dir_fd=parent_descriptor,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if expected_hash is NO_EXPECTED_HASH:
            os.replace(
                temporary_name,
                target_name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
        else:
            _compare_and_replace(
                parent_descriptor=parent_descriptor,
                target_name=target_name,
                temporary_name=temporary_name,
                expected_hash=expected_hash,
                byte_limit=byte_limit,
            )
        os.fsync(parent_descriptor)
    except SourcePublicationConflict:
        raise
    except (OSError, SourcePublicationError):
        raise SourcePublicationError("publication_failed") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=parent_descriptor)


def _compare_and_replace(
    *,
    parent_descriptor: int,
    target_name: str,
    temporary_name: str,
    expected_hash: str | None,
    byte_limit: int,
) -> None:
    """在可安全中断的文件租约下执行 ``fsync`` 后的原子 CAS 替换。

    参数：父目录、目标名和临时名共同固定发布对象；``expected_hash`` 是原稿条件；
    ``byte_limit`` 同时约束原稿、临时文件和发布后文件读取。
    返回：无；CAS 成功时完成替换。
    异常：无法证明原稿身份稳定时抛出 ``SourcePublicationConflict``。
    """

    target_descriptor = -1
    temporary_descriptor = -1
    backup_name = f".{target_name}.{uuid4().hex}.cas"
    backup_created = False
    replacement_attempted = False
    lease_held = False
    previous_signal_mask: set[signal.Signals] | None = None
    try:
        try:
            target_descriptor = os.open(
                target_name,
                os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            if expected_hash is not None:
                raise SourcePublicationConflict("draft_hash_conflict") from None
            try:
                os.link(
                    temporary_name,
                    target_name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError:
                raise SourcePublicationConflict("draft_hash_conflict") from None
            os.unlink(temporary_name, dir_fd=parent_descriptor)
            return

        if expected_hash is None:
            raise SourcePublicationConflict("draft_hash_conflict")
        try:
            previous_signal_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK,
                {_LEASE_BREAK_SIGNAL},
            )
            fcntl.fcntl(
                target_descriptor,
                _F_SETOWN_EX,
                struct.pack("ii", _F_OWNER_TID, threading.get_native_id()),
            )
            fcntl.fcntl(target_descriptor, fcntl.F_SETSIG, _LEASE_BREAK_SIGNAL)
            fcntl.fcntl(target_descriptor, fcntl.F_SETLEASE, fcntl.F_WRLCK)
            lease_held = True
        except (AttributeError, OSError, ValueError):
            raise SourcePublicationConflict("draft_hash_conflict") from None

        original_bytes = _read_regular_descriptor(
            target_descriptor,
            byte_limit=byte_limit,
        )
        if _sha256(original_bytes) != expected_hash or not _target_matches_descriptor(
            parent_descriptor,
            target_name,
            target_descriptor,
        ):
            raise SourcePublicationConflict("draft_hash_conflict")

        temporary_descriptor = os.open(
            temporary_name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
        replacement_hash = _hash_regular_descriptor(
            temporary_descriptor,
            byte_limit=byte_limit,
        )
        os.link(
            target_name,
            backup_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        backup_created = True
        os.fsync(parent_descriptor)
        if _drain_lease_break_signal():
            raise SourcePublicationConflict("draft_hash_conflict")

        replacement_attempted = True
        os.replace(
            temporary_name,
            target_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        os.fsync(parent_descriptor)
        if _drain_lease_break_signal() or not _published_identity_matches(
            parent_descriptor=parent_descriptor,
            target_name=target_name,
            published_descriptor=temporary_descriptor,
            expected_hash=replacement_hash,
            byte_limit=byte_limit,
        ):
            raise SourcePublicationConflict("draft_hash_conflict")

        fcntl.fcntl(target_descriptor, fcntl.F_SETLEASE, fcntl.F_UNLCK)
        lease_held = False
        if _drain_lease_break_signal() or not _published_identity_matches(
            parent_descriptor=parent_descriptor,
            target_name=target_name,
            published_descriptor=temporary_descriptor,
            expected_hash=replacement_hash,
            byte_limit=byte_limit,
        ):
            raise SourcePublicationConflict("draft_hash_conflict")
        with suppress(OSError):
            os.unlink(backup_name, dir_fd=parent_descriptor)
            backup_created = False
            os.fsync(parent_descriptor)
    except Exception:
        # 替换后不能用历史备份覆盖可能已被外部修改的规范路径。
        if backup_created and not replacement_attempted:
            with suppress(OSError):
                os.unlink(backup_name, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
        raise
    finally:
        if lease_held and target_descriptor >= 0:
            with suppress(OSError):
                fcntl.fcntl(target_descriptor, fcntl.F_SETLEASE, fcntl.F_UNLCK)
        if previous_signal_mask is not None:
            with suppress(OSError, ValueError):
                _drain_lease_break_signal()
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_signal_mask)
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if target_descriptor >= 0:
            os.close(target_descriptor)


def _published_identity_matches(
    *,
    parent_descriptor: int,
    target_name: str,
    published_descriptor: int,
    expected_hash: str,
    byte_limit: int,
) -> bool:
    """验证规范路径仍指向本次发布的文件身份与内容。

    参数：目录、文件名和已打开描述符标识本次发布；哈希和上限约束内容。
    返回：文件身份与哈希均保持一致时为 ``True``。
    """

    return (
        _target_matches_descriptor(
            parent_descriptor,
            target_name,
            published_descriptor,
        )
        and _hash_regular_descriptor(
            published_descriptor,
            byte_limit=byte_limit,
        )
        == expected_hash
    )


def _drain_lease_break_signal() -> bool:
    """同步消费当前线程收到的文件租约中断通知。

    参数：无。
    返回：至少观察到一次租约中断时为 ``True``。
    """

    observed = False
    while True:
        try:
            notification = signal.sigtimedwait({_LEASE_BREAK_SIGNAL}, 0)
        except InterruptedError:
            continue
        if notification is None:
            return observed
        observed = True


def _target_matches_descriptor(
    parent_descriptor: int,
    target_name: str,
    descriptor: int,
) -> bool:
    """比较规范路径与已打开文件描述符的物理身份。

    参数：父目录、目标文件名和描述符共同指定两种观察。
    返回：二者均为同一普通文件设备/索引节点时为 ``True``。
    """

    try:
        target_metadata = os.stat(
            target_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return False
    descriptor_metadata = os.fstat(descriptor)
    return (
        stat.S_ISREG(target_metadata.st_mode)
        and target_metadata.st_dev == descriptor_metadata.st_dev
        and target_metadata.st_ino == descriptor_metadata.st_ino
    )


def _read_regular_descriptor(descriptor: int, *, byte_limit: int) -> bytes:
    """在硬上限内重读一个普通文件描述符。

    参数：``descriptor`` 是已打开文件；``byte_limit`` 是最大允许字节数。
    返回：文件全部字节。
    异常：非普通文件、超限或短期读取故障转为 ``SourcePublicationConflict``。
    """

    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > byte_limit:
        raise SourcePublicationConflict("draft_hash_conflict")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = bytearray()
    while len(chunks) <= byte_limit:
        chunk = os.read(descriptor, min(64 * 1024, byte_limit + 1 - len(chunks)))
        if not chunk:
            break
        chunks.extend(chunk)
    if len(chunks) > byte_limit:
        raise SourcePublicationConflict("draft_hash_conflict")
    return bytes(chunks)


def _hash_regular_descriptor(descriptor: int, *, byte_limit: int) -> str:
    """计算受限普通文件的 SHA-256 哈希。

    参数：``descriptor`` 是文件描述符；``byte_limit`` 是读取上限。
    返回：小写十六进制 SHA-256。
    """

    return _sha256(_read_regular_descriptor(descriptor, byte_limit=byte_limit))


def _sha256(content: bytes) -> str:
    """计算字节内容的稳定 SHA-256。

    参数：``content`` 是待摘要字节。
    返回：带 ``sha256:`` 前缀的小写十六进制摘要。
    """

    return f"sha256:{hashlib.sha256(content).hexdigest()}"


__all__ = [
    "NO_EXPECTED_HASH",
    "SourcePublicationConflict",
    "SourcePublicationError",
    "atomic_publish_source",
]
