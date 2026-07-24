"""Canonical resource-hold contracts and executor release semantics."""

from __future__ import annotations

import asyncio

import pytest

from unilabos.scheduler.dag_executor import DagExecutor
from unilabos.scheduler.dag_model import DagNode, NodeState
from unilabos.scheduler.resource_lock import ResourceLockManager
from unilabos.scheduler.result_store import NodeExecutionResult, ResultEnvelope
from unilabos.workflow.canonical import WorkflowRevision
from unilabos.workflow.dag_compile import (
    WorkflowCompileError,
    compile_workflow_revision,
)


def _catalog() -> dict[str, dict[str, object]]:
    return {
        "station.acquire": {
            "inputs": {},
            "outputs": {},
            "resource_claims": [
                {
                    "resource_ref": "held-cell",
                    "resource_type": "cell",
                    "selector": "bound_profile_resource",
                    "scope": "action",
                    "mode": "exclusive",
                },
                {
                    "resource_ref": "retained-cell",
                    "resource_type": "cell",
                    "selector": "bound_profile_resource",
                    "scope": "until_handoff",
                    "mode": "exclusive",
                },
            ],
        },
        "station.release": {"inputs": {}, "outputs": {}},
        "other.wait": {"inputs": {}, "outputs": {}},
    }


def _revision(
    *,
    scope: str,
    resource_ref: str = "held-cell",
) -> WorkflowRevision:
    return WorkflowRevision.model_validate(
        {
            "schema_version": "2",
            "revision_id": f"rev-{scope}",
            "workflow_id": "resource-hold-contract",
            "invocations": [
                {"node_id": "acquire", "action_ref": "station.acquire"},
                {"node_id": "release", "action_ref": "station.release"},
                {"node_id": "blocker", "action_ref": "other.wait"},
            ],
            "control_edges": [{"source": "acquire", "target": "release"}],
            "resource_holds": [
                {
                    "hold_id": f"hold-{scope}",
                    "resource_ref": resource_ref,
                    "scope": scope,
                    "acquire_node_id": "acquire",
                    "release_node_id": "release",
                }
            ],
        }
    )


def _claim_ids(locks: ResourceLockManager, holder_id: str) -> set[str]:
    return {
        claim.resource_id
        for lease in locks.active_leases()
        if lease.holder_id == holder_id
        for claim in lease.claims
    }


def _result(state: NodeState) -> NodeExecutionResult:
    return NodeExecutionResult(
        state=state,
        envelope=ResultEnvelope(outputs={}),
        terminal_info={"physical_state": "confirmed"},
    )


def test_resource_hold_is_canonical_execution_content() -> None:
    revision = _revision(scope="until_handoff")
    changed = _revision(scope="workflow_block")

    assert revision.resource_holds[0].hold_id == "hold-until_handoff"
    assert revision.resource_holds[0].acquire_node_id == "acquire"
    assert revision.resource_holds[0].release_node_id == "release"
    assert revision.content_hash != changed.content_hash


def test_compile_rejects_hold_resource_missing_from_acquire_contract() -> None:
    revision = _revision(
        scope="until_handoff",
        resource_ref="not-in-acquire-contract",
    )

    with pytest.raises(WorkflowCompileError, match="(?i)resource|hold"):
        compile_workflow_revision(
            revision,
            task_id="invalid-resource-hold",
            action_catalog=_catalog(),
        )


@pytest.mark.parametrize("scope", ["until_handoff", "workflow_block"])
def test_release_success_releases_only_the_matching_resource_and_scope(
    scope: str,
) -> None:
    dag = compile_workflow_revision(
        _revision(scope=scope),
        task_id=f"release-success-{scope}",
        action_catalog=_catalog(),
    )
    locks = ResourceLockManager(runtime_epoch="resource-hold-test")
    release_started = asyncio.Event()
    allow_release = asyncio.Event()
    blocker_started = asyncio.Event()
    allow_blocker = asyncio.Event()
    release_terminal = asyncio.Event()

    async def dispatch(node: DagNode) -> NodeExecutionResult:
        if node.node_id == "release":
            release_started.set()
            await allow_release.wait()
        elif node.node_id == "blocker":
            blocker_started.set()
            await allow_blocker.wait()
        return _result(NodeState.SUCCESS)

    def on_terminal(node_id: str, _state: NodeState) -> None:
        if node_id == "release":
            release_terminal.set()

    async def scenario() -> None:
        executor = DagExecutor(
            dag,
            dispatch,
            resource_lock_manager=locks,
            on_node_terminal=on_terminal,
        )
        run_task = asyncio.create_task(executor.run())
        await asyncio.wait_for(
            asyncio.gather(release_started.wait(), blocker_started.wait()),
            timeout=1,
        )

        holder_id = f"{dag.task_id}:acquire"
        assert _claim_ids(locks, holder_id) == {
            "held-cell",
            "retained-cell",
        }

        allow_release.set()
        await asyncio.wait_for(release_terminal.wait(), timeout=1)
        assert _claim_ids(locks, holder_id) == {"retained-cell"}

        allow_blocker.set()
        states = await asyncio.wait_for(run_task, timeout=1)
        assert all(state == NodeState.SUCCESS for state in states.values())
        assert _claim_ids(locks, holder_id) == {"retained-cell"}

    asyncio.run(scenario())


