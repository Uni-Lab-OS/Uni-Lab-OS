"""Phase 01 第十轮冻结 Backend 公共合同测试。"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.models import (
    CandidateCompilation,
    WorkflowEdgeWrite,
    WorkflowNodeWrite,
)
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
SOURCE_NODE_UUID = "20000000-0000-4000-8000-000000000001"
TARGET_NODE_UUID = "20000000-0000-4000-8000-000000000002"
EDGE_UUID = "30000000-0000-4000-8000-000000000001"
SOURCE_TEMPLATE_UUID = "40000000-0000-4000-8000-000000000001"
TARGET_TEMPLATE_UUID = "40000000-0000-4000-8000-000000000002"
RESOURCE_TEMPLATE_UUID = "50000000-0000-4000-8000-000000000001"
SOURCE_HANDLE_UUID = "60000000-0000-4000-8000-000000000001"
TARGET_HANDLE_UUID = "60000000-0000-4000-8000-000000000002"
CATALOG_FINGERPRINT = f"sha256:{'7' * 64}"
INT64_MAX = 9223372036854775807
LONG_ZERO_PREFIX = "0" * 5001

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
CANDIDATE_INVALID_DIAGNOSTIC = {
    "severity": "error",
    "code": "candidate_invalid",
    "message": "工作流校验失败，请检查节点、连线和输入输出",
}


class RecordingEventService(WorkflowService):
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


class DamagedAppliedGraphStore(WorkflowStore):
    """Return one damaged copy only through the public graph seam."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.damage_case: str | None = None

    def get_graph(self, workflow_uuid: str) -> dict[str, Any]:
        graph = deepcopy(super().get_graph(workflow_uuid))
        if self.damage_case == "workflow.name.missing":
            graph["workflow"].pop("name")
        elif self.damage_case == "workflow.tags.type":
            graph["workflow"]["tags"] = {}
        elif self.damage_case == "workflow.revision.type":
            graph["workflow"]["revision"] = True
        elif self.damage_case == "node.disabled.type":
            graph["nodes"][0]["disabled"] = 0
        elif self.damage_case == "node.pose.type":
            graph["nodes"][0]["pose"] = []
        elif self.damage_case == "edge.source_node_uuid.type":
            graph["edges"][0]["source_node_uuid"] = 1
        elif self.damage_case == "edge.meta_data.type":
            graph["edges"][0]["meta_data"] = []
        elif self.damage_case == "node_template.name.type":
            graph["node_templates"][0]["name"] = []
        elif self.damage_case == "node_template.goal.type":
            graph["node_templates"][0]["goal"] = []
        elif self.damage_case == "handle_template.required.type":
            graph["handle_templates"][0]["required"] = 0
        elif self.damage_case == "handle_template.meta_data.type":
            graph["handle_templates"][0]["meta_data"] = []
        return graph


class ValidIndependentCandidateCompiler:
    compiler_version = "phase-01-review-round-10"
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
        del source_uri
        applied_workflow = applied_graph["workflow"]
        return CandidateCompilation(
            diagnostics=[],
            graph={
                "workflow": {
                    "uuid": workflow_uuid,
                    "create_time": applied_workflow["create_time"],
                    "update_time": applied_workflow["update_time"],
                    "meta_data": {},
                    "name": "valid independent candidate",
                    "tags": [],
                    "revision": workflow_revision,
                },
                "nodes": [],
                "edges": [],
                "node_templates": [],
                "handle_templates": [],
            },
            normalized_python_source=python_source,
            source_map=[],
            changeset={
                "kind": "graph",
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


class InvalidNodesContainerCompiler(ValidIndependentCandidateCompiler):
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
        compilation.graph["nodes"] = {}
        return compilation


@pytest.fixture()
def store(tmp_path: Path):
    opened = WorkflowStore(tmp_path / "workflow.db")
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture()
def damaged_store(tmp_path: Path):
    opened = DamagedAppliedGraphStore(tmp_path / "damaged-workflow.db")
    try:
        yield opened
    finally:
        opened.close()


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
                (b"last-event-id", last_event_id.encode("utf-8")),
            ],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=1)
        return messages

    return asyncio.run(invoke())


