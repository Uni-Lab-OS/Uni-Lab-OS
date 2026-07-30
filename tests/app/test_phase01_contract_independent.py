"""Phase 01 core 的独立 frontend HTTP 合同测试。

测试只观察主 FastAPI 组合根，不查询数据库，也不调用 Workflow 私有 helper。
"""

from __future__ import annotations

import importlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path_factory, monkeypatch):
    """创建使用独立 workspace 的公开 OS FastAPI app。"""

    from unilabos.config.config import BasicConfig

    working_dir = tmp_path_factory.getbasetemp() / "phase01-independent-workspace"
    working_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(BasicConfig, "working_dir", str(working_dir))
    server = importlib.reload(importlib.import_module("unilabos.app.web.server"))
    try:
        with TestClient(server.setup_server()) as test_client:
            yield test_client
    finally:
        try:
            composition = importlib.import_module("unilabos.workflow.composition")
        except ModuleNotFoundError:
            # Phase 00 红测基点尚无 Workflow composition；目标分支必须执行公开清理 seam。
            pass
        else:
            composition.reset_workflow_service_for_test()


def _assert_backend_error(response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    payload = response.json()
    assert payload["code"] == status
    assert payload["error"]["code"] == code
    assert "data" not in payload


def _request_properties(openapi: dict, path: str, method: str) -> set[str]:
    schema = openapi["paths"][path][method]["requestBody"]["content"]["application/json"]["schema"]
    while "$ref" in schema:
        schema = openapi["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
    return set(schema.get("properties", {}))


def test_workflow_graph_get_put_are_backend_shaped_routes(client):
    openapi = client.get("/api/openapi.json").json()
    graph_path = "/api/v1/workflows/{workflow_uuid}/graph"
    assert {"get", "put"} <= set(openapi["paths"][graph_path])

    workflow_uuid = str(uuid4())
    _assert_backend_error(
        client.get(f"/api/v1/workflows/{workflow_uuid}/graph"),
        404,
        "not_found",
    )
    _assert_backend_error(
        client.put(
            f"/api/v1/workflows/{workflow_uuid}/graph",
            json={"revision": 1, "nodes": [], "edges": []},
        ),
        404,
        "not_found",
    )


def test_workflow_task_create_detail_and_jobs_use_backend_envelope(client):
    openapi = client.get("/api/openapi.json").json()
    assert "post" in openapi["paths"]["/api/v1/workflow-tasks"]
    assert "get" in openapi["paths"]["/api/v1/workflow-tasks/{task_uuid}"]
    assert "get" in openapi["paths"]["/api/v1/workflow-tasks/{task_uuid}/jobs"]

    unknown_uuid = str(uuid4())
    _assert_backend_error(
        client.post(
            "/api/v1/workflow-tasks",
            json={
                "workflow_uuid": unknown_uuid,
                "run_mode": "normal",
                "target_node_uuid": None,
                "input": {},
                "description": None,
                "meta_data": {},
            },
        ),
        404,
        "not_found",
    )
    _assert_backend_error(
        client.get(f"/api/v1/workflow-tasks/{unknown_uuid}"),
        404,
        "not_found",
    )
    _assert_backend_error(
        client.get(f"/api/v1/workflow-tasks/{unknown_uuid}/jobs"),
        404,
        "not_found",
    )


def test_authoring_and_global_events_replace_old_execution_contract(client):
    openapi = client.get("/api/openapi.json").json()
    authoring_path = "/api/v1/workflows/{workflow_uuid}/authoring"
    draft_path = f"{authoring_path}/draft"
    apply_path = f"{authoring_path}/apply"
    assert "get" in openapi["paths"][authoring_path]
    assert "put" in openapi["paths"][draft_path]
    assert "post" in openapi["paths"][apply_path]
    assert "get" in openapi["paths"]["/api/v1/events"]

    workflow_create_fields = _request_properties(openapi, "/api/v1/workflows", "post")
    assert "name" in workflow_create_fields
    assert "workflow_id" not in workflow_create_fields
    assert "nodes" not in workflow_create_fields

    unknown_uuid = str(uuid4())
    _assert_backend_error(
        client.get(f"/api/v1/workflows/{unknown_uuid}/authoring"),
        404,
        "workflow_not_found",
    )
    token = f"sha256:{'0' * 64}"
    _assert_backend_error(
        client.put(
            f"/api/v1/workflows/{unknown_uuid}/authoring/draft",
            json={
                "python_source": "pass\n",
                "expected_draft_hash": None,
                "expected_workflow_revision": 1,
            },
        ),
        404,
        "workflow_not_found",
    )
    _assert_backend_error(
        client.post(
            f"/api/v1/workflows/{unknown_uuid}/authoring/apply",
            json={
                "expected_draft_hash": token,
                "expected_workflow_revision": 1,
                "expected_candidate_hash": token,
            },
        ),
        404,
        "workflow_not_found",
    )

    invalid_cursor = client.get("/api/v1/events", headers={"Last-Event-ID": "-1"})
    assert invalid_cursor.status_code == 400
    assert invalid_cursor.json() == {
        "error": {
            "code": "invalid_input",
            "message": "Last-Event-ID must be a non-negative integer",
        }
    }
