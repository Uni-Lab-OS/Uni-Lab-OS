"""生产 Edge 协议客户端的持久化和任务闭环测试。"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pytest

from unilabos.app.edge_control.client import (
    EdgeControlClient,
    EdgeControlSettings,
    _stored_event_envelope,
)
from unilabos.app.edge_control.http import (
    BACKEND_UNAUTHORIZED_BUSINESS_CODE,
    EdgeDataPlane,
    EdgeProtocolHTTPError,
)
from unilabos.app.edge_control.store import EdgeControlStore, StoredJob
from unilabos.config.config import BasicConfig, EdgeControlConfig, HTTPConfig


class FakeDataPlane:
    def __init__(self) -> None:
        self.fetched_jobs: List[StoredJob] = []
        self.outcomes: List[Dict[str, Any]] = []

    def fetch_job(self, job: StoredJob) -> Dict[str, Any]:
        self.fetched_jobs.append(job)
        return {
            "job_uuid": job.job_uuid,
            "task_uuid": job.task_uuid,
            "node_uuid": job.node_uuid,
            "command_uuid": job.command_uuid,
            "local_device_id": "heater-01",
            "action_name": "heat",
            "action_type": "UniLabJsonCommand",
            "param": {
                "unilabos_device_id": "heater-01",
				"timeout_seconds": 7200,
				"assignee_user_ids": ["operator-1"],
                "temperature": 37,
            },
        }

    def commit_outcome(
        self,
        job: StoredJob,
        outcome: str,
        return_info: Dict[str, Any],
        error_info: List[Dict[str, Any]],
        unknown_command_ids: List[str] | None = None,
    ) -> Dict[str, Any]:
        self.outcomes.append(
            {
                "job": job,
                "outcome": outcome,
                "return_info": return_info,
                "error_info": error_info,
                "unknown_command_ids": unknown_command_ids or [],
            }
        )
        return {"uuid": str(uuid.uuid4())}


def test_edge_control_settings_derives_split_scheduler_address(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(EdgeControlConfig, "scheduler_addr", "")
    monkeypatch.setattr(EdgeControlConfig, "backend_addr", "")
    monkeypatch.setattr(EdgeControlConfig, "state_db", str(tmp_path / "edge.db"))
    monkeypatch.setattr(HTTPConfig, "schedule_addr", "")
    monkeypatch.setattr(HTTPConfig, "remote_addr", "http://[::1]:8080")
    monkeypatch.setattr(BasicConfig, "machine_name", "edge-fixture")

    settings = EdgeControlSettings.from_config()

    assert settings.backend_address == "http://[::1]:8080"
    assert settings.scheduler_address == "http://[::1]:8081"


def test_edge_control_settings_marks_local_authority_keyless(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(EdgeControlConfig, "api_key", "")
    monkeypatch.setattr(EdgeControlConfig, "state_db", str(tmp_path / "edge.db"))
    monkeypatch.setattr(BasicConfig, "control_plane", "local")

    settings = EdgeControlSettings.from_config()

    assert settings.api_key == ""
    assert settings.api_key_required is False
    assert settings.device_telemetry_enabled is True


class FakeHostNode:
    def __init__(self) -> None:
        self.started: List[Dict[str, Any]] = []
        self.unknown_resolutions: List[Dict[str, str]] = []
        self.dispatch_block_reasons: Dict[str, str] = {}

    def send_goal(
        self,
        item: Any,
        action_type: str,
        action_kwargs: Dict[str, Any],
        sample_material: Dict[str, str],
        server_info: Any,
    ) -> None:
        self.started.append(
            {
                "item": item,
                "action_type": action_type,
                "action_kwargs": action_kwargs,
                "sample_material": sample_material,
                "server_info": server_info,
            }
        )

    def resolve_unknown_device_command(
        self,
        device_id: str,
        device_command_id: str,
        resolution_command_uuid: str,
        reason: str,
    ) -> Dict[str, Any]:
        self.unknown_resolutions.append(
            {
                "device_id": device_id,
                "device_command_id": device_command_id,
                "resolution_command_uuid": resolution_command_uuid,
                "reason": reason,
            }
        )
        return {
            "command_id": device_command_id,
            "state": "CANCELED",
            "previous_state": "UNKNOWN",
            "resolution_committed": True,
            "resolution_command_uuid": resolution_command_uuid,
            "message": reason,
        }

    def device_dispatch_block_reason(self, device_id: str) -> str:
        return self.dispatch_block_reasons.get(device_id, "")

    def device_unknown_command_ids(self, device_id: str) -> List[str]:
        reason = self.device_dispatch_block_reason(device_id)
        prefix = "unresolved_unknown_command:"
        return reason.removeprefix(prefix).split(",") if reason.startswith(prefix) else []


class FakeRegistrationResources:
    def dump(self) -> List[List[Dict[str, Any]]]:
        return [
            [
                {
                    "id": "robot-01",
                    "name": "Robot 01",
                    "barcode": "ROBOT-01",
                }
            ]
        ]


class FakeRegistrationResourcesWithoutBarcode:
    def dump(self) -> List[List[Dict[str, Any]]]:
        return [[{"id": "robot-01", "name": "Robot 01"}]]


class FakeRegistrationResourcesWithClass:
    def dump(self) -> List[List[Dict[str, Any]]]:
        return [[{
            "id": "robot-01",
            "name": "Robot 01",
            "barcode": "ROBOT-01",
            "class": "community.test.robot",
        }]]


class FakeRegistrationHostNode:
    def __init__(self) -> None:
        self.resources_config = FakeRegistrationResources()
        self.devices_names = {"robot-01": "/devices/robot-01"}
        self._action_value_mappings = {
            "robot-01": {
                "_execute_driver_command": {"type": "StrSingleInput"},
                "pick": {"type": "UniLabJsonCommand"},
                "place": {"type": "UniLabJsonCommand"},
            }
        }

    def device_dispatch_block_reason(self, device_id: str) -> str:
        if device_id == "robot-01":
            return "unresolved_unknown_command:workflow-node-job:old-job"
        return ""

    def device_unknown_command_ids(self, device_id: str) -> List[str]:
        if device_id == "robot-01":
            return ["workflow-node-job:00000000-0000-4000-8000-000000000001"]
        return []


class FakeRegistrationHostNodeWithSystemDevice(FakeRegistrationHostNode):
    def __init__(self) -> None:
        super().__init__()
        self.resources_config = type(
            "Resources",
            (),
            {
                "dump": lambda _self: [[
                    {"id": "robot-01", "name": "Robot 01", "barcode": "ROBOT-01"},
                ]]
            },
        )()
        self.device_id = "host_node"
        self.devices_names["host_node"] = "/devices/host_node"
        self._action_value_mappings["host_node"] = {
            "transfer_resource": {"type": "UniLabJsonCommandAsync"},
        }


class FakeRegistrationDataPlane:
    def material_uuids_by_barcode(
        self, barcodes: Iterable[str]
    ) -> Dict[str, str]:
        return {barcode: str(uuid.uuid4()) for barcode in barcodes}


class FakeResponse:
    status_code = 200

    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> Dict[str, Any]:
        return self._payload


class RecordingWebSocket:
    def __init__(self) -> None:
        self.messages: List[Dict[str, Any]] = []

    async def send(self, encoded: str) -> None:
        self.messages.append(__import__("json").loads(encoded))


class RecordingSession:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.headers: Dict[str, str] = {}

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if method == "GET":
            job_uuid = url.rsplit("/", 1)[-1]
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "job_uuid": job_uuid,
                        "task_uuid": kwargs["params"]["task_uuid"],
                        "node_uuid": kwargs["params"]["node_uuid"],
                        "command_uuid": kwargs["headers"]["X-Command-UUID"],
                    },
                }
            )
        return FakeResponse({"code": 0, "data": {"uuid": str(uuid.uuid4())}})


def _settings(path: Path) -> EdgeControlSettings:
    return EdgeControlSettings(
        scheduler_address="http://scheduler:8081",
        backend_address="http://backend:8080",
        api_key="edge-secret",
        edge_key="edge-test",
        capability_revision="test-v1",
        instance_uuid="",
        state_db=str(path),
        reconnect_interval=0.01,
        request_timeout=1,
        event_retry_interval=1,
    )


def test_local_client_starts_without_api_key(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    settings = replace(
        _settings(tmp_path / "local-edge.db"),
        api_key="",
        api_key_required=False,
        device_telemetry_enabled=True,
    )
    client = EdgeControlClient(
        settings,
        data_plane=FakeDataPlane(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(client, "_run", lambda: None)

    client.start()
    assert client._thread is not None
    client._thread.join(timeout=1)
    assert not client._thread.is_alive()
    client.store.close()


def test_production_client_still_requires_api_key(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path / "production-edge.db"), api_key="")
    client = EdgeControlClient(
        settings,
        data_plane=FakeDataPlane(),  # type: ignore[arg-type]
    )
    try:
        with pytest.raises(ValueError, match="production protocol requires api_key"):
            client.start()
    finally:
        client.store.close()


def test_http_data_plane_omits_only_an_empty_api_key() -> None:
    local_plane = EdgeDataPlane(
        "http://backend:8080",
        "http://scheduler:8081",
        "",
    )
    production_plane = EdgeDataPlane(
        "http://backend:8080",
        "http://scheduler:8081",
        "edge-secret",
    )

    try:
        assert "Authorization" not in local_plane._session.headers
        assert (
            production_plane._session.headers["Authorization"]
            == "Bearer edge-secret"
        )
    finally:
        local_plane._session.close()
        production_plane._session.close()


def test_registration_reports_logical_actions_instead_of_transport_endpoint(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    host_node = FakeRegistrationHostNode()
    client = EdgeControlClient(
        _settings(path),
        data_plane=FakeRegistrationDataPlane(),  # type: ignore[arg-type]
        host_node_provider=lambda: host_node,
    )

    devices = client._registration_devices()

    assert devices[0]["actions"] == [
        {"name": "pick", "type": "UniLabJsonCommand"},
        {"name": "place", "type": "UniLabJsonCommand"},
    ]
    assert devices[0]["unknown_command_ids"] == [
        "workflow-node-job:00000000-0000-4000-8000-000000000001"
    ]
    client.store.close()


def test_registration_uses_the_shared_graph_barcode_fallback(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    host_node = FakeRegistrationHostNode()
    host_node.resources_config = FakeRegistrationResourcesWithoutBarcode()
    client = EdgeControlClient(
        _settings(path),
        data_plane=FakeRegistrationDataPlane(),  # type: ignore[arg-type]
        host_node_provider=lambda: host_node,
    )

    devices = client._registration_devices()

    assert devices[0]["barcode"] == "UNILAB-GRAPH-robot-01"
    client.store.close()


def test_registration_falls_back_to_registry_actions_during_discovery_race(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    from unilabos.registry.registry import lab_registry

    host_node = FakeRegistrationHostNode()
    host_node.resources_config = FakeRegistrationResourcesWithClass()
    host_node._action_value_mappings["robot-01"] = {}
    monkeypatch.setitem(
        lab_registry.device_type_registry,
        "community.test.robot",
        {
            "class": {
                "action_value_mappings": {
                    "_execute_driver_command": {"type": "StrSingleInput"},
                    "submit_pick_from_s06": {"type": "UniLabJsonCommand"},
                }
            }
        },
    )
    client = EdgeControlClient(
        _settings(tmp_path / "runtime.db"),
        data_plane=FakeRegistrationDataPlane(),  # type: ignore[arg-type]
        host_node_provider=lambda: host_node,
    )

    devices = client._registration_devices()

    assert devices[0]["actions"] == [
        {"name": "submit_pick_from_s06", "type": "UniLabJsonCommand"}
    ]
    client.store.close()


def test_registration_includes_host_node_as_default_system_device(
    tmp_path: Path,
) -> None:
    client = EdgeControlClient(
        _settings(tmp_path / "runtime.db"),
        data_plane=FakeRegistrationDataPlane(),  # type: ignore[arg-type]
        host_node_provider=FakeRegistrationHostNodeWithSystemDevice,
    )

    devices = client._registration_devices()

    assert [device["local_id"] for device in devices] == ["host_node", "robot-01"]
    assert devices[0]["barcode"] == "UNILAB-GRAPH-host_node"
    assert devices[0]["actions"] == [
        {"name": "transfer_resource", "type": "UniLabJsonCommandAsync"},
    ]
    client.store.close()


def test_store_persists_command_job_and_event_ack(tmp_path: Path) -> None:
    path = tmp_path / "edge-control.db"
    command_uuid = str(uuid.uuid4())
    job_uuid = str(uuid.uuid4())
    task_uuid = str(uuid.uuid4())
    node_uuid = str(uuid.uuid4())
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    tracestate = "vendor=value"
    store = EdgeControlStore(str(path))

    assert store.record_command(
        {
            "message_uuid": command_uuid,
            "sequence": 7,
            "type": "job.start",
            "payload": {"job_uuid": job_uuid},
            "traceparent": traceparent,
            "tracestate": tracestate,
        }
    )
    assert not store.record_command(
        {
            "message_uuid": command_uuid,
            "sequence": 7,
            "type": "job.start",
            "payload": {"job_uuid": job_uuid},
        }
    )
    assert store.save_job_start(
        {
            "job_uuid": job_uuid,
            "task_uuid": task_uuid,
            "node_uuid": node_uuid,
            "job_access_token": "short-token",
        },
        command_uuid,
    )
    store.mark_command_completed(command_uuid)
    event_uuid = store.enqueue_event(
        "command.ack",
        {"command_uuid": command_uuid},
        {"traceparent": traceparent, "tracestate": tracestate},
    )

    assert store.last_ack_command_sequence() == 7
    job = store.get_job(job_uuid)
    assert job is not None
    assert job.traceparent == traceparent
    assert job.tracestate == tracestate
    assert store.save_pending_outcome(
        job_uuid,
        "succeeded",
        {"suc": True},
        [],
    )
    assert store.get_pending_outcome(job_uuid) is not None
    pending_events = store.pending_events(0)
    assert [event.event_uuid for event in pending_events] == [event_uuid]
    assert pending_events[0].traceparent == traceparent
    assert pending_events[0].tracestate == tracestate
    envelope = _stored_event_envelope(pending_events[0])
    assert envelope["traceparent"] == traceparent
    assert envelope["tracestate"] == tracestate
    store.acknowledge_event(event_uuid)
    assert store.pending_events(float("inf")) == []
    instance_uuid = store.get_or_create_instance_uuid()
    store.close()

    reopened = EdgeControlStore(str(path))
    assert reopened.get_or_create_instance_uuid() == instance_uuid
    reopened_job = reopened.get_job(job_uuid)
    assert reopened_job is not None
    assert reopened_job.traceparent == traceparent
    assert reopened_job.tracestate == tracestate
    pending_outcome = reopened.get_pending_outcome(job_uuid)
    assert pending_outcome is not None
    assert pending_outcome.return_info == {"suc": True}
    reopened.close()


def test_explicit_local_reset_preserves_identity_and_clears_protocol_work(
    tmp_path: Path,
) -> None:
    """调试重建只清理协议任务，不改变持久 Edge 身份。"""

    path = tmp_path / "edge-control.db"
    store = EdgeControlStore(str(path))
    instance_uuid = store.get_or_create_instance_uuid()
    command_uuid = str(uuid.uuid4())
    job_uuid = str(uuid.uuid4())
    store.record_command(
        {
            "message_uuid": command_uuid,
            "sequence": 1,
            "type": "job.start",
            "payload": {"job_uuid": job_uuid},
        }
    )
    store.save_job_start(
        {
            "job_uuid": job_uuid,
            "task_uuid": str(uuid.uuid4()),
            "node_uuid": str(uuid.uuid4()),
            "job_access_token": "reset-token",
        },
        command_uuid,
    )
    store.enqueue_event("command.ack", {"command_uuid": command_uuid})

    store.reset_transient_state()

    assert store.get_or_create_instance_uuid() == instance_uuid
    assert store.get_job(job_uuid) is None
    assert store.command_status(command_uuid) == ""
    assert store.pending_events(float("inf")) == []
    store.close()


def test_store_resets_protocol_state_when_backend_edge_identity_changes(
    tmp_path: Path,
) -> None:
    store = EdgeControlStore(str(tmp_path / "runtime.db"))
    instance_uuid = store.get_or_create_instance_uuid()
    first_edge_uuid = str(uuid.uuid4())
    second_edge_uuid = str(uuid.uuid4())
    command_uuid = str(uuid.uuid4())
    job_uuid = str(uuid.uuid4())

    assert store.adopt_authority_edge_uuid(first_edge_uuid) is False
    assert store.record_command(
        {
            "message_uuid": command_uuid,
            "sequence": 7,
            "type": "job.start",
            "payload": {"job_uuid": job_uuid},
        }
    )
    store.mark_command_completed(command_uuid)
    assert store.save_job_start(
        {
            "job_uuid": job_uuid,
            "task_uuid": str(uuid.uuid4()),
            "node_uuid": str(uuid.uuid4()),
            "job_access_token": "test-token",
        },
        command_uuid,
    )
    store.set_job_status(job_uuid, "running")
    store.enqueue_event("job.started", {"job_uuid": job_uuid})

    assert store.adopt_authority_edge_uuid(first_edge_uuid) is False
    assert store.last_ack_command_sequence() == 7
    assert store.get_job(job_uuid) is not None

    assert store.adopt_authority_edge_uuid(second_edge_uuid) is True
    assert store.last_ack_command_sequence() == 0
    assert store.get_job(job_uuid) is None
    assert store.pending_events(float("inf")) == []
    assert store.get_or_create_instance_uuid() == instance_uuid
    assert store.get_meta("authority_edge_uuid") == second_edge_uuid
    store.close()


def test_store_migrates_existing_runtime_and_outbox_schema(tmp_path: Path) -> None:
    path = tmp_path / "old-edge-control.db"
    job_uuid = str(uuid.uuid4())
    event_uuid = str(uuid.uuid4())
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE edge_event_outbox (
            event_uuid TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_sent_at REAL,
            acked_at REAL
        );
        CREATE TABLE edge_job_runtime (
            job_uuid TEXT PRIMARY KEY,
            task_uuid TEXT NOT NULL,
            node_uuid TEXT NOT NULL,
            command_uuid TEXT NOT NULL,
            job_access_token TEXT NOT NULL,
            status TEXT NOT NULL,
            feedback_sequence INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL
        );
        """
    )
    connection.execute(
        """
        INSERT INTO edge_event_outbox(
            event_uuid, type, payload_json, created_at
        ) VALUES (?, 'command.ack', '{}', '2026-08-02T00:00:00Z')
        """,
        (event_uuid,),
    )
    connection.execute(
        """
        INSERT INTO edge_job_runtime(
            job_uuid, task_uuid, node_uuid, command_uuid,
            job_access_token, status, updated_at
        ) VALUES (?, ?, ?, ?, 'token', 'received', 1)
        """,
        (job_uuid, str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())),
    )
    connection.commit()
    connection.close()

    store = EdgeControlStore(str(path))
    job = store.get_job(job_uuid)
    events = store.pending_events(float("inf"))
    assert job is not None
    assert job.traceparent == ""
    assert [event.event_uuid for event in events] == [event_uuid]
    assert events[0].traceparent == ""
    store.close()


