"""Critical generic runtime safety regressions across terminal and restart paths."""

from __future__ import annotations

import asyncio
from pathlib import Path
from queue import Queue

import pytest

from unilabos.runtime.event_store import SQLiteEventJournal
from unilabos.app.ws_client import DeviceActionManager, MessageProcessor
from unilabos.scheduler.dag_executor import DagExecutor
from unilabos.scheduler.dag_model import DagNode, NodeState, TaskDag
from unilabos.scheduler.resource_lock import (
    LeaseRequest,
    ResolvedResourceClaim,
    ResourceLockManager,
    ResourceLease,
)
from unilabos.scheduler.result_store import NodeExecutionResult, ResultEnvelope
from unilabos.scheduler.task_dag_runner import TaskDagRunner
from unilabos.utils.type_check import serialize_result_info


def _dag(
    task_id: str,
    *,
    scope: str = "action",
    with_blocker: bool = False,
) -> TaskDag:
    nodes = [
        {
            "node_id": "scoped-action",
            "device_id": "generic-device-a",
            "action": "execute",
            "resource_claims": [
                {
                    "resource_id": "shared-cell",
                    "quantity": 1,
                    "mode": "exclusive",
                    "scope": scope,
                }
            ],
        }
    ]
    if with_blocker:
        nodes.append(
            {
                "node_id": "blocker",
                "device_id": "generic-device-b",
                "action": "wait",
            }
        )
    return TaskDag.from_message(
        {
            "task_id": task_id,
            "notebook_id": "safety-regression",
            "server_info": {},
            "nodes": nodes,
            "edges": [],
        }
    )


async def _settle() -> None:
    for _ in range(8):
        await asyncio.sleep(0)


def test_message_processor_preserves_ambiguous_terminal_metadata() -> None:
    processor = MessageProcessor(
        "ws://safety-regression",
        Queue(),
        DeviceActionManager(),
    )
    received: list[tuple[str, str, dict[str, object] | None]] = []

    class RecordingRunner:
        def notify_terminal(
            self,
            job_id: str,
            status: str,
            *,
            return_info: dict[str, object] | None = None,
        ) -> None:
            received.append((job_id, status, return_info))

    return_info = {
        **serialize_result_info("connection lost", False, {}),
        "physical_state": "unknown",
        "reconcile_required": True,
    }
    processor._task_dag_runners["ambiguous-run"] = RecordingRunner()  # noqa: SLF001

    processor.notify_task_dag_terminal(
        "ambiguous-run",
        "scoped-action",
        "cancelled",
        return_info=return_info,
    )

    assert received == [("scoped-action", "cancelled", return_info)]


@pytest.mark.parametrize(
    ("status", "return_info", "expected_state", "expected_lease_state"),
    [
        (
            "failed",
            {
                **serialize_result_info("contract rejected", False, {}),
                "physical_state": "confirmed_failed",
                "reconcile_required": False,
            },
            NodeState.FAILED,
            None,
        ),
        (
            "cancelled",
            {
                **serialize_result_info("transport lost", False, {}),
                "physical_state": "unknown",
                "reconcile_required": True,
            },
            NodeState.CANCELLED,
            "unknown",
        ),
    ],
)
def test_runner_terminal_classification_controls_lease_fencing(
    status: str,
    return_info: dict[str, object],
    expected_state: NodeState,
    expected_lease_state: str | None,
) -> None:
    dag = _dag(f"terminal-{status}")
    locks = ResourceLockManager(runtime_epoch="epoch-1")
    started = asyncio.Event()
    runner: TaskDagRunner

    def on_start(_node: DagNode) -> None:
        started.set()

    runner = TaskDagRunner(
        dag,
        on_start,
        resource_lock_manager=locks,
    )

    async def scenario() -> dict[str, NodeState]:
        run_task = asyncio.create_task(runner.run())
        await asyncio.wait_for(started.wait(), timeout=1)
        runner.notify_terminal(
            "scoped-action",
            status,
            return_info=return_info,
        )
        return await asyncio.wait_for(run_task, timeout=1)

    states = asyncio.run(scenario())
    assert states["scoped-action"] == expected_state
    active = locks.active_leases()
    if expected_lease_state is None:
        assert active == ()
    else:
        assert len(active) == 1
        assert active[0].state == expected_lease_state
        assert active[0].reason == "transport lost"


