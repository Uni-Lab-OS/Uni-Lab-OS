"""工作区宿主（Workspace Host）的跨平台进程生命周期接口契约。"""

from __future__ import annotations

import os

from unilabos.workspace_host.process_lifecycle import process_exists


def test_process_exists_is_read_only_for_current_process() -> None:
    """证明公共存活检查不会改变当前测试进程的生命周期。"""

    current_pid = os.getpid()

    assert process_exists(current_pid) is True
    assert process_exists(2_000_000_000) is False
    assert process_exists(current_pid) is True


def test_process_exists_rejects_nonpositive_pid() -> None:
    """证明非正 PID 在调用任何平台进程接口前即被拒绝。"""

    assert process_exists(0) is False
    assert process_exists(-1) is False
