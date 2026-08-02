from __future__ import annotations

import time
from queue import Empty
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app import ws_client as ws_client_module
from unilabos.app.communication import CommunicationClientFactory
from unilabos.app.device_catalog import build_public_device_catalog
from unilabos.app.web.api import setup_api_routes
from unilabos.app.ws_client import JobInfo, JobStatus, WebSocketClient
from unilabos.config.config import BasicConfig
from unilabos.ros.nodes.presets.host_node import HostNode


def _job(job_id: str) -> JobInfo:
    return JobInfo(
        job_id=job_id,
        task_id="task-1",
        device_id="robot",
        notebook_id="",
        action_name="move",
        device_action_key="/devices/robot/move",
        status=JobStatus.QUEUE,
        start_time=time.time(),
    )


def _drain(client: WebSocketClient) -> list[dict]:
    messages = []
    while True:
        try:
            messages.append(client.send_queue.get_nowait())
        except Empty:
            return messages


def test_catalog_projects_schema_defaults_required_and_free_null_holder() -> None:
    host = SimpleNamespace(
        devices_names={"robot": "/cell"},
        device_machine_names={"robot": "机械臂"},
        _online_devices={"/cell/robot"},
        _action_value_mappings={
            "robot": {
                "move": {
                    "label": "移动",
                    "type": "example.Move",
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
                                "properties": {"position": {"type": "string"}},
                                "required": ["position"],
                            },
                        }
                    },
                },
                "_execute_driver_command": {"schema": {}},
            }
        },
    )

    catalog = build_public_device_catalog(
        host,
        machine_name="Edge A",
        is_action_busy=lambda _key: False,
        current_action_job_id=lambda _key: "must-not-leak-while-free",
    )

    device = catalog["items"][0]
    action = device["actions"][0]
    assert device["online"] is True
    assert action["inputSchema"] == {
        "target": {"type": "string", "required": True},
        "speed": {"type": "integer", "default": 20},
    }
    assert action["outputSchema"] == {"position": {"type": "string", "required": True}}
    assert action["busy"] is False
    assert action["currentJobId"] is None


def test_manual_unlock_fence_rejects_synchronous_and_late_success(
    monkeypatch,
) -> None:
    client = WebSocketClient()
    holder = _job("job-holder")
    assert client.device_manager.enqueue_job(holder) == (True, True)
    client.message_processor.connected = True

    host = SimpleNamespace()

    def cancel_goal(job_id: str) -> bool:
        assert job_id == holder.job_id
        client.publish_job_status({}, holder, "success", return_info={})
        return True

    host.cancel_goal = cancel_goal
    host._device_action_status = {}
    monkeypatch.setattr(
        ws_client_module.HostNode,
        "get_instance",
        lambda _index: host,
    )

    result = client.force_unlock_action(
        "robot",
        "move",
        expected_job_id=holder.job_id,
        reason="operator_confirmed_device_safe",
    )
    client.publish_job_status({}, holder, "success", return_info={})

    assert result["status"] == "unlocked"
    statuses = [
        message["data"]["status"]
        for message in _drain(client)
        if message.get("action") == "job_status"
    ]
    assert statuses == ["cancelled"]
    assert (
        client.get_cached_job_start_response_status(
            holder.job_id,
            holder.task_id,
        )
        == "cancelled"
    )


def test_busy_lock_report_includes_full_holder() -> None:
    client = WebSocketClient()
    holder = _job("job-holder-complete-token")
    assert client.device_manager.enqueue_job(holder) == (True, True)
    client.message_processor.connected = True

    client.publish_action_lock("robot", "move", free=False)

    message = _drain(client)[0]
    assert message["action"] == "report_action_lock"
    assert message["data"]["locks"] == [
        {
            "device_id": "robot",
            "action_name": "move",
            "free": False,
            "current_job_id": "job-holder-complete-token",
        }
    ]


def test_catalog_get_and_command_validation_fail_closed(monkeypatch) -> None:
    app = FastAPI()
    setup_api_routes(app)
    client = WebSocketClient()
    host = SimpleNamespace(
        devices_names={},
        device_machine_names={},
        _online_devices=set(),
        _action_value_mappings={},
    )
    monkeypatch.setattr(CommunicationClientFactory, "_client_cache", client)
    monkeypatch.setattr(BasicConfig, "communication_protocol", "websocket")
    monkeypatch.setattr(
        HostNode,
        "get_instance",
        classmethod(lambda cls, timeout=None: host),
    )

    with TestClient(app, client=("::1", 41000)) as http:
        catalog_response = http.get("/api/v1/devices")
        invalid_response = http.post(
            "/api/v1/devices/robot/actions/move/commands",
            json={
                "command": "force_unlock",
                "expectedJobId": "",
                "reason": "unsafe",
            },
        )

    assert catalog_response.status_code == 200
    assert catalog_response.json()["data"]["items"] == []
    assert invalid_response.status_code == 422
    assert invalid_response.json() == {
        "code": 422,
        "error": {
            "code": "INVALID_DEVICE_COMMAND",
            "message": "command、expectedJobId 或安全确认 reason 不合法",
        },
    }
    assert "detail" not in invalid_response.json()


def test_web_api_uses_cached_live_client_without_constructing_another(
    monkeypatch,
) -> None:
    app = FastAPI()
    setup_api_routes(app)
    live_client = WebSocketClient()
    host = SimpleNamespace(
        devices_names={},
        device_machine_names={},
        _online_devices=set(),
        _action_value_mappings={},
    )
    monkeypatch.setattr(CommunicationClientFactory, "_client_cache", live_client)
    monkeypatch.setattr(
        CommunicationClientFactory,
        "create_client",
        Mock(side_effect=AssertionError("must not create a second client")),
    )
    monkeypatch.setattr(BasicConfig, "communication_protocol", "websocket")
    monkeypatch.setattr(
        HostNode,
        "get_instance",
        classmethod(lambda cls, timeout=None: host),
    )

    response = TestClient(app, client=("127.0.0.1", 41000)).get("/api/v1/devices")

    assert response.status_code == 200
    assert CommunicationClientFactory.current_client() is live_client
