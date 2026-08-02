"""Phase 01 第八轮冻结 Backend 公共合同测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
CATALOG_FINGERPRINT = f"sha256:{'6' * 64}"
INT64_MAX = 9223372036854775807
INT64_MAX_PLUS_ONE = 9223372036854775808
VERY_LARGE_REVISION = 10000000000000000000000000000000000000000

INVALID_INPUT = {
    "code": 400,
    "error": {
        "code": "invalid_input",
        "message": "提交内容格式不正确",
    },
}
GRAPH_REVISION_CONFLICT = {
    "code": 409,
    "error": {
        "code": "conflict",
        "message": "资源已发生冲突，请刷新后重试",
    },
}
AUTHORING_REVISION_CONFLICT = {
    "code": 409,
    "error": {
        "code": "workflow_revision_conflict",
        "message": "工作流已在其他位置更新，请刷新并重新确认本次修改",
    },
}
CANDIDATE_INVALID_DIAGNOSTIC = {
    "severity": "error",
    "code": "candidate_invalid",
    "message": "工作流校验失败，请检查节点、连线和输入输出",
}


@pytest.fixture()
def store(tmp_path: Path):
    opened = WorkflowStore(tmp_path / "workflow.db")
    try:
        yield opened
    finally:
        opened.close()


class EmptyGraphCompiler:
    compiler_version = "phase-01-review-round-8"
    template_catalog_fingerprint = CATALOG_FINGERPRINT

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> CandidateCompilation:
        del workflow_uuid, workflow_revision, source_uri
        return CandidateCompilation(
            diagnostics=[],
            graph={
                "workflow": applied_graph["workflow"],
                "nodes": [],
                "edges": [],
                "node_templates": [],
                "handle_templates": [],
            },
            normalized_python_source=python_source,
            source_map=[],
            changeset={
                "kind": "source_only",
                "created_node_uuids": [],
                "updated_node_uuids": [],
                "deleted_node_uuids": [],
                "created_edge_uuids": [],
                "updated_edge_uuids": [],
                "deleted_edge_uuids": [],
                "reserved_metadata_changed": False,
            },
            compiler_version=self.compiler_version,
            template_catalog_fingerprint=self.template_catalog_fingerprint,
        )


class MalformedContainerCompiler(EmptyGraphCompiler):
    def __init__(self, field: str, value: Any) -> None:
        self.field = field
        self.value = value

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> CandidateCompilation:
        compilation = super().compile(
            workflow_uuid=workflow_uuid,
            workflow_revision=workflow_revision,
            python_source=python_source,
            source_uri=source_uri,
            applied_graph=applied_graph,
        )
        assert compilation.graph is not None
        compilation.graph[self.field] = self.value
        return compilation


def _create_workflow(service: WorkflowService) -> dict[str, Any]:
    return service.create_workflow(
        name="phase 01 review round 8",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )


def _authoring_service(
    store: WorkflowStore,
    tmp_path: Path,
    *,
    compiler: Any | None = None,
) -> WorkflowService:
    service = WorkflowService(store, compiler=compiler or EmptyGraphCompiler())
    _create_workflow(service)
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase_01_review_round_8",
        package_root=package_root,
        relative_path="workflows/review.py",
    )
    return service


def _save_draft(
    client: TestClient,
    *,
    expected_workflow_revision: int,
) -> Any:
    return client.put(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": "build()\n",
            "expected_draft_hash": None,
            "expected_workflow_revision": expected_workflow_revision,
        },
    )


@pytest.mark.parametrize(
    "revision",
    [INT64_MAX_PLUS_ONE, VERY_LARGE_REVISION],
    ids=["int64-max-plus-one", "much-larger-than-int64"],
)
def test_graph_revision_rejects_strict_json_integer_above_int64(
    store: WorkflowStore,
    revision: int,
) -> None:
    service = WorkflowService(store)
    _create_workflow(service)

    with TestClient(create_workflow_app(service)) as client:
        response = client.put(
            f"/api/v1/workflows/{WORKFLOW_UUID}/graph",
            json={"revision": revision, "nodes": [], "edges": []},
        )

    assert response.status_code == 400
    assert response.json() == INVALID_INPUT


@pytest.mark.parametrize(
    "revision",
    [INT64_MAX_PLUS_ONE, VERY_LARGE_REVISION],
    ids=["int64-max-plus-one", "much-larger-than-int64"],
)
def test_draft_revision_rejects_strict_json_integer_above_int64(
    store: WorkflowStore,
    tmp_path: Path,
    revision: int,
) -> None:
    service = _authoring_service(store, tmp_path)

    with TestClient(create_workflow_app(service)) as client:
        response = _save_draft(
            client,
            expected_workflow_revision=revision,
        )

    assert response.status_code == 400
    assert response.json() == INVALID_INPUT


def test_graph_revision_accepts_int64_max_as_business_conflict_control(
    store: WorkflowStore,
) -> None:
    service = WorkflowService(store)
    _create_workflow(service)

    with TestClient(create_workflow_app(service)) as client:
        response = client.put(
            f"/api/v1/workflows/{WORKFLOW_UUID}/graph",
            json={"revision": INT64_MAX, "nodes": [], "edges": []},
        )

    assert response.status_code == 409
    assert response.json() == GRAPH_REVISION_CONFLICT


def test_draft_revision_accepts_int64_max_as_business_conflict_control(
    store: WorkflowStore,
    tmp_path: Path,
) -> None:
    service = _authoring_service(store, tmp_path)

    with TestClient(create_workflow_app(service)) as client:
        response = _save_draft(
            client,
            expected_workflow_revision=INT64_MAX,
        )

    assert response.status_code == 409
    assert response.json() == AUTHORING_REVISION_CONFLICT


@pytest.mark.parametrize(
    ("field", "malformed_value"),
    [
        ("workflow", [1]),
        ("node_templates", ["bad"]),
        ("handle_templates", [[]]),
    ],
    ids=[
        "workflow-list",
        "node-templates-string-entry",
        "handle-templates-list-entry",
    ],
)
def test_malformed_candidate_container_is_saved_as_draft_diagnostic(
    store: WorkflowStore,
    tmp_path: Path,
    field: str,
    malformed_value: Any,
) -> None:
    service = _authoring_service(
        store,
        tmp_path,
        compiler=MalformedContainerCompiler(field, malformed_value),
    )

    with TestClient(
        create_workflow_app(service),
        raise_server_exceptions=False,
    ) as client:
        response = _save_draft(
            client,
            expected_workflow_revision=1,
        )

    payload = (
        response.json()
        if response.headers.get("content-type", "").startswith("application/json")
        else None
    )
    aggregate = payload.get("data", {}) if isinstance(payload, dict) else {}
    draft = aggregate.get("draft", {})
    assert {
        "status": response.status_code,
        "envelope_code": payload.get("code") if isinstance(payload, dict) else None,
        "state": aggregate.get("state"),
        "candidate": aggregate.get("candidate"),
        "saved_source": draft.get("python_source"),
        "has_draft_hash": bool(draft.get("draft_hash")),
        "diagnostics": draft.get("diagnostics"),
    } == {
        "status": 200,
        "envelope_code": 0,
        "state": "draft_invalid",
        "candidate": None,
        "saved_source": "build()\n",
        "has_draft_hash": True,
        "diagnostics": [CANDIDATE_INVALID_DIAGNOSTIC],
    }
