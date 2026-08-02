"""M1R InventoryService blocked admission retry 最小纵向合同。

测试只通过 public admission/release/get/open/close 观察同一 blocked
command 在争用消失后转为 admitted，不访问 Store、SQLite 或 Scheduler。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import unilabos.app.scheduler.inventory as inventory_api

MATERIAL_UUID = "5aa00000-0000-4000-8000-000000000107"
ALTERNATE_MATERIAL_UUID = "5aa00000-0000-4000-8000-000000000109"
MOUNT_UUID = "5aa00000-0000-4000-8000-000000000108"
SITE_UUID = "6aa00000-0000-4000-8000-000000000107"
ALTERNATE_SITE_UUID = "6aa00000-0000-4000-8000-000000000109"
RESOURCE_TEMPLATE_UUID = "2bb00000-0000-4000-8000-000000000107"
COMMAND_A_UUID = "80000000-0000-4000-8000-000000000107"
COMMAND_B_UUID = "81000000-0000-4000-8000-000000000107"
COMMAND_C_UUID = "83000000-0000-4000-8000-000000000107"
RELEASE_A_COMMAND_UUID = "82000000-0000-4000-8000-000000000107"
WORKFLOW_TASK_A_UUID = "90000000-0000-4000-8000-000000000107"
WORKFLOW_TASK_B_UUID = "91000000-0000-4000-8000-000000000107"
MATERIAL_SOURCE_NODE_A_UUID = "a0000000-0000-4000-8000-000000000107"
MATERIAL_SOURCE_NODE_B_UUID = "b0000000-0000-4000-8000-000000000107"
MATERIAL_SOURCE_NODE_C_UUID = "c0000000-0000-4000-8000-000000000107"


def _resource_templates() -> dict[str, inventory_api.ResourceTemplateIdentity]:
    identity = inventory_api.ResourceTemplateIdentity(
        uuid=RESOURCE_TEMPLATE_UUID,
        material_class="SampleTube",
    )
    return {identity.uuid: identity}


def _admission_command(
    *,
    command_uuid: str,
    workflow_task_uuid: str,
    material_source_node_uuid: str,
    fingerprint: str,
) -> inventory_api.TaskMaterialAdmissionCommand:
    source = inventory_api.TaskMaterialAdmissionSource(
        material_source_node_uuid=material_source_node_uuid,
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
        idempotency_key=f"m1r-blocked-retry-{workflow_task_uuid}",
        workflow_task_uuid=workflow_task_uuid,
        workflow_snapshot_fingerprint=fingerprint,
        sources=(source,),
    )


def test_blocked_command_becomes_admitted_after_owner_releases_and_reopen(
    tmp_path: Path,
) -> None:
    command_a = _admission_command(
        command_uuid=COMMAND_A_UUID,
        workflow_task_uuid=WORKFLOW_TASK_A_UUID,
        material_source_node_uuid=MATERIAL_SOURCE_NODE_A_UUID,
        fingerprint="a" * 64,
    )
    command_b = _admission_command(
        command_uuid=COMMAND_B_UUID,
        workflow_task_uuid=WORKFLOW_TASK_B_UUID,
        material_source_node_uuid=MATERIAL_SOURCE_NODE_B_UUID,
        fingerprint="b" * 64,
    )
    inventory = inventory_api.InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    try:
        inventory.create_material(
            material_uuid=MOUNT_UUID,
            resource_template_uuid=RESOURCE_TEMPLATE_UUID,
            barcode="MOUNT-107",
            name="Retry mount 107",
        )
        inventory.create_material(
            material_uuid=MATERIAL_UUID,
            resource_template_uuid=RESOURCE_TEMPLATE_UUID,
            barcode="SAMPLE-107",
            name="Retry sample 107",
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
        admitted_a = inventory.admit_task(command_a)
        blocked_b = inventory.admit_task(command_b)

        assert admitted_a.status == "admitted"
        assert admitted_a.reservation_uuid
        assert blocked_b.status == "blocked"
        assert blocked_b.reservation_uuid is None
        assert blocked_b.bindings == ()
        assert tuple(item.get("code") for item in blocked_b.diagnostics) == (
            "material_reserved",
        )
        assert inventory.get_command_result(COMMAND_B_UUID) == blocked_b

        released_a = inventory.release_task(
            inventory_api.TaskMaterialReleaseCommand(
                schema_version=1,
                command_uuid=RELEASE_A_COMMAND_UUID,
                idempotency_key="m1r-release-owner-107",
                workflow_task_uuid=WORKFLOW_TASK_A_UUID,
                reason="workflow_task_terminal",
            )
        )
        assert released_a.status == "released"
        assert released_a.reservation_uuid == admitted_a.reservation_uuid
    finally:
        inventory.close()

    reopened = inventory_api.InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    try:
        retried_b = reopened.admit_task(command_b)

        assert retried_b.status == "admitted"
        assert retried_b.reservation_uuid
        assert retried_b.outbox_sequence > blocked_b.outbox_sequence
        assert len(retried_b.bindings) == 1
        binding = retried_b.bindings[0]
        assert binding.material_source_node_uuid == MATERIAL_SOURCE_NODE_B_UUID
        assert binding.resource_slot == {
            "uuid": MATERIAL_UUID,
            "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
        }
        assert binding.site_uuid == SITE_UUID
        assert reopened.get_command_result(COMMAND_B_UUID) == retried_b
        assert reopened.admit_task(command_b) == retried_b
    finally:
        reopened.close()


def test_blocked_command_can_upgrade_to_deterministic_rejection(
    tmp_path: Path,
) -> None:
    owner_command = _admission_command(
        command_uuid=COMMAND_A_UUID,
        workflow_task_uuid=WORKFLOW_TASK_A_UUID,
        material_source_node_uuid=MATERIAL_SOURCE_NODE_A_UUID,
        fingerprint="a" * 64,
    )
    blocked_command = _admission_command(
        command_uuid=COMMAND_B_UUID,
        workflow_task_uuid=WORKFLOW_TASK_B_UUID,
        material_source_node_uuid=MATERIAL_SOURCE_NODE_B_UUID,
        fingerprint="b" * 64,
    )
    conflicting_command = replace(
        blocked_command,
        command_uuid=COMMAND_C_UUID,
        idempotency_key="m2b-conflicting-task-material-set",
        workflow_snapshot_fingerprint="c" * 64,
        sources=(
            replace(
                blocked_command.sources[0],
                material_source_node_uuid=MATERIAL_SOURCE_NODE_C_UUID,
                material_uuid=ALTERNATE_MATERIAL_UUID,
                site_uuid=ALTERNATE_SITE_UUID,
            ),
        ),
    )
    inventory = inventory_api.InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    try:
        inventory.create_material(
            material_uuid=MOUNT_UUID,
            resource_template_uuid=RESOURCE_TEMPLATE_UUID,
            barcode="MOUNT-107-UPGRADE",
            name="Upgrade mount 107",
        )
        for material_uuid, barcode in (
            (MATERIAL_UUID, "SAMPLE-107-UPGRADE"),
            (ALTERNATE_MATERIAL_UUID, "SAMPLE-109-UPGRADE"),
        ):
            inventory.create_material(
                material_uuid=material_uuid,
                resource_template_uuid=RESOURCE_TEMPLATE_UUID,
                barcode=barcode,
                name=barcode,
            )
        for site_uuid, material_uuid, sort_order in (
            (SITE_UUID, MATERIAL_UUID, 10),
            (ALTERNATE_SITE_UUID, ALTERNATE_MATERIAL_UUID, 20),
        ):
            inventory.create_site(
                site_uuid=site_uuid,
                description=None,
                meta_data={},
                material_uuid=MOUNT_UUID,
                name=f"Site-{sort_order}",
                sort_order=sort_order,
                allowed_resource_template_uuids=[RESOURCE_TEMPLATE_UUID],
                occupied_material_uuid=material_uuid,
                position_x=0.0,
                position_y=0.0,
                position_z=0.0,
                depth=1.0,
                length=1.0,
                width=1.0,
            )

        owner = inventory.admit_task(owner_command)
        blocked = inventory.admit_task(blocked_command)
        other_set = inventory.admit_task(conflicting_command)
        assert owner.status == "admitted"
        assert blocked.status == "blocked"
        assert other_set.status == "admitted"
        inventory.release_task(
            inventory_api.TaskMaterialReleaseCommand(
                schema_version=1,
                command_uuid=RELEASE_A_COMMAND_UUID,
                idempotency_key="m2b-release-owner-before-reject-upgrade",
                workflow_task_uuid=WORKFLOW_TASK_A_UUID,
                reason="workflow_task_terminal",
            )
        )

        rejected = inventory.admit_task(blocked_command)

        assert rejected.status == "rejected"
        assert rejected.outbox_sequence > blocked.outbox_sequence
        assert rejected.diagnostics == (
            {
                "code": "task_material_set_conflict",
                "material_source_node_uuid": None,
            },
        )
        assert inventory.admit_task(blocked_command) == rejected
        assert inventory.get_command_result(COMMAND_B_UUID) == rejected
    finally:
        inventory.close()