def test_store_discards_legacy_pong_events_when_reopened(tmp_path: Path) -> None:
    path = tmp_path / "edge-control.db"
    store = EdgeControlStore(str(path))
    store.enqueue_event("pong", {"ping_uuid": str(uuid.uuid4())})
    store.close()

    reopened = EdgeControlStore(str(path))

    assert reopened.pending_events(float("inf")) == []
    reopened.close()


def test_ping_pong_is_sent_on_current_connection_without_outbox(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "runtime.db"
        store = EdgeControlStore(str(path))
        client = EdgeControlClient(_settings(path), store=store)
        websocket = RecordingWebSocket()
        client._websocket = websocket
        ping_uuid = str(uuid.uuid4())

        await client._handle_envelope(
            {
                "protocol_version": 1,
                "message_uuid": str(uuid.uuid4()),
                "sequence": 0,
                "type": "ping",
                "sent_at": "2026-08-02T00:00:00.000000Z",
                "payload": {"ping_uuid": ping_uuid},
            }
        )

        assert len(websocket.messages) == 1
        assert websocket.messages[0]["type"] == "pong"
        assert websocket.messages[0]["payload"] == {"ping_uuid": ping_uuid}
        assert store.pending_events(float("inf")) == []
        store.close()

    asyncio.run(scenario())


def test_material_changed_is_acknowledged_without_dropping_connection(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = EdgeControlStore(str(tmp_path / "runtime.db"))
        client = EdgeControlClient(_settings(tmp_path / "runtime.db"), store=store)
        command_uuid = str(uuid.uuid4())
        await client._handle_envelope(
            {
                "protocol_version": 1,
                "message_uuid": command_uuid,
                "sequence": 9,
                "type": "material.changed",
                "sent_at": "2026-08-02T00:00:00.000000Z",
                "payload": {
                    "device_material_uuid": str(uuid.uuid4()),
                    "material_uuid": str(uuid.uuid4()),
                    "action": "update",
                },
            }
        )

        assert store.command_status(command_uuid) == "completed"
        assert store.last_ack_command_sequence() == 9
        events = store.pending_events(float("inf"))
        assert [event.event_type for event in events] == ["command.ack"]
        assert events[0].payload == {"command_uuid": command_uuid}
        store.close()

    asyncio.run(scenario())


def test_unknown_resolution_is_committed_locally_before_edge_ack(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "runtime.db"
        store = EdgeControlStore(str(path))
        host_node = FakeHostNode()
        client = EdgeControlClient(
            _settings(path), store=store, host_node_provider=lambda: host_node
        )
        command_uuid = str(uuid.uuid4())
        job_uuid = str(uuid.uuid4())
        await client._handle_envelope(
            {
                "protocol_version": 1,
                "message_uuid": command_uuid,
                "sequence": 10,
                "type": "job.resolve_unknown",
                "sent_at": "2026-08-02T00:00:00.000000Z",
                "payload": {
                    "job_uuid": job_uuid,
                    "local_device_id": "robot-01",
                    "device_command_id": f"workflow-node-job:{job_uuid}",
                    "resolution": "canceled",
                    "reason": "操作员确认 PLC 已复位且设备空闲",
                },
            }
        )

        assert host_node.unknown_resolutions == [
            {
                "device_id": "robot-01",
                "device_command_id": f"workflow-node-job:{job_uuid}",
                "resolution_command_uuid": command_uuid,
                "reason": "操作员确认 PLC 已复位且设备空闲",
            }
        ]
        assert store.command_status(command_uuid) == "completed"
        events = store.pending_events(float("inf"))
        assert [event.event_type for event in events] == [
            "job.unknown_resolution_committed",
            "command.ack",
        ]
        assert events[0].payload == {
            "job_uuid": job_uuid,
            "command_uuid": command_uuid,
            "device_command_id": f"workflow-node-job:{job_uuid}",
            "resolution": "canceled",
            "previous_state": "UNKNOWN",
            "current_state": "CANCELED",
            "dispatch_block_reason": "",
        }
        await client._handle_envelope(
            {
                "protocol_version": 1,
                "message_uuid": command_uuid,
                "sequence": 10,
                "type": "job.resolve_unknown",
                "sent_at": "2026-08-02T00:00:00.000000Z",
                "payload": {
                    "job_uuid": job_uuid,
                    "local_device_id": "robot-01",
                    "device_command_id": f"workflow-node-job:{job_uuid}",
                    "resolution": "canceled",
                    "reason": "操作员确认 PLC 已复位且设备空闲",
                },
            }
        )
        assert len(store.pending_events(float("inf"))) == 2
        store.close()

    asyncio.run(scenario())


def test_http_data_plane_uses_three_uuid_identity() -> None:
    job = StoredJob(
        job_uuid=str(uuid.uuid4()),
        task_uuid=str(uuid.uuid4()),
        node_uuid=str(uuid.uuid4()),
        command_uuid=str(uuid.uuid4()),
        job_access_token="short-token",
        status="received",
        feedback_sequence=0,
    )
    plane = EdgeDataPlane(
        "http://backend:8080/api/v1",
        "http://scheduler:8081",
        "edge-secret",
    )
    session = RecordingSession()
    plane._session = session  # type: ignore[assignment]

    plane.fetch_job(job)
    plane.commit_outcome(job, "succeeded", {"suc": True}, [])

    fetch = session.calls[0]
    assert fetch["params"] == {
        "task_uuid": job.task_uuid,
        "node_uuid": job.node_uuid,
    }
    assert fetch["headers"] == {
        "X-Command-UUID": job.command_uuid,
        "X-Job-Token": job.job_access_token,
    }
    outcome = session.calls[1]
    assert outcome["json"]["task_uuid"] == job.task_uuid
    assert outcome["json"]["node_uuid"] == job.node_uuid
    assert "unknown_command_ids" not in outcome["json"]
    assert outcome["headers"]["Idempotency-Key"] == f"{job.job_uuid}:outcome:v1"


def test_material_identity_lookup_includes_child_resources() -> None:
    """生产资源身份解析必须显式包含有父级的物料。

    参数：无。返回：无；断言 Edge 查询 Backend 时携带 ``with_children=true``。
    """

    class MaterialSession:
        def __init__(self) -> None:
            self.headers: Dict[str, str] = {}
            self.params: Dict[str, Any] = {}

        def request(self, _method: str, _url: str, **kwargs: Any) -> FakeResponse:
            self.params = kwargs["params"]
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "items": [{"barcode": "CHILD-01", "uuid": "material-01"}],
                        "total": 1,
                    },
                }
            )

    plane = EdgeDataPlane(
        "http://backend:8080",
        "http://scheduler:8081",
        "edge-secret",
    )
    session = MaterialSession()
    plane._session = session  # type: ignore[assignment]

    resolved = plane.material_uuids_by_barcode(["CHILD-01"])

    assert resolved == {"CHILD-01": "material-01"}
    assert session.params["with_children"] == "true"


