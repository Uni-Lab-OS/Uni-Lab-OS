from __future__ import annotations

from types import SimpleNamespace

import pytest

from unilabos.config.config import BasicConfig
from unilabos.ros.nodes.presets.host_node import HostNode
from unilabos_msgs.action import EmptyIn


def test_remote_registry_update_reports_only_new_actions() -> None:
    host = SimpleNamespace(
        _action_value_mappings={
            "edge_device": {
                "already_running": {"type": "ExistingAction"},
            }
        }
    )

    first = HostNode._update_remote_action_mappings(  # noqa: SLF001
        host,
        "edge_device",
        {
            "already_running": {"type": "ExistingAction"},
            "new_action": {"type": "NewAction"},
        },
    )
    second = HostNode._update_remote_action_mappings(  # noqa: SLF001
        host,
        "edge_device",
        {
            "already_running": {"type": "ExistingAction"},
            "new_action": {"type": "NewAction"},
        },
    )

    assert first == [("edge_device", "new_action")]
    assert second == []


def test_test_mode_result_uses_only_declared_action_outputs() -> None:
    host = SimpleNamespace(
        _action_value_mappings={
            "host_node": {
                "test_latency": {
                    "schema": {
                        "properties": {
                            "result": {
                                "properties": {
                                    "avg_rtt_ms": {"type": "number"},
                                    "test_count": {"type": "integer"},
                                    "status": {
                                        "type": "string",
                                        "enum": ["ok", "failed"],
                                    },
                                },
                                "required": [
                                    "avg_rtt_ms",
                                    "test_count",
                                    "status",
                                ],
                            }
                        }
                    }
                }
            }
        }
    )

    result = HostNode._build_test_mode_return(  # noqa: SLF001
        host,
        "host_node",
        "test_latency",
        {},
    )

    assert result == {
        "avg_rtt_ms": 0.0,
        "test_count": 0,
        "status": "ok",
    }
    assert "test_mode" not in result
    assert "action_name" not in result


def test_send_goal_fails_when_native_action_server_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingServerClient:
        _action_type = EmptyIn

        def __init__(self) -> None:
            self.wait_timeout: float | None = None

        def wait_for_server(self, *, timeout_sec: float) -> bool:
            self.wait_timeout = timeout_sec
            return False

        def send_goal_async(self, *_args, **_kwargs):
            raise AssertionError("goal must not be sent without an ActionServer")

    client = MissingServerClient()
    host = SimpleNamespace(
        _action_clients={"/devices/host_node/test_latency": client},
        _ACTION_SERVER_WAIT_TIMEOUT_SECONDS=10.0,
        lab_logger=lambda: SimpleNamespace(trace=lambda *_args: None),
    )
    item = SimpleNamespace(
        job_id="886464bd-8fad-4417-8c22-64aaaab34cd2",
        device_id="host_node",
        action_name="test_latency",
    )
    monkeypatch.setattr(BasicConfig, "test_mode", False)

    with pytest.raises(
        TimeoutError,
        match="ActionServer /devices/host_node/test_latency was not available",
    ):
        HostNode.send_goal(
            host,
            item,
            action_type="EmptyIn",
            action_kwargs={},
            sample_material={},
        )

    assert client.wait_timeout == 10.0
