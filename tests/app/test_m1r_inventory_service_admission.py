"""M1R InventoryService Task admission 最小纵向合同。

测试只通过 public ``InventoryService`` 提交和重放一个 existing
Material admission command，不访问 Store、SQLite、Scheduler 或 release 路径。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import unilabos.app.scheduler.inventory as inventory_api

MATERIAL_UUID = "5aa00000-0000-4000-8000-000000000104"
RESOURCE_TEMPLATE_UUID = "2bb00000-0000-4000-8000-000000000104"
COMMAND_UUID = "80000000-0000-4000-8000-000000000104"
WORKFLOW_TASK_UUID = "90000000-0000-4000-8000-000000000104"
MATERIAL_SOURCE_NODE_UUID = "a0000000-0000-4000-8000-000000000104"
WORKFLOW_SNAPSHOT_FINGERPRINT = "f" * 64
MISSING_SITE_UUID = "b0000000-0000-4000-8000-000000000104"
MOUNT_UUID = "5aa00000-0000-4000-8000-000000000105"
SITE_UUID = "b0000000-0000-4000-8000-000000000105"


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
        mount={"uuid": MOUNT_UUID},
        material_uuid=MATERIAL_UUID,
        site_uuid=SITE_UUID,
        candidate_site_uuids=(),
        flow_role="sample",
    )
    return inventory_api.TaskMaterialAdmissionCommand(
        schema_version=1,
        command_uuid=COMMAND_UUID,
        idempotency_key="m1r-admit-existing-material-104",
        workflow_task_uuid=WORKFLOW_TASK_UUID,
        workflow_snapshot_fingerprint=WORKFLOW_SNAPSHOT_FINGERPRINT,
        sources=(source,),
    )


def _assert_admitted_result(
    result: inventory_api.TaskMaterialAdmissionResult,
) -> None:
    assert result.schema_version == 1
    assert result.command_uuid == COMMAND_UUID
    assert result.workflow_task_uuid == WORKFLOW_TASK_UUID
    assert result.status == "admitted"
    assert result.reservation_uuid
    assert result.diagnostics == ()
    assert result.outbox_sequence > 0
    assert len(result.bindings) == 1

    binding = result.bindings[0]
    assert binding.material_source_node_uuid == MATERIAL_SOURCE_NODE_UUID
    assert binding.resource_slot == {
        "uuid": MATERIAL_UUID,
        "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
    }
    assert binding.site_uuid == SITE_UUID


def _seed_located_material(
    inventory: inventory_api.InventoryService,
    *,
    barcode_suffix: str,
) -> None:
    inventory.create_material(
        material_uuid=MOUNT_UUID,
        resource_template_uuid=RESOURCE_TEMPLATE_UUID,
        barcode=f"MOUNT-{barcode_suffix}",
        name="Admission mount",
    )
    inventory.create_material(
        material_uuid=MATERIAL_UUID,
        resource_template_uuid=RESOURCE_TEMPLATE_UUID,
        barcode=f"SAMPLE-{barcode_suffix}",
        name="Admission sample",
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


def test_existing_material_admission_replays_and_survives_reopen(
    tmp_path: Path,
) -> None:
    inventory = inventory_api.InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    try:
        _seed_located_material(inventory, barcode_suffix="104")
        command = _admission_command()
        admitted = inventory.admit_task(command)

        _assert_admitted_result(admitted)
        assert inventory.admit_task(command) == admitted
        assert inventory.get_command_result(COMMAND_UUID) == admitted
    finally:
        inventory.close()

    reopened_inventory = inventory_api.InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    try:
        assert reopened_inventory.get_command_result(COMMAND_UUID) == admitted
        assert reopened_inventory.admit_task(command) == admitted
    finally:
        reopened_inventory.close()


def test_missing_candidate_site_is_a_durable_rejected_result(
    tmp_path: Path,
) -> None:
    inventory = inventory_api.InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    try:
        _seed_located_material(inventory, barcode_suffix="104-REJECTED")
        command = _admission_command()
        command = replace(
            command,
            sources=(
                replace(
                    command.sources[0],
                    site_uuid=None,
                    candidate_site_uuids=(MISSING_SITE_UUID,),
                ),
            ),
        )

        rejected = inventory.admit_task(command)

        assert rejected.status == "rejected"
        assert rejected.reservation_uuid is None
        assert rejected.bindings == ()
        assert rejected.diagnostics == (
            {
                "code": "site_not_found",
                "material_source_node_uuid": MATERIAL_SOURCE_NODE_UUID,
            },
        )
        assert inventory.get_command_result(COMMAND_UUID) == rejected
        assert inventory.admit_task(command) == rejected
        event = next(
            item
            for item in inventory.read_outbox(after_sequence=0, limit=100)
            if item.sequence == rejected.outbox_sequence
        )
        assert event.event_type == "material_admission.rejected"
        assert event.payload["diagnostics"] == [
            {
                "code": "site_not_found",
                "material_source_node_uuid": MATERIAL_SOURCE_NODE_UUID,
            }
        ]
    finally:
        inventory.close()

    reopened = inventory_api.InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    try:
        assert reopened.get_command_result(COMMAND_UUID) == rejected
        assert reopened.admit_task(command) == rejected
    finally:
        reopened.close()


def test_candidate_sites_resolve_the_materials_current_site(
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
            name="Candidate Site mount",
        )
        inventory.create_material(
            material_uuid=MATERIAL_UUID,
            resource_template_uuid=RESOURCE_TEMPLATE_UUID,
            barcode="SAMPLE-105",
            name="Candidate Site sample",
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
        command = _admission_command()
        command = replace(
            command,
            sources=(
                replace(
                    command.sources[0],
                    mount={"uuid": MOUNT_UUID},
                    site_uuid=None,
                    candidate_site_uuids=(SITE_UUID,),
                ),
            ),
        )

        admitted = inventory.admit_task(command)

        assert admitted.status == "admitted"
        assert admitted.bindings[0].site_uuid == SITE_UUID
    finally:
        inventory.close()


def test_automatic_existing_with_empty_candidate_site_is_durably_blocked(
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
            barcode="MOUNT-105-BLOCKED",
            name="Candidate Site blocked mount",
        )
        inventory.create_material(
            material_uuid=MATERIAL_UUID,
            resource_template_uuid=RESOURCE_TEMPLATE_UUID,
            barcode="SAMPLE-105-BLOCKED",
            name="Candidate Site waiting sample",
        )
        inventory.create_site(
            site_uuid=SITE_UUID,
            description=None,
            meta_data={},
            material_uuid=MOUNT_UUID,
            name="A1",
            sort_order=0,
            allowed_resource_template_uuids=[RESOURCE_TEMPLATE_UUID],
            occupied_material_uuid=None,
            position_x=0.0,
            position_y=0.0,
            position_z=0.0,
            depth=1.0,
            length=1.0,
            width=1.0,
        )
        command = _admission_command()
        command = replace(
            command,
            sources=(
                replace(
                    command.sources[0],
                    mount={"uuid": MOUNT_UUID},
                    material_uuid=None,
                    site_uuid=None,
                    candidate_site_uuids=(SITE_UUID,),
                ),
            ),
        )

        blocked = inventory.admit_task(command)

        assert blocked.status == "blocked"
        assert blocked.reservation_uuid is None
        assert blocked.bindings == ()
        assert blocked.diagnostics == (
            {
                "code": "material_unavailable",
                "material_source_node_uuid": MATERIAL_SOURCE_NODE_UUID,
            },
        )
        assert inventory.get_command_result(COMMAND_UUID) == blocked
        assert inventory.admit_task(command) == blocked
    finally:
        inventory.close()

    reopened = inventory_api.InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    try:
        assert reopened.get_command_result(COMMAND_UUID) == blocked
        assert reopened.admit_task(command) == blocked
    finally:
        reopened.close()
