from __future__ import annotations

import asyncio
import json
import threading
import time
from queue import Empty
from types import SimpleNamespace
from unittest.mock import Mock

from action_msgs.msg import GoalStatus
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app import ws_client as ws_client_module
from unilabos.app.communication import CommunicationClientFactory
from unilabos.app.device_catalog import build_public_device_catalog
from unilabos.app.web.api import setup_api_routes
from unilabos.app.web.controller import job_result_store
from unilabos.app.ws_client import JobInfo, JobStatus, QueueItem, WebSocketClient
from unilabos.config.config import BasicConfig
from unilabos.ros.nodes.presets import host_node as host_node_module
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


def test_initial_dispatch_does_not_send_goal_after_manual_fence(monkeypatch) -> None:
    client = WebSocketClient()
    host = SimpleNamespace(send_goal=Mock())

    def unlock_before_host_is_returned(_index: int):
        result = client.device_manager.force_unlock(
            "/devices/robot/move",
            "job-initial",
        )
        assert result.status == "unlocked"
        return host

    monkeypatch.setattr(
        ws_client_module.HostNode,
        "get_instance",
        unlock_before_host_is_returned,
    )

    asyncio.run(
        client.message_processor._handle_job_start(
            {
                "device_id": "robot",
                "action": "move",
                "action_type": "example.Move",
                "sample_material": {},
                "action_args": {},
                "task_id": "task-initial",
                "job_id": "job-initial",
            }
        )
    )

    host.send_goal.assert_not_called()


def test_pending_start_does_not_send_goal_after_manual_fence(monkeypatch) -> None:
    client = WebSocketClient()
    holder = _job("job-holder")
    promoted = _job("job-promoted")
    assert client.device_manager.enqueue_job(holder) == (True, True)
    assert client.device_manager.enqueue_job(promoted) == (False, False)
    next_job, _ = client.device_manager.end_job(holder.job_id)
    assert next_job is promoted
    client.queue_processor.enqueue_pending_start(promoted)
    assert client.device_manager.force_unlock(
        promoted.device_action_key,
        promoted.job_id,
    ).status == "unlocked"

    host = SimpleNamespace(send_goal=Mock())
    monkeypatch.setattr(
        ws_client_module.HostNode,
        "get_instance",
        lambda _index: host,
    )

    client.queue_processor._drain_pending_starts()

    host.send_goal.assert_not_called()


def test_stale_free_report_uses_current_busy_holder() -> None:
    client = WebSocketClient()
    holder = _job("job-new-holder")
    assert client.device_manager.enqueue_job(holder) == (True, True)
    client.message_processor.connected = True

    client.publish_action_lock("robot", "move", free=True)

    message = _drain(client)[0]
    assert message["data"]["locks"] == [
        {
            "device_id": "robot",
            "action_name": "move",
            "free": False,
            "current_job_id": holder.job_id,
        }
    ]


