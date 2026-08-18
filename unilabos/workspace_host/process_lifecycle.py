"""工作区宿主（Workspace Host）的跨平台进程生命周期接口。"""

from __future__ import annotations

import os
import signal
import subprocess
import time

from .model import WorkspaceHostError

_STOP_TIMEOUT_SECONDS = 10.0


def process_exists(pid: int) -> bool:
    """只读检查一个 PID 是否仍指向活动进程。

    Args:
        pid: 要检查的操作系统进程 PID。

    Returns:
        进程仍活动时返回 ``True``，PID 无效、进程已退出或不存在时返回
        ``False``。
    """

    if pid <= 0:
        return False
    if os.name == "nt":
        from .windows_process import process_exists as windows_process_exists

        return windows_process_exists(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        completed = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=1,
        )
        if completed.returncode == 0 and completed.stdout.lstrip().startswith(b"Z"):
            return False
    except (OSError, subprocess.TimeoutExpired):
        pass
    return True


def terminate_process_tree(
    pid: int,
    process: subprocess.Popen[bytes] | None,
) -> None:
    """终止工作区宿主（Workspace Host）管理的一个进程树。

    Args:
        pid: 受管进程树的根进程 PID。
        process: 当前宿主持有的根进程对象；恢复场景中可以为空。

    Raises:
        WorkspaceHostError: Windows 进程树无法可靠终止，或 POSIX 进程组
            在期限内没有退出。
        PermissionError: POSIX 进程组存在但当前进程无权终止。
    """

    if process is not None and process.poll() is not None:
        return
    if os.name == "nt":
        from .windows_process import terminate_process_tree as terminate_windows_tree

        try:
            terminate_windows_tree(pid)
        except OSError as error:
            raise WorkspaceHostError(
                "stop_failed", f"无法停止 Windows 进程树：{pid}"
            ) from error
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        if not process_exists(pid):
            return
        raise
    deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process is not None:
            if process.poll() is not None:
                return
        elif not process_exists(pid):
            return
        time.sleep(0.05)
    if (process is not None and process.poll() is None) or (
        process is None and process_exists(pid)
    ):
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            if not process_exists(pid):
                return
            raise
    if process is not None:
        try:
            process.wait(timeout=_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            raise WorkspaceHostError(
                "stop_failed", f"进程树未退出：{pid}"
            ) from error
