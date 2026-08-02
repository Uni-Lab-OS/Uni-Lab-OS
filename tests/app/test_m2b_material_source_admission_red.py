"""M2B Task-wide MaterialSource admission 的公共接缝 RED。

``WorkflowStore`` 只装配当前 authoring public API 还不能直接创建的
MaterialSource graph fixture。行为从 ``EdgeScheduler`` 进入，并只通过
``WorkflowService`` 与真实 ``InventoryService``/SQLite 的 public API 观察。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import unilabos.app.scheduler.inventory as inventory_api
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.catalog import (
    CatalogAuthority,
    NodeTemplateImport,
    TemplateCatalog,
)
from unilabos.workflow.models import WorkflowNodeWrite
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11000000-0000-4000-8000-0000000002b0"
MATERIAL_SOURCE_TEMPLATE_UUID = "31000000-0000-4000-8000-0000000002b0"
MATERIAL_HANDLE_UUID = "41000000-0000-4000-8000-0000000002b0"
MOUNT_TEMPLATE_UUID = "32000000-0000-4000-8000-0000000002b0"
SAMPLE_TEMPLATE_UUID = "32000000-0000-4000-8000-0000000002b1"
MOUNT_MATERIAL_UUID = "51000000-0000-4000-8000-0000000002b0"
LOW_SITE_MATERIAL_UUID = "52000000-0000-4000-8000-0000000002b1"
ALTERNATE_MATERIAL_UUID = "52000000-0000-4000-8000-0000000002b2"

SOURCE_A_UUID = "21000000-0000-4000-8000-0000000002b1"
SOURCE_B_UUID = "21000000-0000-4000-8000-0000000002b2"
SOURCE_C_UUID = "21000000-0000-4000-8000-0000000002b3"

# UUID order differs from business order for both candidate sets.  In
# particular, create-new's UUID-sorted input puts its sort_order=40 Site first,
# while the durable Site order must select its sort_order=30 Site.
ALTERNATE_SITE_UUID = "61000000-0000-4000-8000-0000000002b1"
CREATE_NEW_ALTERNATE_SITE_UUID = "61000000-0000-4000-8000-0000000002b4"
CREATE_NEW_SITE_UUID = "61000000-0000-4000-8000-0000000002b5"
LOW_SORT_SITE_UUID = "61000000-0000-4000-8000-0000000002b9"

AUTHORITY = CatalogAuthority(authority_id="m2b-red", kind="backend")


class _RecordingInventoryPort:
    """Record the one public Task-wide command and delegate durable work."""

    def __init__(self, inventory: inventory_api.InventoryService) -> None:
        self._inventory = inventory
        self.commands: list[inventory_api.TaskMaterialAdmissionCommand] = []

    def admit_task(
        self,
        command: inventory_api.TaskMaterialAdmissionCommand,
    ) -> inventory_api.TaskMaterialAdmissionResult:
        self.commands.append(command)
        return self._inventory.admit_task(command)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inventory, name)


def _resource_templates() -> dict[str, inventory_api.ResourceTemplateIdentity]:
    identities = (
        inventory_api.ResourceTemplateIdentity(
            uuid=MOUNT_TEMPLATE_UUID,
            material_class="Deck",
        ),
        inventory_api.ResourceTemplateIdentity(
            uuid=SAMPLE_TEMPLATE_UUID,
            material_class="SampleTube",
        ),
    )
    return {identity.uuid: identity for identity in identities}


def _material_source_import() -> NodeTemplateImport:
    return NodeTemplateImport(
        template={
            "uuid": MATERIAL_SOURCE_TEMPLATE_UUID,
            "description": "M2B MaterialSource admission",
            "meta_data": {"framework": "material_source"},
            "resource_template_uuid": MOUNT_TEMPLATE_UUID,
            "name": "material_source",
            "display_name": "Material Source",
            "class": "unilabos.workflow.authoring:material_source",
            "goal": {},
            "goal_default": {},
            "feedback": {},
            "result": {},
            "schema": None,
            "type": "material_source",
            "icon": None,
            "header": None,
            "footer": None,
            "node_type": "material_source",
        },
        handles=[
            {
                "uuid": MATERIAL_HANDLE_UUID,
                "description": "Resolved Material",
                "meta_data": {
                    "unilab": {
                        "value_schema": {"$slot": "ResourceSlot"},
                        "allowed_resource_template_uuids": [SAMPLE_TEMPLATE_UUID],
                    }
                },
                "handle_key": "material",
                "io_type": "source",
                "display_name": "Material",
                "type": "ResourceSlot",
                "required": False,
                "data_source": "executor",
                "data_key": "material",
            }
        ],
    )


def _source_node(
    node_uuid: str,
    *,
    mode: str,
    material_uuid: str | None,
    site_uuid: str | None,
    candidate_site_uuids: tuple[str, ...],
    flow_role: str,
) -> WorkflowNodeWrite:
    return WorkflowNodeWrite(
        uuid=node_uuid,
        workflow_node_template_uuid=MATERIAL_SOURCE_TEMPLATE_UUID,
        name=f"Resolve {flow_role}",
        status="idle",
        type="material_source",
        pose={},
        param={
            "mode": mode,
            "resource_template_uuid": SAMPLE_TEMPLATE_UUID,
            "mount": {"uuid": MOUNT_MATERIAL_UUID},
            "material_uuid": material_uuid,
            "site": site_uuid,
            "slot_range": (
                list(candidate_site_uuids) if candidate_site_uuids else None
            ),
            "flow_role": flow_role,
        },
        execution_policy={},
        disabled=False,
        minimized=False,
        meta_data={},
    )


def _create_workflow_service(
    workflow_database: Path,
    *,
    nodes: list[WorkflowNodeWrite],
) -> tuple[WorkflowService, dict[str, Any]]:
    workflow_database.parent.mkdir(parents=True, exist_ok=True)
    store = WorkflowStore(workflow_database)
    TemplateCatalog(store).replace(AUTHORITY, [_material_source_import()])
    service = WorkflowService(store)
    service.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="M2B MaterialSource admission RED",
        tags=[],
        description=None,
        meta_data={},
    )
    # Fixture-only setup: the public authoring API cannot yet create this
    # standalone graph without source compilation. Runtime assertions below
    # remain entirely on WorkflowService/EdgeScheduler public seams.
    store.save_graph(WORKFLOW_UUID, revision=1, nodes=nodes, edges=[])
    task = service.create_workflow_task(
        workflow_uuid=WORKFLOW_UUID,
        run_mode="normal",
        target_node_uuid=None,
        input_value={},
        description=None,
        meta_data={},
    )
    return service, task


def _create_site(
    inventory: inventory_api.InventoryService,
    *,
    site_uuid: str,
    sort_order: int,
    occupied_material_uuid: str | None,
) -> None:
    inventory.create_site(
        site_uuid=site_uuid,
        description=None,
        meta_data={},
        material_uuid=MOUNT_MATERIAL_UUID,
        name=f"Site-{sort_order}",
        sort_order=sort_order,
        allowed_resource_template_uuids=[SAMPLE_TEMPLATE_UUID],
        occupied_material_uuid=occupied_material_uuid,
        position_x=0.0,
        position_y=0.0,
        position_z=0.0,
        depth=1.0,
        length=1.0,
        width=1.0,
    )


def _seed_inventory(inventory: inventory_api.InventoryService) -> None:
    inventory.create_material(
        material_uuid=MOUNT_MATERIAL_UUID,
        resource_template_uuid=MOUNT_TEMPLATE_UUID,
        barcode="M2B-DECK",
        name="M2B deck",
    )
    inventory.create_material(
        material_uuid=LOW_SITE_MATERIAL_UUID,
        resource_template_uuid=SAMPLE_TEMPLATE_UUID,
        barcode="M2B-SAMPLE-LOW",
        name="Low Site sample",
    )
    inventory.create_material(
        material_uuid=ALTERNATE_MATERIAL_UUID,
        resource_template_uuid=SAMPLE_TEMPLATE_UUID,
        barcode="M2B-SAMPLE-ALT",
        name="Alternate sample",
    )
    _create_site(
        inventory,
        site_uuid=LOW_SORT_SITE_UUID,
        sort_order=10,
        occupied_material_uuid=LOW_SITE_MATERIAL_UUID,
    )
    _create_site(
        inventory,
        site_uuid=ALTERNATE_SITE_UUID,
        sort_order=20,
        occupied_material_uuid=ALTERNATE_MATERIAL_UUID,
    )
    _create_site(
        inventory,
        site_uuid=CREATE_NEW_SITE_UUID,
        sort_order=30,
        occupied_material_uuid=None,
    )
    _create_site(
        inventory,
        site_uuid=CREATE_NEW_ALTERNATE_SITE_UUID,
        sort_order=40,
        occupied_material_uuid=None,
    )


def _jobs_by_node(
    service: WorkflowService,
    task_uuid: str,
) -> dict[str, dict[str, Any]]:
    return {
        job["workflow_node_uuid"]: job
        for job in service.list_workflow_node_jobs(task_uuid)
    }


def test_task_wide_matching_uses_site_order_creates_one_material_and_keeps_lots(
    tmp_path: Path,
) -> None:
    """One complete solution requires backtracking the first existing Source.

    A two-Source automatic-existing/create-new fixture cannot itself create a
    Site conflict: existing needs an occupied Site while create_new needs an
    empty Site.  A/B therefore prove the shared-candidate backtrack, and C
    proves constrained create_new commits atomically in that same Task.
    """

    inventory = inventory_api.InventoryService.open(
        working_dir=tmp_path / "inventory-authority",
        resource_templates=_resource_templates(),
    )
    service: WorkflowService | None = None
    try:
        _seed_inventory(inventory)
        inventory.inbound_lot(
            resource_template_uuid=SAMPLE_TEMPLATE_UUID,
            quantity=23.0,
            unit="mL",
            lot_id="m2b-deduction-non-goal",
        )
        before = inventory.inventory_snapshot()
        nodes = [
            _source_node(
                SOURCE_A_UUID,
                mode="existing",
                material_uuid=None,
                site_uuid=None,
                candidate_site_uuids=(ALTERNATE_SITE_UUID, LOW_SORT_SITE_UUID),
                flow_role="primary_sample",
            ),
            _source_node(
                SOURCE_B_UUID,
                mode="existing",
                material_uuid=None,
                site_uuid=LOW_SORT_SITE_UUID,
                candidate_site_uuids=(),
                flow_role="reagent",
            ),
            _source_node(
                SOURCE_C_UUID,
                mode="create_new",
                material_uuid=None,
                site_uuid=None,
                candidate_site_uuids=(
                    CREATE_NEW_ALTERNATE_SITE_UUID,
                    CREATE_NEW_SITE_UUID,
                ),
                flow_role="consumable",
            ),
        ]
        service, task = _create_workflow_service(
            tmp_path / "workflow-authority" / "workflow.db",
            nodes=nodes,
        )
        recording_inventory = _RecordingInventoryPort(inventory)
        scheduler = EdgeScheduler(
            workflow_tasks=service,
            inventory=recording_inventory,
        )

        admitted = scheduler.reconcile_task_admission(task["uuid"])

        assert admitted is not None
        assert admitted.status == "admitted"
        assert admitted.reservation_uuid is not None
        assert admitted.diagnostics == ()
        assert len(recording_inventory.commands) == 1
        assert [
            source.material_source_node_uuid
            for source in recording_inventory.commands[0].sources
        ] == [SOURCE_A_UUID, SOURCE_B_UUID, SOURCE_C_UUID]
        assert recording_inventory.commands[0].sources[0].candidate_site_uuids == (
            ALTERNATE_SITE_UUID,
            LOW_SORT_SITE_UUID,
        )
        assert recording_inventory.commands[0].sources[2].candidate_site_uuids == (
            CREATE_NEW_ALTERNATE_SITE_UUID,
            CREATE_NEW_SITE_UUID,
        )

        bindings = {
            binding.material_source_node_uuid: binding for binding in admitted.bindings
        }
        assert bindings[SOURCE_A_UUID].site_uuid == ALTERNATE_SITE_UUID
        assert bindings[SOURCE_A_UUID].resource_slot == {
            "uuid": ALTERNATE_MATERIAL_UUID,
            "resource_template_uuid": SAMPLE_TEMPLATE_UUID,
        }
        assert bindings[SOURCE_B_UUID].site_uuid == LOW_SORT_SITE_UUID
        assert bindings[SOURCE_B_UUID].resource_slot == {
            "uuid": LOW_SITE_MATERIAL_UUID,
            "resource_template_uuid": SAMPLE_TEMPLATE_UUID,
        }
        assert bindings[SOURCE_C_UUID].site_uuid == CREATE_NEW_SITE_UUID

        new_material_uuid = bindings[SOURCE_C_UUID].resource_slot["uuid"]
        assert UUID(new_material_uuid).version == 4
        assert bindings[SOURCE_C_UUID].resource_slot == {
            "uuid": new_material_uuid,
            "resource_template_uuid": SAMPLE_TEMPLATE_UUID,
        }
        created = inventory.get_material(new_material_uuid).to_dict()
        assert created | {"create_time": None, "update_time": None} == {
            "uuid": new_material_uuid,
            "create_time": None,
            "update_time": None,
            "deleted_at": None,
            "description": None,
            "meta_data": {},
            "resource_template_uuid": SAMPLE_TEMPLATE_UUID,
            "parent_uuid": None,
            "class": "SampleTube",
            "barcode": "",
            "name": "SampleTube",
            "config": {},
            "data": {},
            "disposition": "active",
            "material_kind": "business",
            "version": 1,
        }

        after = inventory.inventory_snapshot()
        assert after["inventory_lots"] == before["inventory_lots"]
        assert {item["uuid"] for item in after["materials"]} == {
            *{item["uuid"] for item in before["materials"]},
            new_material_uuid,
        }
        assert inventory.get_site(LOW_SORT_SITE_UUID).occupied_material_uuid == (
            LOW_SITE_MATERIAL_UUID
        )
        assert inventory.get_site(ALTERNATE_SITE_UUID).occupied_material_uuid == (
            ALTERNATE_MATERIAL_UUID
        )
        created_site = inventory.get_site(CREATE_NEW_SITE_UUID)
        assert created_site.occupied_material_uuid == new_material_uuid
        assert created_site.version == 2
        alternate_create_site = inventory.get_site(CREATE_NEW_ALTERNATE_SITE_UUID)
        assert alternate_create_site.occupied_material_uuid is None
        assert alternate_create_site.version == 1

        reservations = [
            item
            for item in after["material_reservations"]
            if item["workflow_task_uuid"] == task["uuid"] and item["status"] == "active"
        ]
        assert len(reservations) == 1
        assert reservations[0]["uuid"] == admitted.reservation_uuid
        assert {item["material_uuid"] for item in reservations[0]["members"]} == {
            ALTERNATE_MATERIAL_UUID,
            LOW_SITE_MATERIAL_UUID,
            new_material_uuid,
        }

        jobs = _jobs_by_node(service, task["uuid"])
        assert set(jobs) == {SOURCE_A_UUID, SOURCE_B_UUID, SOURCE_C_UUID}
        for node_uuid, binding in bindings.items():
            assert jobs[node_uuid]["status"] == "succeeded"
            assert jobs[node_uuid]["return_info"] == {"material": binding.resource_slot}
        assert service.get_workflow_task(task["uuid"])["status"] == "pending"
        assert scheduler.can_dispatch_task_materials(task["uuid"])

        durable_events = inventory.read_outbox(after_sequence=0, limit=100)
        replayed = scheduler.reconcile_task_admission(task["uuid"])

        assert replayed == admitted
        assert len(recording_inventory.commands) == 1
        assert inventory.inventory_snapshot() == after
        assert inventory.read_outbox(after_sequence=0, limit=100) == durable_events
        assert _jobs_by_node(service, task["uuid"]) == jobs
    finally:
        if service is not None:
            service.close()
        inventory.close()


def test_fixed_material_location_mismatch_rejects_and_fails_resolution(
    tmp_path: Path,
) -> None:
    inventory = inventory_api.InventoryService.open(
        working_dir=tmp_path / "inventory-authority",
        resource_templates=_resource_templates(),
    )
    service: WorkflowService | None = None
    try:
        _seed_inventory(inventory)
        node = _source_node(
            SOURCE_A_UUID,
            mode="existing",
            material_uuid=LOW_SITE_MATERIAL_UUID,
            site_uuid=ALTERNATE_SITE_UUID,
            candidate_site_uuids=(),
            flow_role="primary_sample",
        )
        service, task = _create_workflow_service(
            tmp_path / "workflow-authority" / "workflow.db",
            nodes=[node],
        )
        scheduler = EdgeScheduler(workflow_tasks=service, inventory=inventory)

        rejected = scheduler.reconcile_task_admission(task["uuid"])

        assert rejected is not None
        assert rejected.status == "rejected"
        assert rejected.reservation_uuid is None
        assert rejected.bindings == ()
        assert len(rejected.diagnostics) == 1
        diagnostic = rejected.diagnostics[0]
        assert diagnostic["code"] == "material_location_mismatch"
        assert diagnostic["material_source_node_uuid"] == SOURCE_A_UUID
        projected_task = service.get_workflow_task(task["uuid"])
        assert projected_task["status"] == "failed"
        assert any(
            item.get("code") == "material_location_mismatch"
            for item in projected_task["error_info"]
        )
        job = _jobs_by_node(service, task["uuid"])[SOURCE_A_UUID]
        assert job["status"] == "failed"
        assert job["return_info"] == {}
        assert any(
            item.get("code") == "material_location_mismatch"
            for item in job["error_info"]
        )
        assert inventory.get_site(LOW_SORT_SITE_UUID).occupied_material_uuid == (
            LOW_SITE_MATERIAL_UUID
        )
        assert inventory.get_site(ALTERNATE_SITE_UUID).occupied_material_uuid == (
            ALTERNATE_MATERIAL_UUID
        )
        assert not scheduler.can_dispatch_task_materials(task["uuid"])
    finally:
        if service is not None:
            service.close()
        inventory.close()


def test_later_rejection_rolls_back_earlier_create_new_candidate(
    tmp_path: Path,
) -> None:
    inventory = inventory_api.InventoryService.open(
        working_dir=tmp_path / "inventory-authority",
        resource_templates=_resource_templates(),
    )
    service: WorkflowService | None = None
    try:
        _seed_inventory(inventory)
        nodes = [
            _source_node(
                SOURCE_A_UUID,
                mode="create_new",
                material_uuid=None,
                site_uuid=CREATE_NEW_SITE_UUID,
                candidate_site_uuids=(),
                flow_role="consumable",
            ),
            _source_node(
                SOURCE_B_UUID,
                mode="existing",
                material_uuid=LOW_SITE_MATERIAL_UUID,
                site_uuid=ALTERNATE_SITE_UUID,
                candidate_site_uuids=(),
                flow_role="primary_sample",
            ),
        ]
        service, task = _create_workflow_service(
            tmp_path / "workflow-authority" / "workflow.db",
            nodes=nodes,
        )
        before = inventory.inventory_snapshot()
        scheduler = EdgeScheduler(workflow_tasks=service, inventory=inventory)

        rejected = scheduler.reconcile_task_admission(task["uuid"])

        assert rejected is not None and rejected.status == "rejected"
        assert rejected.diagnostics == (
            {
                "code": "material_location_mismatch",
                "material_source_node_uuid": SOURCE_B_UUID,
            },
        )
        after = inventory.inventory_snapshot()
        assert after["materials"] == before["materials"]
        assert after["inventory_lots"] == before["inventory_lots"]
        assert after["material_reservations"] == before["material_reservations"]
        empty_site = inventory.get_site(CREATE_NEW_SITE_UUID)
        assert empty_site.occupied_material_uuid is None
        assert empty_site.version == 1
    finally:
        if service is not None:
            service.close()
        inventory.close()


def test_blocked_task_state_and_pending_job_monotonically_upgrade_to_admitted(
    tmp_path: Path,
) -> None:
    inventory = inventory_api.InventoryService.open(
        working_dir=tmp_path / "inventory-authority",
        resource_templates=_resource_templates(),
    )
    service: WorkflowService | None = None
    try:
        _seed_inventory(inventory)
        node = _source_node(
            SOURCE_A_UUID,
            mode="existing",
            material_uuid=LOW_SITE_MATERIAL_UUID,
            site_uuid=LOW_SORT_SITE_UUID,
            candidate_site_uuids=(),
            flow_role="primary_sample",
        )
        service, owner_task = _create_workflow_service(
            tmp_path / "workflow-authority" / "workflow.db",
            nodes=[node],
        )
        waiting_task = service.create_workflow_task(
            workflow_uuid=WORKFLOW_UUID,
            run_mode="normal",
            target_node_uuid=None,
            input_value={},
            description=None,
            meta_data={},
        )
        scheduler = EdgeScheduler(workflow_tasks=service, inventory=inventory)

        owner = scheduler.reconcile_task_admission(owner_task["uuid"])
        blocked = scheduler.reconcile_task_admission(waiting_task["uuid"])

        assert owner is not None and owner.status == "admitted"
        assert blocked is not None and blocked.status == "blocked"
        assert service.get_workflow_task(waiting_task["uuid"])["status"] == (
            "admission_blocked"
        )
        waiting_job = _jobs_by_node(service, waiting_task["uuid"])[SOURCE_A_UUID]
        assert waiting_job["status"] == "pending"
        assert waiting_job["return_info"] == {}
        assert waiting_job["error_info"] == []
        assert blocked.diagnostics[0]["code"] == "material_reserved"
        assert blocked.diagnostics[0]["material_source_node_uuid"] == SOURCE_A_UUID
        assert service.list_workflow_tasks(status="admission_blocked")["items"] == [
            service.get_workflow_task(waiting_task["uuid"])
        ]
        assert not scheduler.can_dispatch_task_materials(waiting_task["uuid"])

        blocked_task = service.get_workflow_task(waiting_task["uuid"])
        blocked_events = service.list_events(after_id=0)["items"]
        blocked_outbox = inventory.read_outbox(after_sequence=0, limit=100)
        same_blocked = scheduler.reconcile_task_admission(waiting_task["uuid"])

        assert same_blocked == blocked
        assert service.get_workflow_task(waiting_task["uuid"]) == blocked_task
        assert service.list_events(after_id=0)["items"] == blocked_events
        assert inventory.read_outbox(after_sequence=0, limit=100) == blocked_outbox

        scheduler.reconcile_task_release(
            owner_task["uuid"],
            "workflow_task_terminal",
        )
        admitted = scheduler.reconcile_task_admission(waiting_task["uuid"])

        assert admitted is not None and admitted.status == "admitted"
        assert admitted.command_uuid == blocked.command_uuid
        assert admitted.outbox_sequence > blocked.outbox_sequence
        assert service.get_material_admission(waiting_task["uuid"])["status"] == (
            "admitted"
        )
        assert service.get_workflow_task(waiting_task["uuid"])["status"] == "pending"
        upgraded_job = _jobs_by_node(service, waiting_task["uuid"])[SOURCE_A_UUID]
        assert upgraded_job["status"] == "succeeded"
        assert upgraded_job["return_info"] == {
            "material": {
                "uuid": LOW_SITE_MATERIAL_UUID,
                "resource_template_uuid": SAMPLE_TEMPLATE_UUID,
            }
        }
        assert scheduler.can_dispatch_task_materials(waiting_task["uuid"])
    finally:
        if service is not None:
            service.close()
        inventory.close()
