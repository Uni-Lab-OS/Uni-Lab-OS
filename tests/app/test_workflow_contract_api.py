"""Phase 01 HTTP and P0-1 Authoring Interface contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from unilabos.app.scheduler.api import create_scheduler_router  # noqa: E402
from unilabos.app.workflow_api import (  # noqa: E402
    create_workflow_app,
    format_sse_event,
)
from unilabos.workflow.models import CandidateCompilation  # noqa: E402
from unilabos.workflow.service import WorkflowService  # noqa: E402
from unilabos.workflow.store import WorkflowStore  # noqa: E402

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
NODE_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class FakeAuthoringCompiler:
    compiler_version = "phase-01-fake-v1"
    template_catalog_fingerprint = "sha256:" + "c" * 64

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> CandidateCompilation:
        del source_uri
        if "syntax error" in python_source:
            return CandidateCompilation(
                diagnostics=[
                    {
                        "severity": "error",
                        "code": "PYTHON_SYNTAX_ERROR",
                        "message": "Python 语法错误",
                        "source_range": {
                            "start_line": 1,
                            "start_column": 1,
                            "end_line": 1,
                            "end_column": 7,
                        },
                    }
                ],
                graph=None,
                normalized_python_source=None,
                source_map=[],
                changeset=None,
                compiler_version=self.compiler_version,
                template_catalog_fingerprint=self.template_catalog_fingerprint,
            )

        normalized = (
            python_source if python_source.endswith("\n") else python_source + "\n"
        )
        if "source_only" in python_source:
            graph = applied_graph
            changeset = {
                "kind": "source_only",
                "created_node_uuids": [],
                "updated_node_uuids": [],
                "deleted_node_uuids": [],
                "created_edge_uuids": [],
                "updated_edge_uuids": [],
                "deleted_edge_uuids": [],
                "reserved_metadata_changed": False,
            }
        else:
            graph = {
                "workflow": applied_graph["workflow"],
                "nodes": [
                    {
                        "uuid": NODE_UUID,
                        "workflow_node_template_uuid": None,
                        "parent_uuid": None,
                        "material_uuid": None,
                        "name": "compiled node",
                        "status": "idle",
                        "type": "compute",
                        "icon": None,
                        "pose": {},
                        "param": {},
                        "footer": None,
                        "action_name": None,
                        "action_type": None,
                        "execution_policy": {},
                        "disabled": False,
                        "minimized": False,
                        "script": None,
                        "description": None,
                        "meta_data": {},
                    }
                ],
                "edges": [],
                "node_templates": [],
                "handle_templates": [],
            }
            changeset = {
                "kind": "graph",
                "created_node_uuids": [NODE_UUID],
                "updated_node_uuids": [],
                "deleted_node_uuids": [],
                "created_edge_uuids": [],
                "updated_edge_uuids": [],
                "deleted_edge_uuids": [],
                "reserved_metadata_changed": False,
            }
        return CandidateCompilation(
            diagnostics=[],
            graph=graph,
            normalized_python_source=normalized,
            source_map=[
                {
                    "workflow_node_uuid": NODE_UUID,
                    "start_line": 1,
                    "start_column": 1,
                    "end_line": 1,
                    "end_column": max(1, len(python_source)),
                }
            ]
            if changeset["kind"] == "graph"
            else [],
            changeset=changeset,
            compiler_version=self.compiler_version,
            template_catalog_fingerprint=self.template_catalog_fingerprint,
        )


@pytest.fixture()
def client(tmp_path: Path):
    package_root = tmp_path / "package"
    package_root.mkdir()
    store = WorkflowStore(tmp_path / "unilabos_data" / "workflow.db")
    service = WorkflowService(store, compiler=FakeAuthoringCompiler())
    service.create_workflow(
        name="authoring",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="fixture_package",
        package_root=package_root,
        relative_path="workflows/demo.py",
    )
    with TestClient(create_workflow_app(service)) as test_client:
        yield test_client, service, package_root
    store.close()


def _authoring(client: TestClient) -> dict[str, Any]:
    response = client.get(f"/api/v1/workflows/{WORKFLOW_UUID}/authoring")
    assert response.status_code == 200
    assert response.json()["code"] == 0
    return response.json()["data"]


def test_main_composition_can_hide_execution_shaped_scheduler_workflows() -> None:
    router = create_scheduler_router(
        lambda: None,
        include_execution_shaped_workflow_routes=False,
    )
    routes = {
        (route.path, ",".join(sorted(route.methods or []))) for route in router.routes
    }

    assert not any(path == "/api/v1/workflows" for path, _methods in routes)
    assert not any(
        path.startswith("/api/v1/workflows/{workflow_id}") for path, _methods in routes
    )
    assert any(path == "/api/v1/health" for path, _methods in routes)


def test_backend_envelope_graph_revision_and_task_snapshot(client) -> None:
    test_client, _service, _package_root = client
    graph = test_client.get(f"/api/v1/workflows/{WORKFLOW_UUID}/graph")
    assert graph.status_code == 200
    assert graph.json()["code"] == 0
    assert graph.json()["data"]["workflow"]["revision"] == 1

    saved = test_client.put(
        f"/api/v1/workflows/{WORKFLOW_UUID}/graph",
        json={
            "revision": 1,
            "nodes": [
                {
                    "uuid": NODE_UUID,
                    "name": "api node",
                    "status": "idle",
                    "type": "compute",
                    "pose": {},
                    "param": {},
                    "execution_policy": {},
                    "disabled": False,
                    "minimized": False,
                    "meta_data": {},
                }
            ],
            "edges": [],
        },
    )
    assert saved.status_code == 200
    assert saved.json()["data"]["workflow"]["revision"] == 2

    conflict = test_client.put(
        f"/api/v1/workflows/{WORKFLOW_UUID}/graph",
        json={"revision": 1, "nodes": [], "edges": []},
    )
    assert conflict.status_code == 409
    assert conflict.json() == {
        "code": 409,
        "error": {
            "code": "conflict",
            "message": "资源已发生冲突，请刷新后重试",
        },
    }

    created = test_client.post(
        "/api/v1/workflow-tasks",
        json={
            "workflow_uuid": WORKFLOW_UUID,
            "run_mode": "normal",
            "input": {},
            "meta_data": {},
        },
    )
    assert created.status_code == 201
    task = created.json()["data"]
    assert task["workflow_uuid"] == WORKFLOW_UUID
    assert task["workflow_snapshot"]["workflow"]["revision"] == 2
    jobs = test_client.get(f"/api/v1/workflow-tasks/{task['uuid']}/jobs").json()["data"]
    assert len(jobs) == 1
    assert jobs[0]["workflow_node_uuid"] == NODE_UUID
    assert "node_id" not in jobs[0]


def test_authoring_missing_draft_invalid_save_and_single_token_apply_flow(
    client,
) -> None:
    test_client, service, package_root = client
    missing = _authoring(test_client)
    assert missing["state"] == "draft_missing"
    assert missing["draft"] is None
    assert missing["candidate"] is None
    assert missing["applied_source"] is None
    assert missing["applied_graph"]["nodes"] == []
    assert not (package_root / "workflows").exists()

    invalid = test_client.put(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": "syntax error",
            "expected_draft_hash": None,
            "expected_workflow_revision": 1,
        },
    )
    assert invalid.status_code == 200
    invalid_aggregate = invalid.json()["data"]
    assert invalid_aggregate["state"] == "draft_invalid"
    assert invalid_aggregate["candidate"] is None
    assert invalid_aggregate["draft"]["diagnostics"][0]["code"] == (
        "PYTHON_SYNTAX_ERROR"
    )

    valid = test_client.put(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": "result = build()\n",
            "expected_draft_hash": invalid_aggregate["draft"]["draft_hash"],
            "expected_workflow_revision": 1,
        },
    )
    assert valid.status_code == 200
    aggregate = valid.json()["data"]
    assert aggregate["state"] == "unapplied_graph"
    assert aggregate["candidate"]["base_workflow_revision"] == 1
    assert aggregate["candidate"]["draft_hash"] == aggregate["draft"]["draft_hash"]

    rejected_bundle = test_client.post(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
        json={
            "candidate_hash": aggregate["candidate"]["candidate_hash"],
            "candidate": aggregate["candidate"],
        },
    )
    assert rejected_bundle.status_code == 400
    assert rejected_bundle.json()["error"]["code"] == "invalid_input"

    applied = test_client.post(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
        json={"candidate_hash": aggregate["candidate"]["candidate_hash"]},
    )
    assert applied.status_code == 200
    data = applied.json()["data"]
    assert data["apply_result"]["kind"] == "graph"
    assert data["apply_result"]["previous_workflow_revision"] == 1
    assert data["apply_result"]["workflow_revision"] == 2
    assert data["apply_result"]["warnings"] == []
    assert data["authoring"]["state"] == "applied"
    assert data["authoring"]["candidate"] is None
    assert data["authoring"]["applied_graph"]["nodes"][0]["uuid"] == NODE_UUID

    assert service._store.count_rows("workflow_task") == 0


def test_source_only_apply_retains_workflow_revision(client) -> None:
    test_client, _service, _package_root = client
    saved = test_client.put(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": "source_only = True\n",
            "expected_draft_hash": None,
            "expected_workflow_revision": 1,
        },
    ).json()["data"]

    applied = test_client.post(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
        json={"candidate_hash": saved["candidate"]["candidate_hash"]},
    )

    assert applied.status_code == 200
    result = applied.json()["data"]
    assert result["apply_result"]["kind"] == "source_only"
    assert result["apply_result"]["previous_workflow_revision"] == 1
    assert result["apply_result"]["workflow_revision"] == 1
    assert result["authoring"]["state"] == "applied"
    assert result["authoring"]["applied_graph"]["nodes"] == []


def test_authoring_conflict_order_and_error_envelopes(client) -> None:
    test_client, service, package_root = client
    saved = test_client.put(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": "result = build()\n",
            "expected_draft_hash": None,
            "expected_workflow_revision": 1,
        },
    ).json()["data"]

    source_path = package_root / "workflows" / "demo.py"
    source_path.write_text("external = change()\n", encoding="utf-8")
    response = test_client.post(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
        json={"candidate_hash": saved["candidate"]["candidate_hash"]},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "draft_hash_conflict"

    refreshed = service.reconcile_registered_source(WORKFLOW_UUID)
    assert refreshed["draft"]["python_source"] == "external = change()\n"
    service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[],
        edges=[],
    )
    stale_revision = test_client.post(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
        json={"candidate_hash": refreshed["candidate"]["candidate_hash"]},
    )
    assert stale_revision.status_code == 409
    assert stale_revision.json()["error"]["code"] == ("workflow_revision_conflict")


def test_malformed_tokens_and_event_cursor_use_frozen_error_shapes(
    client,
) -> None:
    test_client, _service, _package_root = client
    invalid_draft = test_client.put(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": "result = build()\n",
            "expected_draft_hash": "not-a-hash",
            "expected_workflow_revision": 1,
        },
    )
    invalid_cursor = test_client.get(
        "/api/v1/events",
        headers={"Last-Event-ID": "not-an-integer"},
    )

    assert invalid_draft.status_code == 400
    assert invalid_draft.json() == {
        "code": 400,
        "error": {
            "code": "invalid_input",
            "message": "提交内容格式不正确",
        },
    }
    assert invalid_cursor.status_code == 400
    assert invalid_cursor.json() == {
        "error": {
            "code": "invalid_input",
            "message": "Last-Event-ID must be a non-negative integer",
        }
    }


def test_authoring_event_is_durable_small_and_replayable(client) -> None:
    test_client, service, _package_root = client
    saved = test_client.put(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": "result = build()\n",
            "expected_draft_hash": None,
            "expected_workflow_revision": 1,
        },
    ).json()["data"]
    events = service.list_events(after_id=0)["items"]
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "workflow.authoring.changed"
    assert event["data"] == {
        "workflow_uuid": WORKFLOW_UUID,
        "cause": "draft_saved",
        "workflow_revision": 1,
        "draft_hash": saved["draft"]["draft_hash"],
        "candidate_hash": saved["candidate"]["candidate_hash"],
    }
    assert "python_source" not in str(event)
    assert "graph" not in str(event)
    assert service.list_events(after_id=event["id"])["items"] == []
    frame = format_sse_event(event)
    lines = frame.strip().splitlines()
    assert lines[:2] == [
        f"id: {event['id']}",
        "event: workflow.authoring.changed",
    ]
    assert json.loads(lines[2].removeprefix("data: ")) == event["data"]


def test_deleted_source_does_not_delete_applied_workflow(client) -> None:
    test_client, service, package_root = client
    saved = test_client.put(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": "result = build()\n",
            "expected_draft_hash": None,
            "expected_workflow_revision": 1,
        },
    ).json()["data"]
    test_client.post(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
        json={"candidate_hash": saved["candidate"]["candidate_hash"]},
    )

    (package_root / "workflows" / "demo.py").unlink()
    recovered = service.reconcile_registered_source(WORKFLOW_UUID)
    assert recovered["state"] == "draft_missing"
    assert recovered["draft"] is None
    assert recovered["candidate"] is None
    assert recovered["applied_graph"]["nodes"][0]["uuid"] == NODE_UUID

    canonical_path = package_root / "workflows" / "demo.py"
    canonical_path.write_text("source_only = restored\n", encoding="utf-8")
    restored = service.reconcile_registered_source(WORKFLOW_UUID)
    assert restored["state"] == "unapplied_source_only"
    assert restored["draft"]["python_source"] == "source_only = restored\n"
    assert service.list_events(after_id=0)["items"][-1]["data"]["cause"] == (
        "recovered"
    )


def test_authoring_candidate_and_event_survive_store_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "unilabos_data" / "workflow.db"
    package_root = tmp_path / "package"
    package_root.mkdir()
    first_store = WorkflowStore(database)
    first = WorkflowService(first_store, compiler=FakeAuthoringCompiler())
    first.create_workflow(
        name="restart",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    first.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="restart_package",
        package_root=package_root,
        relative_path="workflows/restart.py",
    )
    saved = first.save_draft(
        WORKFLOW_UUID,
        python_source="result = build()",
        expected_draft_hash=None,
        expected_workflow_revision=1,
    )
    event = first.list_events(after_id=0)["items"][0]
    first_store.close()

    reopened_store = WorkflowStore(database)
    reopened = WorkflowService(
        reopened_store,
        compiler=FakeAuthoringCompiler(),
    )
    aggregate = reopened.get_authoring(WORKFLOW_UUID)
    assert aggregate["state"] == "unapplied_graph"
    assert aggregate["draft"]["draft_hash"] == saved["draft"]["draft_hash"]
    assert (
        aggregate["candidate"]["candidate_hash"]
        == (saved["candidate"]["candidate_hash"])
    )
    assert reopened.list_events(after_id=0)["items"] == [event]
    assert reopened.list_events(after_id=event["id"])["items"] == []
    reopened_store.close()
