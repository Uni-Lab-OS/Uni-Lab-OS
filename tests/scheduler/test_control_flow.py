"""Quick Debug Alpha branch/skip, fork/join and finite-loop contracts."""

from __future__ import annotations

import asyncio
import importlib
from types import ModuleType

import pytest

from unilabos.scheduler.dag_model import NodeState, TaskDag
from unilabos.scheduler.result_store import NodeExecutionResult, ResultEnvelope


def _control_api() -> ModuleType:
    return importlib.import_module("unilabos.scheduler.dag_executor")


def _capability(target: object, name: str):
    value = getattr(target, name, None)
    if value is None:
        pytest.fail(f"control-flow capability is missing: DagWalk.{name}", pytrace=False)
    return value


def _dag(nodes: list[dict[str, object]], edges: list[dict[str, object]]) -> TaskDag:
    return TaskDag.from_message(
        {
            "task_id": "control-flow",
            "notebook_id": "quick-debug",
            "server_info": {},
            "nodes": nodes,
            "edges": edges,
        }
    )


def _node(node_id: str, node_type: str = "action") -> dict[str, object]:
    node: dict[str, object] = {
        "node_id": node_id,
        "device_id": "virtual",
        "action": node_id,
        "node_type": node_type,
    }
    if node_type == "branch":
        node["output_schema"] = {"branch": {"type": "string"}}
    return node


async def _settle() -> None:
    for _ in range(8):
        await asyncio.sleep(0)


def test_branch_marks_untaken_path_skipped_and_dispatches_no_side_effect() -> None:
    api = _control_api()
    dag = _dag(
        [_node("branch", "branch"), _node("yes"), _node("no"), _node("join", "join")],
        [
            {"source_node_uuid": "branch", "target_node_uuid": "yes", "branch": "yes"},
            {"source_node_uuid": "branch", "target_node_uuid": "no", "branch": "no"},
            {"source_node_uuid": "yes", "target_node_uuid": "join"},
            {"source_node_uuid": "no", "target_node_uuid": "join"},
        ],
    )
    walk = api.DagWalk(dag)
    walk.mark_running("branch")
    _capability(walk, "on_branch")("branch", selected="yes")

    assert walk.states["no"] == NodeState.SKIPPED
    assert walk.ready() == ["yes"]
    assert "no" not in walk.dispatched_nodes


def test_join_waits_for_every_active_fork_branch() -> None:
    api = _control_api()
    dag = _dag(
        [_node("fork", "fork"), _node("left"), _node("right"), _node("join", "join")],
        [
            {"source_node_uuid": "fork", "target_node_uuid": "left"},
            {"source_node_uuid": "fork", "target_node_uuid": "right"},
            {"source_node_uuid": "left", "target_node_uuid": "join"},
            {"source_node_uuid": "right", "target_node_uuid": "join"},
        ],
    )
    walk = api.DagWalk(dag)
    walk.mark_running("fork")
    walk.on_success("fork")
    assert set(walk.ready()) == {"left", "right"}
    walk.mark_running("left")
    walk.mark_running("right")
    walk.on_success("left")
    assert "join" not in walk.ready()
    walk.on_success("right")
    assert walk.ready() == ["join"]


def test_executor_dispatches_only_selected_branch_then_join() -> None:
    api = _control_api()
    dag = _dag(
        [_node("branch", "branch"), _node("yes"), _node("no"), _node("join", "join")],
        [
            {"source_node_uuid": "branch", "target_node_uuid": "yes", "branch": "yes"},
            {"source_node_uuid": "branch", "target_node_uuid": "no", "branch": "no"},
            {"source_node_uuid": "yes", "target_node_uuid": "join"},
            {"source_node_uuid": "no", "target_node_uuid": "join"},
        ],
    )
    dispatched: list[str] = []

    async def dispatch(node) -> NodeExecutionResult:
        dispatched.append(node.node_id)
        outputs = {"branch": "yes"} if node.node_id == "branch" else {}
        return NodeExecutionResult(
            state=NodeState.SUCCESS,
            envelope=ResultEnvelope(outputs=outputs),
        )

    executor = api.DagExecutor(dag, dispatch)
    states = asyncio.run(executor.run())

    assert states == {
        "branch": NodeState.SUCCESS,
        "yes": NodeState.SUCCESS,
        "no": NodeState.SKIPPED,
        "join": NodeState.SUCCESS,
    }
    assert dispatched == ["branch", "yes", "join"]
    assert "no" not in executor.walk.dispatched_nodes


