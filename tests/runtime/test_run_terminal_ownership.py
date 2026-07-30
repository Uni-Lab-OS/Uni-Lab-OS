"""Run terminal events are written once by the OS executor authority."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from unilabos.app.local_bridge.schedule_ws import ScheduleSession
from unilabos.runtime.event_store import SQLiteEventJournal
from unilabos.runtime.service import RuntimeService
from unilabos.scheduler.dag_executor import DagExecutor
from unilabos.scheduler.dag_model import NodeState, TaskDag


ACTION_CATALOG: dict[str, dict[str, Any]] = {
    "generic-device.execute": {
        "inputs": {},
        "outputs": {},
        "resource_claims": [],
        "effects": [],
    }
}


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


@pytest.mark.parametrize(
    ("node_terminal", "run_terminal"),
    [
        (NodeState.SUCCESS, "run_completed"),
        (NodeState.FAILED, "run_failed"),
        (NodeState.CANCELLED, "run_cancelled"),
    ],
)
def test_dag_executor_persists_exactly_one_run_terminal_event(
    tmp_path: Path,
    node_terminal: NodeState,
    run_terminal: str,
) -> None:
    run_id = f"executor-{node_terminal.value}"
    journal = SQLiteEventJournal(
        tmp_path / f"{run_id}.sqlite",
        runtime_epoch="os-epoch",
    )

    async def submit(_node: object) -> NodeState:
        return node_terminal

    states = asyncio.run(DagExecutor(_dag(run_id), submit, journal=journal).run())

    assert states == {"execute": node_terminal}
    terminal_events = [
        event.type
        for event in journal.list_events(run_id)
        if event.type in {"run_completed", "run_failed", "run_cancelled"}
    ]
    assert terminal_events == [run_terminal]


def test_runtime_service_status_projection_never_writes_run_terminal_events(
    tmp_path: Path,
) -> None:
    journal = SQLiteEventJournal(
        tmp_path / "runtime-service.sqlite",
        runtime_epoch="bridge-epoch",
    )
    schedule: ScheduleSession

    async def os_send(_message: dict[str, Any]) -> None:
        return None

    schedule = ScheduleSession(os_send, session_id="bridge")
    service = RuntimeService(
        schedule,
        journal=journal,
        action_catalog=ACTION_CATALOG,
    )
    accepted = asyncio.run(
        service.start_run(
            {
                "source": {
                    "format": "canonical_workflow_v2",
                    "payload": {
                        "schema_version": "2",
                        "revision_id": "revision-1",
                        "workflow_id": "terminal-owner",
                        "invocations": [
                            {
                                "node_id": "execute",
                                "action_ref": "generic-device.execute",
                            }
                        ],
                        "control_edges": [],
                    },
                }
            }
        )
    )
    run_id = accepted["id"]

    async def report_terminal_twice() -> None:
        message = {
            "action": "job_status",
            "data": {
                "task_id": run_id,
                "job_id": "execute",
                "status": "success",
                "return_info": {"physical_state": "confirmed"},
            },
        }
        await schedule.handle_incoming(message)
        await schedule.handle_incoming(message)

    asyncio.run(report_terminal_twice())
    # The bridge may project per-node transport status, but only the executor
    # is allowed to commit the durable run terminal.
    assert service.get_run(run_id) == {"id": run_id, "status": "running"}
    assert [
        event.type
        for event in journal.list_events(run_id)
        if event.type in {"run_completed", "run_failed", "run_cancelled"}
    ] == []

    asyncio.run(
        schedule.handle_incoming(
            {
                "action": "run_terminal",
                "data": {
                    "run_id": run_id,
                    "status": "completed",
                },
            }
        )
    )
    assert service.get_run(run_id) == {"id": run_id, "status": "completed"}
    assert service.get_run(run_id) == {"id": run_id, "status": "completed"}

    terminal_events = [
        event.type
        for event in journal.list_events(run_id)
        if event.type in {"run_completed", "run_failed", "run_cancelled"}
    ]
    assert terminal_events == ["run_completed"]
