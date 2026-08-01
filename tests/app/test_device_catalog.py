from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from unilabos.app import ws_client as ws_client_module
from unilabos.app.device_catalog import (
    DEVICE_CATALOG_SCHEMA,
    action_catalog_from_device_snapshot,
    apply_action_locks,
    build_device_catalog,
    public_device_catalog,
)


def test_build_device_catalog_projects_online_devices_and_action_schemas() -> None:
    host = SimpleNamespace(
        devices_names={"robot": "/cell"},
        device_machine_names={"robot": "六轴机械臂"},
        _online_devices={"/cell/robot"},
        _action_value_mappings={
            "robot": {
                "move": {
                    "type": "UniLabJsonCommand",
                    "goal_default": {"speed": 20},
                    "schema": {
                        "properties": {
                            "goal": {
                                "properties": {
                                    "target": {"type": "string"},
                                    "speed": {"type": "integer"},
                                },
                                "required": ["target"],
                            },
                            "result": {
                                "properties": {
                                    "position": {"type": "string"}
                                }
                            },
                        }
                    },
                },
                "_execute_driver_command": {"schema": {}},
            }
        },
    )

    snapshot = build_device_catalog(
        host,
        machine_name="Edge A",
        is_action_busy=lambda device_id, action: (
            device_id,
            action,
        )
        == ("robot", "move"),
    )

    assert snapshot["schema"] == DEVICE_CATALOG_SCHEMA
    assert snapshot["devices"] == [
        {
            "device_id": "robot",
            "device_key": "/cell/robot",
            "namespace": "/cell",
            "machine_name": "六轴机械臂",
            "is_online": True,
            "actions": [
                {
                    "action_name": "move",
                    "action_ref": "robot.move",
                    "label": "move",
                    "type_name": "UniLabJsonCommand",
                    "input_schema": {
                        "target": {
                            "type": "string",
                            "required": True,
                        },
                        "speed": {
                            "type": "integer",
                            "default": 20,
                        },
                    },
                    "output_schema": {
                        "position": {"type": "string"}
                    },
                    "contract": {},
                    "is_busy": True,
                    "current_job_id": None,
                }
            ],
        }
    ]

    runtime_catalog = action_catalog_from_device_snapshot(snapshot)
    assert runtime_catalog["robot.move"]["inputs"]["target"]["required"] is True
    assert public_device_catalog(snapshot)["items"][0]["actions"][0][
        "actionRef"
    ] == "robot.move"


def test_action_lock_report_updates_busy_projection_without_mutating_source() -> None:
    snapshot = {
        "schema": DEVICE_CATALOG_SCHEMA,
        "timestamp": 1,
        "devices": [
            {
                "device_id": "camera",
                "actions": [
                    {
                        "action_name": "capture",
                        "action_ref": "camera.capture",
                        "is_busy": False,
                    }
                ],
            }
        ],
    }

    updated = apply_action_locks(
        snapshot,
        [
            {
                "device_id": "camera",
                "action_name": "capture",
                "free": False,
                "current_job_id": "job-capture",
            }
        ],
    )

    assert updated is not None
    assert updated["devices"][0]["actions"][0]["is_busy"] is True
    assert updated["devices"][0]["actions"][0]["current_job_id"] == (
        "job-capture"
    )
    assert snapshot["devices"][0]["actions"][0]["is_busy"] is False


def test_websocket_publisher_adapts_device_action_key(monkeypatch) -> None:
    host = SimpleNamespace(
        devices_names={"robot": "/cell"},
        device_machine_names={"robot": "Edge A"},
        _online_devices={"/cell/robot"},
        _action_value_mappings={
            "robot": {
                "move": {
                    "type": "UniLabJsonCommand",
                    "schema": {},
                }
            }
        },
    )
    monkeypatch.setattr(
        ws_client_module.HostNode,
        "get_instance",
        lambda _index: host,
    )

    client = object.__new__(ws_client_module.WebSocketClient)
    client.is_disabled = False
    client.is_connected = lambda: True
    client.device_manager = SimpleNamespace(
        is_action_busy=Mock(return_value=True),
        current_action_job_id=Mock(return_value="job-move"),
    )
    client.message_processor = SimpleNamespace(
        send_message=Mock(return_value=True)
    )

    assert client.publish_device_catalog(request_id="catalog-1") is True
    client.device_manager.is_action_busy.assert_called_once_with(
        "/devices/robot/move"
    )
    message = client.message_processor.send_message.call_args.args[0]
    assert message["data"]["request_id"] == "catalog-1"
    assert message["data"]["devices"][0]["actions"][0]["is_busy"] is True
    assert message["data"]["devices"][0]["actions"][0][
        "current_job_id"
    ] == "job-move"
