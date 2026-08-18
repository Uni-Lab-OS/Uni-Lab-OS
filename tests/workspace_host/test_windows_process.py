"""工作区宿主（Workspace Host）的 Windows 进程生命周期契约。"""

import os

import pytest

from unilabos.workspace_host.windows_process import (
    _postorder_process_ids,
    process_exists,
)


def test_postorder_process_ids_is_scoped_and_children_first() -> None:
    """证明进程树排序只包含受管子树，且子进程先于父进程。"""

    parents = {
        10: 1,
        11: 10,
        12: 10,
        13: 11,
        99: 1,
        100: 99,
    }

    assert _postorder_process_ids(parents, 10) == [13, 11, 12, 10]


@pytest.mark.skipif(os.name != "nt", reason="验证 Windows 原生只读 PID 探测")
def test_process_exists_on_windows_is_read_only() -> None:
    """证明 PID 存活检查不会终止正在接受检查的 Windows 进程。"""

    current_pid = os.getpid()

    assert process_exists(current_pid) is True
    assert process_exists(2_000_000_000) is False
    assert process_exists(current_pid) is True
