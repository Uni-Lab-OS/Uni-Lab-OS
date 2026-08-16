"""设备遥测投影（DeviceTelemetryProjection）的本地协议纵切测试。"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.edge_control.device_telemetry import (
    DEVICE_PROPERTIES,
    DeviceTelemetryError,
    DeviceTelemetryHub,
)
from unilabos.app.edge_control.device_telemetry_api import (
    create_device_telemetry_router,
)
from unilabos.app.edge_control.local_authority import (
    LocalEdgeAuthorityStore,
    LocalEdgeControlAuthority,
)
from unilabos.app.edge_control.store import EdgeControlStore
from unilabos.app.edge_control.telemetry_publisher import DeviceTelemetryPublisher


def _properties_payload(
    *,
    sequence: int = 1,
    boot_id: str = "edge-boot-1",
    value: float = 21.5,
) -> dict[str, Any]:
    return {
        "local_device_id": "robot-01",
        "boot_id": boot_id,
        "samples": [
            {
                "sequence": sequence,
                "observed_at": "2026-08-15T12:00:00.000000Z",
                "properties": {"temperature": value},
                "property_observed_at": {
                    "temperature": "2026-08-15T12:00:00.000000Z"
                },
            }
        ],
    }


def _registered_authority(
    path: Path,
) -> tuple[LocalEdgeControlAuthority, str]:
    authority = LocalEdgeControlAuthority(
        LocalEdgeAuthorityStore(path),
        api_key="managed-local-secret",
    )
    material_uuid = str(uuid.uuid4())
    authority.store.register_session(
        {
            "edge_key": "workspace-edge",
            "instance_uuid": str(uuid.uuid4()),
            "devices": [
                {
                    "local_id": "robot-01",
                    "name": "Robot",
                    "material_uuid": material_uuid,
                    "actions": [],
                }
            ],
        }
    )
    return authority, material_uuid


def test_http_commit_is_strict_idempotent_and_backend_shaped(
    tmp_path: Path,
) -> None:
    authority, material_uuid = _registered_authority(tmp_path / "authority.db")
    application = FastAPI()
    application.include_router(create_device_telemetry_router(authority))
    client = TestClient(application)
    url = f"/api/v1/edge/devices/{material_uuid}/telemetry/properties"
    headers = {"Authorization": "Bearer managed-local-secret"}
    try:
        first = client.post(url, headers=headers, json=_properties_payload())
        duplicate = client.post(url, headers=headers, json=_properties_payload())
        conflict = client.post(
            url,
            headers=headers,
            json=_properties_payload(value=22.0),
        )
        invalid = client.post(url, headers=headers, json=None)
        denied = client.post(url, json=_properties_payload())

        assert first.status_code == 201
        assert first.json()["code"] == 0
        assert first.json()["data"]["created"] == 1
        assert duplicate.status_code == 200
        assert duplicate.json()["data"]["created"] == 0
        assert conflict.status_code == 409
        assert conflict.json()["code"] == 7001
        assert invalid.status_code == 200
        assert invalid.json()["code"] == 1000
        assert denied.status_code == 401
        assert denied.json() == {
            "code": 1001,
            "error": {"msg": "Unauthorized"},
        }
    finally:
        authority.stop()


def test_notification_promotes_in_process_latest(
    tmp_path: Path,
) -> None:
    path = tmp_path / "authority.db"
    authority, material_uuid = _registered_authority(path)
    commit = authority.telemetry.ingest_properties(
        material_uuid,
        _properties_payload(),
    )
    subscription, snapshot = authority.telemetry.subscribe()
    assert snapshot == []
    event_uuid = str(uuid.uuid4())
    try:
        changed_latest = authority.accept_telemetry_event(
            event_uuid,
            "2026-08-15T12:00:01.000000Z",
            commit.notification_payload(),
        )
        changed = subscription.drain()
        assert changed_latest is True
        assert len(changed) == 1
        assert changed[0]["data"]["properties"] == {"temperature": 21.5}
        assert "properties" not in commit.notification_payload()
    finally:
        authority.telemetry.unsubscribe(subscription)
        authority.stop()


def test_joint_state_stale_transition_is_coalesced() -> None:
    material_uuid = str(uuid.uuid4())
    hub = DeviceTelemetryHub()
    commit = hub.ingest_joint_states(
        material_uuid,
        {
            "local_device_id": "robot-01",
            "boot_id": "edge-boot-1",
            "samples": [
                {
                    "sequence": 1,
                    "observed_at": "2026-08-15T12:00:00.000000Z",
                    "stale_after_s": 1.0,
                    "topology_digest": "a" * 64,
                    "joint_states": {"joint_1": 0.25},
                }
            ],
        },
    )
    subscription, _snapshot = hub.subscribe()
    try:
        assert hub.notify(commit.notification_payload()) is True
        assert hub.expire(now_epoch_s=1786795202.0) == 1
        events = subscription.drain()
        assert len(events) == 1
        assert events[0]["stale"] is True
        assert hub.expire(now_epoch_s=1786795203.0) == 0
    finally:
        hub.unsubscribe(subscription)


def test_duplicate_event_uuid_cannot_change_identity(tmp_path: Path) -> None:
    authority, material_uuid = _registered_authority(tmp_path / "authority.db")
    event_uuid = str(uuid.uuid4())
    commit = authority.telemetry.ingest_properties(
        material_uuid,
        _properties_payload(),
    )
    try:
        authority.accept_telemetry_event(
            event_uuid,
            "2026-08-15T12:00:01.000000Z",
            commit.notification_payload(),
        )
        with pytest.raises(ValueError, match="identity changed"):
            authority.accept_telemetry_event(
                event_uuid,
                "2026-08-15T12:00:02.000000Z",
                commit.notification_payload(),
            )
    finally:
        authority.stop()


class _TelemetryDataPlane:
    def __init__(self) -> None:
        self.hub = DeviceTelemetryHub()
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def commit_device_properties(
        self,
        material_uuid: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append((DEVICE_PROPERTIES, material_uuid, payload))
        return self.hub.ingest_properties(material_uuid, payload).as_dict()

    def commit_joint_states(
        self,
        material_uuid: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(("joint_state", material_uuid, payload))
        return self.hub.ingest_joint_states(material_uuid, payload).as_dict()


def test_edge_publisher_persists_http_then_short_notification(
    tmp_path: Path,
) -> None:
    store = EdgeControlStore(str(tmp_path / "edge.db"))
    data_plane = _TelemetryDataPlane()
    publisher = DeviceTelemetryPublisher(
        store,
        data_plane,  # type: ignore[arg-type]
        enabled=True,
        retry_interval=0.1,
    )
    material_uuid = str(uuid.uuid4())
    publisher.bind_devices(
        [{"local_id": "robot-01", "material_uuid": material_uuid}]
    )
    try:
        assert publisher.submit_properties(
            "robot-01",
            {"temperature": 21.5, "ready": True},
            {"temperature": 1786795200.0, "ready": 1786795200.0},
        )
        assert asyncio.run(publisher.drain()) == 1
        assert store.pending_telemetry() == []
        events = store.pending_events(float("inf"))
        assert len(events) == 1
        assert events[0].event_type == "device.telemetry_committed"
        assert set(events[0].payload) == {
            "material_uuid",
            "local_device_id",
            "telemetry_type",
            "boot_id",
            "through_sequence",
            "accepted_ref",
        }
        assert "properties" not in events[0].payload
        assert len(data_plane.calls) == 1
    finally:
        store.close()


def test_joint_publisher_preserves_projector_frame_identity(tmp_path: Path) -> None:
    """关节帧的运行代际和序列必须由投影器贯穿到 HTTP 与短通知。"""

    store = EdgeControlStore(str(tmp_path / "edge.db"))
    data_plane = _TelemetryDataPlane()
    publisher = DeviceTelemetryPublisher(
        store,
        data_plane,  # type: ignore[arg-type]
        enabled=True,
        retry_interval=0.1,
    )
    material_uuid = str(uuid.uuid4())
    boot_id = str(uuid.uuid4())
    publisher.bind_devices(
        [{"local_id": "robot-01", "material_uuid": material_uuid}]
    )
    try:
        assert publisher.submit_joint_states(
            "robot-01",
            {"robot-01/joint_1": 0.25},
            boot_id=boot_id,
            sequence=17,
            observed_epoch_s=1786795200.0,
            topology_digest="a" * 64,
            stale_after_s=1.0,
        )
        assert asyncio.run(publisher.drain()) == 1
        _kind, _material_uuid, payload = data_plane.calls[0]
        assert payload["boot_id"] == boot_id
        assert payload["samples"][0]["sequence"] == 17
        event = store.pending_events(float("inf"))[0]
        assert event.payload["boot_id"] == boot_id
        assert event.payload["through_sequence"] == 17
    finally:
        store.close()


def test_formal_backend_gate_does_not_persist_or_send(tmp_path: Path) -> None:
    store = EdgeControlStore(str(tmp_path / "edge.db"))
    data_plane = _TelemetryDataPlane()
    publisher = DeviceTelemetryPublisher(
        store,
        data_plane,  # type: ignore[arg-type]
        enabled=False,
        retry_interval=0.1,
    )
    publisher.bind_devices(
        [{"local_id": "robot-01", "material_uuid": str(uuid.uuid4())}]
    )
    try:
        assert not publisher.submit_properties(
            "robot-01",
            {"temperature": 21.5},
            {"temperature": 1786795200.0},
        )
        assert asyncio.run(publisher.drain()) == 0
        assert store.pending_telemetry() == []
        assert data_plane.calls == []
    finally:
        store.close()