@pytest.mark.parametrize("scope", ["until_handoff", "workflow_block"])
def test_scoped_claim_survives_node_terminal_until_explicit_release(
    scope: str,
) -> None:
    dag = _dag(f"scope-{scope}", scope=scope, with_blocker=True)
    locks = ResourceLockManager(runtime_epoch="epoch-1")

    async def scenario() -> None:
        scoped_done = asyncio.Event()
        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()
        scoped_holder_id = f"{dag.task_id}:scoped-action"

        async def dispatch(node: DagNode) -> NodeExecutionResult:
            if node.node_id == "scoped-action":
                scoped_done.set()
            else:
                blocker_started.set()
                await release_blocker.wait()
            return NodeExecutionResult(
                state=NodeState.SUCCESS,
                envelope=ResultEnvelope(outputs={}),
            )

        run_task = asyncio.create_task(
            DagExecutor(dag, dispatch, resource_lock_manager=locks).run()
        )
        await asyncio.wait_for(
            asyncio.gather(scoped_done.wait(), blocker_started.wait()),
            timeout=1,
        )
        await _settle()

        scoped_leases = [
            lease
            for lease in locks.active_leases()
            if lease.holder_id == scoped_holder_id
        ]
        assert len(scoped_leases) == 1
        assert scoped_leases[0].claims[0].scope == scope

        released = await locks.release_holder(scoped_holder_id, scope=scope)
        assert released == 1
        assert all(
            lease.holder_id != scoped_holder_id
            for lease in locks.active_leases()
        )

        release_blocker.set()
        states = await asyncio.wait_for(run_task, timeout=1)
        assert all(state == NodeState.SUCCESS for state in states.values())

    asyncio.run(scenario())


def test_runtime_journals_complete_lock_lifecycle(tmp_path: Path) -> None:
    dag = _dag("lock-events")
    locks = ResourceLockManager(runtime_epoch="epoch-1")
    journal = SQLiteEventJournal(tmp_path / "runtime.sqlite", runtime_epoch="epoch-1")

    async def dispatch(_node: DagNode) -> NodeExecutionResult:
        return NodeExecutionResult(
            state=NodeState.SUCCESS,
            envelope=ResultEnvelope(outputs={}),
        )

    states = asyncio.run(
        DagExecutor(
            dag,
            dispatch,
            resource_lock_manager=locks,
            journal=journal,
        ).run()
    )

    assert states == {"scoped-action": NodeState.SUCCESS}
    assert [event.type for event in journal.list_events(dag.task_id)] == [
        "lock_requested",
        "lock_acquired",
        "node_started",
        "node_succeeded",
        "lock_released",
        "run_completed",
    ]


def test_restart_rebuilds_unknown_lease_from_running_journal(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.sqlite"
    dag = _dag("restart-fencing")
    first_locks = ResourceLockManager(runtime_epoch="epoch-1")
    first_journal = SQLiteEventJournal(db_path, runtime_epoch="epoch-1")

    async def crash_process() -> None:
        dispatched = asyncio.Event()

        async def dispatch(_node: DagNode) -> NodeExecutionResult:
            dispatched.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        run_task = asyncio.create_task(
            DagExecutor(
                dag,
                dispatch,
                resource_lock_manager=first_locks,
                journal=first_journal,
            ).run()
        )
        await asyncio.wait_for(dispatched.wait(), timeout=1)
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)

    asyncio.run(crash_process())
    projection = first_journal.load_node_projection(dag.task_id, "scoped-action")
    assert projection is not None
    assert projection.state == "running"
    first_journal.close()

    recovered_locks = ResourceLockManager(runtime_epoch="epoch-2")
    recovered_journal = SQLiteEventJournal(db_path, runtime_epoch="epoch-2")
    recovered = recovered_journal.reconcile_restart(
        dag.task_id,
        dispatch=lambda _node_id: None,
        dag=dag,
        lock_manager=recovered_locks,
    )

    assert recovered.nodes["scoped-action"].state == "reconciling"
    leases = recovered_locks.active_leases()
    assert len(leases) == 1
    assert leases[0].holder_id == "restart-fencing:scoped-action"
    assert leases[0].state == "unknown"

    async def attempt_reuse() -> object:
        request = DagExecutor._lease_request(
            dag.nodes["scoped-action"],
            run_id=dag.task_id,
        )
        return await recovered_locks.acquire_all(request)

    assert asyncio.run(attempt_reuse()) is None
    event_types = [
        event.type for event in recovered_journal.list_events(dag.task_id)
    ]
    assert "lock_unknown" in event_types
    assert "reconcile_started" in event_types