@pytest.mark.parametrize("terminal", [NodeState.FAILED, NodeState.CANCELLED])
@pytest.mark.parametrize("scope", ["until_handoff", "workflow_block"])
def test_release_failure_or_cancel_does_not_release_hold(
    scope: str,
    terminal: NodeState,
) -> None:
    dag = compile_workflow_revision(
        _revision(scope=scope),
        task_id=f"release-{terminal.value}-{scope}",
        action_catalog=_catalog(),
    )
    locks = ResourceLockManager(runtime_epoch="resource-hold-test")
    release_started = asyncio.Event()
    allow_release = asyncio.Event()
    blocker_started = asyncio.Event()
    allow_blocker = asyncio.Event()
    release_terminal = asyncio.Event()

    async def dispatch(node: DagNode) -> NodeExecutionResult:
        if node.node_id == "release":
            release_started.set()
            await allow_release.wait()
            return _result(terminal)
        if node.node_id == "blocker":
            blocker_started.set()
            await allow_blocker.wait()
        return _result(NodeState.SUCCESS)

    def on_terminal(node_id: str, _state: NodeState) -> None:
        if node_id == "release":
            release_terminal.set()

    async def scenario() -> None:
        run_task = asyncio.create_task(
            DagExecutor(
                dag,
                dispatch,
                resource_lock_manager=locks,
                on_node_terminal=on_terminal,
            ).run()
        )
        await asyncio.wait_for(
            asyncio.gather(release_started.wait(), blocker_started.wait()),
            timeout=1,
        )
        allow_release.set()
        await asyncio.wait_for(release_terminal.wait(), timeout=1)

        holder_id = f"{dag.task_id}:acquire"
        assert _claim_ids(locks, holder_id) == {
            "held-cell",
            "retained-cell",
        }

        if terminal == NodeState.CANCELLED:
            allow_blocker.set()
        states = await asyncio.wait_for(run_task, timeout=1)
        assert states["release"] == terminal
        assert _claim_ids(locks, holder_id) == {
            "held-cell",
            "retained-cell",
        }

    asyncio.run(scenario())


def test_workflow_completion_does_not_implicitly_release_until_handoff() -> None:
    revision = WorkflowRevision.model_validate(
        {
            "schema_version": "2",
            "revision_id": "rev-until-handoff-no-release",
            "workflow_id": "until-handoff-no-release",
            "invocations": [
                {"node_id": "acquire", "action_ref": "station.acquire"}
            ],
        }
    )
    catalog = _catalog()
    catalog["station.acquire"] = {
        "inputs": {},
        "outputs": {},
        "resource_claims": [
            {
                "resource_ref": "held-cell",
                "resource_type": "cell",
                "selector": "bound_profile_resource",
                "scope": "until_handoff",
                "mode": "exclusive",
            }
        ],
    }
    dag = compile_workflow_revision(
        revision,
        task_id="until-handoff-no-release",
        action_catalog=catalog,
    )
    locks = ResourceLockManager(runtime_epoch="resource-hold-test")

    async def dispatch(_node: DagNode) -> NodeExecutionResult:
        return _result(NodeState.SUCCESS)

    states = asyncio.run(
        DagExecutor(dag, dispatch, resource_lock_manager=locks).run()
    )

    assert states == {"acquire": NodeState.SUCCESS}
    assert _claim_ids(locks, f"{dag.task_id}:acquire") == {"held-cell"}
