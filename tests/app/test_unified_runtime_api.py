"""Unified frontend API: lossless DAG, events, debugger, and durable editing."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from unilabos.app.local_bridge.local_api import LocalApiState, create_app
from unilabos.app.local_bridge.offline_os import OfflineOS
from unilabos.app.local_bridge.schedule_ws import ScheduleSession
from unilabos.runtime.event_store import SQLiteEventJournal
from unilabos.runtime.workflow_store import WorkflowDocumentStore
from unilabos.scheduler.dag_model import NodeState
from unilabos.scheduler.resource_lock import ResourceLockManager


ACTION_CATALOG: dict[str, dict[str, Any]] = {
    "balance-1.measure": {
        "inputs": {},
        "outputs": {},
    },
    "pump-1.dose": {
        "inputs": {"volume": {"type": "number", "default": 5}},
        "outputs": {},
    },
    "camera-1.inspect": {
        "inputs": {},
        "outputs": {},
    },
    "heater-1.heat": {
        "inputs": {"temperature": {"type": "number", "default": 60}},
        "outputs": {},
    },
}


def _revision(revision_id: str = "rev-control-1") -> dict[str, Any]:
    return {
        "schema_version": "2",
        "revision_id": revision_id,
        "workflow_id": "control-demo",
        "invocations": [
            {
                "node_id": "measure",
                "action_ref": "balance-1.measure",
                "name": "称量样品",
            },
            {
                "node_id": "branch",
                "action_ref": "os_control.branch",
                "node_type": "branch",
                "name": "质量是否合格",
                "input_bindings": {
                    "condition": {"kind": "literal", "value": True}
                },
            },
            {
                "node_id": "dose",
                "action_ref": "pump-1.dose",
                "name": "合格路径：加液",
                "input_bindings": {
                    "volume": {"kind": "literal", "value": 5}
                },
            },
            {
                "node_id": "inspect",
                "action_ref": "camera-1.inspect",
                "name": "不合格路径：复检",
            },
            {
                "node_id": "join",
                "action_ref": "os_control.join",
                "node_type": "join",
                "name": "路径汇合",
            },
            {
                "node_id": "heat",
                "action_ref": "heater-1.heat",
                "name": "加热",
                "input_bindings": {
                    "temperature": {"kind": "literal", "value": 60}
                },
            },
        ],
        "control_edges": [
            {"edge_id": "e1", "source": "measure", "target": "branch"},
            {
                "edge_id": "e2",
                "source": "branch",
                "target": "dose",
                "branch": "true",
            },
            {
                "edge_id": "e3",
                "source": "branch",
                "target": "inspect",
                "branch": "false",
            },
            {"edge_id": "e4", "source": "dose", "target": "join"},
            {"edge_id": "e5", "source": "inspect", "target": "join"},
            {"edge_id": "e6", "source": "join", "target": "heat"},
        ],
        "layout": {
            "nodes": {
                "measure": {"x": 40, "y": 180},
                "branch": {"x": 250, "y": 180},
                "dose": {"x": 470, "y": 90},
                "inspect": {"x": 470, "y": 280},
                "join": {"x": 690, "y": 180},
                "heat": {"x": 900, "y": 180},
            }
        },
    }


def _client(
    tmp_path: Path,
    *,
    results: dict[str, NodeState] | None = None,
    action_catalog: dict[str, dict[str, Any]] | None = None,
) -> tuple[TestClient, LocalApiState]:
    journal = SQLiteEventJournal(
        tmp_path / "runtime.sqlite",
        runtime_epoch="unified-api-test",
    )
    locks = ResourceLockManager(runtime_epoch="unified-api-test")
    offline = OfflineOS(
        results=results,
        resource_lock_manager=locks,
        journal=journal,
    )
    schedule = ScheduleSession(offline.receive, session_id="unified-api-test")
    offline.bind(schedule)
    state = LocalApiState(
        schedule,
        journal=journal,
        resource_lock_manager=locks,
        action_catalog=action_catalog or ACTION_CATALOG,
        workflow_store=WorkflowDocumentStore(tmp_path / "workflows"),
    )
    return TestClient(create_app(lambda: state)), state


def _wait_for(
    client: TestClient,
    run_id: str,
    predicate,
    *,
    attempts: int = 50,
) -> dict[str, Any]:
    last: dict[str, Any] = {}
    for _ in range(attempts):
        response = client.get(f"/api/v1/runtime/runs/{run_id}")
        assert response.status_code == 200
        last = response.json()
        if predicate(last):
            return last
    raise AssertionError(f"run projection did not settle: {last}")


def test_runtime_capabilities_expose_python_fallback_boundary(
    tmp_path: Path,
) -> None:
    client, _state = _client(tmp_path)

    with client:
        response = client.get("/api/v1/runtime/capabilities")

    assert response.status_code == 200
    body = response.json()
    assert body["engine"]["id"] == "python-fallback"
    assert body["layers"]["layerA"]["resourceKinds"] == ["device"]
    assert body["layers"]["layerB"]["supported"] is False
    assert body["materials"]["automaticAllocation"] is False


def test_layer_b_claim_is_rejected_by_validate_and_run_api(
    tmp_path: Path,
) -> None:
    catalog = {
        **ACTION_CATALOG,
        "balance-1.measure": {
            **ACTION_CATALOG["balance-1.measure"],
            "resource_claims": [
                {
                    "resource_kind": "material",
                    "resource_id": "sample-1",
                }
            ],
        },
    }
    client, _state = _client(tmp_path, action_catalog=catalog)

    with client:
        validation = client.post(
            "/api/v1/workflows:validate",
            json={"revision": _revision("rev-layer-b")},
        )
        created = client.post(
            "/api/v1/runtime/runs",
            json={
                "source": {
                    "format": "workflow_revision_v2",
                    "revision": _revision("rev-layer-b"),
                }
            },
        )

    assert validation.status_code == 200
    assert validation.json()["valid"] is False
    assert validation.json()["issues"][0]["code"] == (
        "PYTHON_FALLBACK_CAPABILITY_UNSUPPORTED"
    )
    assert created.status_code == 422
    assert "only live device locks are supported" in (
        created.json()["detail"]["detail"]
    )


def test_edit_validate_run_step_breakpoint_and_event_sequence(tmp_path: Path) -> None:
    client, _state = _client(tmp_path)
    with client:
        validation = client.post(
            "/api/v1/workflows:validate",
            json={"revision": _revision()},
        )
        assert validation.status_code == 200
        assert validation.json()["valid"] is True

        saved = client.put(
            "/api/v1/workflows/control-demo/graph",
            json={"revision": _revision()},
        )
        assert saved.status_code == 200
        fetched = client.get("/api/v1/workflows/control-demo/graph")
        canonical = fetched.json()["revision"]["canonical"]
        assert canonical["revision_id"] == "rev-control-1"
        assert [item["node_id"] for item in canonical["invocations"]] == [
            "measure",
            "branch",
            "dose",
            "inspect",
            "join",
            "heat",
        ]
        assert canonical["layout"] == _revision()["layout"]

        created = client.post(
            "/api/v1/runtime/runs",
            json={
                "source": {
                    "format": "workflow_revision_v2",
                    "revision": _revision(),
                },
                "debug": {
                    "pause_on_start": True,
                    "breakpoints": ["branch"],
                },
            },
        )
        assert created.status_code == 200
        run_id = created.json()["id"]

        paused = _wait_for(
            client,
            run_id,
            lambda run: run.get("debug", {}).get("status") == "paused",
        )
        assert paused["status"] == "pending"
        nodes = client.get(
            f"/api/v1/runtime/runs/{run_id}/nodes"
        ).json()["items"]
        assert {node["state"] for node in nodes} == {"pending"}

        stepped = client.post(
            f"/api/v1/runtime/runs/{run_id}/commands",
            json={"command": "step", "payload": {}},
        )
        assert stepped.status_code == 200
        breakpoint_pause = _wait_for(
            client,
            run_id,
            lambda run: (
                run.get("debug", {}).get("status") == "paused"
                and run.get("debug", {}).get("pausedBeforeNodeId") == "branch"
                and next(
                    node
                    for node in client.get(
                        f"/api/v1/runtime/runs/{run_id}/nodes"
                    ).json()["items"]
                    if node["nodeId"] == "measure"
                )["state"]
                == "success"
            ),
        )
        assert breakpoint_pause["debug"]["pausedBeforeNodeId"] == "branch"

        assert client.post(
            f"/api/v1/runtime/runs/{run_id}/commands",
            json={"command": "step_over", "payload": {}},
        ).status_code == 200
        _wait_for(
            client,
            run_id,
            lambda run: (
                run.get("debug", {}).get("status") == "paused"
                and {
                    node["nodeId"]: node["state"]
                    for node in client.get(
                        f"/api/v1/runtime/runs/{run_id}/nodes"
                    ).json()["items"]
                }.items()
                >= {("branch", "success"), ("inspect", "skipped")}
            ),
        )
        stepped_nodes = {
            node["nodeId"]: node["state"]
            for node in client.get(
                f"/api/v1/runtime/runs/{run_id}/nodes"
            ).json()["items"]
        }
        assert stepped_nodes["branch"] == "success"
        assert stepped_nodes["inspect"] == "skipped"
        assert client.post(
            f"/api/v1/runtime/runs/{run_id}/commands",
            json={"command": "continue", "payload": {}},
        ).status_code == 200
        terminal = _wait_for(
            client,
            run_id,
            lambda run: run["status"] == "completed",
        )
        assert terminal["status"] == "completed"

        nodes = client.get(
            f"/api/v1/runtime/runs/{run_id}/nodes"
        ).json()["items"]
        states = {node["nodeId"]: node["state"] for node in nodes}
        assert states == {
            "measure": "success",
            "branch": "success",
            "dose": "success",
            "inspect": "skipped",
            "join": "success",
            "heat": "success",
        }

        event_page = client.get(
            f"/api/v1/runtime/runs/{run_id}/events?after_seq=0"
        ).json()
        sequences = [event["seq"] for event in event_page["events"]]
        assert sequences == sorted(sequences)
        assert len(sequences) == len(set(sequences))
        event_types = {event["type"] for event in event_page["events"]}
        assert {
            "debug.paused",
            "debug.stepping",
            "node.started",
            "node.result",
            "node.skipped",
            "run.status",
        }.issubset(event_types)
        tail = client.get(
            f"/api/v1/runtime/runs/{run_id}/events"
            f"?after_seq={event_page['nextSeq']}"
        ).json()
        assert tail["events"] == []

        with client.websocket_connect(
            f"/api/v1/runtime/events?run_id={run_id}"
            f"&after_seq={event_page['nextSeq']}"
        ) as websocket:
            time.sleep(0.2)
            websocket.send_json(
                {"action": "subscribe", "runId": run_id, "afterSeq": 0}
            )
            replayed_event = websocket.receive_json()
            assert replayed_event["seq"] == 1
            assert replayed_event["runId"] == run_id


def test_authoring_api_roundtrips_control_dag_with_exact_device_ids(
    tmp_path: Path,
) -> None:
    client, _state = _client(tmp_path)
    revision = _revision()
    source_uri = "workflows/control-demo.py"

    with client:
        generated = client.post(
            "/api/v1/authoring/generate-python",
            json={
                "base_revision_id": revision["revision_id"],
                "canonical_ir": revision,
                "source_uri": source_uri,
            },
        )
        assert generated.status_code == 200
        generated_result = generated.json()
        assert generated_result["diagnostics"] == []
        python_source = generated_result["candidate"]["python_source"]
        assert "device('balance-1').measure()" in python_source
        assert "device('pump-1').dose(volume=5)" in python_source
        assert "if True:" in python_source

        compiled = client.post(
            "/api/v1/authoring/compile",
            json={
                "base_revision_id": revision["revision_id"],
                "python_source": python_source,
                "source_uri": source_uri,
            },
        )
        assert compiled.status_code == 200
        compiled_result = compiled.json()
        assert compiled_result["diagnostics"] == []
        canonical = compiled_result["candidate"]["canonical_ir"]
        assert canonical["workflow_id"] == revision["workflow_id"]
        assert [
            invocation["action_ref"] for invocation in canonical["invocations"]
        ] == [
            "balance-1.measure",
            "os_control.branch",
            "pump-1.dose",
            "camera-1.inspect",
            "os_control.join",
            "heater-1.heat",
        ]
        assert {
            edge.get("branch")
            for edge in canonical["control_edges"]
        }.issuperset({"true", "false"})


def test_structured_validation_problem_and_optimistic_save_conflict(
    tmp_path: Path,
) -> None:
    client, _state = _client(tmp_path)
    with client:
        invalid = _revision()
        invalid["control_edges"].append(
            {"edge_id": "cycle", "source": "heat", "target": "measure"}
        )
        validation = client.post(
            "/api/v1/workflows:validate",
            json={"revision": invalid},
        )
        assert validation.status_code == 200
        assert validation.json()["valid"] is False

        assert client.put(
            "/api/v1/workflows/control-demo/graph",
            json={"revision": _revision()},
        ).status_code == 200
        conflict = client.put(
            "/api/v1/workflows/control-demo/graph",
            json={
                "revision": _revision("rev-control-2"),
                "expectedRevisionId": "stale-revision",
            },
        )
        assert conflict.status_code == 409
        problem = conflict.json()["detail"]
        assert problem["code"] == "WORKFLOW_REVISION_CONFLICT"
        assert problem["status"] == 409


def test_failed_node_projects_exception_and_structured_command_rejection(
    tmp_path: Path,
) -> None:
    client, _state = _client(
        tmp_path,
        results={"measure": NodeState.FAILED},
    )
    with client:
        created = client.post(
            "/api/v1/runtime/runs",
            json={
                "source": {
                    "format": "workflow_revision_v2",
                    "revision": _revision(),
                }
            },
        )
        assert created.status_code == 200
        run_id = created.json()["id"]
        terminal = _wait_for(
            client,
            run_id,
            lambda run: run["status"] == "reconciling",
        )
        # A physical action failure keeps the public run in the conservative
        # reconciliation state until an operator confirms device safety.
        assert terminal == {"id": run_id, "status": "reconciling"}

        nodes = client.get(
            f"/api/v1/runtime/runs/{run_id}/nodes"
        ).json()["items"]
        states = {node["nodeId"]: node["state"] for node in nodes}
        assert states["measure"] == "failed"
        assert all(
            state == "cancelled"
            for node_id, state in states.items()
            if node_id != "measure"
        )

        events = client.get(
            f"/api/v1/runtime/runs/{run_id}/events?after_seq=0"
        ).json()["events"]
        assert any(
            event["type"] == "node.exception"
            and event["nodeId"] == "measure"
            for event in events
        )

        rejected = client.post(
            f"/api/v1/runtime/runs/{run_id}/commands",
            json={"command": "step", "payload": {}},
        )
        assert rejected.status_code == 409
        problem = rejected.json()["detail"]
        assert problem["code"] == "DEBUG_COMMAND_REJECTED"
        assert problem["runId"] == run_id
