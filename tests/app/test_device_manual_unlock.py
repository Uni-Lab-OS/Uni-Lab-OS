from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app import ws_client as ws_client_module
from unilabos.app.local_bridge.device_control_api import (
    DeviceControlProxy,
    DeviceControlProxyError,
)
from unilabos.app.local_bridge.local_api import LocalApiState, create_app
from unilabos.app.local_bridge.schedule_ws import ScheduleSession
from unilabos.app.web.device_control import create_device_control_router
from unilabos.app.ws_client import DeviceActionManager, JobInfo, JobStatus


ACTION_KEY = "/devices/robot/move"


def _job(job_id: str) -> JobInfo:
    return JobInfo(
        job_id=job_id,
        task_id="task-1",
        device_id="robot",
        notebook_id="",
        action_name="move",
        device_action_key=ACTION_KEY,
        status=JobStatus.QUEUE,
        start_time=time.time(),
    )


def _busy_manager() -> DeviceActionManager:
    manager = DeviceActionManager()
    assert manager.enqueue_job(_job("job-active")) == (True, True)
    assert manager.enqueue_job(_job("job-queued")) == (False, False)
    return manager


def test_force_release_action_compares_holder_and_clears_active_and_queue() -> None:
    manager = _busy_manager()

    status, released = manager.force_release_action(
        ACTION_KEY,
        expected_job_id="a-new-holder",
    )
    assert status == "lock_changed"
    assert released == []
    assert manager.current_action_job_id(ACTION_KEY) == "job-active"

    status, released = manager.force_release_action(
        ACTION_KEY,
        expected_job_id="job-active",
    )
    assert status == "released"
    assert [job.job_id for job in released] == ["job-active", "job-queued"]
    assert manager.is_action_busy(ACTION_KEY) is False
    assert manager.all_jobs == {}

    status, released = manager.force_release_action(
        ACTION_KEY,
        expected_job_id="job-active",
    )
    assert status == "already_unlocked"
    assert released == []


def test_websocket_force_unlock_requests_physical_cancel_and_reports_free(
    monkeypatch,
) -> None:
    manager = _busy_manager()
    host = SimpleNamespace(cancel_goal=Mock(return_value=True))
    monkeypatch.setattr(
        ws_client_module.HostNode,
        "get_instance",
        lambda _index: host,
    )
    client = object.__new__(ws_client_module.WebSocketClient)
    client.device_manager = manager
    client.queue_processor = SimpleNamespace(notify_queue_update=Mock())
    client.publish_job_status = Mock()
    client.publish_action_lock = Mock()

    result = client.force_unlock_action(
        "robot",
        "move",
        expected_job_id="job-active",
        reason="operator_confirmed_device_safe",
    )

    assert result == {
        "status": "released",
        "deviceId": "robot",
        "actionName": "move",
        "releasedJobIds": ["job-active", "job-queued"],
        "cancelRequestedJobIds": ["job-active", "job-queued"],
    }
    assert [call.args[0] for call in host.cancel_goal.call_args_list] == [
        "job-active",
        "job-queued",
    ]
    assert client.publish_job_status.call_count == 2
    client.publish_action_lock.assert_called_once_with(
        "robot",
        "move",
        free=True,
    )


def test_websocket_force_unlock_isolates_queue_from_fast_cancel_callback(
    monkeypatch,
) -> None:
    """A fast ROS cancel callback must not promote the queued holder mid-CAS."""

    manager = _busy_manager()

    def cancel_with_immediate_terminal(job_id: str) -> bool:
        manager.cancel_job(job_id)
        return True

    host = SimpleNamespace(cancel_goal=Mock(side_effect=cancel_with_immediate_terminal))
    monkeypatch.setattr(
        ws_client_module.HostNode,
        "get_instance",
        lambda _index: host,
    )
    client = object.__new__(ws_client_module.WebSocketClient)
    client.device_manager = manager
    client.queue_processor = SimpleNamespace(
        enqueue_pending_start=Mock(),
        notify_queue_update=Mock(),
    )
    client.publish_job_status = Mock()
    client.publish_action_lock = Mock()

    result = client.force_unlock_action(
        "robot",
        "move",
        expected_job_id="job-active",
        reason="operator_confirmed_device_safe",
    )

    assert result["status"] == "released"
    assert result["releasedJobIds"] == ["job-active", "job-queued"]
    assert manager.is_action_busy(ACTION_KEY) is False
    client.queue_processor.enqueue_pending_start.assert_not_called()
    client.publish_action_lock.assert_called_once_with(
        "robot",
        "move",
        free=True,
    )


def test_internal_device_command_returns_409_when_holder_changed() -> None:
    app = FastAPI()
    app.include_router(
        create_device_control_router(
            lambda *_args, **_kwargs: {
                "status": "lock_changed",
                "currentJobId": "job-new",
            }
        ),
        prefix="/internal/v1",
    )
    client = TestClient(app)

    response = client.post(
        "/internal/v1/device-actions/robot/move/commands",
        json={
            "command": "force_unlock",
            "expectedJobId": "job-old",
            "reason": "operator_confirmed_device_safe",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DEVICE_LOCK_CHANGED"


def test_device_control_proxy_preserves_command_and_structured_conflict() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if json.loads(request.content)["expectedJobId"] == "job-old":
            return httpx.Response(
                409,
                json={
                    "error": {
                        "code": "DEVICE_LOCK_CHANGED",
                        "message": "holder changed",
                        "retryable": False,
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "released",
                "releasedJobIds": ["job-active"],
            },
        )

    proxy = DeviceControlProxy(
        "http://127.0.0.1:18003",
        transport=httpx.MockTransport(handler),
    )

    result = asyncio.run(
        proxy.force_unlock_action(
            "robot",
            "move",
            expected_job_id="job-active",
            reason="operator_confirmed_device_safe",
        )
    )
    assert result["status"] == "released"
    assert requests[0].url.path == (
        "/internal/v1/device-actions/robot/move/commands"
    )

    try:
        asyncio.run(
            proxy.force_unlock_action(
                "robot",
                "move",
                expected_job_id="job-old",
                reason="operator_confirmed_device_safe",
            )
        )
    except DeviceControlProxyError as exc:
        assert exc.status == 409
        assert exc.code == "DEVICE_LOCK_CHANGED"
        assert exc.retryable is False
    else:
        raise AssertionError("expected DeviceControlProxyError")


def test_public_bridge_command_uses_device_control_proxy() -> None:
    async def send(_message: dict) -> None:
        return None

    state = LocalApiState(ScheduleSession(send), action_catalog={})
    class FakeProxy:
        def __init__(self) -> None:
            self.calls: list[tuple[tuple, dict]] = []

        async def force_unlock_action(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return {
                "status": "released",
                "releasedJobIds": ["job-active"],
            }

    proxy = FakeProxy()
    client = TestClient(
        create_app(lambda: state, device_control_proxy=proxy)
    )

    response = client.post(
        "/api/v1/devices/robot/actions/move/commands",
        json={
            "command": "force_unlock",
            "expectedJobId": "job-active",
            "reason": "operator_confirmed_device_safe",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "released"
    assert proxy.calls == [
        (
            ("robot", "move"),
            {
                "expected_job_id": "job-active",
                "reason": "operator_confirmed_device_safe",
            },
        )
    ]
