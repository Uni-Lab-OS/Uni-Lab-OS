"""Generic Runtime submission is compiled once, inside the OS service."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from unilabos.app.local_bridge.local_api import LocalApiState, create_app
from unilabos.app.local_bridge.schedule_ws import ScheduleSession
from unilabos.workflow.canonical import WorkflowRevision
from unilabos.workflow.dag_compile import (
    WorkflowCompileError,
    compile_workflow_revision,
)


ACTION_CATALOG: dict[str, dict[str, Any]] = {
    "generic-router.choose": {
        "action_type": "UniLabJsonCommand",
        "inputs": {"reading": {"type": "number"}},
        "outputs": {"branch": {"type": "string"}},
        "timing": {"estimated_duration_s": 2.0},
        "resource_claims": [
            {
                "resource_id": "routing-cell-1",
                "quantity": 1,
                "scope": "workflow_block",
                "mode": "exclusive",
            }
        ],
        "effects": [{"op": "observe", "resource_id": "routing-cell-1"}],
    },
    "generic-worker.execute": {
        "action_type": "UniLabJsonCommand",
        "inputs": {},
        "outputs": {"completed": {"type": "boolean"}},
        "resource_claims": [],
        "effects": [],
    },
}


class FakeTransport:
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []

    async def send(self, message: dict[str, Any]) -> None:
        self.received.append(message)


def _make_client() -> tuple[TestClient, FakeTransport, ScheduleSession]:
    os_side = FakeTransport()
    schedule = ScheduleSession(os_side.send)
    state = LocalApiState(schedule, action_catalog=ACTION_CATALOG)
    return TestClient(create_app(lambda: state)), os_side, schedule


def _source_request(*, forged_suffix: str) -> dict[str, Any]:
    return {
        "profile_ref": "profiles/generic-branch.yaml",
        "source": {
            "format": "canonical_workflow_v2",
            "payload": {
                "schema_version": "2",
                "revision_id": f"client-revision-{forged_suffix}",
                "workflow_id": "generic-branch",
                "invocations": [
                    {
                        "node_id": "route",
                        "action_ref": "generic-router.choose",
                        "node_type": "branch",
                        "input_bindings": {
                            "reading": {"kind": "literal", "value": 1.25}
                        },
                        "output_schema": {
                            "forged": {"type": f"client-{forged_suffix}"}
                        },
                        "resource_claims": [
                            {
                                "resource_id": f"forged-{forged_suffix}",
                                "mode": "exclusive",
                            }
                        ],
                        "effects": [{"op": "forged", "value": forged_suffix}],
                        "estimated_duration_s": 999,
                    },
                    {
                        "node_id": "selected",
                        "action_ref": "generic-worker.execute",
                    },
                    {
                        "node_id": "not-selected",
                        "action_ref": "generic-worker.execute",
                    },
                ],
                "control_edges": [
                    {
                        "edge_id": "yes-edge",
                        "source": "route",
                        "target": "selected",
                        "branch": "yes",
                    },
                    {
                        "edge_id": "no-edge",
                        "source": "route",
                        "target": "not-selected",
                        "branch": "no",
                    },
                ],
            },
        },
        "parameters": {"operator_note": "server-compile"},
    }


def test_runtime_service_compiles_catalog_contract_and_ignores_forged_fields() -> None:
    client, os_side, _schedule = _make_client()

    first_response = client.post(
        "/api/runtime/local/runs",
        json=_source_request(forged_suffix="one"),
    )
    second_response = client.post(
        "/api/runtime/local/runs",
        json=_source_request(forged_suffix="two"),
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert len(os_side.received) == 2
    first_dag = os_side.received[0]["data"]
    second_dag = os_side.received[1]["data"]
    first_route = next(
        node for node in first_dag["nodes"] if node["node_id"] == "route"
    )
    second_route = next(
        node for node in second_dag["nodes"] if node["node_id"] == "route"
    )

    assert first_dag["workflow_revision_hash"] == second_dag[
        "workflow_revision_hash"
    ]
    assert len(first_dag["workflow_revision_hash"]) == 64
    assert first_route["idempotency_key"] == second_route["idempotency_key"]
    assert len(first_route["idempotency_key"]) == 64
    assert first_route["input_schema"] == ACTION_CATALOG["generic-router.choose"][
        "inputs"
    ]
    assert first_route["output_schema"] == ACTION_CATALOG["generic-router.choose"][
        "outputs"
    ]
    assert first_route["resource_claims"] == ACTION_CATALOG[
        "generic-router.choose"
    ]["resource_claims"]
    assert first_route["effects"] == ACTION_CATALOG["generic-router.choose"][
        "effects"
    ]
    assert first_route["estimated_duration_s"] == 2.0
    assert first_route["action_type"] == "UniLabJsonCommand"
    assert first_route["source_node_id"] == "route"
    assert first_route["canonical_index"] == 0
    assert first_route == second_route
    assert first_dag["edges"] == [
        {
            "source_node_uuid": "route",
            "target_node_uuid": "selected",
            "branch": "yes",
        },
        {
            "source_node_uuid": "route",
            "target_node_uuid": "not-selected",
            "branch": "no",
        },
    ]


def test_runtime_service_rejects_client_precompiled_workflow_with_zero_dispatch() -> None:
    client, os_side, _schedule = _make_client()
    request = _source_request(forged_suffix="raw")
    precompiled = {
        "workflow": {
            "name": "client-compiled",
            "workflow_revision_hash": "client-is-not-authority",
            "nodes": request["source"]["payload"]["invocations"],
            "edges": request["source"]["payload"]["control_edges"],
        }
    }

    response = client.post("/api/runtime/local/runs", json=precompiled)

    assert response.status_code == 400
    assert os_side.received == []


def test_runtime_service_rejects_empty_source_with_zero_dispatch() -> None:
    client, os_side, _schedule = _make_client()
    invalid = copy.deepcopy(_source_request(forged_suffix="empty"))
    invalid["source"]["payload"]["invocations"] = []

    response = client.post("/api/runtime/local/runs", json=invalid)

    assert response.status_code == 400
    assert os_side.received == []


def test_compiler_rejects_unknown_action_even_with_self_supplied_contract() -> None:
    payload = copy.deepcopy(_source_request(forged_suffix="unknown")["source"]["payload"])
    payload["invocations"] = [
        {
            "node_id": "unknown-action",
            "action_ref": "unregistered-device.execute",
            "output_schema": {"forged": {"type": "string"}},
            "resource_claims": [
                {
                    "resource_id": "forged-resource",
                    "mode": "exclusive",
                    "scope": "action",
                }
            ],
            "effects": [{"op": "forged-effect"}],
            "estimated_duration_s": 999,
        }
    ]
    payload["control_edges"] = []
    revision = WorkflowRevision.model_validate(payload)

    with pytest.raises(WorkflowCompileError) as caught:
        compile_workflow_revision(
            revision,
            task_id="unknown-action-run",
            action_catalog=ACTION_CATALOG,
        )

    assert caught.value.code == "ACTION_NOT_FOUND"
    assert "unregistered-device.execute" in str(caught.value)


def test_runtime_service_rejects_unknown_canonical_action_before_scheduling() -> None:
    client, os_side, schedule = _make_client()
    request = copy.deepcopy(_source_request(forged_suffix="unknown-api"))
    unknown_ref = "unregistered-device.execute"
    request["source"]["payload"]["invocations"][0]["action_ref"] = unknown_ref

    response = client.post("/api/runtime/local/runs", json=request)

    assert os_side.received == []
    assert schedule._runs == {}  # noqa: SLF001
    assert response.status_code == 400
    detail = str(response.json()["detail"])
    assert detail.split(":", maxsplit=1)[0] == "ACTION_NOT_FOUND"
    assert unknown_ref in detail


def test_compiler_uses_registered_catalog_contract_not_invocation_fields() -> None:
    request = _source_request(forged_suffix="compiler-guard")
    revision = WorkflowRevision.model_validate(request["source"]["payload"])

    dag = compile_workflow_revision(
        revision,
        task_id="catalog-authority-run",
        action_catalog=ACTION_CATALOG,
    )

    route = dag.nodes["route"]
    assert route.output_schema == ACTION_CATALOG["generic-router.choose"]["outputs"]
    assert route.resource_claims == ACTION_CATALOG["generic-router.choose"][
        "resource_claims"
    ]
    assert route.effects == ACTION_CATALOG["generic-router.choose"]["effects"]
    assert route.estimated_duration_s == 2.0
