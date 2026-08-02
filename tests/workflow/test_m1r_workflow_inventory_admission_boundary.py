"""M1R Workflow Task 与 Inventory admission 的跨库边界 RED。

WorkflowStore 只构造当前 public authoring 尚不能写入的 I/O contract fixture；
所有行为观察均通过 WorkflowService 与独立 InventoryService 的公共接口完成。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import unilabos.app.scheduler.inventory as inventory_api
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "10000000-0000-4000-8000-000000000201"
WORKFLOW_TASK_COMMAND_UUID = "80000000-0000-4000-8000-000000000201"
MATERIAL_SOURCE_NODE_UUID = "a0000000-0000-4000-8000-000000000201"
MATERIAL_UUID = "5aa00000-0000-4000-8000-000000000201"
RESOURCE_TEMPLATE_UUID = "2bb00000-0000-4000-8000-000000000201"


class _InventoryResourceSlotResolver:
    """把旧 Workflow resolver 形状限制在 Inventory public read API。"""

    def __init__(self, inventory: inventory_api.InventoryService) -> None:
        self._inventory = inventory

    def resolve(
        self,
        *,
        material_uuid: str,
        allowed_resource_template_uuids: tuple[str, ...] | None,
    ) -> inventory_api.ResourceSlotResolution:
        return self._inventory.resolve_resource_slot(
            material_uuid=material_uuid,
            allowed_resource_template_uuids=allowed_resource_template_uuids,
        )


class _RecordingAdmissionProvider:
    """只接受 closed command；不存在可接收 Workflow UoW 的方法。"""

    def __init__(self, inventory: inventory_api.InventoryService) -> None:
        self._inventory = inventory
        self.calls: list[inventory_api.TaskMaterialAdmissionCommand] = []

    def admit_task(
        self,
        command: inventory_api.TaskMaterialAdmissionCommand,
    ) -> inventory_api.TaskMaterialAdmissionResult:
        self.calls.append(command)
        return self._inventory.admit_task(command)


def _resource_templates() -> dict[str, inventory_api.ResourceTemplateIdentity]:
    identity = inventory_api.ResourceTemplateIdentity(
        uuid=RESOURCE_TEMPLATE_UUID,
        material_class="SampleTube",
    )
    return {identity.uuid: identity}


def _seed_workflow_contract(store: WorkflowStore) -> None:
    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="M1R cross-database admission boundary",
        tags=[],
        description=None,
        meta_data={
            "unilab": {
                "input_contract": {
                    "version": 1,
                    "parameters": [
                        {
                            "name": "sample",
                            "schema": {
                                "$slot": "ResourceSlot",
                                "allowed_resource_template_uuids": [
                                    RESOURCE_TEMPLATE_UUID
                                ],
                            },
                            "required": True,
                        }
                    ],
                },
                "output_contract": {
                    "version": 1,
                    "outputs": [
                        {
                            "name": "sample",
                            "schema": {
                                "$slot": "ResourceSlot",
                                "allowed_resource_template_uuids": [
                                    RESOURCE_TEMPLATE_UUID
                                ],
                            },
                            "implicit": True,
                        }
                    ],
                },
                "output_bindings": {
                    "sample": {
                        "kind": "workflow_input",
                        "parameter": "sample",
                    }
                },
            }
        },
    )


def _snapshot_fingerprint(task: dict[str, Any]) -> str:
    encoded = json.dumps(
        task["workflow_snapshot"],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _admission_command(
    task: dict[str, Any],
) -> inventory_api.TaskMaterialAdmissionCommand:
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
        command_uuid=WORKFLOW_TASK_COMMAND_UUID,
        idempotency_key=f"m1r-admit-{task['uuid']}",
        workflow_task_uuid=task["uuid"],
        workflow_snapshot_fingerprint=_snapshot_fingerprint(task),
        sources=(source,),
    )


def test_task_persists_before_independent_inventory_admission(
    tmp_path: Path,
) -> None:
    workflow_dir = tmp_path / "workflow-authority"
    inventory_dir = tmp_path / "inventory-authority"
    inventory = inventory_api.InventoryService.open(
        working_dir=inventory_dir,
        resource_templates=_resource_templates(),
    )
    store = WorkflowStore(workflow_dir / "workflow.db")
    provider = _RecordingAdmissionProvider(inventory)
    service = WorkflowService(
        store,
        resource_resolver=_InventoryResourceSlotResolver(inventory),
        material_reservations=provider,  # type: ignore[arg-type]
    )
    try:
        inventory.create_material(
            material_uuid=MATERIAL_UUID,
            resource_template_uuid=RESOURCE_TEMPLATE_UUID,
            barcode="M1R-SAMPLE-201",
            name="M1R admission boundary sample",
        )
        _seed_workflow_contract(store)

        task = service.create_workflow_task(
            workflow_uuid=WORKFLOW_UUID,
            run_mode="normal",
            target_node_uuid=None,
            input_value={"sample": {"uuid": MATERIAL_UUID}},
            description=None,
            meta_data={},
        )

        assert task["status"] == "pending"
        assert task["output"] == {}
        assert task["error_info"] == []
        assert service.list_workflow_node_jobs(task["uuid"]) == []
        assert provider.calls == []
        workflow_before_admission = service.get_workflow_task(task["uuid"])

        command = _admission_command(task)
        result = provider.admit_task(command)

        assert provider.calls == [command]
        assert result.status == "admitted"
        assert result.workflow_task_uuid == task["uuid"]
        assert result.bindings[0].resource_slot == {
            "uuid": MATERIAL_UUID,
            "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
        }
        assert inventory.get_command_result(command.command_uuid) == result
        assert service.get_workflow_task(task["uuid"]) == workflow_before_admission
        assert (workflow_dir / "workflow.db").is_file()
        assert not (workflow_dir / "inventory.db").exists()
        assert (inventory_dir / "inventory.db").is_file()
        assert not (inventory_dir / "workflow.db").exists()
    finally:
        service.close()
        inventory.close()
