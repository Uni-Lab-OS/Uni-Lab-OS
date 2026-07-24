"""Canonical projection, dual-edge consistency, and result publication contracts."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from unilabos.runtime.service import RuntimeService
from unilabos.scheduler.dag_executor import DagExecutor
from unilabos.scheduler.dag_model import NodeState, TaskDag
from unilabos.scheduler.result_store import NodeExecutionResult, ResultEnvelope
from unilabos.workflow.canonical import WorkflowRevision


ACTION_CATALOG: dict[str, dict[str, Any]] = {
    "sensor.measure": {
        "inputs": {},
        "outputs": {"reading": {"type": "number"}},
        "resource_claims": [],
        "effects": [],
    },
    "router.choose": {
        "inputs": {"reading": {"type": "number"}},
        "outputs": {"branch": {"type": "string"}},
        "resource_claims": [],
        "effects": [],
    },
    "worker.execute": {
        "inputs": {},
        "outputs": {"completed": {"type": "boolean"}},
        "resource_claims": [],
        "effects": [],
    },
}


class RecordingSchedule:
    def __init__(self) -> None:
        self.callback: Any = None
        self.submitted: list[TaskDag] = []

    def on_job_status(self, callback: Any) -> None:
        self.callback = callback

    async def submit_dag(self, dag: TaskDag) -> Any:
        self.submitted.append(dag)
        return type("Handle", (), {"dag": dag})()

    def get_run(self, _task_id: str) -> None:
        return None


def _rich_canonical_payload() -> dict[str, Any]:
    return {
        "schema_version": "2",
        "revision_id": "revision-rich-1",
        "workflow_id": "rich-branch-workflow",
        "invocations": [
            {
                "node_id": "measure",
                "action_ref": "sensor.measure",
                "name": "Measure input",
            },
            {
                "node_id": "route",
                "action_ref": "router.choose",
                "node_type": "branch",
                "name": "Choose route",
                "input_bindings": {
                    "reading": {
                        "kind": "node_output",
                        "node_id": "measure",
                        "output": "reading",
                    }
                },
            },
            {"node_id": "yes", "action_ref": "worker.execute"},
            {"node_id": "no", "action_ref": "worker.execute"},
        ],
        "control_edges": [
            {
                "edge_id": "route-yes",
                "source": "route",
                "target": "yes",
                "branch": "yes",
            },
            {
                "edge_id": "route-no",
                "source": "route",
                "target": "no",
                "branch": "no",
            },
        ],
        "data_edges": [
            {
                "edge_id": "reading-to-route",
                "source": "measure",
                "source_output": "reading",
                "target": "route",
                "target_input": "reading",
            }
        ],
    }


def test_runtime_workflow_projection_carries_lossless_canonical_payload() -> None:
    schedule = RecordingSchedule()
    service = RuntimeService(schedule, action_catalog=ACTION_CATALOG)
    source_payload = _rich_canonical_payload()
    canonical = WorkflowRevision.model_validate(source_payload)

    asyncio.run(
        service.start_run(
            {
                "source": {
                    "format": "canonical_workflow_v2",
                    "payload": source_payload,
                }
            }
        )
    )

    revision_projection = service.get_workflow()["revision"]
    projected_canonical = WorkflowRevision.model_validate(
        revision_projection["canonical"]
    )
    assert projected_canonical.content_hash == revision_projection["contentHash"]
    assert (
        projected_canonical.content_hash
        == schedule.submitted[0].workflow_revision_hash
    )
    assert projected_canonical.invocations[1].input_bindings == (
        canonical.invocations[1].input_bindings
    )
    assert revision_projection["canonical"]["invocations"][1][
        "input_bindings"
    ] == source_payload["invocations"][1]["input_bindings"]
    assert revision_projection["canonical"]["data_edges"] == source_payload[
        "data_edges"
    ]
    assert [
        edge["branch"]
        for edge in revision_projection["canonical"]["control_edges"]
    ] == ["yes", "no"]
    assert [
        invocation.output_schema
        for invocation in projected_canonical.invocations
    ] == [
        ACTION_CATALOG[invocation.action_ref]["outputs"]
        for invocation in projected_canonical.invocations
    ]


def test_workflow_revision_rejects_data_edge_binding_contradiction() -> None:
    payload = _rich_canonical_payload()
    payload["invocations"][1]["input_bindings"]["reading"] = {
        "kind": "literal",
        "value": 1.25,
    }

    with pytest.raises(ValueError):
        WorkflowRevision.model_validate(payload)


def _typed_output_dag() -> TaskDag:
    return TaskDag.from_message(
        {
            "task_id": "typed-output-publication",
            "nodes": [
                {
                    "node_id": "measure",
                    "device_id": "sensor",
                    "action": "measure",
                    "output_schema": {"reading": {"type": "number"}},
                },
                {
                    "node_id": "consume",
                    "device_id": "consumer",
                    "action": "consume",
                    "input_bindings": {
                        "amount": {
                            "kind": "node_output",
                            "node_id": "measure",
                            "output": "reading",
                        }
                    },
                    "input_schema": {"amount": {"type": "number"}},
                },
            ],
            "edges": [
                {
                    "source_node_uuid": "measure",
                    "target_node_uuid": "consume",
                }
            ],
        }
    )


def test_invalid_success_output_fails_producer_before_downstream_consumption() -> None:
    dispatched: list[str] = []

    async def submit(node: Any) -> NodeExecutionResult:
        dispatched.append(node.node_id)
        return NodeExecutionResult(
            state=NodeState.SUCCESS,
            envelope=ResultEnvelope(outputs={"reading": "not-a-number"}),
        )

    executor = DagExecutor(_typed_output_dag(), submit)
    states = asyncio.run(executor.run())

    assert dispatched == ["measure"]
    assert states == {
        "measure": NodeState.FAILED,
        "consume": NodeState.CANCELLED,
    }
    assert "measure" not in executor.results
    assert executor.errors["measure"].code == "OUTPUT_SCHEMA_MISMATCH"
