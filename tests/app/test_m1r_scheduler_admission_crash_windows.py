"""M1R EdgeScheduler admission saga 的 W1/W2 public RED。

WorkflowStore/TemplateCatalog 仅装配当前 composition 尚不能公开创建的
MaterialSource graph；全部 Task、Job、event、Inventory 与 replay 断言都通过
WorkflowService、InventoryService 和 EdgeScheduler 的 public seam 完成。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

import unilabos.app.scheduler.inventory as inventory_api
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.catalog import (
    CatalogAuthority,
    NodeTemplateImport,
    TemplateCatalog,
)
from unilabos.workflow.models import WorkflowNodeWrite
from unilabos.workflow.runtime import WorkflowRuntimeCoordinator
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "10000000-0000-4000-8000-000000000203"
MATERIAL_SOURCE_NODE_UUID = "20000000-0000-4000-8000-000000000203"
MATERIAL_SOURCE_TEMPLATE_UUID = "30000000-0000-4000-8000-000000000203"
MATERIAL_HANDLE_UUID = "40000000-0000-4000-8000-000000000203"
MOUNT_MATERIAL_UUID = "5aa00000-0000-4000-8000-000000000203"
SAMPLE_MATERIAL_UUID = "5bb00000-0000-4000-8000-000000000203"
MOUNT_TEMPLATE_UUID = "2aa00000-0000-4000-8000-000000000203"
SAMPLE_TEMPLATE_UUID = "2bb00000-0000-4000-8000-000000000203"
SITE_UUID = "6aa00000-0000-4000-8000-000000000203"
NO_MATERIAL_WORKFLOW_UUID = "10000000-0000-4000-8000-000000000204"
ALIAS_TASK_UUID = "abcdefab-cdef-4abc-8def-abcdefabcdef"

AUTHORITY = CatalogAuthority(authority_id="m1r-saga", kind="backend")


class _InjectedCrash(RuntimeError):
    """模拟进程在一个 durable commit 边界退出。"""


class _CrashAt:
    def __init__(self, stage: str) -> None:
        self._stage = stage
        self.observed: list[str] = []

    def __call__(self, stage: str) -> None:
        self.observed.append(stage)
        if stage == self._stage:
            raise _InjectedCrash(stage)


class _RecordingInventoryPort:
    """记录 Scheduler 提交的 closed command，其余调用委托给真实 Inventory。"""

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


class _LockOrderProbeInventoryPort(_RecordingInventoryPort):
    """Prove an unrelated Task can enter Scheduler while Inventory is active."""

    def __init__(
        self,
        inventory: inventory_api.InventoryService,
        no_material_task_uuid: str,
    ) -> None:
        super().__init__(inventory)
        self.no_material_task_uuid = no_material_task_uuid
        self.scheduler: EdgeScheduler | None = None
        self.parallel_result: list[object] = []

    def admit_task(
        self,
        command: inventory_api.TaskMaterialAdmissionCommand,
    ) -> inventory_api.TaskMaterialAdmissionResult:
        assert self.scheduler is not None

        def enter_unrelated_task() -> None:
            try:
                self.parallel_result.append(
                    self.scheduler.reconcile_task_admission(self.no_material_task_uuid)
                )
            except Exception as error:  # noqa: BLE001 - surfaced in caller thread
                self.parallel_result.append(error)

        thread = threading.Thread(target=enter_unrelated_task)
        thread.start()
        thread.join(timeout=1)
        assert not thread.is_alive(), "Scheduler mutex was held across Inventory I/O"
        assert self.parallel_result == [None]
        return super().admit_task(command)


class _StalePendingWorkflowPort:
    """Return one already-captured pending page, then delegate latest reads."""

    def __init__(self, service: WorkflowService, stale_task: dict[str, Any]) -> None:
        self._service = service
        self._stale_task = stale_task
        self._served = False

    def list_workflow_tasks(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("status") == "pending" and not self._served:
            self._served = True
            return {"items": [self._stale_task], "total": 1}
        return self._service.list_workflow_tasks(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._service, name)


class _AliasReleaseDuringAdmissionPort(_RecordingInventoryPort):
    """Terminalize through canonical UUID while alias admission owns its slot."""

    def __init__(
        self,
        inventory: inventory_api.InventoryService,
        service: WorkflowService,
        workflow_database: Path,
        task_uuid: str,
    ) -> None:
        super().__init__(inventory)
        self._service = service
        self._workflow_database = workflow_database
        self._task_uuid = task_uuid
        self.scheduler: EdgeScheduler | None = None
        self.release_finished_before_admission = False
        self.release_outcome: list[object] = []
        self.release_thread: threading.Thread | None = None
        self.admission_result: inventory_api.TaskMaterialAdmissionResult | None = None

    def admit_task(
        self,
        command: inventory_api.TaskMaterialAdmissionCommand,
    ) -> inventory_api.TaskMaterialAdmissionResult:
        assert self.scheduler is not None
        _cancel_task(
            self._service,
            self._workflow_database,
            self._task_uuid,
            idempotency_key="terminal-during-alias-admission",
        )
        release_finished = threading.Event()

        def release_with_canonical_uuid() -> None:
            try:
                self.release_outcome.append(
                    self.scheduler.reconcile_task_material_state(self._task_uuid)
                )
            except Exception as error:  # noqa: BLE001 - asserted by caller
                self.release_outcome.append(error)
            finally:
                release_finished.set()

        self.release_thread = threading.Thread(target=release_with_canonical_uuid)
        self.release_thread.start()
        self.release_finished_before_admission = release_finished.wait(timeout=0.25)
        self.admission_result = super().admit_task(command)
        return self.admission_result


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
            "description": "M1R MaterialSource resolution",
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


def _material_source_node() -> WorkflowNodeWrite:
    return WorkflowNodeWrite(
        uuid=MATERIAL_SOURCE_NODE_UUID,
        workflow_node_template_uuid=MATERIAL_SOURCE_TEMPLATE_UUID,
        name="Resolve existing sample",
        status="idle",
        type="material_source",
        pose={},
        param={
            "mode": "existing",
            "resource_template_uuid": SAMPLE_TEMPLATE_UUID,
            "mount": {"uuid": MOUNT_MATERIAL_UUID},
            "material_uuid": SAMPLE_MATERIAL_UUID,
            "site": SITE_UUID,
            "slot_range": None,
            "flow_role": "primary_sample",
        },
        execution_policy={},
        disabled=False,
        minimized=False,
        meta_data={},
    )


def _seed_inventory(inventory: inventory_api.InventoryService) -> None:
    inventory.create_material(
        material_uuid=MOUNT_MATERIAL_UUID,
        resource_template_uuid=MOUNT_TEMPLATE_UUID,
        barcode="M1R-DECK-203",
        name="M1R saga deck",
    )
    inventory.create_material(
        material_uuid=SAMPLE_MATERIAL_UUID,
        resource_template_uuid=SAMPLE_TEMPLATE_UUID,
        barcode="M1R-SAMPLE-203",
        name="M1R saga sample",
    )
    inventory.create_site(
        site_uuid=SITE_UUID,
        description="M1R saga position",
        meta_data={"slot": "A1"},
        material_uuid=MOUNT_MATERIAL_UUID,
        name="A1",
        sort_order=0,
        allowed_resource_template_uuids=[SAMPLE_TEMPLATE_UUID],
        occupied_material_uuid=SAMPLE_MATERIAL_UUID,
        position_x=0.0,
        position_y=0.0,
        position_z=0.0,
        depth=1.0,
        length=1.0,
        width=1.0,
    )


def _create_pending_task(
    workflow_database: Path,
) -> tuple[WorkflowService, dict[str, Any]]:
    workflow_database.parent.mkdir(parents=True, exist_ok=True)
    store = WorkflowStore(workflow_database)
    TemplateCatalog(store).replace(AUTHORITY, [_material_source_import()])
    service = WorkflowService(store)
    service.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="M1R admission crash windows",
        tags=[],
        description=None,
        meta_data={},
    )
    # composition 后继 gap：当前 public authoring 没有独立 MaterialSource fixture
    # 装配入口；测试行为仍从 public create_workflow_task 开始。
    store.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[_material_source_node()],
        edges=[],
    )
    task = service.create_workflow_task(
        workflow_uuid=WORKFLOW_UUID,
        run_mode="normal",
        target_node_uuid=None,
        input_value={},
        description=None,
        meta_data={},
    )
    return service, task


def _open_workflow_service(workflow_database: Path) -> WorkflowService:
    return WorkflowService(WorkflowStore(workflow_database))


def _cancel_task(
    service: WorkflowService,
    workflow_database: Path,
    task_uuid: str,
    *,
    idempotency_key: str,
) -> None:
    service.create_workflow_task_command(
        task_uuid,
        command_type="cancel",
        target_node_uuid=None,
        idempotency_key=idempotency_key,
        description=None,
        meta_data={},
    )
    runtime_store = WorkflowStore(workflow_database)
    try:
        consumed = WorkflowRuntimeCoordinator(runtime_store).consume_next_command(
            task_uuid
        )
        assert consumed is not None
    finally:
        runtime_store.close()
    assert service.get_workflow_task(task_uuid)["status"] == "canceled"


def _task_runtime_events(
    service: WorkflowService,
    task_uuid: str,
) -> tuple[dict[str, Any], ...]:
    return tuple(
        event
        for event in service.list_events(after_id=0, limit=1000)["items"]
        if event["event"] == "workflow.runtime.changed"
        and event["data"] == {"workflow_task_uuid": task_uuid}
    )


def _admission_events(
    inventory: inventory_api.InventoryService,
    *,
    task_uuid: str,
) -> tuple[inventory_api.InventoryEvent, ...]:
    return tuple(
        event
        for event in inventory.read_outbox(after_sequence=0, limit=1000)
        if event.event_type == "material_reservation.admitted"
        and event.payload["workflow_task_uuid"] == task_uuid
    )


def _assert_projected_binding(
    service: WorkflowService,
    task_uuid: str,
) -> tuple[dict[str, Any], ...]:
    jobs = tuple(service.list_workflow_node_jobs(task_uuid))
    assert len(jobs) == 1
    assert jobs[0]["workflow_node_uuid"] == MATERIAL_SOURCE_NODE_UUID
    assert jobs[0]["executor_kind"] == "material_source"
    assert jobs[0]["status"] == "succeeded"
    assert jobs[0]["return_info"] == {
        "material": {
            "uuid": SAMPLE_MATERIAL_UUID,
            "resource_template_uuid": SAMPLE_TEMPLATE_UUID,
        }
    }
    return jobs


@pytest.mark.parametrize(
    "fault_stage",
    ["after_inventory_commit", "after_workflow_projection"],
    ids=["w1", "w2"],
)
def test_reconcile_task_admission_recovers_both_crash_windows(
    tmp_path: Path,
    fault_stage: str,
) -> None:
    workflow_database = tmp_path / "workflow-authority" / "workflow.db"
    inventory_dir = tmp_path / "inventory-authority"
    inventory = inventory_api.InventoryService.open(
        working_dir=inventory_dir,
        resource_templates=_resource_templates(),
    )
    service: WorkflowService | None = None
    try:
        _seed_inventory(inventory)
        service, task = _create_pending_task(workflow_database)
        task_uuid = task["uuid"]
        assert task["status"] == "pending"
        initial_jobs = tuple(service.list_workflow_node_jobs(task_uuid))
        assert len(initial_jobs) == 1
        assert initial_jobs[0]["status"] == "pending"
        assert initial_jobs[0]["return_info"] == {}
        assert _task_runtime_events(service, task_uuid) == ()

        recording_inventory = _RecordingInventoryPort(inventory)
        fault = _CrashAt(fault_stage)
        scheduler = EdgeScheduler(
            workflow_tasks=service,
            inventory=recording_inventory,
            admission_fault_hook=fault,
        )

        with pytest.raises(_InjectedCrash, match=fault_stage):
            scheduler.reconcile_task_admission(task_uuid)

        assert fault_stage in fault.observed
        assert len(recording_inventory.commands) == 1
        first_command = recording_inventory.commands[0]
        assert first_command.workflow_task_uuid == task_uuid
        assert first_command.sources == (
            inventory_api.TaskMaterialAdmissionSource(
                material_source_node_uuid=MATERIAL_SOURCE_NODE_UUID,
                mode="existing",
                resource_template_uuid=SAMPLE_TEMPLATE_UUID,
                mount={"uuid": MOUNT_MATERIAL_UUID},
                material_uuid=SAMPLE_MATERIAL_UUID,
                site_uuid=SITE_UUID,
                candidate_site_uuids=(),
                flow_role="primary_sample",
            ),
        )

        committed = inventory.get_command_result(first_command.command_uuid)
        assert isinstance(committed, inventory_api.TaskMaterialAdmissionResult)
        assert committed.status == "admitted"
        assert committed.reservation_uuid is not None
        admission_events_at_crash = _admission_events(
            inventory,
            task_uuid=task_uuid,
        )
        assert len(admission_events_at_crash) == 1
        assert admission_events_at_crash[0].causation_id == first_command.command_uuid
        assert inventory.get_acknowledged_sequence() == 0
        assert inventory.has_active_task_reservation(
            task_uuid,
            committed.reservation_uuid,
        )

        jobs_at_crash = tuple(service.list_workflow_node_jobs(task_uuid))
        events_at_crash = _task_runtime_events(service, task_uuid)
        if fault_stage == "after_inventory_commit":
            assert jobs_at_crash == initial_jobs
            assert events_at_crash == ()
        else:
            _assert_projected_binding(service, task_uuid)
            assert len(events_at_crash) == 1
    finally:
        if service is not None:
            service.close()
        inventory.close()

    reopened_inventory = inventory_api.InventoryService.open(
        working_dir=inventory_dir,
        resource_templates=_resource_templates(),
    )
    reopened_service = _open_workflow_service(workflow_database)
    replay_inventory = _RecordingInventoryPort(reopened_inventory)
    try:
        scheduler = EdgeScheduler(
            workflow_tasks=reopened_service,
            inventory=replay_inventory,
        )

        recovered = scheduler.reconcile_task_admission(task_uuid)

        assert replay_inventory.commands == [first_command]
        assert recovered == committed
        assert reopened_inventory.get_command_result(first_command.command_uuid) == (
            committed
        )
        assert (
            _admission_events(
                reopened_inventory,
                task_uuid=task_uuid,
            )
            == admission_events_at_crash
        )
        assert reopened_inventory.has_active_task_reservation(
            task_uuid,
            committed.reservation_uuid,
        )
        assert (
            reopened_inventory.get_acknowledged_sequence() == committed.outbox_sequence
        )
        projected_jobs = _assert_projected_binding(reopened_service, task_uuid)
        projected_events = _task_runtime_events(reopened_service, task_uuid)
        assert len(projected_events) == 1
        assert reopened_service.get_workflow_task(task_uuid)["status"] == "pending"
        if fault_stage == "after_workflow_projection":
            assert projected_jobs == jobs_at_crash
            assert projected_events == events_at_crash
    finally:
        reopened_service.close()
        reopened_inventory.close()


def test_scheduler_does_not_hold_its_serialization_mutex_during_inventory_io(
    tmp_path: Path,
) -> None:
    inventory = inventory_api.InventoryService.open(
        working_dir=tmp_path / "inventory-authority",
        resource_templates=_resource_templates(),
    )
    service = None
    try:
        _seed_inventory(inventory)
        service, material_task = _create_pending_task(
            tmp_path / "workflow-authority" / "workflow.db"
        )
        service.create_workflow(
            workflow_uuid=NO_MATERIAL_WORKFLOW_UUID,
            name="No Material lock-order probe",
            tags=[],
            description=None,
            meta_data={},
        )
        no_material_task = service.create_workflow_task(
            workflow_uuid=NO_MATERIAL_WORKFLOW_UUID,
            run_mode="normal",
            target_node_uuid=None,
            input_value={},
            description=None,
            meta_data={},
        )
        inventory_port = _LockOrderProbeInventoryPort(
            inventory,
            no_material_task["uuid"],
        )
        scheduler = EdgeScheduler(workflow_tasks=service, inventory=inventory_port)
        inventory_port.scheduler = scheduler

        admitted = scheduler.reconcile_task_admission(material_task["uuid"])

        assert admitted is not None
        assert admitted.status == "admitted"
        assert inventory_port.parallel_result == [None]
    finally:
        if service is not None:
            service.close()
        inventory.close()


def test_direct_admission_rechecks_terminal_task_before_inventory_commit(
    tmp_path: Path,
) -> None:
    workflow_database = tmp_path / "workflow-authority" / "workflow.db"
    inventory = inventory_api.InventoryService.open(
        working_dir=tmp_path / "inventory-authority",
        resource_templates=_resource_templates(),
    )
    service = None
    try:
        _seed_inventory(inventory)
        service, task = _create_pending_task(workflow_database)
        task_uuid = task["uuid"]
        _cancel_task(
            service,
            workflow_database,
            task_uuid,
            idempotency_key="terminal-before-direct-admission",
        )
        scheduler = EdgeScheduler(workflow_tasks=service, inventory=inventory)
        first_release = scheduler.reconcile_task_material_state(task_uuid)
        assert isinstance(first_release, inventory_api.TaskMaterialReleaseResult)
        assert first_release.reservation_uuid is None

        assert scheduler.reconcile_task_admission(task_uuid) is None

        assert _admission_events(inventory, task_uuid=task_uuid) == ()
        assert scheduler.reconcile_task_material_state(task_uuid) == first_release
    finally:
        if service is not None:
            service.close()
        inventory.close()


def test_pending_scan_routes_stale_item_through_latest_terminal_state(
    tmp_path: Path,
) -> None:
    workflow_database = tmp_path / "workflow-authority" / "workflow.db"
    inventory = inventory_api.InventoryService.open(
        working_dir=tmp_path / "inventory-authority",
        resource_templates=_resource_templates(),
    )
    service = None
    try:
        _seed_inventory(inventory)
        service, task = _create_pending_task(workflow_database)
        _cancel_task(
            service,
            workflow_database,
            task["uuid"],
            idempotency_key="terminal-after-pending-page",
        )
        stale_workflow = _StalePendingWorkflowPort(service, task)
        scheduler = EdgeScheduler(workflow_tasks=stale_workflow, inventory=inventory)

        assert scheduler.reconcile_pending_task_admissions() == (task["uuid"],)

        release = service.get_material_release(task["uuid"])
        assert release is not None
        assert release["status"] == "released"
        assert release["reservation_uuid"] is None
        assert service.get_material_admission(task["uuid"]) is None
        assert _admission_events(inventory, task_uuid=task["uuid"]) == ()
    finally:
        if service is not None:
            service.close()
        inventory.close()


def test_equivalent_uuid_spelling_shares_one_task_saga_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_database = tmp_path / "workflow-authority" / "workflow.db"
    inventory = inventory_api.InventoryService.open(
        working_dir=tmp_path / "inventory-authority",
        resource_templates=_resource_templates(),
    )
    service = None
    try:
        _seed_inventory(inventory)
        with monkeypatch.context() as patch:
            patch.setattr(
                "unilabos.workflow.service.uuid4",
                lambda: UUID(ALIAS_TASK_UUID),
            )
            service, task = _create_pending_task(workflow_database)
        assert task["uuid"] == ALIAS_TASK_UUID
        inventory_port = _AliasReleaseDuringAdmissionPort(
            inventory,
            service,
            workflow_database,
            task["uuid"],
        )
        scheduler = EdgeScheduler(workflow_tasks=service, inventory=inventory_port)
        inventory_port.scheduler = scheduler

        with pytest.raises(WorkflowError):
            scheduler.reconcile_task_admission(task["uuid"].upper())

        assert inventory_port.release_thread is not None
        inventory_port.release_thread.join(timeout=1)
        assert not inventory_port.release_thread.is_alive()
        assert not inventory_port.release_finished_before_admission
        admission = inventory_port.admission_result
        assert admission is not None
        assert admission.status == "admitted"
        assert admission.reservation_uuid is not None
        assert len(inventory_port.release_outcome) == 1
        release = inventory_port.release_outcome[0]
        assert isinstance(release, inventory_api.TaskMaterialReleaseResult)
        assert release.reservation_uuid == admission.reservation_uuid
        assert not inventory.has_active_task_reservation(
            task["uuid"],
            admission.reservation_uuid,
        )
    finally:
        if service is not None:
            service.close()
        inventory.close()
