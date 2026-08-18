"""工作区宿主（Workspace Host）使用的 Windows 进程生命周期实现。"""

from __future__ import annotations

import ctypes
import os
from collections.abc import Mapping
from ctypes import wintypes

_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x00000102
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def terminate_process_tree(root_pid: int, *, wait_timeout_ms: int = 100) -> None:
    """按子进程优先顺序终止一个受管 Windows 进程树。

    Args:
        root_pid: 工作区宿主（Workspace Host）已记录的根进程 PID。
        wait_timeout_ms: 终止每个进程后等待句柄结束的毫秒数。

    Raises:
        RuntimeError: 当前操作系统不是 Windows。
        ValueError: 根进程 PID 不是正整数。
        OSError: 受管根进程或其子进程无法可靠终止。

    ``taskkill /T`` 可能阻塞在 ROS 2 进程树上；Toolhelp 快照与
    ``TerminateProcess`` 避免依赖该外部 RPC 路径。
    """

    if os.name != "nt":
        raise RuntimeError("Windows process-tree termination requires Windows")
    if root_pid < 1:
        raise ValueError("root_pid must be positive")
    kernel32 = _kernel32()
    failures: list[int] = []
    for pid in _postorder_process_ids(_snapshot_parents(kernel32), root_pid):
        handle = kernel32.OpenProcess(
            _PROCESS_TERMINATE | _SYNCHRONIZE,
            False,
            pid,
        )
        if not handle:
            continue
        try:
            if not kernel32.TerminateProcess(handle, 1):
                failures.append(pid)
                continue
            kernel32.WaitForSingleObject(handle, wait_timeout_ms)
        finally:
            kernel32.CloseHandle(handle)
    if _process_is_active(kernel32, root_pid):
        failures.append(root_pid)
    if failures:
        unique = ", ".join(str(pid) for pid in sorted(set(failures)))
        raise OSError(f"无法终止 Windows 进程树：{unique}")


def process_exists(pid: int) -> bool:
    """只读检查一个 Windows PID 是否仍指向活动进程。

    Args:
        pid: 要检查的操作系统进程 PID。

    Returns:
        进程仍活动或因权限不足无法读取时返回 ``True``；PID 无效、进程已退出
        或不存在时返回 ``False``。

    Raises:
        RuntimeError: 当前操作系统不是 Windows。

    Windows 的 ``os.kill(pid, 0)`` 可能调用 ``TerminateProcess``，因此这里
    必须使用只读查询句柄，避免存活探测改变被检查进程的生命周期。
    """

    if os.name != "nt":
        raise RuntimeError("Windows 进程存活检查只能在 Windows 上运行")
    if pid <= 0:
        return False
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        pid,
    )
    if not handle:
        # 权限不足本身可以证明 PID 存在；其他错误按不存在处理。
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            # 已取得进程句柄但查询失败时失败关闭，避免误判为可回收的陈旧 PID。
            return True
        return exit_code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _postorder_process_ids(
    parent_by_pid: Mapping[int, int],
    root_pid: int,
) -> list[int]:
    """返回根进程子树的后序 PID，确保子进程排在父进程之前。

    Args:
        parent_by_pid: 进程 PID 到父进程 PID 的系统快照映射。
        root_pid: 受管进程树的根进程 PID。

    Returns:
        仅包含根进程及其子孙进程的后序 PID 列表。
    """

    children: dict[int, list[int]] = {}
    for pid, parent_pid in parent_by_pid.items():
        if pid != root_pid:
            children.setdefault(parent_pid, []).append(pid)
    ordered: list[int] = []
    visited: set[int] = set()

    def visit(pid: int) -> None:
        if pid in visited:
            return
        visited.add(pid)
        for child_pid in sorted(children.get(pid, [])):
            visit(child_pid)
        ordered.append(pid)

    visit(root_pid)
    return ordered


def _kernel32() -> object:
    """加载并声明本模块使用的 Kernel32 函数接口。

    Returns:
        已配置参数类型与返回类型的 Kernel32 动态库对象。
    """

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessEntry32W),
    ]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessEntry32W),
    ]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _snapshot_parents(kernel32: object) -> dict[int, int]:
    """读取当前 Windows 进程快照中的父子关系。

    Args:
        kernel32: 已声明 Toolhelp 函数接口的 Kernel32 对象。

    Returns:
        进程 PID 到父进程 PID 的映射。

    Raises:
        OSError: 无法创建系统进程快照。
    """

    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    parents: dict[int, int] = {}
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        available = bool(kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
        while available:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            available = bool(kernel32.Process32NextW(snapshot, ctypes.byref(entry)))
    finally:
        kernel32.CloseHandle(snapshot)
    return parents


def _process_is_active(kernel32: object, pid: int) -> bool:
    """通过同步句柄判断 Windows 进程是否仍处于活动状态。

    Args:
        kernel32: 已声明进程句柄函数接口的 Kernel32 对象。
        pid: 要检查的操作系统进程 PID。

    Returns:
        进程仍未结束时返回 ``True``，否则返回 ``False``。
    """

    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)
