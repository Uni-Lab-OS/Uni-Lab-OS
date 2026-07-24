"""Device-agnostic integration contract for local runtime orchestration."""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

from unilabos.runtime.event_store import SQLiteEventJournal
from unilabos.scheduler.dag_executor import DagExecutor
from unilabos.scheduler.dag_model import DagNode, NodeState, TaskDag
from unilabos.scheduler.resource_lock import (
    LeaseRequest,
    ResolvedResourceClaim,
    ResourceLockManager,
)
from unilabos.scheduler.result_store import (
    NodeExecutionResult,
    ResultEnvelope,
)


def _node(node_id: str, resource_id: str) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "device_id": f"device-{node_id}",
        "action": "execute",
        "output_schema": {"node_id": {"type": "string"}},
        "resource_claims": [
            {
                "resource_id": resource_id,
                "quantity": 1,
                "mode": "exclusive",
            }
        ],
        "effects": [{"op": "observe", "resource_id": resource_id}],
    }


def _dag(*nodes: dict[str, Any], task_id: str) -> TaskDag:
    return TaskDag.from_message(
        {
            "task_id": task_id,
            "notebook_id": "runtime-integration",
            "server_info": {},
            "nodes": list(nodes),
            "edges": [],
        }
    )


async def _settle() -> None:
    for _ in range(8):
        await asyncio.sleep(0)


def _assert_ordered_subsequence(
    actual: list[str],
    expected: list[str],
) -> None:
    remaining = iter(actual)
    assert all(
        any(item == expected_item for item in remaining)
        for expected_item in expected
    ), f"expected ordered lifecycle {expected!r}, got {actual!r}"


def test_runtime_owns_resource_waiting_concurrency_and_journal(tmp_path) -> None:
    dag = _dag(
        _node("exclusive-a", "shared-vessel"),
        _node("exclusive-b", "shared-vessel"),
        _node("independent", "free-camera"),
        task_id="run-resource-contention",
    )
    locks = ResourceLockManager(runtime_epoch="epoch-1")
    journal = SQLiteEventJournal(tmp_path / "runtime.sqlite", runtime_epoch="epoch-1")
    release_first = asyncio.Event()
    release_independent = asyncio.Event()
    first_started = asyncio.Event()
    independent_started = asyncio.Event()
    active: set[str] = set()
    dispatch_count: Counter[str] = Counter()

    async def dispatch(node: DagNode) -> NodeExecutionResult:
        dispatch_count[node.node_id] += 1
        active.add(node.node_id)
        try:
            if node.node_id == "exclusive-a":
                assert "exclusive-b" not in active
                first_started.set()
                await release_first.wait()
            elif node.node_id == "exclusive-b":
                assert "exclusive-a" not in active
            else:
                independent_started.set()
                await release_independent.wait()
            return NodeExecutionResult(
                state=NodeState.SUCCESS,
                envelope=ResultEnvelope(outputs={"node_id": node.node_id}),
            )
        finally:
            active.remove(node.node_id)

    async def scenario() -> tuple[dict[str, NodeState], DagExecutor]:
        executor = DagExecutor(
            dag,
            dispatch,
            resource_lock_manager=locks,
            journal=journal,
        )
        run_task = asyncio.create_task(executor.run())
        await asyncio.wait_for(
            asyncio.gather(first_started.wait(), independent_started.wait()),
            timeout=1,
        )
        await _settle()

        assert active == {"exclusive-a", "independent"}
        assert dispatch_count["exclusive-b"] == 0
        assert executor.walk.states["exclusive-b"] == NodeState.READY

        release_independent.set()
        await _settle()
        assert dispatch_count["exclusive-b"] == 0

        release_first.set()
        states = await asyncio.wait_for(run_task, timeout=1)
        return states, executor

    states, executor = asyncio.run(scenario())

    assert states == {
        "exclusive-a": NodeState.SUCCESS,
        "exclusive-b": NodeState.SUCCESS,
        "independent": NodeState.SUCCESS,
    }
    assert dispatch_count == Counter(
        {"exclusive-a": 1, "exclusive-b": 1, "independent": 1}
    )
    assert executor.results.keys() == states.keys()
    assert locks.active_leases() == ()

    completed = journal.load_cursor(dag.task_id)
    assert completed is not None
    assert set(completed["completed"]) == set(states)
    for node_id in states:
        projection = journal.load_node_projection(dag.task_id, node_id)
        assert projection is not None
        assert projection.terminal == "succeeded"
        assert projection.result == {"node_id": node_id}
        assert projection.effects == dag.nodes[node_id].effects
        node_events = [
            event.type
            for event in journal.list_events(dag.task_id)
            if event.node_id == node_id
        ]
        _assert_ordered_subsequence(
            node_events,
            [
                "lock_requested",
                "lock_acquired",
                "node_started",
                "node_succeeded",
                "lock_released",
            ],
        )
        assert node_events.count("lock_acquired") == 1
        assert node_events.count("lock_released") == 1


def test_ambiguous_dispatch_failure_fences_lease_without_retry(tmp_path) -> None:
    dag = _dag(
        _node("uncertain-action", "sealed-reactor"),
        task_id="run-ambiguous-failure",
    )
    locks = ResourceLockManager(runtime_epoch="epoch-1")
    journal = SQLiteEventJournal(tmp_path / "runtime.sqlite", runtime_epoch="epoch-1")
    dispatch_count = 0

    async def dispatch(node: DagNode) -> NodeExecutionResult:
        nonlocal dispatch_count
        dispatch_count += 1
        raise ConnectionError(f"physical state unknown for {node.node_id}")

    executor = DagExecutor(
        dag,
        dispatch,
        resource_lock_manager=locks,
        journal=journal,
    )
    states = asyncio.run(executor.run())

    assert states == {"uncertain-action": NodeState.FAILED}
    assert dispatch_count == 1
    leases = locks.active_leases()
    assert len(leases) == 1
    assert leases[0].holder_id == "run-ambiguous-failure:uncertain-action"
    assert leases[0].state == "unknown"
    assert "physical state unknown" in (leases[0].reason or "")

    async def acquire_same_resource() -> object:
        return await locks.acquire_all(
            LeaseRequest(
                holder_id="later-action",
                claims=(
                    ResolvedResourceClaim(
                        resource_id="sealed-reactor",
                        mode="exclusive",
                    ),
                ),
            )
        )

    assert asyncio.run(acquire_same_resource()) is None
    projection = journal.load_node_projection(dag.task_id, "uncertain-action")
    assert projection is not None
    assert projection.terminal == "failed"
    assert projection.result["physical_state"] == "unknown"
    assert "physical state unknown" in projection.result["error"]
    event_types = [event.type for event in journal.list_events(dag.task_id)]
    _assert_ordered_subsequence(
        event_types,
        [
            "lock_requested",
            "lock_acquired",
            "node_started",
            "lock_unknown",
            "node_failed",
        ],
    )
    assert "lock_released" not in event_types