def test_executor_fork_join_waits_for_both_active_branches() -> None:
    api = _control_api()
    dag = _dag(
        [_node("fork", "fork"), _node("left"), _node("right"), _node("join", "join")],
        [
            {"source_node_uuid": "fork", "target_node_uuid": "left"},
            {"source_node_uuid": "fork", "target_node_uuid": "right"},
            {"source_node_uuid": "left", "target_node_uuid": "join"},
            {"source_node_uuid": "right", "target_node_uuid": "join"},
        ],
    )

    async def scenario() -> tuple[dict[str, NodeState], list[str]]:
        left_release = asyncio.Event()
        right_release = asyncio.Event()
        left_started = asyncio.Event()
        right_started = asyncio.Event()
        join_started = asyncio.Event()
        dispatched: list[str] = []

        async def dispatch(node) -> NodeExecutionResult:
            dispatched.append(node.node_id)
            if node.node_id == "left":
                left_started.set()
                await left_release.wait()
            elif node.node_id == "right":
                right_started.set()
                await right_release.wait()
            elif node.node_id == "join":
                join_started.set()
            return NodeExecutionResult(
                state=NodeState.SUCCESS,
                envelope=ResultEnvelope(outputs={}),
            )

        run_task = asyncio.create_task(api.DagExecutor(dag, dispatch).run())
        await asyncio.wait_for(
            asyncio.gather(left_started.wait(), right_started.wait()),
            timeout=1,
        )
        assert not join_started.is_set()

        left_release.set()
        await _settle()
        assert not join_started.is_set()

        right_release.set()
        states = await asyncio.wait_for(run_task, timeout=1)
        assert join_started.is_set()
        return states, dispatched

    states, dispatched = asyncio.run(scenario())
    assert states == {
        "fork": NodeState.SUCCESS,
        "left": NodeState.SUCCESS,
        "right": NodeState.SUCCESS,
        "join": NodeState.SUCCESS,
    }
    assert dispatched[0] == "fork"
    assert set(dispatched[1:3]) == {"left", "right"}
    assert dispatched[-1] == "join"
    assert len(dispatched) == len(set(dispatched))


def test_group_marker_completes_in_kernel_without_device_dispatch() -> None:
    api = _control_api()
    group = _node("nested", "group")
    group.update({"device_id": "os_control", "action": "group"})
    dag = _dag(
        [group, _node("inside")],
        [{"source_node_uuid": "nested", "target_node_uuid": "inside"}],
    )
    dispatched: list[str] = []

    async def dispatch(node) -> NodeExecutionResult:
        dispatched.append(node.node_id)
        return NodeExecutionResult(
            state=NodeState.SUCCESS,
            envelope=ResultEnvelope(outputs={}),
        )

    states = asyncio.run(api.DagExecutor(dag, dispatch).run())

    assert states == {
        "nested": NodeState.SUCCESS,
        "inside": NodeState.SUCCESS,
    }
    assert dispatched == ["inside"]


def test_all_skipped_join_propagates_skip_without_deadlock() -> None:
    api = _control_api()
    dag = _dag(
        [_node("branch", "branch"), _node("left"), _node("right"), _node("join", "join")],
        [
            {"source_node_uuid": "branch", "target_node_uuid": "left", "branch": "left"},
            {"source_node_uuid": "branch", "target_node_uuid": "right", "branch": "right"},
            {"source_node_uuid": "left", "target_node_uuid": "join"},
            {"source_node_uuid": "right", "target_node_uuid": "join"},
        ],
    )
    walk = api.DagWalk(dag)
    _capability(walk, "skip_outgoing")("branch")
    assert walk.states["left"] == NodeState.SKIPPED
    assert walk.states["right"] == NodeState.SKIPPED
    assert walk.states["join"] == NodeState.SKIPPED
    assert walk.is_done()


def test_static_finite_loop_expands_nodes_bindings_and_source_map() -> None:
    authoring = importlib.import_module("unilabos.workflow.from_python_script")
    compiler = getattr(authoring, "compile_python_script", None)
    if compiler is None:
        pytest.fail("finite-loop Canonical Python compiler capability is missing", pytrace=False)
    revision = compiler(
        "for volume in [1, 2, 3]:\n    pump.dose(amount=volume)\n",
        action_catalog={
            "pump.dose": {"inputs": {"amount": {"type": "number"}}, "outputs": {}}
        },
    )
    assert [node.input_bindings["amount"].value for node in revision.invocations] == [1, 2, 3]
    assert len(revision.source_map.entries) == 3
    assert len({node.node_id for node in revision.invocations}) == 3