def test_command_fails_closed_when_host_node_is_unavailable(monkeypatch) -> None:
    app = FastAPI()
    setup_api_routes(app)
    client = WebSocketClient()
    monkeypatch.setattr(CommunicationClientFactory, "_client_cache", client)
    monkeypatch.setattr(BasicConfig, "communication_protocol", "websocket")
    monkeypatch.setattr(
        HostNode,
        "get_instance",
        classmethod(lambda cls, timeout=None: None),
    )

    response = TestClient(app, client=("127.0.0.1", 41000)).post(
        "/api/v1/devices/robot/actions/move/commands",
        json={
            "command": "force_unlock",
            "expectedJobId": "job-holder",
            "reason": "operator_confirmed_device_safe",
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEVICE_CONTROL_UNAVAILABLE"


def test_cancel_exception_does_not_skip_remaining_snapshot(monkeypatch) -> None:
    client = WebSocketClient()
    holder = _job("job-holder")
    queued = _job("job-queued")
    assert client.device_manager.enqueue_job(holder) == (True, True)
    assert client.device_manager.enqueue_job(queued) == (False, False)
    attempted: list[str] = []

    def cancel_goal(job_id: str) -> bool:
        attempted.append(job_id)
        if job_id == holder.job_id:
            raise RuntimeError("driver cancel failed")
        return True

    host = SimpleNamespace(
        cancel_goal=cancel_goal,
        _device_action_status={},
    )
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

    assert result["status"] == "unlocked"
    assert attempted == [holder.job_id, queued.job_id]
    assert result["cancelRequestedJobIds"] == [queued.job_id]


def test_malformed_or_missing_command_body_uses_backend_envelope(monkeypatch) -> None:
    app = FastAPI()
    setup_api_routes(app)
    client = WebSocketClient()
    host = SimpleNamespace(
        devices_names={"robot": "/devices"},
        _action_value_mappings={"robot": {"move": {}}},
    )
    monkeypatch.setattr(CommunicationClientFactory, "_client_cache", client)
    monkeypatch.setattr(BasicConfig, "communication_protocol", "websocket")
    monkeypatch.setattr(
        HostNode,
        "get_instance",
        classmethod(lambda cls, timeout=None: host),
    )

    with TestClient(app, client=("127.0.0.1", 41000)) as http:
        missing = http.post(
            "/api/v1/devices/robot/actions/move/commands",
        )
        malformed = http.post(
            "/api/v1/devices/robot/actions/move/commands",
            content="{",
            headers={"content-type": "application/json"},
        )

    for response in (missing, malformed):
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_DEVICE_COMMAND"
        assert "detail" not in response.json()


def test_setup_api_routes_is_idempotent() -> None:
    app = FastAPI()

    setup_api_routes(app)
    setup_api_routes(app)

    command_routes = [
        route
        for route in app.routes
        if getattr(route, "path", "")
        == "/api/v1/devices/{device_id}/actions/{action_name}/commands"
    ]
    assert len(command_routes) == 1


def test_ordinary_failed_result_can_still_be_corrected_to_success(
    monkeypatch,
) -> None:
    client = WebSocketClient()
    job = _job("job-corrected")
    assert client.device_manager.enqueue_job(job) == (True, True)
    client.message_processor.connected = True
    host = SimpleNamespace(_device_action_status={})
    monkeypatch.setattr(
        ws_client_module.HostNode,
        "get_instance",
        lambda _index: host,
    )

    client.publish_job_status({}, job, "failed", return_info={})
    client.publish_job_status({}, job, "success", return_info={})

    statuses = [
        message["data"]["status"]
        for message in _drain(client)
        if message.get("action") == "job_status"
    ]
    assert statuses == ["failed", "success"]
    assert client.get_cached_job_start_response_status(
        job.job_id,
        job.task_id,
    ) == "success"


class _DoneFuture:
    def __init__(self, value) -> None:
        self.value = value
        self.callbacks = []

    def result(self):
        return self.value

    def add_done_callback(self, callback) -> None:
        self.callbacks.append(callback)


class _AcceptedGoal:
    accepted = True

    def __init__(self) -> None:
        self.cancel_calls = 0
        self.result_future = _DoneFuture(None)

    def cancel_goal_async(self):
        self.cancel_calls += 1
        return _DoneFuture(SimpleNamespace(goals_canceling=[self]))

    def get_result_async(self):
        return self.result_future


class _ActionClientBeforeSubmit:
    def __init__(self) -> None:
        self.send_goal_async = Mock()

    def wait_for_server(self, *, timeout_sec: float) -> bool:
        return True


def _bare_host(*, bridges=None):
    host = object.__new__(HostNode)
    host._goals = {}
    host._goal_trace_contexts = {}
    host._pending_goal_requests = set()
    host._pending_goal_cancellations = set()
    host._goals_lock = threading.RLock()
    host._device_action_status = {}
    host.bridges = list(bridges or [])
    logger = Mock()
    logger.info = Mock()
    logger.warning = Mock()
    logger.error = Mock()
    logger.trace = Mock()
    host.lab_logger = Mock(return_value=logger)
    return host


def _queue_item(job: JobInfo) -> QueueItem:
    return QueueItem(
        task_type="job_call_back_status",
        device_id=job.device_id,
        action_name=job.action_name,
        task_id=job.task_id,
        job_id=job.job_id,
        notebook_id=job.notebook_id,
        device_action_key=job.device_action_key,
    )


def test_manual_cancel_is_deferred_until_async_goal_is_accepted() -> None:
    host = _bare_host()
    job = _job("00000000-0000-0000-0000-000000000321")
    goal = _AcceptedGoal()
    host._pending_goal_requests.add(job.job_id)

    assert host.cancel_goal_or_defer(job.job_id) is True
    assert job.job_id in host._pending_goal_cancellations

    host.goal_response_callback(
        _queue_item(job),
        "/devices/robot/move",
        _DoneFuture(goal),
    )

    assert goal.cancel_calls == 1
    assert job.job_id not in host._pending_goal_cancellations
    assert host._goals[job.job_id] is goal


def test_deferred_trace_send_is_dropped_if_unlock_wins_before_ros_submit(
    monkeypatch,
) -> None:
    host = _bare_host()
    host._shutting_down = False
    job = _job("00000000-0000-0000-0000-000000000432")
    action_client = _ActionClientBeforeSubmit()
    host._pending_goal_requests.add(job.job_id)
    monkeypatch.setattr(
        host_node_module.uuid,
        "UUID",
        Mock(side_effect=AssertionError("goal UUID must not be built")),
    )

    assert host.cancel_goal_or_defer(job.job_id) is True
    HostNode._send_action_goal(
        host,
        _queue_item(job),
        "/devices/robot/move",
        action_client,
        object(),
        {},
        server_wait_timeout=0.1,
    )

    action_client.send_goal_async.assert_not_called()
    assert job.job_id not in host._pending_goal_requests
    assert job.job_id not in host._pending_goal_cancellations


def test_late_host_result_cannot_overwrite_manual_cancelled_store(
    monkeypatch,
) -> None:
    client = WebSocketClient()
    job = _job("00000000-0000-0000-0000-000000000654")
    assert client.device_manager.enqueue_job(job) == (True, True)
    host = _bare_host(bridges=[client])
    host._pending_goal_requests.add(job.job_id)
    monkeypatch.setattr(
        ws_client_module.HostNode,
        "get_instance",
        lambda _index: host,
    )
    monkeypatch.setattr(
        host_node_module,
        "convert_from_ros_msg",
        lambda _message: {
            "return_info": json.dumps(
                {"success": True, "message": "late", "data": {}}
            )
        },
    )
    job_result_store.get_and_remove(job.job_id)

    result = client.force_unlock_action(
        "robot",
        "move",
        expected_job_id=job.job_id,
        reason="operator_confirmed_device_safe",
    )
    stored_after_unlock = job_result_store.get_result(job.job_id)
    assert result["cancelRequestedJobIds"] == [job.job_id]
    assert stored_after_unlock is None

    host.get_result_callback(
        _queue_item(job),
        "/devices/robot/move",
        _DoneFuture(
            SimpleNamespace(
                status=GoalStatus.STATUS_SUCCEEDED,
                result=object(),
            )
        ),
    )

    stored_after_late_result = job_result_store.get_result(job.job_id)
    assert stored_after_late_result is None
    assert client.get_cached_job_start_response_status(
        job.job_id,
        job.task_id,
    ) == "cancelled"
    job_result_store.get_and_remove(job.job_id)


def test_manual_unlock_clears_legacy_result_written_before_fence(
    monkeypatch,
) -> None:
    client = WebSocketClient()
    job = _job("00000000-0000-0000-0000-000000000765")
    assert client.device_manager.enqueue_job(job) == (True, True)
    monkeypatch.setattr(
        ws_client_module.HostNode,
        "get_instance",
        lambda _index: None,
    )
    job_result_store.get_and_remove(job.job_id)

    assert client.store_job_result_if_unfenced(
        job.job_id,
        "success",
        {"success": True},
        {},
    ) is True
    assert job_result_store.get_result(job.job_id) is not None

    result = client.force_unlock_action(
        "robot",
        "move",
        expected_job_id=job.job_id,
        reason="operator_confirmed_device_safe",
    )

    assert result["status"] == "unlocked"
    assert job_result_store.get_result(job.job_id) is None
    assert client.store_job_result_if_unfenced(
        job.job_id,
        "success",
        {"success": True},
        {},
    ) is False
    assert job_result_store.get_result(job.job_id) is None
