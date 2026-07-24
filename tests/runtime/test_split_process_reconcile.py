"""Reconcile crosses the bridge and is authorized only by the execution OS."""

from __future__ import annotations

import asyncio
from pathlib import Path
from queue import Queue
from typing import Any

import pytest

from unilabos.app.local_bridge.server import build_offline_session
from unilabos.app.local_bridge.schedule_ws import ScheduleSession
from unilabos.app.ws_client import DeviceActionManager, MessageProcessor
from unilabos.runtime.event_store import SQLiteEventJournal
from unilabos.runtime.service import RuntimeService
from unilabos.scheduler.dag_model import TaskDag
from unilabos.scheduler.dag_wire import serialize_task_dag
from unilabos.scheduler.resource_lock import (
    ResolvedResourceClaim,
    ResourceLockManager,
)


def _dag(run_id: str) -> TaskDag:
    return TaskDag.from_message(
        {
            "task_id": run_id,
            "nodes": [
                {
                    "node_id": "execute",
                    "device_id": "generic-device",
                    "action": "execute",
                }
            ],
            "edges": [],
        }
    )


def _record_run(journal: SQLiteEventJournal, run_id: str) -> None:
    journal.record_run_submission(
        run_id=run_id,
        source={"format": "canonical_workflow_v2", "payload": {}},
        profile_ref="",
        compiled_dag=serialize_task_dag(_dag(run_id)),
        status="reconciling",
    )


def _record_logical_terminal(
    journal: SQLiteEventJournal,
    run_id: str,
    terminal: str,
) -> None:
    journal.commit_node_terminal(
        run_id=run_id,
        node_id="execute",
        terminal=terminal,
        result={},
        effects=[],
        cursor={"completed": []},
        outbox=[],
    )


def _install_unknown(
    *,
    run_id: str,
    lease_id: str,
    locks: ResourceLockManager,
    journal: SQLiteEventJournal,
) -> None:
    lease = locks.install_unknown(
        holder_id=f"{run_id}:execute",
        claims=(
            ResolvedResourceClaim(
                resource_id="physical-cell",
                mode="exclusive",
            ),
        ),
        reason="transport disconnected",
        lease_id=lease_id,
    )
    journal.record_lock_unknown(
        run_id=run_id,
        node_id="execute",
        lease_id=lease.lease_id,
        holder_id=lease.holder_id,
        claims=[
            {
                "resource_id": "physical-cell",
                "quantity": 1,
                "mode": "exclusive",
                "scope": "action",
            }
        ],
        reason="transport disconnected",
    )


def _decision(lease_id: str) -> dict[str, str]:
    return {
        "lease_id": lease_id,
        "resolution": "confirmed_safe",
        "actor": "operator-1",
        "reason": "physical cell inspected empty",
    }


def test_runtime_service_delegates_reconcile_without_touching_bridge_lock_manager(
    tmp_path: Path,
) -> None:
    run_id = "split-process-run"
    lease_id = "bridge-shadow-lease"
    bridge_journal = SQLiteEventJournal(
        tmp_path / "bridge.sqlite",
        runtime_epoch="bridge-epoch",
    )
    bridge_locks = ResourceLockManager(runtime_epoch="bridge-epoch")
    _record_run(bridge_journal, run_id)
    _install_unknown(
        run_id=run_id,
        lease_id=lease_id,
        locks=bridge_locks,
        journal=bridge_journal,
    )

    class RecordingSchedule:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str]]] = []

        def on_job_status(self, _callback: Any) -> None:
            return None

        def get_run(self, _task_id: str) -> None:
            return None

        async def reconcile_run(
            self,
            requested_run_id: str,
            decision: dict[str, str],
        ) -> dict[str, str]:
            self.calls.append((requested_run_id, decision))
            return {"id": requested_run_id, "status": "reconciled"}

    schedule = RecordingSchedule()
    service = RuntimeService(
        schedule,
        journal=bridge_journal,
        resource_lock_manager=bridge_locks,
    )
    decision = _decision(lease_id)

    result = asyncio.run(service.reconcile_run(run_id, decision))

    assert result == {"id": run_id, "status": "reconciled"}
    assert schedule.calls == [(run_id, decision)]
    assert bridge_locks.get_lease(lease_id).state == "unknown"
    assert not any(
        event.type == "reconcile_resolved"
        for event in bridge_journal.list_events(run_id)
    )


