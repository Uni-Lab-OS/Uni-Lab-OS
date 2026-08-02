"""M1R terminal release W2 的 public crash/replay RED。"""

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
from tests.app.test_m1r_scheduler_release_dispatch_proof import (
    _RecordingReleaseInventoryPort,
    _release_events,
)
from unilabos.app.scheduler.service import EdgeScheduler


def _projected_release(
    *,
    task_uuid: str,
    result: inventory_api.TaskMaterialReleaseResult,
) -> dict[str, Any]:
    return {
        "workflow_task_uuid": task_uuid,
        "command_uuid": result.command_uuid,
        "status": result.status,
        "reservation_uuid": result.reservation_uuid,
        "outbox_sequence": result.outbox_sequence,
    }


def test_terminal_release_w2_replays_without_duplicate_projection_or_event(
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
        service, task = _create_pending_task(workflow_database)
        scheduler = EdgeScheduler(workflow_tasks=service, inventory=inventory)
        admission = scheduler.reconcile_task_admission(task["uuid"])

        assert scheduler.can_dispatch_task_materials(task["uuid"])
        events_before_release = _task_runtime_events(service, task["uuid"])
        acknowledged_before_release = inventory.get_acknowledged_sequence()
        release_port = _RecordingReleaseInventoryPort(inventory)
        release_fault = _CrashAt("after_workflow_release_projection")
        release_scheduler = EdgeScheduler(
            workflow_tasks=service,
            inventory=release_port,
            admission_fault_hook=release_fault,
        )

        with pytest.raises(
            _InjectedCrash,
            match="after_workflow_release_projection",
        ):
            release_scheduler.reconcile_task_release(
                task["uuid"],
                "workflow_task_terminal",
            )

        assert release_fault.observed[-1] == "after_workflow_release_projection"
        assert len(release_port.commands) == 1
        first_release_command = release_port.commands[0]
        committed_release = inventory.get_command_result(
            first_release_command.command_uuid
        )
        assert isinstance(
            committed_release,
            inventory_api.TaskMaterialReleaseResult,
        )
        assert committed_release.status == "released"
        assert committed_release.reservation_uuid == admission.reservation_uuid
        release_projection_at_crash = service.get_material_release(task["uuid"])
        assert release_projection_at_crash == _projected_release(
            task_uuid=task["uuid"],
            result=committed_release,
        )
        workflow_events_at_crash = _task_runtime_events(service, task["uuid"])
        assert len(workflow_events_at_crash) == len(events_before_release) + 1
        release_events_at_crash = _release_events(
            inventory,
            task_uuid=task["uuid"],
        )
        assert len(release_events_at_crash) == 1
        assert inventory.get_acknowledged_sequence() == acknowledged_before_release
        assert committed_release.outbox_sequence > acknowledged_before_release
        assert not inventory.has_active_task_reservation(
            task["uuid"],
            admission.reservation_uuid,
        )
        assert not release_scheduler.can_dispatch_task_materials(task["uuid"])
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
            task["uuid"],
            "workflow_task_terminal",
        )

        assert replay_port.commands == [first_release_command]
        assert recovered_release == committed_release
        assert reopened_service.get_material_release(task["uuid"]) == (
            release_projection_at_crash
        )
        assert (
            _task_runtime_events(reopened_service, task["uuid"])
            == workflow_events_at_crash
        )
        assert (
            _release_events(reopened_inventory, task_uuid=task["uuid"])
            == release_events_at_crash
        )
        assert (
            reopened_inventory.get_acknowledged_sequence()
            == committed_release.outbox_sequence
        )
        assert not reopened_inventory.has_active_task_reservation(
            task["uuid"],
            admission.reservation_uuid,
        )
        assert not replay_scheduler.can_dispatch_task_materials(task["uuid"])
    finally:
        reopened_service.close()
        reopened_inventory.close()
