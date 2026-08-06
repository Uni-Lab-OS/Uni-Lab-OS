"""Core #160 device Action manual-unlock integration contract.

The supported composition is the current ``app.web.server`` FastAPI root plus
the process-live communication client.  The retired local bridge and legacy
``/api/v1/runtime/runs`` surface are deliberately outside this test seam.
"""

from __future__ import annotations

from collections.abc import Mapping
from queue import Empty
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.communication import CommunicationClientFactory
from unilabos.app.web.controller import job_result_store
from unilabos.app.ws_client import (
    DeviceActionManager,
    JobInfo,
    JobStatus,
    WebSocketClient,
)
from unilabos.config.config import BasicConfig
from unilabos.ros.nodes.presets.host_node import HostNode

DEVICE_ID = "pump-1"
ACTION_NAME = "dose"
ACTION_KEY = f"/devices/{DEVICE_ID}/{ACTION_NAME}"
HOLDER_JOB_ID = "job-holder-00000000-0000-0000-0000-000000000001"
QUEUED_JOB_ID = "job-queued-00000000-0000-0000-0000-000000000002"
NEW_JOB_ID = "job-new-holder-0000-0000-0000-000000000003"


def _job(job_id: str, *, task_id: str) -> JobInfo:
    return JobInfo(
        job_id=job_id,
        task_id=task_id,
        device_id=DEVICE_ID,
        notebook_id="",
        action_name=ACTION_NAME,
        device_action_key=ACTION_KEY,
        status=JobStatus.QUEUE,
        start_time=0.0,
    )


def _force_unlock(
    manager: DeviceActionManager,
    expected_job_id: str,
) -> Any:
    operation = getattr(manager, "force_unlock", None)
    assert callable(operation), (
        "Core #160 missing capability: DeviceActionManager.force_unlock must "
        "atomically compare the holder and isolate the active/queued snapshot"
    )
    return operation(ACTION_KEY, expected_job_id)


def _status_of(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, Mapping):
        return str(result.get("status") or "")
    status = getattr(result, "status", "")
    if hasattr(status, "value"):
        status = status.value
    if status:
        return str(status)
    if isinstance(result, tuple) and result:
        return _status_of(result[0])
    return ""


def _assert_lock_changed_without_clearing_new_holder(
    manager: DeviceActionManager,
    *,
    stale_expected_job_id: str,
) -> None:
    try:
        result = _force_unlock(manager, stale_expected_job_id)
    except AssertionError:
        raise
    except Exception as exc:  # noqa: BLE001 - internal form is not frozen.
        marker = f"{type(exc).__name__} {exc}".upper()
        assert "LOCK" in marker and "CHANG" in marker
    else:
        assert _status_of(result) in {"lock_changed", "conflict"}

    active = manager.get_active_jobs()
    assert [job.job_id for job in active] == [NEW_JOB_ID]
    assert manager.is_action_busy(ACTION_KEY) is True


class TestDeviceActionManagerManualUnlock:
    def test_expected_holder_is_cas_token_and_mismatch_preserves_holder(self) -> None:
        manager = DeviceActionManager()
        should_start, became_busy = manager.enqueue_job(
            _job(NEW_JOB_ID, task_id="task-new")
        )
        assert (should_start, became_busy) == (True, True)

        _assert_lock_changed_without_clearing_new_holder(
            manager,
            stale_expected_job_id=HOLDER_JOB_ID,
        )

    def test_force_unlock_isolates_active_and_queue_before_new_holder(self) -> None:
        manager = DeviceActionManager()
        assert manager.enqueue_job(_job(HOLDER_JOB_ID, task_id="task-holder")) == (
            True,
            True,
        )
        assert manager.enqueue_job(_job(QUEUED_JOB_ID, task_id="task-queued")) == (
            False,
            False,
        )

        result = _force_unlock(manager, HOLDER_JOB_ID)

        assert _status_of(result) == "unlocked"
        assert manager.get_job_info(HOLDER_JOB_ID) is None
        assert manager.get_job_info(QUEUED_JOB_ID) is None
        assert manager.get_active_jobs() == []
        assert manager.get_queued_jobs() == []
        assert manager.is_action_busy(ACTION_KEY) is False

        # A job admitted after snapshot isolation is the new holder. A delayed
        # cancellation/result callback for the old holder must not remove it.
        assert manager.enqueue_job(_job(NEW_JOB_ID, task_id="task-new")) == (True, True)
        assert manager.end_job(HOLDER_JOB_ID) == (None, False)
        _assert_lock_changed_without_clearing_new_holder(
            manager,
            stale_expected_job_id=HOLDER_JOB_ID,
        )

    def test_missing_holder_is_idempotent_already_unlocked(self) -> None:
        manager = DeviceActionManager()

        first = _force_unlock(manager, HOLDER_JOB_ID)
        second = _force_unlock(manager, HOLDER_JOB_ID)

        assert _status_of(first) == "already_unlocked"
        assert _status_of(second) == "already_unlocked"
        assert manager.get_active_jobs() == []
        assert manager.get_queued_jobs() == []