def test_schedule_session_sends_reconcile_command_and_waits_for_os_ack() -> None:
    sent: list[dict[str, Any]] = []
    session: ScheduleSession

    async def os_send(message: dict[str, Any]) -> None:
        sent.append(message)
        await session.handle_incoming(
            {
                "action": "reconcile_ack",
                "data": {
                    "request_id": message["data"]["request_id"],
                    "run_id": message["data"]["run_id"],
                    "lease_id": message["data"]["lease_id"],
                    "status": "reconciled",
                },
            }
        )

    session = ScheduleSession(os_send, session_id="bridge-session")
    decision = _decision("lease-1")

    result = asyncio.run(session.reconcile_run("run-1", decision))

    assert result == {"id": "run-1", "status": "reconciled"}
    assert len(sent) == 1
    assert sent[0]["action"] == "reconcile_run"
    assert sent[0]["data"] == {
        "request_id": sent[0]["data"]["request_id"],
        "run_id": "run-1",
        **decision,
    }


def test_offline_runtime_reconcile_uses_shared_os_authority_and_audits_once(
    tmp_path: Path,
) -> None:
    run_id = "offline-reconcile-run"
    lease_id = "offline-unknown-lease"
    journal = SQLiteEventJournal(
        tmp_path / "offline-runtime.sqlite",
        runtime_epoch="offline-epoch",
    )
    locks = ResourceLockManager(runtime_epoch="offline-epoch")
    _record_run(journal, run_id)
    _install_unknown(
        run_id=run_id,
        lease_id=lease_id,
        locks=locks,
        journal=journal,
    )
    session, offline_os = build_offline_session(
        resource_lock_manager=locks,
        journal=journal,
    )
    service = RuntimeService(
        session,
        journal=journal,
        resource_lock_manager=locks,
    )

    result = asyncio.run(service.reconcile_run(run_id, _decision(lease_id)))

    assert result == {"id": run_id, "status": "reconciled"}
    assert offline_os.received[0]["action"] == "reconcile_run"
    assert locks.get_lease(lease_id).state == "released"
    audit = [
        event
        for event in journal.list_events(run_id)
        if event.type == "reconcile_resolved"
    ]
    assert len(audit) == 1
    assert audit[0].payload == {
        "actor": "operator-1",
        "lease_id": lease_id,
        "reason": "physical cell inspected empty",
        "resolution": "confirmed_safe",
    }


def _processor(
    tmp_path: Path,
) -> tuple[
    MessageProcessor,
    Queue[dict[str, Any]],
    ResourceLockManager,
    SQLiteEventJournal,
]:
    queue: Queue[dict[str, Any]] = Queue()
    locks = ResourceLockManager(runtime_epoch="os-epoch")
    journal = SQLiteEventJournal(
        tmp_path / "os-runtime.sqlite",
        runtime_epoch="os-epoch",
    )
    processor = MessageProcessor(
        "ws://split-process-reconcile",
        queue,
        DeviceActionManager(),
        resource_lock_manager=locks,
        journal=journal,
    )
    return processor, queue, locks, journal


