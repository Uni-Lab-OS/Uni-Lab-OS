"""M1R InventoryService Task admission 最小纵向合同。

测试只通过 public ``InventoryService`` 提交和重放一个 existing
Material admission command，不访问 Store、SQLite、Scheduler 或 release 路径。
"""

from __future__ import annotations

from pathlib import Path

import unilabos.app.scheduler.inventory as inventory_api

MATERIAL_UUID = "5aa00000-0000-4000-8000-000000000104"
RESOURCE_TEMPLATE_UUID = "2bb00000-0000-4000-8000-000000000104"
COMMAND_UUID = "80000000-0000-4000-8000-000000000104"
WORKFLOW_TASK_UUID = "90000000-0000-4000-8000-000000000104"
MATERIAL_SOURCE_NODE_UUID = "a0000000-0000-4000-8000-000000000104"
WORKFLOW_SNAPSHOT_FINGERPRINT = "f" * 64


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
    assert binding.site_uuid is None


def test_existing_material_admission_replays_and_survives_reopen(
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
            barcode="SAMPLE-104",
            name="Admission sample 104",
        )
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
