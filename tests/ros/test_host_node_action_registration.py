from __future__ import annotations

from types import SimpleNamespace

from unilabos.ros.nodes.presets.host_node import HostNode


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
