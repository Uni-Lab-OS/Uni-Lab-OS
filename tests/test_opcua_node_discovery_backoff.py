"""验证 OPC UA 节点浏览失败不会按遥测变量重复制造日志风暴。"""

from __future__ import annotations

from unilabos.devices.workstation.post_process.post_process import BaseClient
from unilabos.utils.log import logger


class _FailingRoot:
    """模拟已断开的 OPC UA 根节点。"""

    def get_child(self, _path):
        """每次浏览 Objects 都返回同一个连接错误。"""

        raise ConnectionError("socket closed")


class _FailingClient:
    """记录根节点浏览次数的最小 OPC UA 客户端替身。"""

    def __init__(self) -> None:
        """初始化浏览次数。"""

        self.root_calls = 0

    def get_root_node(self) -> _FailingRoot:
        """返回断连根节点并累计真实浏览次数。"""

        self.root_calls += 1
        return _FailingRoot()


def test_node_discovery_failure_enters_backoff_without_traceback(monkeypatch) -> None:
    """同一退避窗口只执行一次整树浏览，并只记录一条连接失败摘要。"""

    client = _FailingClient()
    device = object.__new__(BaseClient)
    device.client = client
    device._variables_to_find = {"sensor": {}}
    device._node_registry = {}
    device._node_discovery_retry_after = 0.0
    device._node_discovery_failure_count = 0

    warnings: list[str] = []
    monkeypatch.setattr(
        logger,
        "warning",
        lambda message, *args: warnings.append(message % args),
    )

    device._find_nodes()
    device._find_nodes()

    assert client.root_calls == 1
    diagnostic_output = "\n".join(warnings)
    assert diagnostic_output.count("节点浏览失败") == 1
    assert "Traceback" not in diagnostic_output
