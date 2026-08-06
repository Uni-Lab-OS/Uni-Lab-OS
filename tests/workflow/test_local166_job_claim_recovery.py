"""LOCAL-166 占用重启、不确定派发与物理结算回归。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from tests.workflow.test_local166_unified_job_claim import (
    DEVICE_MATERIAL_UUID,
    _acquire_d1a_claim,
    _inventory,
    _RecordingDispatcher,
    _wait,
    _workflow_task,
)
from unilabos.app.scheduler.inventory import JobClaimAcquireCommand
from unilabos.workflow.runtime import WorkflowRuntimeCoordinator, WorkflowRuntimeWorker
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import StoreConflict, WorkflowStore


class _UncertainDispatcher(_RecordingDispatcher):
    def dispatch(self, payload: dict[str, Any]) -> None:
        super().dispatch(payload)
        raise RuntimeError("transport acknowledgement lost")


class _RejectingCancelDispatcher(_RecordingDispatcher):
    def request_cancel(self, _job_uuid: str) -> bool:
        return False


def test_claim_projection_conflict_retries_same_attempt_without_send(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store)
    inventory = _inventory(tmp_path)
    _task, job = _workflow_task(service)
    dispatcher = _RecordingDispatcher()
    worker = WorkflowRuntimeWorker(
        WorkflowRuntimeCoordinator(store),
        dispatcher=dispatcher,
        device_identity_resolver=lambda _identity: "stirrer",
        inventory=inventory,
        poll_interval_seconds=0.01,
    )
    claim_execution = worker._job_claim_execution
    assert claim_execution is not None
    prepare_dispatch = claim_execution._claims.prepare_dispatch

    def reject_projection(**_kwargs: Any) -> str:
        raise StoreConflict("transient workflow projection conflict")

    claim_execution._claims.prepare_dispatch = reject_projection
    try:
        worker.start()
        _wait(lambda: len(inventory.list_unsettled_claims()) == 1)
        observed = service.get_workflow_node_job(job["uuid"])
        assert observed["status"] == "pending"
        assert observed["attempt"] == 1
        assert dispatcher.payloads == []

        claim_execution._claims.prepare_dispatch = prepare_dispatch
        _wait(lambda: len(dispatcher.payloads) == 1)
        replayed = service.get_workflow_node_job(job["uuid"])
        assert replayed["attempt"] == 1
        assert replayed["claim_uuid"] == inventory.get_job_claim(job["uuid"], 1).uuid
    finally:
        worker.stop()
        worker.join(timeout=1)
        service.close()
        inventory.close()


def test_restart_replays_claim_committed_before_dispatch_intent(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store)
    inventory = _inventory(tmp_path)
    task, job = _workflow_task(service)
    acquired = inventory.acquire_job_claim(
        JobClaimAcquireCommand(
            schema_version=1,
            command_uuid=str(uuid4()),
            idempotency_key=f"local166:crash-after-claim:{job['uuid']}",
            workflow_task_uuid=task["uuid"],
            workflow_node_job_uuid=job["uuid"],
            attempt=1,
            device_material_uuid=DEVICE_MATERIAL_UUID,
            mutable_material_root_uuids=(),
            occupancy_changing_site_uuids=(),
        )
    )
    dispatcher = _RecordingDispatcher()
    worker = WorkflowRuntimeWorker(
        WorkflowRuntimeCoordinator(store),
        dispatcher=dispatcher,
        device_identity_resolver=lambda _identity: "stirrer",
        inventory=inventory,
        poll_interval_seconds=0.01,
    )
    try:
        assert acquired.status == "acquired"
        worker.start()
        _wait(lambda: len(dispatcher.payloads) == 1)

        observed = service.get_workflow_node_job(job["uuid"])
        assert observed["attempt"] == 1
        assert observed["claim_uuid"] == acquired.claim.uuid
        assert observed["edge_command_uuid"]
        assert len(inventory.list_unsettled_claims()) == 1
    finally:
        worker.stop()
        worker.join(timeout=1)
        service.close()
        inventory.close()


def test_dispatch_uncertainty_keeps_claim_and_blocks_d1a(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store)
    inventory = _inventory(tmp_path)
    _task, job = _workflow_task(service)
    dispatcher = _UncertainDispatcher()
    worker = WorkflowRuntimeWorker(
        WorkflowRuntimeCoordinator(store),
        dispatcher=dispatcher,
        device_identity_resolver=lambda _identity: "stirrer",
        inventory=inventory,
        poll_interval_seconds=0.01,
    )
    try:
        worker.start()
        _wait(
            lambda: (
                service.get_workflow_node_job(job["uuid"])["status"]
                == "execution_unknown"
            )
        )
        claim = inventory.get_job_claim(job["uuid"], 1)
        blocked = _acquire_d1a_claim(
            inventory,
            task_uuid=str(uuid4()),
            job_uuid=str(uuid4()),
        )

        observed = service.get_workflow_node_job(job["uuid"])
        assert claim.state == "uncertain"
        assert observed["claim_status"] == "unknown"
        assert blocked.status == "blocked"
        assert blocked.diagnostics[0]["blocking_claim_uuid"] == claim.uuid
        assert len(dispatcher.payloads) == 1
    finally:
        worker.stop()
        worker.join(timeout=1)
        service.close()
        inventory.close()


def test_unconfirmed_cancel_keeps_uncertain_claim(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store)
    inventory = _inventory(tmp_path)
    task, job = _workflow_task(service)
    dispatcher = _RejectingCancelDispatcher()
    worker = WorkflowRuntimeWorker(
        WorkflowRuntimeCoordinator(store),
        dispatcher=dispatcher,
        device_identity_resolver=lambda _identity: "stirrer",
        inventory=inventory,
        poll_interval_seconds=0.01,
    )
    try:
        worker.start()
        _wait(lambda: len(dispatcher.payloads) == 1)
        service.create_workflow_task_command(
            task["uuid"],
            command_type="cancel",
            target_node_uuid=None,
            idempotency_key="local166-cancel",
            description="operator cancel",
            meta_data={},
        )
        _wait(
            lambda: (
                service.get_workflow_node_job(job["uuid"])["status"]
                == "execution_unknown"
            )
        )

        claim = inventory.get_job_claim(job["uuid"], 1)
        assert claim.state == "uncertain"
        assert service.get_workflow_node_job(job["uuid"])["claim_status"] == ("unknown")
        assert inventory.list_unsettled_claims() == (claim,)
    finally:
        worker.stop()
        worker.join(timeout=1)
        service.close()
        inventory.close()


def test_restart_marks_dispatched_claim_uncertain_without_replay(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workflow.db"
    first_store = WorkflowStore(database)
    first_service = WorkflowService(first_store)
    inventory = _inventory(tmp_path)
    _task, job = _workflow_task(first_service)
    first_dispatcher = _RecordingDispatcher()
    first_worker = WorkflowRuntimeWorker(
        WorkflowRuntimeCoordinator(first_store),
        dispatcher=first_dispatcher,
        device_identity_resolver=lambda _identity: "stirrer",
        inventory=inventory,
        poll_interval_seconds=0.01,
    )
    first_worker.start()
    _wait(lambda: len(first_dispatcher.payloads) == 1)
    first_worker.stop()
    first_worker.join(timeout=1)
    first_service.close()

    reopened_store = WorkflowStore(database)
    reopened_service = WorkflowService(reopened_store)
    coordinator = WorkflowRuntimeCoordinator(reopened_store)
    coordinator.recover_startup()
    replacement_dispatcher = _RecordingDispatcher()
    replacement = WorkflowRuntimeWorker(
        coordinator,
        dispatcher=replacement_dispatcher,
        device_identity_resolver=lambda _identity: "stirrer",
        inventory=inventory,
        poll_interval_seconds=0.01,
    )
    try:
        replacement.start()
        _wait(
            lambda: (
                reopened_service.get_workflow_node_job(job["uuid"])["claim_status"]
                == "unknown"
            )
        )
        claim = inventory.get_job_claim(job["uuid"], 1)
        blocked = _acquire_d1a_claim(
            inventory,
            task_uuid=str(uuid4()),
            job_uuid=str(uuid4()),
        )

        assert claim.state == "uncertain"
        assert reopened_service.get_workflow_node_job(job["uuid"])["status"] == (
            "execution_unknown"
        )
        assert replacement_dispatcher.payloads == []
        assert blocked.status == "blocked"
        assert blocked.diagnostics[0]["blocking_claim_uuid"] == claim.uuid
    finally:
        replacement.stop()
        replacement.join(timeout=1)
        reopened_service.close()
        inventory.close()


def test_terminal_projection_without_physical_receipt_stays_fenced(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workflow.db"
    first_store = WorkflowStore(database)
    first_service = WorkflowService(first_store)
    inventory = _inventory(tmp_path)
    _task, job = _workflow_task(first_service)
    dispatcher = _RecordingDispatcher()
    first_coordinator = WorkflowRuntimeCoordinator(first_store)
    first_worker = WorkflowRuntimeWorker(
        first_coordinator,
        dispatcher=dispatcher,
        device_identity_resolver=lambda _identity: "stirrer",
        inventory=inventory,
        poll_interval_seconds=0.01,
    )
    first_worker.start()
    _wait(lambda: len(dispatcher.payloads) == 1)
    first_worker.stop()
    first_worker.join(timeout=1)
    first_coordinator.transition_job(
        job["uuid"],
        "failed",
        error_info=[{"code": "terminal_projection_without_receipt"}],
    )
    first_service.close()

    reopened_store = WorkflowStore(database)
    reopened_service = WorkflowService(reopened_store)
    replacement = WorkflowRuntimeWorker(
        WorkflowRuntimeCoordinator(reopened_store),
        dispatcher=_RecordingDispatcher(),
        device_identity_resolver=lambda _identity: "stirrer",
        inventory=inventory,
        poll_interval_seconds=0.01,
    )
    try:
        claim = inventory.get_job_claim(job["uuid"], 1)
        observed = reopened_service.get_workflow_node_job(job["uuid"])
        assert claim.state == "uncertain"
        assert observed["claim_status"] == "unknown"
        assert inventory.list_unsettled_claims() == (claim,)
    finally:
        replacement.stop()
        reopened_service.close()
        inventory.close()


def test_physical_terminal_releases_claim_before_next_admission(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store)
    inventory = _inventory(tmp_path)
    _task, job = _workflow_task(service)
    dispatcher = _RecordingDispatcher()
    worker = WorkflowRuntimeWorker(
        WorkflowRuntimeCoordinator(store),
        dispatcher=dispatcher,
        device_identity_resolver=lambda _identity: "stirrer",
        inventory=inventory,
        poll_interval_seconds=0.01,
    )
    try:
        worker.start()
        _wait(lambda: len(dispatcher.payloads) == 1)
        assert inventory.get_job_claim(job["uuid"], 1).state == "reserved"

        dispatcher.listeners[0](job["uuid"], True, {"completed": True}, "normal")
        _wait(
            lambda: (
                service.get_workflow_node_job(job["uuid"])["claim_status"] == "released"
            )
        )
        admitted = _acquire_d1a_claim(
            inventory,
            task_uuid=str(uuid4()),
            job_uuid=str(uuid4()),
        )

        assert service.get_workflow_node_job(job["uuid"])["status"] == "succeeded"
        assert inventory.get_job_claim(job["uuid"], 1).state == "released"
        assert admitted.status == "acquired"
    finally:
        worker.stop()
        worker.join(timeout=1)
        service.close()
        inventory.close()


def test_restart_replays_terminal_settlement_before_unlocking_device(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workflow.db"
    first_store = WorkflowStore(database)
    first_service = WorkflowService(first_store)
    inventory = _inventory(tmp_path)
    _task, job = _workflow_task(first_service)
    dispatcher = _RecordingDispatcher()
    first_worker = WorkflowRuntimeWorker(
        WorkflowRuntimeCoordinator(first_store),
        dispatcher=dispatcher,
        device_identity_resolver=lambda _identity: "stirrer",
        inventory=inventory,
        poll_interval_seconds=0.01,
    )
    first_worker.start()
    _wait(lambda: len(dispatcher.payloads) == 1)
    claim_execution = first_worker._job_claim_execution
    assert claim_execution is not None

    def crash_before_release(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("process crashed after Workflow terminal commit")

    claim_execution.release_terminal = crash_before_release  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="after Workflow terminal"):
        dispatcher.listeners[0](job["uuid"], True, {"completed": True}, "normal")
    assert first_service.get_workflow_node_job(job["uuid"])["status"] == "succeeded"
    assert inventory.get_job_claim(job["uuid"], 1).state == "running"
    first_worker.stop()
    first_worker.join(timeout=1)
    first_service.close()

    reopened_store = WorkflowStore(database)
    reopened_service = WorkflowService(reopened_store)
    coordinator = WorkflowRuntimeCoordinator(reopened_store)
    coordinator.recover_startup()
    replacement = WorkflowRuntimeWorker(
        coordinator,
        dispatcher=_RecordingDispatcher(),
        device_identity_resolver=lambda _identity: "stirrer",
        inventory=inventory,
        poll_interval_seconds=0.01,
    )
    try:
        assert inventory.get_job_claim(job["uuid"], 1).state == "released"
        assert (
            reopened_service.get_workflow_node_job(job["uuid"])["claim_status"]
            == "released"
        )
        admitted = _acquire_d1a_claim(
            inventory,
            task_uuid=str(uuid4()),
            job_uuid=str(uuid4()),
        )
        assert admitted.status == "acquired"
    finally:
        replacement.stop()
        reopened_service.close()
        inventory.close()
