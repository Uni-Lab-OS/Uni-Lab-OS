"""M1R InventoryService cross-DB replay public seam RED。

测试只通过 InventoryService 的 closed command、outbox/ACK 和 Reservation
proof 接口观察 durable coordination，不访问 Store、SQLite 或私有 allocator。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import unilabos.app.scheduler.inventory as inventory_api

MATERIAL_UUID = "5aa00000-0000-4000-8000-000000000202"
RESOURCE_TEMPLATE_UUID = "2bb00000-0000-4000-8000-000000000202"
WORKFLOW_TASK_UUID = "90000000-0000-4000-8000-000000000202"
MATERIAL_SOURCE_NODE_UUID = "a0000000-0000-4000-8000-000000000202"
ADMISSION_COMMAND_UUID = "80000000-0000-4000-8000-000000000202"
RELEASE_COMMAND_UUID = "81000000-0000-4000-8000-000000000202"
WORKFLOW_SNAPSHOT_FINGERPRINT = "2" * 64


def _resource_templates() -> dict[str, inventory_api.ResourceTemplateIdentity]:
    identity = inventory_api.ResourceTemplateIdentity(
        uuid=RESOURCE_TEMPLATE_UUID,
        material_class="SampleTube",
    )
    return {identity.uuid: identity}


def _admission_command() -> inventory_api.TaskMaterialAdmissionCommand:
    source = inventory_api.TaskMaterialAdmissionSource(
        material_source_node_uuid=MATERIAL_SOURCE_NODE_UUID,
        mode="existing",
        resource_template_uuid=RESOURCE_TEMPLATE_UUID,
        mount={"uuid": MATERIAL_UUID},
        material_uuid=MATERIAL_UUID,
        site_uuid=None,
        candidate_site_uuids=(),
        flow_role="primary_sample",
    )
    return inventory_api.TaskMaterialAdmissionCommand(
        schema_version=1,
        command_uuid=ADMISSION_COMMAND_UUID,
        idempotency_key="m1r-replay-admit-202",
        workflow_task_uuid=WORKFLOW_TASK_UUID,
        workflow_snapshot_fingerprint=WORKFLOW_SNAPSHOT_FINGERPRINT,
        sources=(source,),
    )


def _release_command() -> inventory_api.TaskMaterialReleaseCommand:
    return inventory_api.TaskMaterialReleaseCommand(
        schema_version=1,
        command_uuid=RELEASE_COMMAND_UUID,
        idempotency_key="m1r-replay-release-202",
        workflow_task_uuid=WORKFLOW_TASK_UUID,
        reason="workflow_task_terminal",
    )


def test_outbox_ack_and_active_reservation_proof_survive_reopen(
    tmp_path: Path,
) -> None:
    inventory = inventory_api.InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    try:
        inventory.create_material(
            material_uuid=MATERIAL_UUID,
            resource_template_uuid=RESOURCE_TEMPLATE_UUID,
            barcode="M1R-SAMPLE-202",
            name="M1R replay sample 202",
        )
        admitted = inventory.admit_task(_admission_command())
        assert admitted.status == "admitted"
        assert admitted.reservation_uuid is not None

        events = inventory.read_outbox(after_sequence=0, limit=100)

        assert isinstance(events, tuple)
        assert [event.sequence for event in events] == sorted(
            event.sequence for event in events
        )
        admission_event = next(
            event for event in events if event.sequence == admitted.outbox_sequence
        )
        assert isinstance(admission_event, inventory_api.InventoryEvent)
        assert admission_event.event_type == "material_reservation.admitted"
        assert admission_event.causation_id == ADMISSION_COMMAND_UUID
        assert admission_event.payload["workflow_task_uuid"] == WORKFLOW_TASK_UUID
        assert admission_event.payload["material_uuids"] == [MATERIAL_UUID]
        with pytest.raises(FrozenInstanceError):
            admission_event.sequence = 0

        assert (
            inventory.read_outbox(
                after_sequence=admitted.outbox_sequence,
                limit=100,
            )
            == ()
        )
        assert inventory.get_acknowledged_sequence() == 0
        inventory.acknowledge(admitted.outbox_sequence)
        inventory.acknowledge(admitted.outbox_sequence)
        inventory.acknowledge(admitted.outbox_sequence - 1)
        assert inventory.get_acknowledged_sequence() == admitted.outbox_sequence
        with pytest.raises(inventory_api.MaterialConflict) as future_ack:
            inventory.acknowledge(admitted.outbox_sequence + 1)
        assert future_ack.value.code == "conflict"
        assert inventory.get_acknowledged_sequence() == admitted.outbox_sequence
        assert inventory.has_active_task_reservation(
            WORKFLOW_TASK_UUID,
            admitted.reservation_uuid,
        )
    finally:
        inventory.close()

    reopened = inventory_api.InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    try:
        assert reopened.get_acknowledged_sequence() == admitted.outbox_sequence
        assert reopened.has_active_task_reservation(
            WORKFLOW_TASK_UUID,
            admitted.reservation_uuid,
        )

        released = reopened.release_task(_release_command())

        assert released.status == "released"
        assert released.reservation_uuid == admitted.reservation_uuid
        assert not reopened.has_active_task_reservation(
            WORKFLOW_TASK_UUID,
            admitted.reservation_uuid,
        )
    finally:
        reopened.close()

    final = inventory_api.InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    try:
        assert not final.has_active_task_reservation(
            WORKFLOW_TASK_UUID,
            admitted.reservation_uuid,
        )
    finally:
        final.close()
