"""M1R InventoryService blocked admission retry 最小纵向合同。

测试只通过 public admission/release/get/open/close 观察同一 blocked
command 在争用消失后转为 admitted，不访问 Store、SQLite 或 Scheduler。
"""

from __future__ import annotations

from pathlib import Path

import unilabos.app.scheduler.inventory as inventory_api

MATERIAL_UUID = "5aa00000-0000-4000-8000-000000000107"
RESOURCE_TEMPLATE_UUID = "2bb00000-0000-4000-8000-000000000107"
COMMAND_A_UUID = "80000000-0000-4000-8000-000000000107"
COMMAND_B_UUID = "81000000-0000-4000-8000-000000000107"
RELEASE_A_COMMAND_UUID = "82000000-0000-4000-8000-000000000107"
WORKFLOW_TASK_A_UUID = "90000000-0000-4000-8000-000000000107"
WORKFLOW_TASK_B_UUID = "91000000-0000-4000-8000-000000000107"
MATERIAL_SOURCE_NODE_A_UUID = "a0000000-0000-4000-8000-000000000107"
MATERIAL_SOURCE_NODE_B_UUID = "b0000000-0000-4000-8000-000000000107"


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
        mount={"uuid": MATERIAL_UUID},
        material_uuid=MATERIAL_UUID,
        site_uuid=None,
        candidate_site_uuids=(),
        flow_role="sample",
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
            material_uuid=MATERIAL_UUID,
            resource_template_uuid=RESOURCE_TEMPLATE_UUID,
            barcode="SAMPLE-107",
            name="Retry sample 107",
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
        assert binding.site_uuid is None
        assert reopened.get_command_result(COMMAND_B_UUID) == retried_b
        assert reopened.admit_task(command_b) == retried_b
    finally:
        reopened.close()
