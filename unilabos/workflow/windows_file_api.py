"""Windows Workflow Draft CAS 使用的窄 Win32 文件 API Adapter。"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Protocol

FILE_READ_ATTRIBUTES = 0x0080
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_CAS_CONFLICT_WINERRORS = frozenset({2, 3, 32, 33})


class WindowsFileConflict(Exception):
    """Win32 文件消失、共享冲突或锁冲突。"""


class WindowsFileInternalError(Exception):
    """Win32 ACL、空间或 I/O 等基础设施故障。"""


class WindowsFileInvalidTarget(Exception):
    """替换路径没有位于同一个稳定父目录。"""


class CtypesApi(Protocol):
    """Win32 Adapter 所需的最小 `ctypes` Interface。"""

    c_int: Any
    c_uint32: Any
    c_void_p: Any
    c_wchar_p: Any

    def WinDLL(self, name: str, *, use_last_error: bool) -> Any:
        """加载 `name` 指定的 Win32 DLL 并启用线程局部错误码。"""

    def WinError(self, error_number: int) -> OSError:
        """把 `error_number` 转换成携带 WinError 的系统异常。"""

    def get_last_error(self) -> int:
        """返回当前线程最近一次 Win32 API 错误码。"""


def extended_path(path: Path) -> str:
    """返回供 Win32 Unicode API 使用的绝对长路径表示。"""

    absolute = str(path.absolute())
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return f"\\\\?\\UNC\\{absolute[2:]}"
    return f"\\\\?\\{absolute}"


def _raise_last_error(ctypes_api: CtypesApi) -> None:
    """按最近 WinError 抛出 CAS 竞争或基础设施故障。"""

    error_number = ctypes_api.get_last_error()
    system_error = ctypes_api.WinError(error_number)
    if error_number in _CAS_CONFLICT_WINERRORS:
        raise WindowsFileConflict from system_error
    raise WindowsFileInternalError from system_error


def replace_file_with_backup(
    target: Path,
    replacement: Path,
    backup: Path,
    *,
    ctypes_api: CtypesApi,
) -> None:
    """原子替换 `target`，并把替换瞬间的原文件保存到 `backup`。

    `target`、`replacement` 与 `backup` 必须位于同一卷，`ctypes_api` 提供原生
    loader 与类型。成功没有返回值；文件竞争与基础设施故障分别抛出窄异常。
    """

    kernel32 = ctypes_api.WinDLL("kernel32", use_last_error=True)
    replace_file = kernel32.ReplaceFileW
    replace_file.argtypes = [
        ctypes_api.c_wchar_p,
        ctypes_api.c_wchar_p,
        ctypes_api.c_wchar_p,
        ctypes_api.c_uint32,
        ctypes_api.c_void_p,
        ctypes_api.c_void_p,
    ]
    replace_file.restype = ctypes_api.c_int
    succeeded = replace_file(
        extended_path(target),
        extended_path(replacement),
        extended_path(backup),
        0,
        None,
        None,
    )
    if not succeeded:
        _raise_last_error(ctypes_api)


def portable_replace_file_with_backup(
    target: Path,
    replacement: Path,
    backup: Path,
) -> None:
    """在非 Windows 合同测试中模拟同目录替换与 backup。"""

    if (
        target.parent != replacement.parent
        or target.parent != backup.parent
        or target.parent.is_symlink()
    ):
        raise WindowsFileInvalidTarget
    os.replace(target, backup)
    try:
        os.replace(replacement, target)
    except OSError:
        with suppress(OSError):
            os.replace(backup, target)
        raise


def _open_directory(path: Path, *, ctypes_api: CtypesApi) -> int:
    """打开不共享 delete/rename 的目录句柄并返回其整数值。"""

    kernel32 = ctypes_api.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes_api.c_wchar_p,
        ctypes_api.c_uint32,
        ctypes_api.c_uint32,
        ctypes_api.c_void_p,
        ctypes_api.c_uint32,
        ctypes_api.c_uint32,
        ctypes_api.c_void_p,
    ]
    create_file.restype = ctypes_api.c_void_p
    handle = create_file(
        extended_path(path),
        FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    raw_handle = getattr(handle, "value", handle)
    invalid_handle = ctypes_api.c_void_p(-1).value
    handle_value = int(raw_handle) if raw_handle is not None else invalid_handle
    if handle_value == invalid_handle:
        _raise_last_error(ctypes_api)
    return handle_value


def _close_directory(handle: int, *, ctypes_api: CtypesApi) -> None:
    """关闭 `handle`；cleanup 失败不会覆盖原 Draft CAS 结论。"""

    kernel32 = ctypes_api.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes_api.c_void_p]
    close_handle.restype = ctypes_api.c_int
    close_handle(handle)


@contextmanager
def hold_directory_chain(
    paths: Sequence[Path],
    *,
    ctypes_api: CtypesApi,
) -> Iterator[None]:
    """持有目录链句柄，禁止窗口内重命名、删除或换成 reparse point。

    `paths` 必须按 registered root 到 Draft parent 排序，`ctypes_api` 提供原生
    Win32 调用。上下文没有产出值；任一目录无法固定时失败关闭，退出时逆序释放。
    """

    handles: list[int] = []
    try:
        for path in paths:
            handles.append(_open_directory(path, ctypes_api=ctypes_api))
        yield
    finally:
        for handle in reversed(handles):
            with suppress(Exception):
                _close_directory(handle, ctypes_api=ctypes_api)