@pytest.mark.parametrize(
    ("last_event_id", "expected_cursor"),
    [
        (LONG_ZERO_PREFIX, 0),
        (f"{LONG_ZERO_PREFIX}{INT64_MAX}", INT64_MAX),
        ("+0", 0),
        ("-0", 0),
        ("+10", 10),
        (f"+{INT64_MAX}", INT64_MAX),
    ],
    ids=[
        "long-leading-zeros",
        "long-leading-zeros-before-int64-max",
        "plus-zero-control",
        "minus-zero-control",
        "plus-ten-control",
        "plus-int64-max-control",
    ],
)
def test_last_event_id_accepts_frozen_parse_int_values(
    store: WorkflowStore,
    last_event_id: str,
    expected_cursor: int,
) -> None:
    service = RecordingEventService(store)

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
    "last_event_id",
    [
        f"{LONG_ZERO_PREFIX}{INT64_MAX + 1}",
        "-1",
        f"+{INT64_MAX + 1}",
    ],
    ids=[
        "long-leading-zeros-before-int64-overflow",
        "negative-one-control",
        "plus-int64-overflow-control",
    ],
)
def test_last_event_id_rejects_values_outside_frozen_parse_int_cursor_range(
    store: WorkflowStore,
    last_event_id: str,
) -> None:
    service = RecordingEventService(store)

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


def _create_workflow(service: WorkflowService) -> None:
    service.create_workflow(
        name="phase 01 review round 10",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )


def _node(
    node_uuid: str,
    *,
    template_uuid: str,
) -> WorkflowNodeWrite:
    return WorkflowNodeWrite(
        uuid=node_uuid,
        workflow_node_template_uuid=template_uuid,
        name=node_uuid,
        status="idle",
        type="compute",
        pose={},
        param={},
        execution_policy={},
        disabled=False,
        minimized=False,
        meta_data={},
    )


def _edge() -> WorkflowEdgeWrite:
    return WorkflowEdgeWrite(
        uuid=EDGE_UUID,
        source_node_uuid=SOURCE_NODE_UUID,
        target_node_uuid=TARGET_NODE_UUID,
        source_handle_uuid=SOURCE_HANDLE_UUID,
        target_handle_uuid=TARGET_HANDLE_UUID,
        meta_data={},
    )


def _seed_template_catalog(store: WorkflowStore) -> None:
    timestamp = "2026-07-31T00:00:00Z"
    with store.transaction() as connection:
        for template_uuid, name in (
            (SOURCE_TEMPLATE_UUID, "source"),
            (TARGET_TEMPLATE_UUID, "target"),
        ):
            connection.execute(
                """
                INSERT INTO workflow_node_template(
                    uuid, create_time, update_time, meta_data, authority_id,
                    resource_template_uuid, name, display_name, class, goal,
                    goal_default, feedback, result, schema, type, icon, header,
                    footer, node_type
                ) VALUES (?, ?, ?, '{}', 'os-local', ?, ?, ?, NULL, '{}', '{}',
                          '{}', '{}', NULL, 'action', NULL, NULL, NULL, 'compute')
                """,
                (
                    template_uuid,
                    timestamp,
                    timestamp,
                    RESOURCE_TEMPLATE_UUID,
                    name,
                    name,
                ),
            )
        for values in (
            (
                SOURCE_HANDLE_UUID,
                SOURCE_TEMPLATE_UUID,
                "result",
                "source",
            ),
            (
                TARGET_HANDLE_UUID,
                TARGET_TEMPLATE_UUID,
                "input",
                "target",
            ),
        ):
            connection.execute(
                """
                INSERT INTO workflow_handle_template(
                    uuid, create_time, update_time, meta_data, authority_id,
                    workflow_node_template_uuid, handle_key, io_type,
                    display_name, type, required, data_source, data_key
                ) VALUES (?, ?, ?, '{}', 'os-local', ?, ?, ?, ?, 'number', 0,
                          NULL, NULL)
                """,
                (
                    values[0],
                    timestamp,
                    timestamp,
                    values[1],
                    values[2],
                    values[3],
                    values[2],
                ),
            )


