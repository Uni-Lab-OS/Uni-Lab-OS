"""Executable capability boundary for the deployment-time Python fallback."""

from __future__ import annotations

import asyncio

import pytest

from unilabos.scheduler.dag_model import DagNode, TaskDag
from unilabos.scheduler.python_fallback import (
    PythonFallbackCapabilityError,
    python_fallback_capabilities,
    python_fallback_lease_request,
    validate_python_fallback_dag,
)
from unilabos.scheduler.resource_lock import ResourceLockManager


def _dag(node: DagNode, *, task_id: str = "run-1") -> TaskDag:
    return TaskDag(
        task_id=task_id,
        notebook_id="",
        server_info={},
        nodes={node.node_id: node},
        edges=[],
    )


def test_capabilities_fail_closed_on_layer_b_and_allocation() -> None:
    capabilities = python_fallback_capabilities()

    assert capabilities["engine"]["id"] == "python-fallback"
    assert capabilities["engine"]["selection"] == "deployment"
    assert capabilities["engine"]["hotFailover"] is False
    assert capabilities["layers"]["layerA"]["resourceKinds"] == ["device"]
    assert capabilities["layers"]["layerB"]["supported"] is False
    assert capabilities["materials"]["automaticAllocation"] is False
    assert capabilities["materials"]["acceptedAssignmentModes"] == ["prebound"]


def test_bound_execution_device_is_always_part_of_layer_a_admission() -> None:
    node = DagNode(
        node_id="move",
        device_id="robot-1",
        action="move",
        resource_claims=[
            {
                "resource_kind": "device",
                "resource_id": "camera-1",
                "mode": "exclusive",
            }
        ],
    )

    request = python_fallback_lease_request(node, run_id="run-1")

    assert request.holder_id == "run-1:move"
    assert [claim.resource_id for claim in request.claims] == [
        "camera-1",
        "device:robot-1",
    ]
    assert {claim.resource_kind for claim in request.claims} == {"device"}


@pytest.mark.parametrize("resource_kind", ["material", "slot"])
def test_layer_b_claims_are_rejected_before_dispatch(resource_kind: str) -> None:
    node = DagNode(
        node_id="process",
        device_id="station-1",
        action="process",
        resource_claims=[
            {
                "resource_kind": resource_kind,
                "resource_id": f"{resource_kind}-1",
            }
        ],
    )

    with pytest.raises(
        PythonFallbackCapabilityError,
        match="only live device locks are supported",
    ):
        validate_python_fallback_dag(_dag(node))


def test_unresolved_candidate_selector_is_not_silently_allocated() -> None:
    node = DagNode(
        node_id="inspect",
        device_id="camera-1",
        action="inspect",
        resource_claims=[
            {
                "resource_kind": "device",
                "resource_type": "camera",
                "selector": "any_available",
            }
        ],
    )

    with pytest.raises(
        PythonFallbackCapabilityError,
        match="does not allocate candidates",
    ):
        validate_python_fallback_dag(_dag(node))


def test_same_bound_device_conflicts_across_runs() -> None:
    async def scenario() -> None:
        manager = ResourceLockManager(runtime_epoch="epoch-1")
        first = python_fallback_lease_request(
            DagNode(node_id="a", device_id="robot-1", action="pick"),
            run_id="run-a",
        )
        second = python_fallback_lease_request(
            DagNode(node_id="b", device_id="robot-1", action="place"),
            run_id="run-b",
        )

        assert await manager.acquire_all(first) is not None
        assert await manager.acquire_all(second) is None

    asyncio.run(scenario())


def test_always_free_node_does_not_invent_a_device_lease() -> None:
    request = python_fallback_lease_request(
        DagNode(
            node_id="read-cache",
            device_id="cache",
            action="read",
            always_free=True,
        ),
        run_id="run-1",
    )

    assert request.claims == ()
