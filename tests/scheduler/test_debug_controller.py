"""Debugger admission is safe, deterministic, and run scoped."""

from __future__ import annotations

import asyncio

from unilabos.scheduler.dag_executor import DagExecutor
from unilabos.scheduler.dag_model import NodeState, TaskDag
from unilabos.scheduler.debug_controller import DebugController


def _dag() -> TaskDag:
    return TaskDag.from_message(
        {
            "task_id": "debug-run",
            "nodes": [
                {"node_id": "first", "device_id": "d1", "action": "first"},
                {"node_id": "second", "device_id": "d2", "action": "second"},
            ],
            "edges": [
                {
                    "source_node_uuid": "first",
                    "target_node_uuid": "second",
                }
            ],
        }
    )


async def _settle() -> None:
    for _ in range(12):
        await asyncio.sleep(0)


def test_pause_before_admission_and_step_exactly_one_node() -> None:
    async def scenario() -> tuple[list[str], dict[str, NodeState], list[str]]:
        dag = _dag()
        dispatched: list[str] = []
        events: list[str] = []

        async def submit(node) -> NodeState:
            dispatched.append(node.node_id)
            await asyncio.sleep(0)
            return NodeState.SUCCESS

        debugger = DebugController(
            run_id=dag.task_id,
            node_ids=set(dag.nodes),
            config={"pause_on_start": True},
            on_event=lambda event_type, _payload: events.append(event_type),
        )
        executor = DagExecutor(dag, submit, debug_controller=debugger)
        run_task = asyncio.create_task(executor.run())
        await _settle()

        assert dispatched == []
        assert debugger.projection()["status"] == "paused"
        assert debugger.projection()["pausedBeforeNodeId"] == "first"

        await executor.debug_command("step")
        await _settle()
        assert dispatched == ["first"]
        assert debugger.projection()["status"] == "paused"
        assert debugger.projection()["pausedBeforeNodeId"] == "second"

        await executor.debug_command("continue")
        states = await run_task
        return dispatched, states, events

    dispatched, states, events = asyncio.run(scenario())
    assert dispatched == ["first", "second"]
    assert states == {
        "first": NodeState.SUCCESS,
        "second": NodeState.SUCCESS,
    }
    assert "debug.paused" in events
    assert "debug.stepping" in events


def test_breakpoint_stops_before_dispatch_and_continue_bypasses_once() -> None:
    async def scenario() -> list[str]:
        dag = _dag()
        dispatched: list[str] = []

        async def submit(node) -> NodeState:
            dispatched.append(node.node_id)
            return NodeState.SUCCESS

        debugger = DebugController(
            run_id=dag.task_id,
            node_ids=set(dag.nodes),
            config={"breakpoints": ["second"]},
        )
        executor = DagExecutor(dag, submit, debug_controller=debugger)
        run_task = asyncio.create_task(executor.run())
        await _settle()

        assert dispatched == ["first"]
        projection = debugger.projection()
        assert projection["status"] == "paused"
        assert projection["pausedBeforeNodeId"] == "second"

        await executor.debug_command("continue")
        await run_task
        return dispatched

    assert asyncio.run(scenario()) == ["first", "second"]


def test_marked_start_skips_predecessors_and_pauses_at_start() -> None:
    async def scenario() -> tuple[list[str], dict[str, NodeState], dict]:
        dag = TaskDag.from_message(
            {
                "task_id": "marked-start",
                "nodes": [
                    {"node_id": "before", "device_id": "d1", "action": "before"},
                    {"node_id": "start", "device_id": "d2", "action": "start"},
                    {"node_id": "after", "device_id": "d3", "action": "after"},
                ],
                "edges": [
                    {
                        "source_node_uuid": "before",
                        "target_node_uuid": "start",
                    },
                    {
                        "source_node_uuid": "start",
                        "target_node_uuid": "after",
                    },
                ],
                "debug": {
                    "pause_on_start": True,
                    "start_node_id": "start",
                },
            }
        )
        dispatched: list[str] = []

        async def submit(node) -> NodeState:
            dispatched.append(node.node_id)
            return NodeState.SUCCESS

        debugger = DebugController(
            run_id=dag.task_id,
            node_ids=set(dag.nodes),
            config=dag.debug,
        )
        executor = DagExecutor(dag, submit, debug_controller=debugger)
        run_task = asyncio.create_task(executor.run())
        await _settle()

        assert dispatched == []
        assert debugger.projection()["pausedBeforeNodeId"] == "start"
        assert debugger.projection()["startNodeId"] == "start"

        await executor.debug_command("continue")
        states = await run_task
        return dispatched, states, debugger.projection()

    dispatched, states, projection = asyncio.run(scenario())
    assert dispatched == ["start", "after"]
    assert states == {
        "before": NodeState.SKIPPED,
        "start": NodeState.SUCCESS,
        "after": NodeState.SUCCESS,
    }
    assert projection["startNodeId"] == "start"


def test_continue_stops_at_two_breakpoints_in_order() -> None:
    async def scenario() -> list[tuple[str, list[str]]]:
        dag = TaskDag.from_message(
            {
                "task_id": "two-breakpoints",
                "nodes": [
                    {"node_id": "start", "device_id": "d1", "action": "start"},
                    {
                        "node_id": "breakpoint-1",
                        "device_id": "d2",
                        "action": "middle",
                    },
                    {
                        "node_id": "breakpoint-2",
                        "device_id": "d3",
                        "action": "finish",
                    },
                ],
                "edges": [
                    {
                        "source_node_uuid": "start",
                        "target_node_uuid": "breakpoint-1",
                    },
                    {
                        "source_node_uuid": "breakpoint-1",
                        "target_node_uuid": "breakpoint-2",
                    },
                ],
            }
        )
        dispatched: list[str] = []

        async def submit(node) -> NodeState:
            dispatched.append(node.node_id)
            return NodeState.SUCCESS

        debugger = DebugController(
            run_id=dag.task_id,
            node_ids=set(dag.nodes),
            config={
                "pause_on_start": True,
                "breakpoints": ["breakpoint-1", "breakpoint-2"],
            },
        )
        executor = DagExecutor(dag, submit, debug_controller=debugger)
        run_task = asyncio.create_task(executor.run())
        await _settle()
        checkpoints = [
            (debugger.projection()["pausedBeforeNodeId"], list(dispatched))
        ]

        await executor.debug_command("continue")
        await _settle()
        checkpoints.append(
            (debugger.projection()["pausedBeforeNodeId"], list(dispatched))
        )

        await executor.debug_command("continue")
        await _settle()
        checkpoints.append(
            (debugger.projection()["pausedBeforeNodeId"], list(dispatched))
        )

        await executor.debug_command("continue")
        await run_task
        return checkpoints

    assert asyncio.run(scenario()) == [
        ("start", []),
        ("breakpoint-1", ["start"]),
        ("breakpoint-2", ["start", "breakpoint-1"]),
    ]
