"""Round R1B durable runtime kernel contracts through public seams."""

from __future__ import annotations

import asyncio
import importlib
import json
import multiprocessing
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow import composition
from unilabos.workflow.composition import (
    compose_workflow_runtime,
    get_workflow_service,
    reset_workflow_service_for_test,
)
from unilabos.workflow.models import WorkflowNodeWrite
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import StoreConflict, WorkflowStore

_TASK_ALLOWED = {
    "pending": {"admission_blocked", "running", "failed", "canceled"},
    "admission_blocked": {"pending", "failed", "canceled"},
    "running": {"succeeded", "failed", "canceling", "timeout"},
    "canceling": {"canceled", "failed", "timeout"},
    "succeeded": set(),
    "failed": set(),
    "canceled": set(),
    "timeout": set(),
}
_JOB_ALLOWED = {
    "pending": {"dispatched", "failed", "skipped", "canceled"},
    "dispatched": {
        "running",
        "cancel_requested",
        "succeeded",
        "failed",
        "canceled",
        "timeout",
        "execution_unknown",
    },
    "running": {
        "intervention_required",
        "cancel_requested",
        "succeeded",
        "failed",
        "canceled",
        "timeout",
        "execution_unknown",
    },
    "intervention_required": {
        "running",
        "cancel_requested",
        "failed",
        "timeout",
        "execution_unknown",
    },
    "cancel_requested": {
        "canceled",
        "failed",
        "timeout",
        "execution_unknown",
    },
    "execution_unknown": {"running", "succeeded", "failed", "canceled", "timeout"},
    "succeeded": set(),
    "failed": set(),
    "skipped": set(),
    "canceled": set(),
    "timeout": set(),
}
_TASK_ALLOWED_CASES = [
    (source, target)
    for source, targets in _TASK_ALLOWED.items()
    for target in sorted(targets)
]
_TASK_FORBIDDEN_CASES = [
    (source, target)
    for source, targets in _TASK_ALLOWED.items()
    for target in _TASK_ALLOWED
    if target not in targets
]
_JOB_ALLOWED_CASES = [
    (source, target)
    for source, targets in _JOB_ALLOWED.items()
    for target in sorted(targets)
]
_JOB_FORBIDDEN_CASES = [
    (source, target)
    for source, targets in _JOB_ALLOWED.items()
    for target in _JOB_ALLOWED
    if target not in targets
]
_TERMINAL_TASK = {"succeeded", "failed", "canceled", "timeout"}
_TERMINAL_JOB = {"succeeded", "failed", "skipped", "canceled", "timeout"}
_OBSERVED_AT = "2026-08-01T01:02:03Z"


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[WorkflowStore]:
    opened = WorkflowStore(tmp_path / "workflow.db")
    yield opened
    opened.close()


@pytest.fixture()
def service(store: WorkflowStore) -> WorkflowService:
    return WorkflowService(store)


@pytest.fixture()
def client(service: WorkflowService) -> Iterator[TestClient]:
    with TestClient(create_workflow_app(service)) as opened:
        yield opened


def _runtime_module() -> Any:
    return importlib.import_module("unilabos.workflow.runtime")


def _coordinator(store: WorkflowStore) -> Any:
    return _runtime_module().WorkflowRuntimeCoordinator(store)


def _node(index: int) -> WorkflowNodeWrite:
    return WorkflowNodeWrite(
        uuid=str(uuid4()),
        name=f"runtime-node-{index}",
        status="idle",
        type="compute",
        pose={},
        param={},
        execution_policy={},
        disabled=False,
        minimized=False,
        meta_data={},
    )


