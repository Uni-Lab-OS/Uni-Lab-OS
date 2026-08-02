"""M1R dispatch proof 与 terminal release W1 的 public RED。

复用 admission saga 的 graph fixture；全部行为断言只经过 WorkflowService、
InventoryService 与 EdgeScheduler，不调用 Store、SQLite 或 DAG dispatch。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import unilabos.app.scheduler.inventory as inventory_api
from tests.app.test_m1r_scheduler_admission_crash_windows import (
    _CrashAt,
    _create_pending_task,
    _InjectedCrash,
    _open_workflow_service,
    _resource_templates,
    _seed_inventory,
    _task_runtime_events,
)
from unilabos.app.scheduler.service import EdgeScheduler


class _RecordingReleaseInventoryPort:
    """记录 closed release command，其余调用委托给真实 Inventory。"""

    def __init__(self, inventory: inventory_api.InventoryService) -> None:
        self._inventory = inventory
        self.commands: list[inventory_api.TaskMaterialReleaseCommand] = []

    def release_task(
        self,
        command: inventory_api.TaskMaterialReleaseCommand,
    ) -> inventory_api.TaskMaterialReleaseResult:
        self.commands.append(command)
        return self._inventory.release_task(command)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inventory, name)


def _projected_admission(
    *,
    task_uuid: str,
    result: inventory_api.TaskMaterialAdmissionResult,
) -> dict[str, Any]:
    return {
        "workflow_task_uuid": task_uuid,
        "command_uuid": result.command_uuid,
        "status": result.status,
        "reservation_uuid": result.reservation_uuid,
        "outbox_sequence": result.outbox_sequence,
    }


def _release_events(
    inventory: inventory_api.InventoryService,
    *,
    task_uuid: str,
) -> tuple[inventory_api.InventoryEvent, ...]:
    return tuple(
        event
        for event in inventory.read_outbox(after_sequence=0, limit=1000)
        if event.event_type == "material_reservation.released"
        and event.payload["workflow_task_uuid"] == task_uuid
    )


def test_dispatch_proof_and_release_w1_recover_through_public_services(
    tmp_path: Path,
) -> None:
    workflow_database = tmp_path / "workflow-authority" / "workflow.db"
    inventory_dir = tmp_path / "inventory-authority"
    inventory = inventory_api.InventoryService.open(
        working_dir=inventory_dir,
        resource_templates=_resource_templates(),
    )
    service = None
    try:
        _seed_inventory(inventory)
        service, admitted_task = _create_pending_task(workflow_database)
        blocked_task = service.create_workflow_task(
            workflow_uuid=admitted_task["workflow_uuid"],
            run_mode="normal",
            target_node_uuid=None,
            input_value={},
            description=None,
            meta_data={},
        )
        scheduler = EdgeScheduler(
            workflow_tasks=service,
            inventory=inventory,
        )

        assert not scheduler.can_dispatch_task_materials(admitted_task["uuid"])
        admitted = scheduler.reconcile_task_admission(admitted_task["uuid"])

        assert admitted.status == "admitted"
        assert admitted.reservation_uuid is not None
        assert service.get_material_admission(admitted_task["uuid"]) == (
            _projected_admission(
                task_uuid=admitted_task["uuid"],
                result=admitted,
            )
        )
        assert scheduler.can_dispatch_task_materials(admitted_task["uuid"])

        blocked = scheduler.reconcile_task_admission(blocked_task["uuid"])

        assert blocked.status == "blocked"
        assert blocked.reservation_uuid is None
        assert service.get_material_admission(blocked_task["uuid"]) == (
            _projected_admission(
                task_uuid=blocked_task["uuid"],
                result=blocked,
            )
        )
        assert not scheduler.can_dispatch_task_materials(blocked_task["uuid"])

        workflow_events_before_release = _task_runtime_events(
            service,
            admitted_task["uuid"],
        )
        acknowledged_before_release = inventory.get_acknowledged_sequence()
        release_port = _RecordingReleaseInventoryPort(inventory)
        release_fault = _CrashAt("after_inventory_release_commit")
        release_scheduler = EdgeScheduler(
            workflow_tasks=service,
            inventory=release_port,
            admission_fault_hook=release_fault,
        )

        with pytest.raises(
            _InjectedCrash,
            match="after_inventory_release_commit",
        ):
            release_scheduler.reconcile_task_release(
                admitted_task["uuid"],
                "workflow_task_terminal",
            )

        assert release_fault.observed[-1] == "after_inventory_release_commit"
        assert len(release_port.commands) == 1
        first_release_command = release_port.commands[0]
        assert first_release_command.schema_version == 1
        assert first_release_command.workflow_task_uuid == admitted_task["uuid"]
        assert first_release_command.reason == "workflow_task_terminal"
        assert first_release_command.idempotency_key

        committed_release = inventory.get_command_result(
            first_release_command.command_uuid
        )
        assert isinstance(
            committed_release,
            inventory_api.TaskMaterialReleaseResult,
        )
        assert committed_release.status == "released"
        assert committed_release.reservation_uuid == admitted.reservation_uuid
        release_events_at_crash = _release_events(
            inventory,
            task_uuid=admitted_task["uuid"],
        )
        assert len(release_events_at_crash) == 1
        assert (
            release_events_at_crash[0].causation_id
            == first_release_command.command_uuid
        )
        assert inventory.get_acknowledged_sequence() == acknowledged_before_release
        assert not inventory.has_active_task_reservation(
            admitted_task["uuid"],
            admitted.reservation_uuid,
        )
        assert not release_scheduler.can_dispatch_task_materials(admitted_task["uuid"])
        assert (
            _task_runtime_events(service, admitted_task["uuid"])
            == workflow_events_before_release
        )
    finally:
        if service is not None:
            service.close()
        inventory.close()

    reopened_inventory = inventory_api.InventoryService.open(
        working_dir=inventory_dir,
        resource_templates=_resource_templates(),
    )
    reopened_service = _open_workflow_service(workflow_database)
    replay_port = _RecordingReleaseInventoryPort(reopened_inventory)
    try:
        replay_scheduler = EdgeScheduler(
            workflow_tasks=reopened_service,
            inventory=replay_port,
        )

        recovered_release = replay_scheduler.reconcile_task_release(
            admitted_task["uuid"],
            "workflow_task_terminal",
        )

        assert replay_port.commands == [first_release_command]
        assert recovered_release == committed_release
        assert (
            reopened_inventory.get_command_result(first_release_command.command_uuid)
            == committed_release
        )
        assert (
            _release_events(
                reopened_inventory,
                task_uuid=admitted_task["uuid"],
            )
            == release_events_at_crash
        )
        assert (
            reopened_inventory.get_acknowledged_sequence()
            == committed_release.outbox_sequence
        )
        assert not reopened_inventory.has_active_task_reservation(
            admitted_task["uuid"],
            admitted.reservation_uuid,
        )
        assert not replay_scheduler.can_dispatch_task_materials(admitted_task["uuid"])
        workflow_events_after_recovery = _task_runtime_events(
            reopened_service,
            admitted_task["uuid"],
        )
        assert len(workflow_events_after_recovery) == (
            len(workflow_events_before_release) + 1
        )
    finally:
        reopened_service.close()
        reopened_inventory.close()