def _authoring_service(
    store: WorkflowStore,
    tmp_path: Path,
    *,
    compiler: Any,
) -> tuple[WorkflowService, int]:
    service = WorkflowService(store, compiler=compiler)
    _create_workflow(service)
    _seed_template_catalog(store)
    service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[
            _node(
                SOURCE_NODE_UUID,
                template_uuid=SOURCE_TEMPLATE_UUID,
            ),
            _node(
                TARGET_NODE_UUID,
                template_uuid=TARGET_TEMPLATE_UUID,
            ),
        ],
        edges=[_edge()],
    )
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase_01_review_round_10",
        package_root=package_root,
        relative_path="workflows/review.py",
    )
    return service, 2


def _save_draft(
    client: TestClient,
    *,
    revision: int,
) -> Any:
    return client.put(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": "build()\n",
            "expected_draft_hash": None,
            "expected_workflow_revision": revision,
        },
    )


def _json_payload(response: Any) -> dict[str, Any] | None:
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    return None


@pytest.mark.parametrize(
    "damage_case",
    [
        "workflow.tags.type",
        "workflow.revision.type",
        "node.disabled.type",
        "node.pose.type",
        "edge.source_node_uuid.type",
        "edge.meta_data.type",
        "node_template.name.type",
        "node_template.goal.type",
        "handle_template.required.type",
        "handle_template.meta_data.type",
    ],
    ids=[
        "workflow-tags-object",
        "workflow-revision-bool",
        "node-disabled-integer",
        "node-pose-array",
        "edge-source-node-uuid-integer",
        "edge-meta-data-array",
        "node-template-name-array",
        "node-template-goal-array",
        "handle-template-required-integer",
        "handle-template-meta-data-array",
    ],
)
def test_draft_reports_applied_store_type_invariant_as_internal_error(
    damaged_store: DamagedAppliedGraphStore,
    tmp_path: Path,
    damage_case: str,
) -> None:
    service, revision = _authoring_service(
        damaged_store,
        tmp_path,
        compiler=ValidIndependentCandidateCompiler(),
    )
    damaged_store.damage_case = damage_case

    with TestClient(
        create_workflow_app(service),
        raise_server_exceptions=False,
    ) as client:
        response = _save_draft(client, revision=revision)

    assert {
        "status": response.status_code,
        "body": _json_payload(response),
    } == {
        "status": 500,
        "body": INTERNAL_ERROR,
    }


@pytest.mark.parametrize(
    "damage_case",
    [
        "workflow.name.missing",
        "workflow.tags.type",
    ],
    ids=["applied-required-field-missing", "applied-field-type-invalid"],
)
def test_applied_authority_fault_precedes_candidate_container_diagnostic(
    damaged_store: DamagedAppliedGraphStore,
    tmp_path: Path,
    damage_case: str,
) -> None:
    service, revision = _authoring_service(
        damaged_store,
        tmp_path,
        compiler=InvalidNodesContainerCompiler(),
    )
    damaged_store.damage_case = damage_case

    with TestClient(
        create_workflow_app(service),
        raise_server_exceptions=False,
    ) as client:
        response = _save_draft(client, revision=revision)

    assert {
        "status": response.status_code,
        "body": _json_payload(response),
    } == {
        "status": 500,
        "body": INTERNAL_ERROR,
    }


def test_candidate_container_fault_alone_remains_successful_draft_diagnostic(
    store: WorkflowStore,
    tmp_path: Path,
) -> None:
    service, revision = _authoring_service(
        store,
        tmp_path,
        compiler=InvalidNodesContainerCompiler(),
    )

    with TestClient(
        create_workflow_app(service),
        raise_server_exceptions=False,
    ) as client:
        response = _save_draft(client, revision=revision)

    payload = _json_payload(response) or {}
    aggregate = payload.get("data", {})
    draft = aggregate.get("draft", {})
    assert {
        "status": response.status_code,
        "envelope_code": payload.get("code"),
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