class _FakeHostNode:
    def __init__(self) -> None:
        self.devices_names = {DEVICE_ID: "/devices"}
        self.device_machine_names = {DEVICE_ID: "Pump 1"}
        self._online_devices = {f"/devices/{DEVICE_ID}"}
        self._action_value_mappings = {
            DEVICE_ID: {
                ACTION_NAME: {
                    "label": "Dose",
                    "type": "example.Dose",
                    "schema": {
                        "properties": {
                            "goal": {"type": "object", "properties": {}},
                            "result": {"type": "object", "properties": {}},
                        }
                    },
                    "goal_default": {},
                }
            }
        }
        self._device_action_status: dict[str, Any] = {}
        self.devices_config = {}
        self.cancelled_job_ids: list[str] = []
        self.on_cancel: Any = None

    def cancel_goal(self, job_id: str) -> bool:
        self.cancelled_job_ids.append(job_id)
        if self.on_cancel is not None:
            self.on_cancel(job_id)
        return True


@pytest.fixture()
def live_edge(monkeypatch: pytest.MonkeyPatch) -> tuple[WebSocketClient, _FakeHostNode]:
    client = WebSocketClient()
    host_node = _FakeHostNode()
    monkeypatch.setattr(
        HostNode,
        "get_instance",
        classmethod(lambda cls, timeout=None: host_node),
    )
    monkeypatch.setattr(CommunicationClientFactory, "_client_cache", client)
    monkeypatch.setattr(BasicConfig, "communication_protocol", "websocket")
    monkeypatch.setattr(BasicConfig, "working_dir", "")
    return client, host_node


@pytest.fixture()
def composed_app(monkeypatch: pytest.MonkeyPatch, live_edge: Any) -> FastAPI:
    from unilabos.app.web import server

    fresh_app = FastAPI(title="manual-unlock-integration-test")
    monkeypatch.setattr(server, "app", fresh_app)
    monkeypatch.setattr(server, "pages", None)
    monkeypatch.setattr(server, "workflow_routes_mounted", True)
    monkeypatch.setattr(server, "observability_routes_mounted", True)
    monkeypatch.setattr(server, "setup_web_pages", lambda router: None)
    return server.setup_server()


def _http_client(app: FastAPI, *, host: str = "127.0.0.1") -> TestClient:
    return TestClient(app, client=(host, 41000))


def _command_body(*, expected_job_id: str = HOLDER_JOB_ID) -> dict[str, str]:
    return {
        "command": "force_unlock",
        "expectedJobId": expected_job_id,
        "reason": "operator_confirmed_device_safe",
    }


def _command_path() -> str:
    return f"/api/v1/devices/{DEVICE_ID}/actions/{ACTION_NAME}/commands"


COMMAND_ROUTE_TEMPLATE = "/api/v1/devices/{device_id}/actions/{action_name}/commands"


def _find_action(response_body: dict[str, Any]) -> dict[str, Any]:
    data = response_body["data"]
    for device in data["items"]:
        if device["id"] != DEVICE_ID:
            continue
        for action in device["actions"]:
            if action["id"] == ACTION_NAME:
                return action
    raise AssertionError("device Action missing from GET /api/v1/devices")


def _drain_messages(client: WebSocketClient) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    while True:
        try:
            messages.append(client.send_queue.get_nowait())
        except Empty:
            return messages