@pytest.mark.parametrize("terminal", [NodeState.FAILED, NodeState.CANCELLED])
def test_terminal_node_with_unresolved_unknown_lease_remains_restart_incomplete(
    tmp_path: Path,
    terminal: NodeState,
) -> None:
    """A logical terminal must not hide a still-unknown physical lease."""

    db_path = tmp_path / f"terminal-{terminal.value}.sqlite"
    task_id = f"terminal-unknown-{terminal.value}"
    dag = _dag(task_id)
    first_locks = ResourceLockManager(runtime_epoch="epoch-1")
    first_journal = SQLiteEventJournal(db_path, runtime_epoch="epoch-1")

    async def dispatch(_node: DagNode) -> NodeExecutionResult:
        return NodeExecutionResult(
            state=terminal,
            terminal_info={
                "error": "transport disconnected before physical confirmation",
                "physical_state": "unknown",
                "reconcile_required": True,
            },
        )

    states = asyncio.run(
        DagExecutor(
            dag,
            dispatch,
            resource_lock_manager=first_locks,
            journal=first_journal,
        ).run()
    )
    assert states == {"scoped-action": terminal}
    projection = first_journal.load_node_projection(task_id, "scoped-action")
    assert projection is not None
    assert projection.terminal == terminal.value
    first_live = first_locks.active_leases()
    assert len(first_live) == 1
    assert first_live[0].state == "unknown"
    persisted_unknown = next(
        event
        for event in first_journal.list_events(task_id)
        if event.type == "lock_unknown"
    )
    persisted_lease_id = str(persisted_unknown.payload["lease_id"])
    first_journal.close()

    recovered_locks = ResourceLockManager(runtime_epoch="epoch-2")
    recovered_journal = SQLiteEventJournal(db_path, runtime_epoch="epoch-2")

    assert task_id in recovered_journal.list_incomplete_run_ids()
    recovery = recovered_journal.reconcile_restart(
        task_id,
        dispatch=lambda _node_id: None,
        dag=dag,
        lock_manager=recovered_locks,
    )

    assert recovery.nodes["scoped-action"].state == "reconciling"
    recovered = recovered_locks.active_leases()
    assert len(recovered) == 1
    assert recovered[0].lease_id == persisted_lease_id
    assert recovered[0].state == "unknown"
    assert recovered[0].claims == first_live[0].claims


