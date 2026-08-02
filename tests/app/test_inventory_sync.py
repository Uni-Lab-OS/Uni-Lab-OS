"""Canonical outbox/cursor/snapshot replay regressions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unilabos.app.scheduler.inventory import (
    InventoryService,
    OutboxWorker,
    ResourceTemplateIdentity,
    build_snapshot,
)
from unilabos.app.scheduler.inventory.sync import CloudProjectionReference

TEMPLATE_UUID = "20000000-0000-4000-8000-000000000701"


def _open(path: Path) -> InventoryService:
    identity = ResourceTemplateIdentity(TEMPLATE_UUID, "SampleTube")
    return InventoryService.open(
        working_dir=path,
        resource_templates={identity.uuid: identity},
    )


def _create_material(inventory: InventoryService, index: int) -> str:
    material_uuid = f"50000000-0000-4000-8000-{index:012d}"
    inventory.create_material(
        material_uuid=material_uuid,
        resource_template_uuid=TEMPLATE_UUID,
        barcode=f"SYNC-{index}",
        name=f"Sync material {index}",
    )
    return material_uuid


def test_failed_send_keeps_durable_events_for_retry(tmp_path: Path) -> None:
    inventory = _open(tmp_path)
    _create_material(inventory, 701)

    def fail(_events: list[dict[str, Any]]) -> int:
        raise ConnectionError("offline")

    try:
        worker = OutboxWorker(inventory, fail)
        with pytest.raises(ConnectionError):
            worker.flush_once()
        assert inventory.get_acknowledged_sequence(consumer="cloud") == 0
        assert worker.backlog() == 1
        assert len(inventory.read_outbox(after_sequence=0, limit=100)) == 1
    finally:
        inventory.close()


def test_partial_ack_replays_only_unacknowledged_suffix(tmp_path: Path) -> None:
    inventory = _open(tmp_path)
    for index in (702, 703, 704):
        _create_material(inventory, index)
    batches: list[list[int]] = []

    def sender(events: list[dict[str, Any]]) -> int:
        sequences = [event["sequence"] for event in events]
        batches.append(sequences)
        return sequences[0] if len(batches) == 1 else sequences[-1]

    try:
        worker = OutboxWorker(inventory, sender)
        assert worker.flush_once() == 1
        assert inventory.get_acknowledged_sequence(consumer="cloud") == 1
        assert worker.flush_once() == 2
        assert batches == [[1, 2, 3], [2, 3]]
        assert worker.backlog() == 0
    finally:
        inventory.close()


def test_snapshot_and_event_replay_converge_on_versions(tmp_path: Path) -> None:
    inventory = _open(tmp_path)
    material_uuid = _create_material(inventory, 705)
    try:
        snapshot = build_snapshot(inventory)
        events = inventory.read_outbox(after_sequence=0, limit=100)
        event_projection = CloudProjectionReference()
        event_projection.ingest(
            [
                {
                    "event_id": event.event_id,
                    "edge_id": event.edge_id,
                    "sequence": event.sequence,
                    "aggregate_type": event.aggregate_type,
                    "aggregate_id": event.aggregate_id,
                    "aggregate_version": event.aggregate_version,
                    "event_type": event.event_type,
                    "payload": event.payload,
                }
                for event in events
            ]
        )
        snapshot_projection = CloudProjectionReference()
        snapshot_projection.load_snapshot(snapshot)

        key = f"material:{material_uuid}"
        assert event_projection.versions[key] == snapshot_projection.versions[key] == 1
        assert snapshot_projection.acked_sequence == snapshot["snapshot_sequence"]
    finally:
        inventory.close()


def test_reopen_preserves_cursor_and_snapshot_sequence(tmp_path: Path) -> None:
    inventory = _open(tmp_path)
    _create_material(inventory, 706)
    sent: list[dict[str, Any]] = []

    def sender(events: list[dict[str, Any]]) -> int:
        sent.extend(events)
        return events[-1]["sequence"]

    try:
        assert OutboxWorker(inventory, sender).flush_all() == 1
        sequence = inventory.get_acknowledged_sequence(consumer="cloud")
    finally:
        inventory.close()

    reopened = _open(tmp_path)
    try:
        assert reopened.get_acknowledged_sequence(consumer="cloud") == sequence
        assert build_snapshot(reopened)["snapshot_sequence"] == sequence
        assert reopened.outbox_status()["backlog"] == 0
    finally:
        reopened.close()


def test_scheduler_ack_does_not_skip_cloud_delivery(tmp_path: Path) -> None:
    inventory = _open(tmp_path)
    _create_material(inventory, 707)
    _create_material(inventory, 708)
    sent: list[int] = []

    def sender(events: list[dict[str, Any]]) -> int:
        sent.extend(event["sequence"] for event in events)
        return events[-1]["sequence"]

    try:
        inventory.acknowledge(2, consumer="scheduler")

        assert OutboxWorker(inventory, sender).flush_once() == 2
        assert sent == [1, 2]
        assert inventory.get_acknowledged_sequence(consumer="scheduler") == 2
        assert inventory.get_acknowledged_sequence(consumer="cloud") == 2
    finally:
        inventory.close()
