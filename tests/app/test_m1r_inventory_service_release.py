"""M1R InventoryService Task release 最小纵向合同。

测试只通过 public ``InventoryService`` admission/release command 观察
Reservation 释放、重放和重开语义，不访问 Store、SQLite 或 Scheduler。
"""

from __future__ import annotations

from pathlib import Path

import unilabos.app.scheduler.inventory as inventory_api

MATERIAL_UUID = "5aa00000-0000-4000-8000-000000000105"
MOUNT_UUID = "5aa00000-0000-4000-8000-000000000106"
SITE_UUID = "6aa00000-0000-4000-8000-000000000105"
RESOURCE_TEMPLATE_UUID = "2bb00000-0000-4000-8000-000000000105"
WORKFLOW_TASK_UUID = "90000000-0000-4000-8000-000000000105"
MATERIAL_SOURCE_NODE_UUID = "a0000000-0000-4000-8000-000000000105"
ADMISSION_COMMAND_UUID = "80000000-0000-4000-8000-000000000105"
RELEASE_COMMAND_UUID = "81000000-0000-4000-8000-000000000105"
READMISSION_COMMAND_UUID = "82000000-0000-4000-8000-000000000105"
WORKFLOW_SNAPSHOT_FINGERPRINT = "a" * 64


def _resource_templates() -> dict[str, inventory_api.ResourceTemplateIdentity]:
    identity = inventory_api.ResourceTemplateIdentity(
        uuid=RESOURCE_TEMPLATE_UUID,
        material_class="SampleTube",
    )
    return {identity.uuid: identity}


def _admission_command(
    command_uuid: str,
    idempotency_key: str,
) -> inventory_api.TaskMaterialAdmissionCommand:
    source = inventory_api.TaskMaterialAdmissionSource(
        material_source_node_uuid=MATERIAL_SOURCE_NODE_UUID,
        mode="existing",
        resource_template_uuid=RESOURCE_TEMPLATE_UUID,
        mount={"uuid": MOUNT_UUID},
        material_uuid=MATERIAL_UUID,
        site_uuid=SITE_UUID,
        candidate_site_uuids=(),
        flow_role="primary_sample",
    )
    return inventory_api.TaskMaterialAdmissionCommand(
        schema_version=1,
        command_uuid=command_uuid,
        idempotency_key=idempotency_key,
        workflow_task_uuid=WORKFLOW_TASK_UUID,
        workflow_snapshot_fingerprint=WORKFLOW_SNAPSHOT_FINGERPRINT,
        sources=(source,),
    )


def test_release_replays_survives_reopen_and_allows_readmission(
    tmp_path: Path,
) -> None:
    inventory = inventory_api.InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    try:
        inventory.create_material(
            material_uuid=MOUNT_UUID,
            resource_template_uuid=RESOURCE_TEMPLATE_UUID,
            barcode="MOUNT-105",
            name="Release mount 105",
        )
        inventory.create_material(
            material_uuid=MATERIAL_UUID,
            resource_template_uuid=RESOURCE_TEMPLATE_UUID,
            barcode="SAMPLE-105",
            name="Release sample 105",
        )
        inventory.create_site(
            site_uuid=SITE_UUID,
            description=None,
            meta_data={},
            material_uuid=MOUNT_UUID,
            name="A1",
            sort_order=0,
            allowed_resource_template_uuids=[RESOURCE_TEMPLATE_UUID],
            occupied_material_uuid=MATERIAL_UUID,
            position_x=0.0,
            position_y=0.0,
            position_z=0.0,
            depth=1.0,
            length=1.0,
            width=1.0,
        )
        admitted = inventory.admit_task(
            _admission_command(
                ADMISSION_COMMAND_UUID,
                "m1r-admit-existing-material-105",
            )
        )
        assert admitted.status == "admitted"
        assert admitted.reservation_uuid

        release_command = inventory_api.TaskMaterialReleaseCommand(
            schema_version=1,
            command_uuid=RELEASE_COMMAND_UUID,
            idempotency_key="m1r-release-existing-material-105",
            workflow_task_uuid=WORKFLOW_TASK_UUID,
            reason="workflow_task_terminal",
        )
        released = inventory.release_task(release_command)

        assert isinstance(released, inventory_api.TaskMaterialReleaseResult)
        assert released.schema_version == 1
        assert released.command_uuid == RELEASE_COMMAND_UUID
        assert released.workflow_task_uuid == WORKFLOW_TASK_UUID
        assert released.status == "released"
        assert released.reservation_uuid == admitted.reservation_uuid
        assert released.outbox_sequence > admitted.outbox_sequence
        assert inventory.release_task(release_command) == released
        assert inventory.get_command_result(RELEASE_COMMAND_UUID) == released
    finally:
        inventory.close()

    reopened_inventory = inventory_api.InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    try:
        assert reopened_inventory.get_command_result(RELEASE_COMMAND_UUID) == released
        assert reopened_inventory.release_task(release_command) == released

        readmitted = reopened_inventory.admit_task(
            _admission_command(
                READMISSION_COMMAND_UUID,
                "m1r-readmit-existing-material-105",
            )
        )
        assert readmitted.status == "admitted"
        assert readmitted.reservation_uuid
        assert readmitted.outbox_sequence > released.outbox_sequence
        assert readmitted.bindings == admitted.bindings
    finally:
        reopened_inventory.close()