def test_restart_replays_partial_scope_release_for_one_lease_id(
    tmp_path: Path,
) -> None:
    """A scope release subtracts claims; it does not release the whole lease."""

    db_path = tmp_path / "mixed-scope.sqlite"
    run_id = "mixed-scope-run"
    node_id = "mixed-scope-node"
    first_locks = ResourceLockManager(runtime_epoch="epoch-1")
    first_journal = SQLiteEventJournal(db_path, runtime_epoch="epoch-1")
    claims = (
        ResolvedResourceClaim(
            resource_id="transient-tool",
            scope="action",
        ),
        ResolvedResourceClaim(
            resource_id="retained-sample",
            scope="until_handoff",
        ),
    )

    async def acquire_then_release_action_scope() -> ResourceLease:
        lease = await first_locks.acquire_all(
            LeaseRequest(holder_id=f"{run_id}:{node_id}", claims=claims)
        )
        assert lease is not None
        released = await first_locks.release(lease.lease_id, scope="action")
        assert released is True
        return lease

    lease = asyncio.run(acquire_then_release_action_scope())
    serialized_claims = [
        {
            "resource_id": claim.resource_id,
            "quantity": claim.quantity,
            "mode": claim.mode,
            "scope": claim.scope,
        }
        for claim in claims
    ]
    first_journal.record_node_started(run_id=run_id, node_id=node_id, attempt=1)
    first_journal.record_lock_acquired(
        run_id=run_id,
        node_id=node_id,
        lease_id=lease.lease_id,
        holder_id=lease.holder_id,
        claims=serialized_claims,
    )
    first_journal.record_lock_released(
        run_id=run_id,
        node_id=node_id,
        lease_id=lease.lease_id,
        released_scope="action",
    )
    remaining_before_restart = first_locks.get_lease(lease.lease_id)
    assert remaining_before_restart.state == "active"
    assert remaining_before_restart.claims == (claims[1],)
    first_journal.close()

    recovered_locks = ResourceLockManager(runtime_epoch="epoch-2")
    recovered_journal = SQLiteEventJournal(db_path, runtime_epoch="epoch-2")
    recovered_journal.reconcile_restart(
        run_id,
        dispatch=lambda _node_id: None,
        lock_manager=recovered_locks,
    )

    recovered = recovered_locks.active_leases()
    assert len(recovered) == 1
    assert recovered[0].lease_id == lease.lease_id
    assert recovered[0].state == "unknown"
    assert recovered[0].claims == (claims[1],)


def test_restart_scopes_same_node_holder_identity_across_runs(
    tmp_path: Path,
) -> None:
    """The same node_id in two runs must not share a release identity."""

    db_path = tmp_path / "cross-run-holder.sqlite"
    first_journal = SQLiteEventJournal(db_path, runtime_epoch="epoch-1")
    node_id = "shared-node-id"
    runs = (
        ("run-a", "lease-a", "resource-a"),
        ("run-b", "lease-b", "resource-b"),
    )
    for run_id, lease_id, resource_id in runs:
        first_journal.record_node_started(
            run_id=run_id,
            node_id=node_id,
            attempt=1,
        )
        first_journal.record_lock_acquired(
            run_id=run_id,
            node_id=node_id,
            lease_id=lease_id,
            holder_id=f"{run_id}:{node_id}",
            claims=[
                {
                    "resource_id": resource_id,
                    "quantity": 1,
                    "mode": "exclusive",
                    "scope": "until_handoff",
                }
            ],
        )
    first_journal.close()

    recovered_locks = ResourceLockManager(runtime_epoch="epoch-2")
    recovered_journal = SQLiteEventJournal(db_path, runtime_epoch="epoch-2")
    for run_id, _lease_id, _resource_id in runs:
        recovered_journal.reconcile_restart(
            run_id,
            dispatch=lambda _node_id: None,
            lock_manager=recovered_locks,
        )

    recovered = recovered_locks.active_leases()
    assert {lease.lease_id for lease in recovered} == {"lease-a", "lease-b"}
    assert {lease.holder_id for lease in recovered} == {
        "run-a:shared-node-id",
        "run-b:shared-node-id",
    }

    first_holder = next(
        lease.holder_id for lease in recovered if lease.lease_id == "lease-a"
    )

    async def reconcile_first_run_only() -> None:
        released = await recovered_locks.release_holder(
            first_holder,
            scope="until_handoff",
        )
        assert released == 0
        assert {lease.lease_id for lease in recovered_locks.active_leases()} == {
            "lease-a",
            "lease-b",
        }
        resolved = await recovered_locks.resolve_unknown(
            "lease-a",
            release=True,
        )
        assert resolved.state == "released"

    asyncio.run(reconcile_first_run_only())
    assert {lease.lease_id for lease in recovered_locks.active_leases()} == {
        "lease-b"
    }