def test_message_processor_os_authority_releases_and_audits_unknown_fence(
    tmp_path: Path,
) -> None:
    run_id = "os-authority-run"
    lease_id = "os-lease"
    processor, queue, locks, journal = _processor(tmp_path)
    _record_run(journal, run_id)
    _record_logical_terminal(journal, run_id, "failed")
    _install_unknown(
        run_id=run_id,
        lease_id=lease_id,
        locks=locks,
        journal=journal,
    )
    request = {
        "request_id": "request-success",
        "run_id": run_id,
        **_decision(lease_id),
    }

    asyncio.run(processor._process_message("reconcile_run", request))  # noqa: SLF001

    assert locks.get_lease(lease_id).state == "released"
    audit = [
        event
        for event in journal.list_events(run_id)
        if event.type == "reconcile_resolved"
    ]
    assert len(audit) == 1
    assert audit[0].payload == {
        "actor": "operator-1",
        "lease_id": lease_id,
        "reason": "physical cell inspected empty",
        "resolution": "confirmed_safe",
    }
    assert queue.get_nowait() == {
        "action": "reconcile_ack",
        "data": {
            "request_id": "request-success",
            "run_id": run_id,
            "lease_id": lease_id,
            "status": "reconciled",
            "node_id": "execute",
            "terminal": "failed",
        },
    }


@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
def test_reconcile_ack_restores_original_run_terminal(
    terminal: str,
) -> None:
    sessions: list[ScheduleSession] = []

    async def bridge_to_os(message: dict[str, Any]) -> None:
        if message["action"] != "reconcile_run":
            return
        await sessions[0].handle_incoming(
            {
                "action": "reconcile_ack",
                "data": {
                    "request_id": message["data"]["request_id"],
                    "run_id": message["data"]["run_id"],
                    "lease_id": message["data"]["lease_id"],
                    "status": "reconciled",
                    "node_id": "execute",
                    "terminal": terminal,
                },
            }
        )

    async def scenario() -> None:
        run_id = f"restore-{terminal}"
        session = ScheduleSession(bridge_to_os, session_id="bridge-session")
        sessions.append(session)
        handle = await session.submit_dag(_dag(run_id))
        handle.apply_status(
            "execute",
            terminal,
            return_info={
                "physical_state": "unknown",
                "reconcile_required": True,
            },
        )
        assert handle.node_states == {"execute": "reconciling"}
        assert handle.finished is False

        service = RuntimeService(session)
        result = await service.reconcile_run(run_id, _decision("lease-1"))

        assert handle.finished is True
        assert handle.node_states["execute"].value == terminal
        assert result == {"id": run_id, "status": terminal}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("wrong_lease", "lease_not_found"),
        ("wrong_run", "lease_not_found"),
        ("not_unknown", "lease_not_unknown"),
        ("missing_actor", "invalid_decision"),
        ("missing_reason", "invalid_decision"),
    ],
)
def test_message_processor_rejects_unsafe_reconcile_requests(
    tmp_path: Path,
    case: str,
    expected_code: str,
) -> None:
    run_id = "rejected-run"
    lease_id = "rejected-lease"
    processor, queue, locks, journal = _processor(tmp_path)
    _record_run(journal, run_id)
    _install_unknown(
        run_id=run_id,
        lease_id=lease_id,
        locks=locks,
        journal=journal,
    )
    request = {
        "request_id": f"request-{case}",
        "run_id": run_id,
        **_decision(lease_id),
    }
    if case == "wrong_lease":
        request["lease_id"] = "missing-lease"
    elif case == "wrong_run":
        request["run_id"] = "different-run"
    elif case == "not_unknown":
        asyncio.run(locks.resolve_unknown(lease_id, release=True))
    elif case == "missing_actor":
        request.pop("actor")
    elif case == "missing_reason":
        request.pop("reason")

    asyncio.run(processor._process_message("reconcile_run", request))  # noqa: SLF001

    assert queue.get_nowait() == {
        "action": "reconcile_ack",
        "data": {
            "request_id": f"request-{case}",
            "run_id": request["run_id"],
            "lease_id": request["lease_id"],
            "status": "rejected",
            "code": expected_code,
        },
    }
    assert not any(
        event.type == "reconcile_resolved"
        for event in journal.list_events(run_id)
    )
    if case != "not_unknown":
        assert locks.get_lease(lease_id).state == "unknown"
