"""Phase 01 Backend 合同的独立对抗性 HTTP 测试。

这些测试只经过 OS 的公共 FastAPI 组合根观察行为，不依赖 Workflow 模块的
私有 helper 或数据库结构。
"""

import importlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from unilabos.config.config import BasicConfig


@pytest.fixture()
def client(tmp_path_factory):
    """使用真实组合根构造一个隔离 workspace 的公共 HTTP app。"""

    working_dir = tmp_path_factory.getbasetemp() / "phase01-independent-workspace"
    working_dir.mkdir(exist_ok=True)
    BasicConfig.working_dir = str(working_dir)
    web_server = importlib.reload(
        importlib.import_module("unilabos.app.web.server"),
    )

    with TestClient(web_server.setup_server()) as test_client:
        yield test_client


def _assert_backend_error(response, *, status: int, code: str) -> dict:
    assert response.status_code == status
    payload = response.json()
    assert "detail" not in payload
    assert payload["code"] == status
    assert payload["error"]["code"] == code
    assert isinstance(payload["error"]["message"], str)
    assert payload["error"]["message"]
    return payload


def test_malformed_workflow_request_never_returns_fastapi_detail(client):
    response = client.post("/api/v1/workflows", json={"unexpected": True})

    _assert_backend_error(response, status=400, code="invalid_input")


def test_apply_rejects_client_owned_candidate_and_graph_payload(client):
    digest = "sha256:" + ("a" * 64)
    response = client.post(
        f"/api/v1/workflows/{uuid4()}/authoring/apply",
        json={
            "expected_draft_hash": digest,
            "expected_workflow_revision": 1,
            "expected_candidate_hash": digest,
            "candidate": {"nodes": [], "edges": []},
            "graph": {"nodes": [], "edges": []},
        },
    )

    _assert_backend_error(response, status=400, code="invalid_input")


def test_runtime_run_route_is_absent_and_events_is_the_sse_seam(client):
    openapi_paths = client.get("/api/openapi.json").json()["paths"]
    assert "/api/v1/runtime/runs" not in openapi_paths
    assert "/api/v1/events" in openapi_paths

    assert client.get("/api/v1/runtime/runs").status_code == 404
    response = client.get(
        "/api/v1/events",
        headers={"Last-Event-ID": "not-a-non-negative-integer"},
    )
    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "code": "invalid_input",
            "message": "Last-Event-ID must be a non-negative integer",
        }
    }