def test_http_data_plane_injects_w3c_trace_headers(monkeypatch) -> None:
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

    def inject(carrier: Dict[str, Any]) -> Dict[str, Any]:
        carrier["traceparent"] = traceparent
        carrier["tracestate"] = "vendor=value"
        return carrier

    monkeypatch.setattr(
        "unilabos.app.edge_control.http.inject_trace_context", inject
    )
    job = StoredJob(
        job_uuid=str(uuid.uuid4()),
        task_uuid=str(uuid.uuid4()),
        node_uuid=str(uuid.uuid4()),
        command_uuid=str(uuid.uuid4()),
        job_access_token="short-token",
        status="received",
        feedback_sequence=0,
    )
    plane = EdgeDataPlane(
        "http://backend:8080",
        "http://scheduler:8081",
        "edge-secret",
    )
    session = RecordingSession()
    plane._session = session  # type: ignore[assignment]

    plane.fetch_job(job)

    assert session.calls[0]["headers"]["traceparent"] == traceparent
    assert session.calls[0]["headers"]["tracestate"] == "vendor=value"


def test_job_start_fetches_http_payload_and_outcome_precedes_notification(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        data_plane = FakeDataPlane()
        host_node = FakeHostNode()
        store = EdgeControlStore(str(tmp_path / "runtime.db"))
        client = EdgeControlClient(
            _settings(tmp_path / "runtime.db"),
            store=store,
            data_plane=data_plane,  # type: ignore[arg-type]
            host_node_provider=lambda: host_node,
        )
        client._connected.set()
        job_uuid = str(uuid.uuid4())
        task_uuid = str(uuid.uuid4())
        node_uuid = str(uuid.uuid4())
        command_uuid = str(uuid.uuid4())
        traceparent = (
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        )
        await client._handle_envelope(
            {
                "protocol_version": 1,
                "message_uuid": command_uuid,
                "sequence": 11,
                "type": "job.start",
                "sent_at": "2026-08-02T00:00:00.000000Z",
                "traceparent": traceparent,
                "tracestate": "vendor=value",
                "payload": {
                    "job_uuid": job_uuid,
                    "task_uuid": task_uuid,
                    "node_uuid": node_uuid,
                    "executor_kind": "device_action",
                    "job_access_token": "short-token",
                },
            }
        )
        if client._tasks:
            await asyncio.gather(*list(client._tasks))

        assert len(data_plane.fetched_jobs) == 1
        assert len(host_node.started) == 1
        context = host_node.started[0]["item"]
        assert context.task_id == task_uuid
        assert context.node_id == node_uuid
        assert host_node.started[0]["action_kwargs"] == {"temperature": 37}
        assert context.trace_context["traceparent"] == traceparent

        client.publish_job_started(context)
        block_reason = f"unresolved_unknown_command:workflow-node-job:{job_uuid}"
        host_node.dispatch_block_reasons["heater-01"] = block_reason
        await client._commit_terminal_status(
            job_uuid,
            "failed",
            {"actual_temperature": 37},
            {"suc": False, "return_value": {"state": "UNKNOWN"}},
            "heater-01",
        )

        assert len(data_plane.outcomes) == 1
        assert data_plane.outcomes[0]["job"].task_uuid == task_uuid
        assert data_plane.outcomes[0]["outcome"] == "failed"
        assert data_plane.outcomes[0]["unknown_command_ids"] == [
            f"workflow-node-job:{job_uuid}"
        ]
        events = store.pending_events(float("inf"))
        assert [event.event_type for event in events] == [
            "command.ack",
            "job.started",
            "job.outcome_committed",
        ]
        assert all(event.traceparent == traceparent for event in events)
        assert all(event.tracestate == "vendor=value" for event in events)
        assert store.get_job(job_uuid).status == "outcome_committed"  # type: ignore[union-attr]
        assert store.get_pending_outcome(job_uuid) is None
        store.close()

    asyncio.run(scenario())


def test_duplicate_job_start_envelope_dispatches_goal_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        data_plane = FakeDataPlane()
        host_node = FakeHostNode()
        store = EdgeControlStore(str(tmp_path / "runtime.db"))
        client = EdgeControlClient(
            _settings(tmp_path / "runtime.db"),
            store=store,
            data_plane=data_plane,  # type: ignore[arg-type]
            host_node_provider=lambda: host_node,
        )
        client._connected.set()
        envelope = {
            "protocol_version": 1,
            "message_uuid": str(uuid.uuid4()),
            "sequence": 12,
            "type": "job.start",
            "sent_at": "2026-08-21T03:38:50.075000Z",
            "payload": {
                "job_uuid": str(uuid.uuid4()),
                "task_uuid": str(uuid.uuid4()),
                "node_uuid": str(uuid.uuid4()),
                "executor_kind": "device_action",
                "job_access_token": "short-token",
            },
        }

        await client._handle_envelope(envelope)
        await client._handle_envelope(envelope)
        if client._tasks:
            await asyncio.gather(*list(client._tasks))

        assert len(data_plane.fetched_jobs) == 1
        assert len(host_node.started) == 1
        assert [event.event_type for event in store.pending_events(float("inf"))] == [
            "command.ack"
        ]
        store.close()

    asyncio.run(scenario())


def test_distinct_job_start_envelopes_still_dispatch_in_parallel(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        data_plane = FakeDataPlane()
        host_node = FakeHostNode()
        store = EdgeControlStore(str(tmp_path / "runtime.db"))
        client = EdgeControlClient(
            _settings(tmp_path / "runtime.db"),
            store=store,
            data_plane=data_plane,  # type: ignore[arg-type]
            host_node_provider=lambda: host_node,
        )
        client._connected.set()
        job_uuids = [str(uuid.uuid4()), str(uuid.uuid4())]
        for sequence, job_uuid in enumerate(job_uuids, start=20):
            await client._handle_envelope(
                {
                    "protocol_version": 1,
                    "message_uuid": str(uuid.uuid4()),
                    "sequence": sequence,
                    "type": "job.start",
                    "sent_at": "2026-08-21T03:30:23.000000Z",
                    "payload": {
                        "job_uuid": job_uuid,
                        "task_uuid": str(uuid.uuid4()),
                        "node_uuid": str(uuid.uuid4()),
                        "executor_kind": "device_action",
                        "job_access_token": "short-token",
                    },
                }
            )
        if client._tasks:
            await asyncio.gather(*list(client._tasks))

        assert {job.job_uuid for job in data_plane.fetched_jobs} == set(job_uuids)
        assert {entry["item"].job_id for entry in host_node.started} == set(job_uuids)
        store.close()

    asyncio.run(scenario())


def test_terminal_job_token_rejection_retires_pending_outcome(
    tmp_path: Path,
) -> None:
    class RevokedJobDataPlane(FakeDataPlane):
        def commit_outcome(
            self,
            job: StoredJob,
            outcome: str,
            return_info: Dict[str, Any],
            error_info: List[Dict[str, Any]],
            unknown_command_ids: List[str] | None = None,
        ) -> Dict[str, Any]:
            self.outcomes.append({"job": job, "outcome": outcome})
            raise EdgeProtocolHTTPError(
                "Job token was revoked after manual reconciliation",
                business_code=BACKEND_UNAUTHORIZED_BUSINESS_CODE,
            )

    async def scenario() -> None:
        path = tmp_path / "runtime.db"
        store = EdgeControlStore(str(path))
        job_uuid = str(uuid.uuid4())
        task_uuid = str(uuid.uuid4())
        node_uuid = str(uuid.uuid4())
        command_uuid = str(uuid.uuid4())
        store.save_job_start(
            {
                "job_uuid": job_uuid,
                "task_uuid": task_uuid,
                "node_uuid": node_uuid,
                "job_access_token": "revoked-token",
            },
            command_uuid,
        )
        store.save_pending_outcome(job_uuid, "succeeded", {"suc": True}, [])
        data_plane = RevokedJobDataPlane()
        client = EdgeControlClient(
            _settings(path),
            store=store,
            data_plane=data_plane,  # type: ignore[arg-type]
        )

        await asyncio.wait_for(client._commit_pending_outcome(job_uuid), timeout=1)

        assert len(data_plane.outcomes) == 1
        assert store.get_pending_outcome(job_uuid) is None
        stored_job = store.get_job(job_uuid)
        assert stored_job is not None
        assert stored_job.status == "outcome_retired"
        store.close()

    asyncio.run(scenario())