class TestCurrentFastApiComposition:
    def test_devices_projects_full_holder_from_live_communication_client(
        self,
        composed_app: FastAPI,
        live_edge: tuple[WebSocketClient, _FakeHostNode],
    ) -> None:
        client, _ = live_edge
        assert client.device_manager.enqueue_job(
            _job(HOLDER_JOB_ID, task_id="task-holder")
        ) == (True, True)

        response = _http_client(composed_app).get("/api/v1/devices")

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"code", "data"}
        assert body["code"] == 0
        action = _find_action(body)
        assert action["busy"] is True
        assert action["currentJobId"] == HOLDER_JOB_ID
        assert action["currentJobId"] != HOLDER_JOB_ID[:8]

    def test_command_isolates_snapshot_before_cancel_and_preserves_new_holder(
        self,
        composed_app: FastAPI,
        live_edge: tuple[WebSocketClient, _FakeHostNode],
    ) -> None:
        client, host_node = live_edge
        manager = client.device_manager
        assert manager.enqueue_job(_job(HOLDER_JOB_ID, task_id="task-holder")) == (
            True,
            True,
        )
        assert manager.enqueue_job(_job(QUEUED_JOB_ID, task_id="task-queued")) == (
            False,
            False,
        )

        def admit_new_holder_after_isolation(job_id: str) -> None:
            # A fast ROS cancellation callback may arrive here too. Both the
            # callback and the newly admitted holder must be outside the old
            # active+queue snapshot.
            manager.end_job(job_id)
            if manager.get_job_info(NEW_JOB_ID) is None:
                assert manager.enqueue_job(_job(NEW_JOB_ID, task_id="task-new")) == (
                    True,
                    True,
                )

        host_node.on_cancel = admit_new_holder_after_isolation

        response = _http_client(composed_app).post(
            _command_path(),
            json=_command_body(),
        )

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"code", "data"}
        assert body["code"] == 0
        assert body["data"]["status"] == "unlocked"
        assert body["data"]["currentJobId"] == NEW_JOB_ID
        assert sorted(host_node.cancelled_job_ids) == sorted(
            [HOLDER_JOB_ID, QUEUED_JOB_ID]
        )
        assert NEW_JOB_ID not in host_node.cancelled_job_ids
        assert [job.job_id for job in manager.get_active_jobs()] == [NEW_JOB_ID]

        catalog_response = _http_client(composed_app).get("/api/v1/devices")
        assert _find_action(catalog_response.json())["currentJobId"] == NEW_JOB_ID

    def test_changed_holder_returns_409_without_releasing_it(
        self,
        composed_app: FastAPI,
        live_edge: tuple[WebSocketClient, _FakeHostNode],
    ) -> None:
        client, host_node = live_edge
        assert client.device_manager.enqueue_job(
            _job(NEW_JOB_ID, task_id="task-new")
        ) == (True, True)

        response = _http_client(composed_app).post(
            _command_path(),
            json=_command_body(expected_job_id=HOLDER_JOB_ID),
        )

        assert response.status_code == 409
        body = response.json()
        assert set(body) == {"code", "error"}
        assert body["code"] == 409
        assert set(body["error"]) == {"code", "message"}
        assert body["error"]["code"] == "DEVICE_LOCK_CHANGED"
        assert "detail" not in body
        assert host_node.cancelled_job_ids == []
        assert [job.job_id for job in client.device_manager.get_active_jobs()] == [
            NEW_JOB_ID
        ]

    def test_repeat_command_is_idempotent_and_does_not_fabricate_job_success(
        self,
        composed_app: FastAPI,
        live_edge: tuple[WebSocketClient, _FakeHostNode],
    ) -> None:
        client, _ = live_edge
        manager = client.device_manager
        assert manager.enqueue_job(_job(HOLDER_JOB_ID, task_id="task-holder")) == (
            True,
            True,
        )

        first = _http_client(composed_app).post(
            _command_path(),
            json=_command_body(),
        )
        second = _http_client(composed_app).post(
            _command_path(),
            json=_command_body(),
        )

        assert first.status_code == 200
        assert first.json()["data"]["status"] == "unlocked"
        assert second.status_code == 200
        assert second.json() == {
            "code": 0,
            "data": {"status": "already_unlocked", "currentJobId": None},
        }
        terminal_messages = [
            message
            for message in _drain_messages(client)
            if message.get("action") == "job_status"
            and message.get("data", {}).get("status") == "success"
        ]
        assert terminal_messages == []
        assert job_result_store.get_result(HOLDER_JOB_ID) is None

    @pytest.mark.parametrize(
        ("host", "profile"),
        [
            ("203.0.113.7", "websocket"),
            ("127.0.0.1", "unknown-edge-profile"),
        ],
    )
    def test_non_loopback_or_unknown_profile_is_denied_by_default(
        self,
        composed_app: FastAPI,
        live_edge: tuple[WebSocketClient, _FakeHostNode],
        monkeypatch: pytest.MonkeyPatch,
        host: str,
        profile: str,
    ) -> None:
        client, host_node = live_edge
        assert client.device_manager.enqueue_job(
            _job(HOLDER_JOB_ID, task_id="task-holder")
        ) == (True, True)
        monkeypatch.setattr(BasicConfig, "communication_protocol", profile)

        response = _http_client(composed_app, host=host).post(
            _command_path(),
            json=_command_body(),
        )

        assert response.status_code == 403
        body = response.json()
        assert body["code"] == 403
        assert body["error"]["code"] == "DEVICE_UNLOCK_FORBIDDEN"
        assert host_node.cancelled_job_ids == []
        assert [job.job_id for job in client.device_manager.get_active_jobs()] == [
            HOLDER_JOB_ID
        ]

    def test_composition_does_not_restore_local_bridge_or_legacy_runtime(
        self,
        composed_app: FastAPI,
    ) -> None:
        assert all(
            not route.endpoint.__module__.startswith("unilabos.app.local_bridge")
            for route in composed_app.routes
            if hasattr(route, "endpoint")
        )
        assert all(
            not getattr(route, "path", "").startswith("/api/v1/runtime/runs")
            for route in composed_app.routes
        )

    def test_manual_unlock_route_is_owned_by_current_web_api(
        self,
        composed_app: FastAPI,
    ) -> None:
        routes = {
            (method, route.path): route
            for route in composed_app.routes
            for method in getattr(route, "methods", set())
        }
        command_route = routes[("POST", COMMAND_ROUTE_TEMPLATE)]
        assert command_route.endpoint.__module__ == "unilabos.app.web.api"
