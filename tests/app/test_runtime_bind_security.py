"""Quick Debug Alpha 本地 Runtime 监听地址安全边界。"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from unilabos.app.local_bridge.local_api import LocalApiServer
from unilabos.app.local_bridge.server import LocalBridgeServer, _parse_args


def _build_local_api(host: str) -> LocalApiServer:
    return LocalApiServer(lambda: None, host=host)


def test_runtime_bind_defaults_to_ipv4_loopback() -> None:
    args = _parse_args([])
    bridge = LocalBridgeServer()
    local_api = LocalApiServer(lambda: None)

    assert args.host == "127.0.0.1"
    assert bridge.host == "127.0.0.1"
    assert local_api.host == "127.0.0.1"
    assert not hasattr(args, "workflow_port")
    assert not hasattr(bridge, "_workflow_server")


def test_bridge_graph_flag_is_only_for_offline_execution_os() -> None:
    args = _parse_args(["--offline", "-g", "graph.json"])

    assert args.graph == "graph.json"
    with pytest.raises(ValueError, match="belongs to the execution OS"):
        LocalBridgeServer(graph_path="graph.json")


def test_offline_node_delay_is_rejected_for_real_os_bridge() -> None:
    with pytest.raises(ValueError, match="requires --offline"):
        LocalBridgeServer(offline_node_delay=0.1)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
@pytest.mark.parametrize(
    "build_server",
    [
        pytest.param(LocalBridgeServer, id="combined-bridge"),
        pytest.param(_build_local_api, id="runtime-http"),
    ],
)
def test_runtime_bind_accepts_explicit_loopback_hosts(
    host: str,
    build_server: Callable[[str], object],
) -> None:
    server = build_server(host=host)

    assert server.host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.42"])
@pytest.mark.parametrize(
    "build_server",
    [
        pytest.param(LocalBridgeServer, id="combined-bridge"),
        pytest.param(_build_local_api, id="runtime-http"),
    ],
)
def test_runtime_bind_rejects_non_loopback_before_server_start(
    host: str,
    build_server: Callable[[str], object],
) -> None:
    with pytest.raises(ValueError, match=r"\bUNSAFE_RUNTIME_BIND\b"):
        build_server(host=host)
