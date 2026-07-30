"""Phase 01 第七轮冻结 Backend 公共合同测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
UNKNOWN_UUID = "99999999-9999-4999-8999-999999999999"
NODE_UUID = "22222222-2222-4222-8222-222222222222"
CATALOG_FINGERPRINT = f"sha256:{'7' * 64}"
INT64_MAX = "9223372036854775807"

INVALID_INPUT = {
    "code": 400,
    "error": {
        "code": "invalid_input",
        "message": "提交内容格式不正确",
    },
}
INVALID_EVENT_CURSOR = {
    "error": {
        "code": "invalid_input",
        "message": "Last-Event-ID must be a non-negative integer",
    }
}
INTERNAL_ERROR = {
    "code": 500,
    "error": {
        "code": "internal_error",
        "message": "本地工作流服务出现错误，请重试或查看日志",
    },
}


@pytest.fixture()
def store(tmp_path: Path):
    opened = WorkflowStore(tmp_path / "workflow.db")
    try:
        yield opened
    finally:
        opened.close()


class EmptyGraphCompiler:
    compiler_version = "phase-01-review-round-7"
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
                "kind": (
                    "graph"
                    if applied_graph["nodes"] or applied_graph["edges"]
                    else "source_only"
                ),
                "created_node_uuids": [],
                "updated_node_uuids": [],
                "deleted_node_uuids": [node["uuid"] for node in applied_graph["nodes"]],
                "created_edge_uuids": [],
                "updated_edge_uuids": [],
                "deleted_edge_uuids": [edge["uuid"] for edge in applied_graph["edges"]],
                "reserved_metadata_changed": False,
            },
            compiler_version=self.compiler_version,
            template_catalog_fingerprint=self.template_catalog_fingerprint,
        )


class MalformedCandidateCompiler(EmptyGraphCompiler):
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
        compilation.graph["node_templates"] = [
            {
                "uuid": "33333333-3333-4333-8333-333333333333",
            }
        ]
        return compilation


class InternalErrorCandidateService(WorkflowService):
    """Dependency-injected fault seam for the public Draft HTTP boundary."""

    @classmethod
    def _backend_candidate_graph(
        cls,
        graph: dict[str, Any],
        *,
        applied_graph: dict[str, Any],
    ) -> dict[str, Any]:
        del cls, graph, applied_graph
        raise WorkflowError("internal_error")


class CursorSafeEventService(WorkflowService):
    """Keep an accepted cursor from reaching SQLite while the stream is sampled."""

    def __init__(self, store: WorkflowStore) -> None:
        super().__init__(store)
        self.seen_after_ids: list[int] = []

    def list_events(
        self,
        *,
        after_id: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        del limit
        self.seen_after_ids.append(after_id)
        return {"items": [], "after_id": after_id}


def _create_workflow(service: WorkflowService) -> dict[str, Any]:
    return service.create_workflow(
        name="phase 01 review round 7",
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
    service_type: type[WorkflowService] = WorkflowService,
) -> WorkflowService:
    service = service_type(store, compiler=compiler or EmptyGraphCompiler())
    _create_workflow(service)
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase_01_review_round_7",
        package_root=package_root,
        relative_path="workflows/review.py",
    )
    return service


def _node_payload() -> dict[str, Any]:
    return {
        "uuid": NODE_UUID,
        "name": "strict controls",
        "status": "idle",
        "type": "compute",
        "pose": {},
        "param": {},
        "execution_policy": {},
        "disabled": False,
        "minimized": False,
        "meta_data": {},
    }


def _save_draft(
    client: TestClient,
    *,
    expected_workflow_revision: Any,
) -> Any:
    return client.put(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": "build()\n",
            "expected_draft_hash": None,
            "expected_workflow_revision": expected_workflow_revision,
        },
    )


def _response_status(messages: list[dict[str, Any]]) -> int:
    start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    return int(start["status"])


def _response_json(messages: list[dict[str, Any]]) -> dict[str, Any]:
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return json.loads(body)


def _get_events_through_asgi(
    app: FastAPI,
    *,
    last_event_id: str,
) -> list[dict[str, Any]]:
    async def invoke() -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        request_sent = False

        async def receive() -> dict[str, Any]:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {
                    "type": "http.request",
                    "body": b"",
                    "more_body": False,
                }
            await asyncio.sleep(0.01)
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/events",
            "raw_path": b"/api/v1/events",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"last-event-id", last_event_id.encode("ascii")),
            ],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=1)
        return messages

    return asyncio.run(invoke())


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/workflows/not-a-uuid",
        "/api/v1/workflows/not-a-uuid/graph",
        "/api/v1/workflows/not-a-uuid/authoring",
        "/api/v1/workflow-tasks/not-a-uuid",
        "/api/v1/workflow-tasks/not-a-uuid/jobs",
        "/api/v1/workflow-node-jobs/not-a-uuid",
    ],
    ids=[
        "workflow-detail",
        "workflow-graph",
        "workflow-authoring",
        "task-detail",
        "task-jobs",
        "job-detail",
    ],
)
def test_malformed_resource_uuid_path_is_standard_invalid_input_400(
    store: WorkflowStore,
    path: str,
) -> None:
    with TestClient(create_workflow_app(WorkflowService(store))) as client:
        response = client.get(path)

    assert response.status_code == 400
    assert response.json() == INVALID_INPUT


@pytest.mark.parametrize(
    "path",
    [
        f"/api/v1/workflows/{UNKNOWN_UUID}",
        f"/api/v1/workflows/{UNKNOWN_UUID}/graph",
        f"/api/v1/workflows/{UNKNOWN_UUID}/authoring",
        f"/api/v1/workflow-tasks/{UNKNOWN_UUID}",
        f"/api/v1/workflow-tasks/{UNKNOWN_UUID}/jobs",
        f"/api/v1/workflow-node-jobs/{UNKNOWN_UUID}",
    ],
    ids=[
        "workflow-detail",
        "workflow-graph",
        "workflow-authoring",
        "task-detail",
        "task-jobs",
        "job-detail",
    ],
)
def test_well_formed_missing_resource_uuid_path_remains_404(
    store: WorkflowStore,
    path: str,
) -> None:
    with TestClient(create_workflow_app(WorkflowService(store))) as client:
        response = client.get(path)

    assert response.status_code == 404


@pytest.mark.parametrize(
    "last_event_id",
    [
        "1_0",
        "not-an-integer",
        "-1",
        "9223372036854775808",
    ],
    ids=[
        "underscore",
        "invalid-text",
        "negative",
        "greater-than-int64-max",
    ],
)
def test_last_event_id_rejects_text_outside_frozen_parse_int64(
    store: WorkflowStore,
    last_event_id: str,
) -> None:
    service = CursorSafeEventService(store)
    messages = _get_events_through_asgi(
        create_workflow_app(service),
        last_event_id=last_event_id,
    )

    assert {
        "status": _response_status(messages),
        "body": _response_json(messages),
        "seen_after_ids": service.seen_after_ids,
    } == {
        "status": 400,
        "body": INVALID_EVENT_CURSOR,
        "seen_after_ids": [],
    }


@pytest.mark.parametrize(
    ("last_event_id", "expected_cursor"),
    [
        ("", 0),
        ("   ", 0),
        (" 10", 10),
        ("10 ", 10),
        ("10", 10),
        (INT64_MAX, 9223372036854775807),
    ],
    ids=[
        "explicit-empty",
        "whitespace-only",
        "leading-whitespace",
        "trailing-whitespace",
        "ordinary-integer",
        "int64-max",
    ],
)
def test_last_event_id_accepts_frozen_non_negative_int64_controls(
    store: WorkflowStore,
    last_event_id: str,
    expected_cursor: int,
) -> None:
    service = CursorSafeEventService(store)
    messages = _get_events_through_asgi(
        create_workflow_app(service),
        last_event_id=last_event_id,
    )

    assert {
        "status": _response_status(messages),
        "first_after_ids": service.seen_after_ids[:1],
    } == {
        "status": 200,
        "first_after_ids": [expected_cursor],
    }


@pytest.mark.parametrize(
    "revision",
    [True, "1", 1.0],
    ids=["boolean", "string", "float"],
)
def test_graph_revision_requires_strict_json_integer(
    store: WorkflowStore,
    revision: Any,
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
    [True, "1", 1.0],
    ids=["boolean", "string", "float"],
)
def test_draft_expected_workflow_revision_requires_strict_json_integer(
    store: WorkflowStore,
    tmp_path: Path,
    revision: Any,
) -> None:
    service = _authoring_service(store, tmp_path)

    with TestClient(create_workflow_app(service)) as client:
        response = _save_draft(
            client,
            expected_workflow_revision=revision,
        )

    assert response.status_code == 400
    assert response.json() == INVALID_INPUT


@pytest.mark.parametrize(
    "revision",
    [True, "1", 1.0],
    ids=["boolean", "string", "float"],
)
def test_apply_expected_workflow_revision_requires_strict_json_integer(
    store: WorkflowStore,
    tmp_path: Path,
    revision: Any,
) -> None:
    service = _authoring_service(store, tmp_path)

    with TestClient(create_workflow_app(service)) as client:
        draft = _save_draft(
            client,
            expected_workflow_revision=1,
        ).json()["data"]
        response = client.post(
            f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
            json={
                "expected_draft_hash": draft["draft"]["draft_hash"],
                "expected_workflow_revision": revision,
                "expected_candidate_hash": draft["candidate"]["candidate_hash"],
            },
        )

    assert response.status_code == 400
    assert response.json() == INVALID_INPUT


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("disabled", 1),
        ("disabled", "false"),
        ("minimized", 1),
        ("minimized", "false"),
    ],
    ids=[
        "disabled-number",
        "disabled-string",
        "minimized-number",
        "minimized-string",
    ],
)
def test_workflow_node_controls_require_strict_json_boolean(
    store: WorkflowStore,
    field: str,
    value: Any,
) -> None:
    service = WorkflowService(store)
    _create_workflow(service)
    node = _node_payload()
    node[field] = value

    with TestClient(create_workflow_app(service)) as client:
        response = client.put(
            f"/api/v1/workflows/{WORKFLOW_UUID}/graph",
            json={"revision": 1, "nodes": [node], "edges": []},
        )

    assert response.status_code == 400
    assert response.json() == INVALID_INPUT


def test_strict_integer_and_boolean_json_controls_succeed(
    store: WorkflowStore,
    tmp_path: Path,
) -> None:
    service = _authoring_service(store, tmp_path)
    node = _node_payload()
    node["disabled"] = True
    node["minimized"] = False

    with TestClient(create_workflow_app(service)) as client:
        graph_response = client.put(
            f"/api/v1/workflows/{WORKFLOW_UUID}/graph",
            json={"revision": 1, "nodes": [node], "edges": []},
        )
        draft_response = _save_draft(
            client,
            expected_workflow_revision=2,
        )
        draft = draft_response.json()["data"]
        apply_response = client.post(
            f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
            json={
                "expected_draft_hash": draft["draft"]["draft_hash"],
                "expected_workflow_revision": 2,
                "expected_candidate_hash": draft["candidate"]["candidate_hash"],
            },
        )

    assert {
        "graph_status": graph_response.status_code,
        "graph_revision": graph_response.json()["data"]["workflow"]["revision"],
        "disabled": graph_response.json()["data"]["nodes"][0]["disabled"],
        "minimized": graph_response.json()["data"]["nodes"][0]["minimized"],
        "draft_status": draft_response.status_code,
        "apply_status": apply_response.status_code,
        "apply_revision": apply_response.json()["data"]["apply_result"][
            "workflow_revision"
        ],
    } == {
        "graph_status": 200,
        "graph_revision": 2,
        "disabled": True,
        "minimized": False,
        "draft_status": 200,
        "apply_status": 200,
        "apply_revision": 3,
    }


def test_malformed_candidate_shape_remains_draft_http_200_diagnostic(
    store: WorkflowStore,
    tmp_path: Path,
) -> None:
    service = _authoring_service(
        store,
        tmp_path,
        compiler=MalformedCandidateCompiler(),
    )

    with TestClient(create_workflow_app(service)) as client:
        response = _save_draft(
            client,
            expected_workflow_revision=1,
        )

    aggregate = response.json()["data"]
    assert {
        "status": response.status_code,
        "code": response.json()["code"],
        "state": aggregate["state"],
        "candidate": aggregate["candidate"],
        "diagnostics": aggregate["draft"]["diagnostics"],
    } == {
        "status": 200,
        "code": 0,
        "state": "draft_invalid",
        "candidate": None,
        "diagnostics": [
            {
                "severity": "error",
                "code": "candidate_invalid",
                "message": "工作流校验失败，请检查节点、连线和输入输出",
            }
        ],
    }


def test_draft_candidate_internal_error_remains_http_500(
    store: WorkflowStore,
    tmp_path: Path,
) -> None:
    service = _authoring_service(
        store,
        tmp_path,
        service_type=InternalErrorCandidateService,
    )

    with TestClient(create_workflow_app(service)) as client:
        response = _save_draft(
            client,
            expected_workflow_revision=1,
        )

    assert response.status_code == 500
    assert response.json() == INTERNAL_ERROR
