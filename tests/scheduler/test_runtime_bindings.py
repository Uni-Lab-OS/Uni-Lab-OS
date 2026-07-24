"""TaskDag v2 result binding is validated before downstream dispatch."""

from __future__ import annotations

import asyncio
import importlib

import pytest

from unilabos.scheduler.dag_executor import DagExecutor
from unilabos.scheduler.dag_model import NodeState, TaskDag


def _result_api():
    try:
        return importlib.import_module("unilabos.scheduler.result_store")
    except ModuleNotFoundError as exc:
        if exc.name != "unilabos.scheduler.result_store":
            raise
        pytest.fail("typed ResultEnvelope capability is missing", pytrace=False)


def _dag() -> TaskDag:
    return TaskDag.from_message(
        {
            "task_id": "bindings",
            "nodes": [
                {
                    "node_id": "measure",
                    "device_id": "balance",
                    "action": "measure",
                    "input_bindings": {"sample": {"kind": "literal", "value": "sample-1"}},
                    "output_schema": {"mass": {"type": "number"}},
                },
                {
                    "node_id": "dose",
                    "device_id": "pump",
                    "action": "dose",
                    "input_bindings": {
                        "amount": {
                            "kind": "node_output",
                            "node_id": "measure",
                            "output": "mass",
                        }
                    },
                    "input_schema": {"amount": {"type": "number"}},
                    "output_schema": {},
                },
            ],
            "edges": [{"source_node_uuid": "measure", "target_node_uuid": "dose"}],
        }
    )


def test_result_is_committed_then_materialized_before_downstream_dispatch() -> None:
    result_api = _result_api()
    dispatched: list[tuple[str, dict[str, object]]] = []

    async def submit(node):
        dispatched.append((node.node_id, dict(node.action_args)))
        if node.node_id == "measure":
            return result_api.NodeExecutionResult(
                state=NodeState.SUCCESS,
                envelope=result_api.ResultEnvelope(outputs={"mass": 1.25}),
            )
        return result_api.NodeExecutionResult(
            state=NodeState.SUCCESS,
            envelope=result_api.ResultEnvelope(outputs={}),
        )

    states = asyncio.run(DagExecutor(_dag(), submit).run())
    assert states == {"measure": NodeState.SUCCESS, "dose": NodeState.SUCCESS}
    assert dispatched == [("measure", {"sample": "sample-1"}), ("dose", {"amount": 1.25})]


@pytest.mark.parametrize(
    "outputs",
    [
        {"other": 1.25},
        {"mass": "wrong"},
    ],
)
def test_bad_upstream_result_rejects_preflight_with_zero_downstream_dispatch(
    outputs: dict[str, object],
) -> None:
    result_api = _result_api()
    dispatched: list[str] = []
    executor: DagExecutor

    async def submit(node):
        dispatched.append(node.node_id)
        return result_api.NodeExecutionResult(
            state=NodeState.SUCCESS,
            envelope=result_api.ResultEnvelope(outputs=outputs),
        )

    executor = DagExecutor(_dag(), submit)
    states = asyncio.run(executor.run())
    assert dispatched == ["measure"]
    assert states == {
        "measure": NodeState.FAILED,
        "dose": NodeState.CANCELLED,
    }
    assert executor.errors["measure"].code == "OUTPUT_SCHEMA_MISMATCH"