def _create_task(
    service: WorkflowService,
    *,
    node_count: int = 0,
    run_mode: str = "normal",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    workflow = service.create_workflow(
        name="R1B runtime kernel",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=str(uuid4()),
    )
    if node_count:
        service.save_graph(
            workflow["uuid"],
            revision=1,
            nodes=[_node(index) for index in range(node_count)],
            edges=[],
        )
    task = service.create_workflow_task(
        workflow_uuid=workflow["uuid"],
        run_mode=run_mode,
        target_node_uuid=None,
        input_value={},
        description=None,
        meta_data={},
    )
    return task, service.list_workflow_node_jobs(task["uuid"])


def _create_command(
    service: WorkflowService,
    task_uuid: str,
    command_type: str,
    key: str,
    *,
    target_node_uuid: str | None = None,
) -> dict[str, Any]:
    return service.create_workflow_task_command(
        task_uuid,
        command_type=command_type,
        target_node_uuid=target_node_uuid,
        idempotency_key=key,
        description=None,
        meta_data={},
    )


def _runtime_events(
    service: WorkflowService, *, after_id: int = 0
) -> list[dict[str, Any]]:
    return [
        event
        for event in service.list_events(after_id=after_id, limit=1000)["items"]
        if event["event"] == "workflow.runtime.changed"
    ]


def _rows(
    store: WorkflowStore, query: str, values: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    with store.transaction() as connection:
        return [dict(row) for row in connection.execute(query, values)]


def _table_count(store: WorkflowStore, table: str) -> int:
    return int(_rows(store, f"SELECT COUNT(*) AS count FROM {table}")[0]["count"])


def _journal(store: WorkflowStore, task_uuid: str) -> list[dict[str, Any]]:
    return _rows(
        store,
        """
        SELECT * FROM workflow_runtime_journal
        WHERE workflow_task_uuid = ? ORDER BY sequence
        """,
        (task_uuid,),
    )


def _transition_task(coordinator: Any, task_uuid: str, target: str) -> dict[str, Any]:
    if target == "running":
        return coordinator.start_task(task_uuid)
    return coordinator.transition_task(task_uuid, target)


def _task_in_status(
    service: WorkflowService,
    coordinator: Any,
    status: str,
) -> dict[str, Any]:
    task, _ = _create_task(service)
    if status == "pending":
        return task
    if status == "admission_blocked":
        return coordinator.transition_task(task["uuid"], "admission_blocked")
    if status == "running":
        return coordinator.start_task(task["uuid"])
    if status == "canceling":
        coordinator.start_task(task["uuid"])
        return coordinator.transition_task(task["uuid"], "canceling")
    if status == "canceled":
        return coordinator.transition_task(task["uuid"], "canceled")
    coordinator.start_task(task["uuid"])
    return coordinator.transition_task(task["uuid"], status)


def _transition_job(
    coordinator: Any,
    job_uuid: str,
    source: str,
    target: str,
) -> dict[str, Any]:
    if target == "execution_unknown":
        return coordinator.mark_job_unknown(job_uuid, "transport outcome unavailable")
    if source == "execution_unknown":
        return coordinator.resolve_job_uncertainty(
            job_uuid,
            target,
            reason="operator reconciled device",
        )
    return coordinator.transition_job(job_uuid, target)


def _job_in_status(
    service: WorkflowService,
    coordinator: Any,
    status: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    task, jobs = _create_task(service, node_count=1)
    coordinator.start_task(task["uuid"])
    job = jobs[0]
    if status == "pending":
        return task, job
    if status in {"failed", "skipped", "canceled"}:
        return task, coordinator.transition_job(job["uuid"], status)
    coordinator.transition_job(job["uuid"], "dispatched")
    if status == "dispatched":
        return task, service.get_workflow_node_job(job["uuid"])
    if status in {"succeeded", "failed", "canceled", "timeout"}:
        return task, coordinator.transition_job(job["uuid"], status)
    if status == "cancel_requested":
        return task, coordinator.transition_job(job["uuid"], status)
    coordinator.transition_job(job["uuid"], "running")
    if status == "running":
        return task, service.get_workflow_node_job(job["uuid"])
    if status == "intervention_required":
        return task, coordinator.transition_job(job["uuid"], status)
    if status == "execution_unknown":
        return task, coordinator.mark_job_unknown(job["uuid"], "lost transport")
    raise AssertionError(f"unsupported job setup status {status}")


@pytest.mark.parametrize(("source", "target"), _TASK_ALLOWED_CASES)
def test_each_allowed_task_transition_persists_timestamps_and_one_invalidation(
    service: WorkflowService,
    store: WorkflowStore,
    source: str,
    target: str,
) -> None:
    coordinator = _coordinator(store)
    task = _task_in_status(service, coordinator, source)
    cursor = (
        service.list_events(after_id=0)["items"][-1]["id"] if source != "pending" else 0
    )

    updated = _transition_task(coordinator, task["uuid"], target)

    assert updated["status"] == target
    if target == "running":
        datetime.fromisoformat(updated["started_at"].replace("Z", "+00:00"))
        assert "finished_at" not in updated
    if target in _TERMINAL_TASK:
        datetime.fromisoformat(updated["finished_at"].replace("Z", "+00:00"))
    if source == "running" and "started_at" in task:
        assert updated["started_at"] == task["started_at"]
    events = _runtime_events(service, after_id=cursor)
    assert len(events) == 1
    assert events[0]["data"] == {"workflow_task_uuid": task["uuid"]}


@pytest.mark.parametrize(("source", "target"), _TASK_FORBIDDEN_CASES)
def test_each_forbidden_task_transition_is_zero_write(
    service: WorkflowService,
    store: WorkflowStore,
    source: str,
    target: str,
) -> None:
    coordinator = _coordinator(store)
    task = _task_in_status(service, coordinator, source)
    before = service.get_workflow_task(task["uuid"])
    journal_count = _table_count(store, "workflow_runtime_journal")
    event_count = _table_count(store, "frontend_event")

    with pytest.raises(StoreConflict):
        _transition_task(coordinator, task["uuid"], target)

    assert service.get_workflow_task(task["uuid"]) == before
    assert _table_count(store, "workflow_runtime_journal") == journal_count
    assert _table_count(store, "frontend_event") == event_count


@pytest.mark.parametrize(("source", "target"), _JOB_ALLOWED_CASES)
def test_each_allowed_job_transition_persists_timestamps_and_one_invalidation(
    service: WorkflowService,
    store: WorkflowStore,
    source: str,
    target: str,
) -> None:
    coordinator = _coordinator(store)
    task, job = _job_in_status(service, coordinator, source)
    cursor = service.list_events(after_id=0)["items"][-1]["id"]

    updated = _transition_job(coordinator, job["uuid"], source, target)

    assert updated["status"] == target
    if target == "running" and source != "running":
        datetime.fromisoformat(updated["started_at"].replace("Z", "+00:00"))
    if target in _TERMINAL_JOB:
        datetime.fromisoformat(updated["finished_at"].replace("Z", "+00:00"))
    if target == "execution_unknown":
        assert updated["uncertainty_reason"] == "transport outcome unavailable"
    if source == "execution_unknown" and target != "execution_unknown":
        assert "uncertainty_reason" not in updated
    events = _runtime_events(service, after_id=cursor)
    assert len(events) == 1
    assert events[0]["data"] == {"workflow_task_uuid": task["uuid"]}


@pytest.mark.parametrize(("source", "target"), _JOB_FORBIDDEN_CASES)
def test_each_forbidden_job_transition_is_zero_write(
    service: WorkflowService,
    store: WorkflowStore,
    source: str,
    target: str,
) -> None:
    coordinator = _coordinator(store)
    _task, job = _job_in_status(service, coordinator, source)
    before = service.get_workflow_node_job(job["uuid"])
    journal_count = _table_count(store, "workflow_runtime_journal")
    event_count = _table_count(store, "frontend_event")

    with pytest.raises(StoreConflict):
        _transition_job(coordinator, job["uuid"], source, target)

    assert service.get_workflow_node_job(job["uuid"]) == before
    assert _table_count(store, "workflow_runtime_journal") == journal_count
    assert _table_count(store, "frontend_event") == event_count


def test_commands_are_consumed_fifo_once_and_replay_is_zero_write(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    coordinator = _coordinator(store)
    task, _ = _create_task(service)
    commands = [
        _create_command(service, task["uuid"], "pause", "fifo-pause"),
        _create_command(service, task["uuid"], "resume", "fifo-resume"),
    ]
    expected = sorted(commands, key=lambda item: (item["create_time"], item["uuid"]))

    first = coordinator.consume_next_command(task["uuid"])
    second = coordinator.consume_next_command(task["uuid"])

    assert [first["uuid"], second["uuid"]] == [item["uuid"] for item in expected]
    assert first["status"] == second["status"] == "succeeded"
    assert first["result"] == second["result"] == {"outcome": "applied"}
    assert service.get_workflow_task(task["uuid"])["control_status"] == "active"
    event_count = _table_count(store, "frontend_event")
    journal_count = _table_count(store, "workflow_runtime_journal")
    assert coordinator.consume_next_command(task["uuid"]) is None
    replay = _create_command(service, task["uuid"], "pause", "fifo-pause")
    assert replay["uuid"] == commands[0]["uuid"]
    assert coordinator.consume_next_command(task["uuid"]) is None
    assert _table_count(store, "frontend_event") == event_count
    assert _table_count(store, "workflow_runtime_journal") == journal_count


@pytest.mark.parametrize("command_type", ["pause", "resume", "step"])
def test_admission_blocked_rejects_non_cancel_controls_without_changing_task(
    service: WorkflowService,
    store: WorkflowStore,
    command_type: str,
) -> None:
    coordinator = _coordinator(store)
    run_mode = "step" if command_type == "step" else "normal"
    task, jobs = _create_task(service, node_count=1, run_mode=run_mode)
    coordinator.transition_task(task["uuid"], "admission_blocked")
    blocked_before = service.get_workflow_task(task["uuid"])
    command = _create_command(
        service,
        task["uuid"],
        command_type,
        f"blocked-{command_type}",
        target_node_uuid=(
            jobs[0]["workflow_node_uuid"] if command_type == "step" else None
        ),
    )

    consumed = coordinator.consume_next_command(task["uuid"])

    assert consumed["uuid"] == command["uuid"]
    assert consumed["status"] == "rejected"
    assert consumed["result"] == {
        "outcome": "rejected",
        "error_code": "invalid_transition",
    }
    assert service.get_workflow_task(task["uuid"]) == blocked_before
    assert service.get_workflow_node_job(jobs[0]["uuid"])["status"] == "pending"


def test_admission_blocked_cancel_reuses_pending_terminal_cleanup(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    coordinator = _coordinator(store)
    task, jobs = _create_task(service, node_count=2)
    coordinator.transition_task(task["uuid"], "admission_blocked")
    _create_command(service, task["uuid"], "cancel", "blocked-cancel")

    consumed = coordinator.consume_next_command(task["uuid"])

    assert consumed["status"] == "succeeded"
    canceled = service.get_workflow_task(task["uuid"])
    assert canceled["status"] == "canceled"
    assert canceled["control_status"] == "paused"
    assert canceled["cleanup_status"] == "settled"
    assert {service.get_workflow_node_job(job["uuid"])["status"] for job in jobs} == {
        "canceled"
    }


def test_terminal_race_rejects_pending_command_durably(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    coordinator = _coordinator(store)
    task, _ = _create_task(service)
    command = _create_command(service, task["uuid"], "cancel", "terminal-race")
    coordinator.transition_task(task["uuid"], "canceled")
    cursor = service.list_events(after_id=0)["items"][-1]["id"]

    consumed = coordinator.consume_next_command(task["uuid"])

    assert consumed["uuid"] == command["uuid"]
    assert consumed["status"] == "rejected"
    assert consumed["result"] == {
        "outcome": "rejected",
        "error_code": "invalid_transition",
    }
    assert "consumed_at" in consumed
    events = _runtime_events(service, after_id=cursor)
    assert len(events) == 1
    assert events[0]["data"] == {"workflow_task_uuid": task["uuid"]}


def test_cancel_updates_many_jobs_atomically_with_one_runtime_event(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    coordinator = _coordinator(store)
    task, jobs = _create_task(service, node_count=4)
    coordinator.start_task(task["uuid"])
    coordinator.transition_job(jobs[1]["uuid"], "dispatched")
    coordinator.transition_job(jobs[2]["uuid"], "dispatched")
    coordinator.transition_job(jobs[2]["uuid"], "running")
    coordinator.transition_job(jobs[3]["uuid"], "dispatched")
    coordinator.mark_job_unknown(jobs[3]["uuid"], "lost cancel acknowledgement")
    command = _create_command(service, task["uuid"], "cancel", "cancel-many")
    cursor = service.list_events(after_id=0)["items"][-1]["id"]

    consumed = coordinator.consume_next_command(task["uuid"])

    statuses = {
        job["uuid"]: service.get_workflow_node_job(job["uuid"])["status"]
        for job in jobs
    }
    assert statuses == {
        jobs[0]["uuid"]: "canceled",
        jobs[1]["uuid"]: "cancel_requested",
        jobs[2]["uuid"]: "cancel_requested",
        jobs[3]["uuid"]: "execution_unknown",
    }
    updated_task = service.get_workflow_task(task["uuid"])
    assert updated_task["status"] == "canceling"
    assert updated_task["cleanup_status"] == "requires_attention"
    assert consumed["uuid"] == command["uuid"]
    assert consumed["result"] == {"outcome": "applied"}
    assert len(_runtime_events(service, after_id=cursor)) == 1
    assert _journal(store, task["uuid"])[-1]["kind"] == "command_consumed"


def test_running_cancel_without_active_job_journals_two_legal_task_transitions(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    coordinator = _coordinator(store)
    task, jobs = _create_task(service, node_count=1)
    coordinator.start_task(task["uuid"])
    _create_command(service, task["uuid"], "cancel", "cancel-finished-inline")

    coordinator.consume_next_command(task["uuid"])

    updated = service.get_workflow_task(task["uuid"])
    assert updated["status"] == "canceled"
    assert updated["control_status"] == "paused"
    assert service.get_workflow_node_job(jobs[0]["uuid"])["status"] == "canceled"
    task_transitions = [
        (row["from_status"], row["to_status"])
        for row in _journal(store, task["uuid"])
        if row["kind"] == "task_transition"
    ]
    assert task_transitions[-2:] == [
        ("running", "canceling"),
        ("canceling", "canceled"),
    ]


def test_running_cancel_with_active_job_pauses_control_and_stays_canceling(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    coordinator = _coordinator(store)
    task, jobs = _create_task(service, node_count=1)
    coordinator.start_task(task["uuid"])
    coordinator.transition_job(jobs[0]["uuid"], "dispatched")
    _create_command(service, task["uuid"], "cancel", "cancel-active")

    coordinator.consume_next_command(task["uuid"])

    updated = service.get_workflow_task(task["uuid"])
    assert updated["status"] == "canceling"
    assert updated["control_status"] == "paused"
    assert updated["cleanup_status"] == "canceling"
    assert service.get_workflow_node_job(jobs[0]["uuid"])["status"] == (
        "cancel_requested"
    )


def test_step_command_creates_one_durable_available_permit_without_job_mutation(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    coordinator = _coordinator(store)
    task, jobs = _create_task(service, node_count=1, run_mode="step")
    target = jobs[0]["workflow_node_uuid"]
    command = _create_command(
        service,
        task["uuid"],
        "step",
        "step-permit",
        target_node_uuid=target,
    )
    task_before = service.get_workflow_task(task["uuid"])
    job_before = service.get_workflow_node_job(jobs[0]["uuid"])

    consumed = coordinator.consume_next_command(task["uuid"])

    permits = _rows(
        store,
        "SELECT * FROM workflow_task_step_permit WHERE workflow_task_command_uuid = ?",
        (command["uuid"],),
    )
    assert len(permits) == 1
    assert permits[0]["workflow_task_uuid"] == task["uuid"]
    assert permits[0]["target_node_uuid"] == target
    assert permits[0]["status"] == "available"
    assert permits[0]["consumed_at"] is None
    assert consumed["result"] == {"outcome": "applied"}
    assert service.get_workflow_task(task["uuid"])["status"] == task_before["status"]
    assert service.get_workflow_node_job(jobs[0]["uuid"]) == job_before


def _sample(
    sequence: int,
    key: str,
    value: int,
    *,
    feedback_type: str = "progress",
) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "feedback_type": feedback_type,
        "data": {"value": value},
        "observed_at": _OBSERVED_AT,
        "idempotency_key": key,
    }


def test_feedback_batch_is_atomic_idempotent_and_updates_latest_summary(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    coordinator = _coordinator(store)
    task, jobs = _create_task(service, node_count=1)
    samples = [_sample(3, "feedback-3", 30), _sample(1, "feedback-1", 10)]

    coordinator.commit_job_feedback(jobs[0]["uuid"], samples)

    job = service.get_workflow_node_job(jobs[0]["uuid"])
    assert job["feedback_sequence"] == 3
    assert job["feedback_data"] == {"value": 30}
    history = service.list_workflow_node_job_feedback(
        jobs[0]["uuid"], after_sequence=0, limit=100
    )
    assert [item["sequence"] for item in history["items"]] == [1, 3]
    assert all(item["published_at"] for item in history["items"])
    assert history["next_cursor"] == 3
    assert history["has_more"] is False
    event_count = _table_count(store, "frontend_event")
    journal_count = _table_count(store, "workflow_runtime_journal")

    coordinator.commit_job_feedback(jobs[0]["uuid"], list(reversed(samples)))

    assert _table_count(store, "frontend_event") == event_count
    assert _table_count(store, "workflow_runtime_journal") == journal_count
    assert len(_runtime_events(service)) == 1
    assert _runtime_events(service)[0]["data"] == {"workflow_task_uuid": task["uuid"]}


@pytest.mark.parametrize(
    "conflicting_sample",
    [
        pytest.param(_sample(1, "different-key", 99), id="sequence-reused"),
        pytest.param(_sample(2, "feedback-1", 99), id="key-reused"),
    ],
)
def test_feedback_conflict_rolls_back_complete_batch(
    service: WorkflowService,
    store: WorkflowStore,
    conflicting_sample: dict[str, Any],
) -> None:
    coordinator = _coordinator(store)
    _task, jobs = _create_task(service, node_count=1)
    coordinator.commit_job_feedback(jobs[0]["uuid"], [_sample(1, "feedback-1", 10)])
    before_job = service.get_workflow_node_job(jobs[0]["uuid"])
    history_count = _table_count(store, "workflow_node_job_feedback_history")
    event_count = _table_count(store, "frontend_event")
    journal_count = _table_count(store, "workflow_runtime_journal")

    with pytest.raises(StoreConflict):
        coordinator.commit_job_feedback(
            jobs[0]["uuid"],
            [_sample(3, "new-in-same-batch", 30), conflicting_sample],
        )

    assert service.get_workflow_node_job(jobs[0]["uuid"]) == before_job
    assert _table_count(store, "workflow_node_job_feedback_history") == history_count
    assert _table_count(store, "frontend_event") == event_count
    assert _table_count(store, "workflow_runtime_journal") == journal_count


def test_feedback_http_paginates_and_survives_sqlite_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "workflow.db"
    first_store = WorkflowStore(db_path)
    first_service = WorkflowService(first_store)
    coordinator = _coordinator(first_store)
    _task, jobs = _create_task(first_service, node_count=1)
    coordinator.commit_job_feedback(
        jobs[0]["uuid"],
        [_sample(1, "page-1", 10), _sample(2, "page-2", 20), _sample(3, "page-3", 30)],
    )
    first_store.close()

    reopened_store = WorkflowStore(db_path)
    try:
        reopened_service = WorkflowService(reopened_store)
        with TestClient(create_workflow_app(reopened_service)) as http:
            response = http.get(
                f"/api/v1/workflow-node-jobs/{jobs[0]['uuid']}/feedback",
                params={"after_sequence": 1, "limit": 1},
            )
        assert response.status_code == 200
        payload = response.json()
        assert payload["code"] == 0
        assert [item["sequence"] for item in payload["data"]["items"]] == [2]
        assert payload["data"]["next_cursor"] == 2
        assert payload["data"]["has_more"] is True
        assert (
            reopened_service.get_workflow_node_job(jobs[0]["uuid"])["feedback_sequence"]
            == 3
        )
    finally:
        reopened_store.close()


@pytest.mark.parametrize(
    ("job_uuid", "query"),
    [
        pytest.param("not-a-uuid", "", id="invalid-job-uuid"),
        pytest.param(str(uuid4()), "", id="unknown-job"),
        pytest.param(str(uuid4()), "?after_sequence=-1", id="negative-cursor"),
        pytest.param(str(uuid4()), "?limit=0", id="zero-limit"),
        pytest.param(str(uuid4()), "?limit=501", id="limit-too-large"),
    ],
)
def test_feedback_http_uses_backend_error_envelope(
    client: TestClient,
    job_uuid: str,
    query: str,
) -> None:
    response = client.get(f"/api/v1/workflow-node-jobs/{job_uuid}/feedback{query}")
    expected_status = 404 if query == "" and job_uuid != "not-a-uuid" else 400
    expected_code = "not_found" if expected_status == 404 else "invalid_input"
    assert response.status_code == expected_status
    assert response.json()["code"] == expected_status
    assert response.json()["error"]["code"] == expected_code


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("?after_sequence=1.0", id="decimal-cursor"),
        pytest.param("?limit=1.0", id="decimal-limit"),
        pytest.param(
            f"?after_sequence={1 << 63}",
            id="cursor-overflow",
        ),
    ],
)
def test_feedback_http_rejects_non_backend_integer_spellings_before_job_lookup(
    client: TestClient,
    query: str,
) -> None:
    response = client.get(f"/api/v1/workflow-node-jobs/{uuid4()}/feedback{query}")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_input"


def test_feedback_http_empty_trimmed_query_uses_defaults_and_accepts_boundaries(
    client: TestClient,
    service: WorkflowService,
) -> None:
    _task, jobs = _create_task(service, node_count=1)
    route = f"/api/v1/workflow-node-jobs/{jobs[0]['uuid']}/feedback"

    empty = client.get(f"{route}?after_sequence=&limit=")
    trimmed = client.get(f"{route}?after_sequence=%20&limit=%20")
    boundary = client.get(
        route,
        params={"after_sequence": (1 << 63) - 1, "limit": 500},
    )

    assert empty.status_code == trimmed.status_code == boundary.status_code == 200
    assert (
        empty.json()["data"]
        == trimmed.json()["data"]
        == {
            "items": [],
            "next_cursor": 0,
            "has_more": False,
        }
    )
    assert boundary.json()["data"]["next_cursor"] == (1 << 63) - 1


def test_task_runtime_events_http_exposes_durable_dispatch_and_result(
    client: TestClient,
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    coordinator = _coordinator(store)
    task, jobs = _create_task(service, node_count=1)
    job = jobs[0]
    coordinator.start_task(task["uuid"])
    coordinator.transition_job(job["uuid"], "dispatched")
    coordinator.transition_job(job["uuid"], "running")
    coordinator.transition_job(
        job["uuid"],
        "succeeded",
        return_info={"completed": True, "message": "action finished"},
    )
    coordinator.transition_task(task["uuid"], "succeeded")

    response = client.get(f"/api/v1/workflow-tasks/{task['uuid']}/events")

    assert response.status_code == 200
    page = response.json()["data"]
    assert page["has_more"] is False
    assert page["next_cursor"] == page["items"][-1]["sequence"]
    assert [
        (item["kind"], item.get("from_status"), item.get("to_status"))
        for item in page["items"]
    ] == [
        ("task_transition", "pending", "running"),
        ("job_transition", "pending", "dispatched"),
        ("job_transition", "dispatched", "running"),
        ("job_transition", "running", "succeeded"),
        ("task_transition", "running", "succeeded"),
    ]
    dispatched = page["items"][1]
    assert dispatched["workflow_node_job_uuid"] == job["uuid"]
    assert dispatched["workflow_node_uuid"] == job["workflow_node_uuid"]
    assert dispatched["executor_kind"] == job["executor_kind"]
    assert dispatched["attempt"] == 1
    assert dispatched["param"] == {}
    result = page["items"][3]
    assert result["return_info"] == {
        "completed": True,
        "message": "action finished",
    }
    assert result["error_info"] == []

    cursor = page["items"][0]["sequence"]
    paged = client.get(
        f"/api/v1/workflow-tasks/{task['uuid']}/events",
        params={"after_sequence": cursor, "limit": 2},
    ).json()["data"]
    assert [item["to_status"] for item in paged["items"]] == [
        "dispatched",
        "running",
    ]
    assert paged["has_more"] is True
    assert paged["next_cursor"] == paged["items"][-1]["sequence"]


def test_unknown_open_and_last_resolution_restore_saved_control_state(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    coordinator = _coordinator(store)
    task, jobs = _create_task(service, node_count=2)
    coordinator.start_task(task["uuid"])
    for job in jobs:
        coordinator.transition_job(job["uuid"], "dispatched")
        coordinator.transition_job(job["uuid"], "running")
        coordinator.mark_job_unknown(job["uuid"], f"uncertain-{job['uuid']}")
    pause = _create_command(service, task["uuid"], "pause", "pause-while-unknown")
    coordinator.consume_next_command(task["uuid"])
    waiting = service.get_workflow_task(task["uuid"])
    assert waiting["control_status"] == "waiting_reconciliation"
    assert waiting["reconciliation_resume_control_status"] == "paused"
    assert waiting["cleanup_status"] == "requires_attention"

    coordinator.resolve_job_uncertainty(
        jobs[0]["uuid"], "running", reason="device still executing"
    )
    assert service.get_workflow_task(task["uuid"])["control_status"] == (
        "waiting_reconciliation"
    )
    coordinator.resolve_job_uncertainty(
        jobs[1]["uuid"], "failed", reason="operator observed failure"
    )

    restored = service.get_workflow_task(task["uuid"])
    assert restored["control_status"] == "paused"
    assert "reconciliation_resume_control_status" not in restored
    assert restored["cleanup_status"] == "none"
    assert service.get_workflow_node_job(jobs[0]["uuid"])["status"] == "running"
    assert service.get_workflow_node_job(jobs[1]["uuid"])["status"] == "failed"
    assert store.get_task_command(pause["uuid"])["status"] == "succeeded"


def test_partial_reconcile_points_attention_at_a_remaining_unknown_job(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    coordinator = _coordinator(store)
    task, jobs = _create_task(service, node_count=2)
    coordinator.start_task(task["uuid"])
    for job, reason in zip(jobs, ("reason-a", "reason-b"), strict=True):
        coordinator.transition_job(job["uuid"], "dispatched")
        coordinator.mark_job_unknown(job["uuid"], reason)

    coordinator.resolve_job_uncertainty(
        jobs[1]["uuid"],
        "failed",
        reason="operator resolved B first",
    )

    waiting = service.get_workflow_task(task["uuid"])
    assert waiting["control_status"] == "waiting_reconciliation"
    assert waiting["attention_reason"] == "reason-a"


def test_cancel_with_unknown_job_keeps_attention_until_explicit_reconcile(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    coordinator = _coordinator(store)
    task, jobs = _create_task(service, node_count=2)
    coordinator.start_task(task["uuid"])
    for job in jobs:
        coordinator.transition_job(job["uuid"], "dispatched")
    coordinator.mark_job_unknown(jobs[0]["uuid"], "unknown physical outcome")
    _create_command(service, task["uuid"], "cancel", "cancel-with-unknown")
    coordinator.consume_next_command(task["uuid"])

    canceling = service.get_workflow_task(task["uuid"])
    assert canceling["status"] == "canceling"
    assert canceling["cleanup_status"] == "requires_attention"
    assert service.get_workflow_node_job(jobs[0]["uuid"])["status"] == (
        "execution_unknown"
    )
    assert service.get_workflow_node_job(jobs[1]["uuid"])["status"] == (
        "cancel_requested"
    )

    coordinator.resolve_job_uncertainty(
        jobs[0]["uuid"], "canceled", reason="device confirmed stopped"
    )

    after_reconcile = service.get_workflow_task(task["uuid"])
    assert after_reconcile["status"] == "canceling"
    assert after_reconcile["cleanup_status"] == "canceling"


def test_startup_recovery_marks_inflight_unknown_once_without_blind_replay(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    coordinator = _coordinator(store)
    task, jobs = _create_task(service, node_count=4, run_mode="step")
    coordinator.start_task(task["uuid"])
    coordinator.transition_job(jobs[0]["uuid"], "dispatched")
    coordinator.transition_job(jobs[1]["uuid"], "dispatched")
    coordinator.transition_job(jobs[1]["uuid"], "running")
    coordinator.transition_job(jobs[2]["uuid"], "dispatched")
    coordinator.transition_job(jobs[2]["uuid"], "running")
    coordinator.transition_job(jobs[2]["uuid"], "intervention_required")
    step = _create_command(
        service,
        task["uuid"],
        "step",
        "durable-permit",
        target_node_uuid=jobs[3]["workflow_node_uuid"],
    )
    coordinator.consume_next_command(task["uuid"])
    pending = _create_command(service, task["uuid"], "pause", "pending-at-restart")
    cursor = service.list_events(after_id=0)["items"][-1]["id"]

    coordinator.recover_startup()

    assert [service.get_workflow_node_job(job["uuid"])["status"] for job in jobs] == [
        "execution_unknown",
        "execution_unknown",
        "execution_unknown",
        "pending",
    ]
    assert all(
        service.get_workflow_node_job(job["uuid"])["uncertainty_reason"]
        == "runtime_restarted_in_flight"
        for job in jobs[:3]
    )
    recovered_task = service.get_workflow_task(task["uuid"])
    assert recovered_task["control_status"] == "waiting_reconciliation"
    assert recovered_task["cleanup_status"] == "requires_attention"
    assert store.get_task_command(pending["uuid"])["status"] == "pending"
    assert (
        _rows(
            store,
            "SELECT status FROM workflow_task_step_permit "
            "WHERE workflow_task_command_uuid = ?",
            (step["uuid"],),
        )[0]["status"]
        == "available"
    )
    assert [row["kind"] for row in _journal(store, task["uuid"])[-3:]] == [
        "startup_recovered",
        "startup_recovered",
        "startup_recovered",
    ]
    assert len(_runtime_events(service, after_id=cursor)) == 1
    journal_count = _table_count(store, "workflow_runtime_journal")
    event_count = _table_count(store, "frontend_event")

    coordinator.recover_startup()

    assert _table_count(store, "workflow_runtime_journal") == journal_count
    assert _table_count(store, "frontend_event") == event_count


def test_state_journal_and_exact_runtime_outbox_commit_or_rollback_together(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    coordinator = _coordinator(store)
    task, _ = _create_task(service)

    coordinator.start_task(task["uuid"])

    journal = _journal(store, task["uuid"])
    assert len(journal) == 1
    assert journal[0]["kind"] == "task_transition"
    assert journal[0]["from_status"] == "pending"
    assert journal[0]["to_status"] == "running"
    assert isinstance(json.loads(journal[0]["data"]), dict)
    events = _runtime_events(service)
    assert len(events) == 1
    assert events[0]["data"] == {"workflow_task_uuid": task["uuid"]}

    with store.transaction() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_runtime_outbox
            BEFORE INSERT ON frontend_event
            WHEN NEW.event = 'workflow.runtime.changed'
            BEGIN SELECT RAISE(ABORT, 'forced runtime outbox failure'); END
            """
        )
    before = service.get_workflow_task(task["uuid"])
    with pytest.raises(sqlite3.IntegrityError, match="forced runtime outbox failure"):
        coordinator.transition_task(task["uuid"], "canceling")
    assert service.get_workflow_task(task["uuid"]) == before
    assert _journal(store, task["uuid"]) == journal
    assert _runtime_events(service) == events


def test_runtime_timestamps_follow_transaction_linearization_order(
    service: WorkflowService,
    store: WorkflowStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _runtime_module()
    coordinator = module.WorkflowRuntimeCoordinator(store)
    task, jobs = _create_task(service, node_count=1)
    coordinator.start_task(task["uuid"])
    job_uuid = jobs[0]["uuid"]
    first_transition_waiting = threading.Event()
    allow_first_transition = threading.Event()
    original_transaction = store.transaction
    allocated: list[str] = []
    allocation_lock = threading.Lock()

    def ordered_now() -> str:
        with allocation_lock:
            value = f"2026-08-01T00:00:0{len(allocated) + 1}Z"
            allocated.append(value)
            return value

    @contextmanager
    def gated_transaction() -> Iterator[sqlite3.Connection]:
        if threading.current_thread().name == "delayed-running-transition":
            first_transition_waiting.set()
            assert allow_first_transition.wait(timeout=2)
        with original_transaction() as connection:
            yield connection

    monkeypatch.setattr(module, "utc_now", ordered_now)
    monkeypatch.setattr(store, "transaction", gated_transaction)
    thread_errors: list[BaseException] = []

    def transition_to_running() -> None:
        try:
            coordinator.transition_job(job_uuid, "running")
        except BaseException as error:  # noqa: BLE001 - 回传线程断言证据
            thread_errors.append(error)

    delayed = threading.Thread(
        target=transition_to_running,
        name="delayed-running-transition",
    )
    delayed.start()
    assert first_transition_waiting.wait(timeout=2)
    coordinator.transition_job(job_uuid, "dispatched")
    allow_first_transition.set()
    delayed.join(timeout=2)

    assert not delayed.is_alive()
    assert thread_errors == []
    transitions = [
        row for row in _journal(store, task["uuid"]) if row["kind"] == "job_transition"
    ][-2:]
    assert [(row["from_status"], row["to_status"]) for row in transitions] == [
        ("pending", "dispatched"),
        ("dispatched", "running"),
    ]
    assert [row["create_time"] for row in transitions] == sorted(
        row["create_time"] for row in transitions
    )
    assert (
        service.get_workflow_node_job(job_uuid)["update_time"]
        == transitions[-1]["create_time"]
    )


async def _read_sse_until_runtime_event(
    app: FastAPI,
    *,
    last_event_id: int,
) -> tuple[int, str]:
    disconnected = asyncio.Event()
    request_sent = False
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)
        if b"event: workflow.runtime.changed" in message.get("body", b""):
            disconnected.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "GET",
        "root_path": "",
        "path": "/api/v1/events",
        "raw_path": b"/api/v1/events",
        "query_string": b"",
        "headers": [(b"last-event-id", str(last_event_id).encode("ascii"))],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=2)
    status = next(
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    ).decode("utf-8")
    return status, body


def test_sse_last_event_id_replays_only_later_runtime_invalidation(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    coordinator = _coordinator(store)
    task, _ = _create_task(service)
    coordinator.start_task(task["uuid"])
    first = _runtime_events(service)[0]
    coordinator.transition_task(task["uuid"], "canceling")
    second = _runtime_events(service, after_id=first["id"])[0]

    status, body = asyncio.run(
        _read_sse_until_runtime_event(
            create_workflow_app(service),
            last_event_id=first["id"],
        )
    )

    assert status == 200
    assert f"id: {second['id']}\n" in body
    assert f"id: {first['id']}\n" not in body
    assert "event: workflow.runtime.changed\n" in body
    assert f'data: {{"workflow_task_uuid":"{task["uuid"]}"}}\n\n' in body


def test_runtime_schema_has_frozen_tables_indexes_and_foreign_keys(
    store: WorkflowStore,
) -> None:
    expected_columns = {
        "workflow_runtime_journal": {
            "sequence",
            "workflow_task_uuid",
            "workflow_node_job_uuid",
            "workflow_task_command_uuid",
            "kind",
            "from_status",
            "to_status",
            "data",
            "create_time",
        },
        "workflow_task_step_permit": {
            "workflow_task_command_uuid",
            "workflow_task_uuid",
            "target_node_uuid",
            "status",
            "create_time",
            "consumed_at",
        },
        "workflow_node_job_feedback_history": {
            "uuid",
            "create_time",
            "update_time",
            "deleted_at",
            "description",
            "meta_data",
            "workflow_node_job_uuid",
            "sequence",
            "feedback_type",
            "data",
            "observed_at",
            "received_at",
            "published_at",
            "idempotency_key",
        },
    }
    with store.transaction() as connection:
        observed = {
            table: {
                row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for table in expected_columns
        }
        index_columns = {
            tuple(
                item["name"]
                for item in connection.execute(f"PRAGMA index_info({row['name']})")
            )
            for table in expected_columns
            for row in connection.execute(f"PRAGMA index_list({table})")
        }
        journal_foreign_keys = {
            (row["from"], row["table"], row["to"])
            for row in connection.execute(
                "PRAGMA foreign_key_list(workflow_runtime_journal)"
            )
        }

    assert observed == expected_columns
    assert {
        ("workflow_task_uuid", "sequence"),
        ("workflow_node_job_uuid", "sequence"),
        (
            "workflow_task_uuid",
            "status",
            "create_time",
            "workflow_task_command_uuid",
        ),
        ("workflow_node_job_uuid", "sequence"),
        ("workflow_node_job_uuid", "idempotency_key"),
    }.issubset(index_columns)
    assert journal_foreign_keys == {
        ("workflow_task_uuid", "workflow_task", "uuid"),
        ("workflow_node_job_uuid", "workflow_node_job", "uuid"),
        ("workflow_task_command_uuid", "workflow_task_command", "uuid"),
    }


def test_public_worker_consumes_commands_and_stops_before_store_close(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    module = _runtime_module()
    coordinator = module.WorkflowRuntimeCoordinator(store)
    task, _ = _create_task(service)
    _create_command(service, task["uuid"], "pause", "worker-command")
    worker = module.WorkflowRuntimeWorker(coordinator, poll_interval_seconds=0.01)

    try:
        worker.start()
        assert worker.is_alive() is True
        deadline = time.monotonic() + 1
        while (
            store.get_task_command_by_key(task["uuid"], "worker-command")["status"]
            == "pending"
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
    finally:
        worker.stop()
        worker.join(timeout=1)

    assert worker.is_alive() is False
    assert store.get_task_command_by_key(task["uuid"], "worker-command")["status"] == (
        "succeeded"
    )


def test_production_composition_recovers_before_ready_and_keeps_single_worker(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    seed_store = WorkflowStore(working_dir / "workflow.db")
    seed_service = WorkflowService(seed_store)
    coordinator = _coordinator(seed_store)
    task, jobs = _create_task(seed_service, node_count=1)
    coordinator.start_task(task["uuid"])
    coordinator.transition_job(jobs[0]["uuid"], "dispatched")
    cursor = seed_service.list_events(after_id=0)["items"][-1]["id"]
    seed_store.close()

    try:
        first = compose_workflow_runtime(working_dir)
        second = compose_workflow_runtime(working_dir)

        assert second is first
        recovered = first.get_workflow_node_job(jobs[0]["uuid"])
        assert recovered["status"] == "execution_unknown"
        assert recovered["uncertainty_reason"] == "runtime_restarted_in_flight"
        assert len(_runtime_events(first, after_id=cursor)) == 1
    finally:
        reset_workflow_service_for_test()

    replacement = compose_workflow_runtime(working_dir)
    try:
        assert replacement is not first
        assert replacement.get_workflow_node_job(jobs[0]["uuid"])["status"] == (
            "execution_unknown"
        )
        assert len(_runtime_events(replacement, after_id=cursor)) == 1
    finally:
        reset_workflow_service_for_test()


def _try_compose_in_second_process(working_dir: str, outcome: Any) -> None:
    try:
        compose_workflow_runtime(working_dir)
    except RuntimeError as error:
        outcome.put(("rejected", str(error)))
    except Exception as error:  # noqa: BLE001 - 子进程错误必须返回父进程
        outcome.put(("unexpected_error", type(error).__name__, str(error)))
    else:
        outcome.put(("opened", ""))
    finally:
        reset_workflow_service_for_test()


def _second_process_result(working_dir: Path) -> tuple[str, ...]:
    context = multiprocessing.get_context("spawn")
    outcome = context.Queue()
    process = context.Process(
        target=_try_compose_in_second_process,
        args=(str(working_dir), outcome),
    )
    process.start()
    process.join(timeout=8)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("第二个 Workflow runtime authority 未在限定时间内退出")
    result = outcome.get(timeout=2)
    outcome.close()
    outcome.join_thread()
    return result


def test_runtime_worker_stop_failure_retains_service_store_and_workspace_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    working_dir = tmp_path / "unilabos_data"

    class FailFirstStopWorker:
        instances: list[FailFirstStopWorker] = []

        def __init__(self, coordinator: Any) -> None:
            del coordinator
            self.stop_calls = 0
            self.started = False
            self.__class__.instances.append(self)

        def start(self) -> None:
            self.started = True

        def stop(self) -> None:
            self.stop_calls += 1

        def join(self, timeout: float | None = None) -> None:
            del timeout

        def is_alive(self) -> bool:
            return (
                self.started
                and self is self.__class__.instances[0]
                and self.stop_calls < 2
            )

    reset_workflow_service_for_test()
    monkeypatch.setattr(composition, "WorkflowRuntimeWorker", FailFirstStopWorker)
    service = compose_workflow_runtime(working_dir)

    with pytest.raises(RuntimeError, match="runtime worker 未能停止"):
        reset_workflow_service_for_test()

    assert get_workflow_service() is service
    assert service.list_workflows()["items"] == []
    assert _second_process_result(working_dir) == (
        "rejected",
        "当前工作区已由另一个 OS Workflow Authority 占用",
    )

    reset_workflow_service_for_test()
    replacement = compose_workflow_runtime(working_dir)
    try:
        assert replacement is not service
    finally:
        reset_workflow_service_for_test()
