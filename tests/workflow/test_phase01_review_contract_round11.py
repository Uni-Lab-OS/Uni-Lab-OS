"""Phase 01 第十一轮冻结 Backend 公共合同测试。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.models import CandidateCompilation, WorkflowNodeWrite
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
NODE_UUID = "20000000-0000-4000-8000-000000000001"
NODE_TEMPLATE_UUID = "40000000-0000-4000-8000-000000000001"
RESOURCE_TEMPLATE_UUID = "50000000-0000-4000-8000-000000000001"
HANDLE_TEMPLATE_UUID = "60000000-0000-4000-8000-000000000001"
CATALOG_FINGERPRINT = f"sha256:{'8' * 64}"
INT64_MAX = (1 << 63) - 1

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


class DamagedAppliedGraphStore(WorkflowStore):
    """Return one damaged authority graph only through the public store seam."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.damage_case: str | None = None

    def get_graph(self, workflow_uuid: str) -> dict[str, Any]:
        graph = deepcopy(super().get_graph(workflow_uuid))
        if self.damage_case == "workflow.name.missing":
            graph["workflow"].pop("name")
        elif self.damage_case == "workflow.tags.type":
            graph["workflow"]["tags"] = {}
        return graph


class InvalidCompilationCompiler:
    compiler_version = "phase-01-review-round-11-invalid"
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
        del (
            workflow_uuid,
            workflow_revision,
            python_source,
            source_uri,
            applied_graph,
        )
        return CandidateCompilation(
            diagnostics=[
                {
                    "severity": "error",
                    "code": "syntax_error",
                    "message": "invalid source",
                }
            ],
            graph=None,
            normalized_python_source=None,
            source_map=[],
            changeset=None,
            compiler_version=self.compiler_version,
            template_catalog_fingerprint=self.template_catalog_fingerprint,
        )


class CatalogCandidateCompiler:
    compiler_version = "phase-01-review-round-11-candidate"
    template_catalog_fingerprint = CATALOG_FINGERPRINT

    def __init__(
        self,
        *,
        entity_kind: str | None = None,
        field: str | None = None,
        value: Any = None,
    ) -> None:
        self.entity_kind = entity_kind
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
        del source_uri
        applied_workflow = applied_graph["workflow"]
        graph = {
            "workflow": {
                "uuid": workflow_uuid,
                "create_time": applied_workflow["create_time"],
                "update_time": applied_workflow["update_time"],
                "description": None,
                "meta_data": {"round": 11},
                "name": "valid round 11 candidate",
                "tags": ["stable", 1, None, {"nested": True}],
                "revision": workflow_revision,
            },
            "nodes": [],
            "edges": [],
            "node_templates": [
                {
                    "uuid": NODE_TEMPLATE_UUID,
                    "description": None,
                    "meta_data": {"catalog": "valid"},
                    "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
                    "name": "source",
                    "display_name": "Source",
                    "class": None,
                    "goal": {},
                    "goal_default": {},
                    "feedback": {},
                    "result": {},
                    "schema": None,
                    "type": "action",
                    "icon": None,
                    "header": None,
                    "footer": None,
                    "node_type": "compute",
                }
            ],
            "handle_templates": [
                {
                    "uuid": HANDLE_TEMPLATE_UUID,
                    "description": None,
                    "meta_data": {"catalog": "valid"},
                    "workflow_node_template_uuid": NODE_TEMPLATE_UUID,
                    "handle_key": "result",
                    "io_type": "source",
                    "display_name": "Result",
                    "type": "number",
                    "required": False,
                    "data_source": None,
                    "data_key": None,
                }
            ],
        }
        if self.entity_kind is not None:
            assert self.field is not None
            entity = (
                graph["workflow"]
                if self.entity_kind == "workflow"
                else graph[self.entity_kind][0]
            )
            entity[self.field] = self.value
        return CandidateCompilation(
            diagnostics=[],
            graph=graph,
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


def _node() -> WorkflowNodeWrite:
    return WorkflowNodeWrite(
        uuid=NODE_UUID,
        workflow_node_template_uuid=NODE_TEMPLATE_UUID,
        name="applied node",
        status="idle",
        type="compute",
        pose={},
        param={},
        execution_policy={},
        disabled=False,
        minimized=False,
        meta_data={},
    )


def _seed_template_catalog(store: WorkflowStore) -> None:
    timestamp = "2026-07-31T00:00:00Z"
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workflow_node_template(
                uuid, create_time, update_time, meta_data, authority_id,
                resource_template_uuid, name, display_name, class, goal,
                goal_default, feedback, result, schema, type, icon, header,
                footer, node_type
            ) VALUES (?, ?, ?, '{}', 'os-local', ?, 'source', 'Source', NULL,
                      '{}', '{}', '{}', '{}', NULL, 'action', NULL, NULL, NULL,
                      'compute')
            """,
            (
                NODE_TEMPLATE_UUID,
                timestamp,
                timestamp,
                RESOURCE_TEMPLATE_UUID,
            ),
        )
        connection.execute(
            """
            INSERT INTO workflow_handle_template(
                uuid, create_time, update_time, meta_data, authority_id,
                workflow_node_template_uuid, handle_key, io_type,
                display_name, type, required, data_source, data_key
            ) VALUES (?, ?, ?, '{}', 'os-local', ?, 'result', 'source',
                      'Result', 'number', 0, NULL, NULL)
            """,
            (
                HANDLE_TEMPLATE_UUID,
                timestamp,
                timestamp,
                NODE_TEMPLATE_UUID,
            ),
        )


def _authoring_service(
    store: WorkflowStore,
    tmp_path: Path,
    *,
    compiler: Any,
) -> tuple[WorkflowService, int]:
    service = WorkflowService(store, compiler=compiler)
    service.create_workflow(
        name="phase 01 review round 11",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    _seed_template_catalog(store)
    service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[_node()],
        edges=[],
    )
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase_01_review_round_11",
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


def _assert_internal_error(response: Any) -> None:
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
    ids=["required-field-missing", "field-type-invalid"],
)
def test_draft_prioritizes_applied_authority_fault_over_invalid_compilation(
    damaged_store: DamagedAppliedGraphStore,
    tmp_path: Path,
    damage_case: str,
) -> None:
    service, revision = _authoring_service(
        damaged_store,
        tmp_path,
        compiler=InvalidCompilationCompiler(),
    )
    damaged_store.damage_case = damage_case

    with TestClient(
        create_workflow_app(service),
        raise_server_exceptions=False,
    ) as client:
        response = _save_draft(client, revision=revision)

    _assert_internal_error(response)


@pytest.mark.parametrize(
    "damage_case",
    [
        "workflow.name.missing",
        "workflow.tags.type",
    ],
    ids=["required-field-missing", "field-type-invalid"],
)
def test_get_authoring_rejects_damaged_applied_backend_dto(
    damaged_store: DamagedAppliedGraphStore,
    tmp_path: Path,
    damage_case: str,
) -> None:
    service, _ = _authoring_service(
        damaged_store,
        tmp_path,
        compiler=CatalogCandidateCompiler(),
    )
    damaged_store.damage_case = damage_case

    with TestClient(
        create_workflow_app(service),
        raise_server_exceptions=False,
    ) as client:
        response = client.get(f"/api/v1/workflows/{WORKFLOW_UUID}/authoring")

    _assert_internal_error(response)


INVALID_CANDIDATE_ENTITY_TYPES = [
    pytest.param("workflow", "tags", {}, id="workflow-tags-object"),
    pytest.param("workflow", "revision", True, id="workflow-revision-bool"),
    pytest.param("workflow", "revision", 0, id="workflow-revision-zero"),
    pytest.param(
        "workflow",
        "revision",
        INT64_MAX + 1,
        id="workflow-revision-int64-overflow",
    ),
    pytest.param("workflow", "meta_data", [], id="workflow-meta-data-array"),
    pytest.param("workflow", "name", [], id="workflow-name-array"),
    pytest.param("workflow", "description", [], id="workflow-description-array"),
    pytest.param(
        "node_templates",
        "goal",
        [],
        id="node-template-goal-array",
    ),
    pytest.param(
        "node_templates",
        "goal_default",
        [],
        id="node-template-goal-default-array",
    ),
    pytest.param(
        "node_templates",
        "feedback",
        [],
        id="node-template-feedback-array",
    ),
    pytest.param(
        "node_templates",
        "result",
        [],
        id="node-template-result-array",
    ),
    pytest.param(
        "node_templates",
        "meta_data",
        [],
        id="node-template-meta-data-array",
    ),
    pytest.param(
        "node_templates",
        "name",
        [],
        id="node-template-required-text-array",
    ),
    pytest.param(
        "node_templates",
        "description",
        [],
        id="node-template-nullable-text-array",
    ),
    pytest.param(
        "node_templates",
        "schema",
        {},
        id="node-template-schema-object",
    ),
    pytest.param(
        "node_templates",
        "uuid",
        1,
        id="node-template-uuid-integer",
    ),
    pytest.param(
        "node_templates",
        "resource_template_uuid",
        1,
        id="node-template-resource-uuid-integer",
    ),
    pytest.param(
        "handle_templates",
        "required",
        0,
        id="handle-template-required-integer",
    ),
    pytest.param(
        "handle_templates",
        "meta_data",
        [],
        id="handle-template-meta-data-array",
    ),
    pytest.param(
        "handle_templates",
        "handle_key",
        [],
        id="handle-template-required-text-array",
    ),
    pytest.param(
        "handle_templates",
        "data_source",
        [],
        id="handle-template-nullable-text-array",
    ),
    pytest.param(
        "handle_templates",
        "uuid",
        1,
        id="handle-template-uuid-integer",
    ),
    pytest.param(
        "handle_templates",
        "workflow_node_template_uuid",
        1,
        id="handle-template-node-template-uuid-integer",
    ),
]


@pytest.mark.parametrize(
    ("entity_kind", "field", "value"),
    INVALID_CANDIDATE_ENTITY_TYPES,
)
def test_invalid_candidate_backend_dto_type_is_saved_as_draft_diagnostic(
    store: WorkflowStore,
    tmp_path: Path,
    entity_kind: str,
    field: str,
    value: Any,
) -> None:
    service, revision = _authoring_service(
        store,
        tmp_path,
        compiler=CatalogCandidateCompiler(
            entity_kind=entity_kind,
            field=field,
            value=value,
        ),
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


def test_candidate_accepts_nullable_and_empty_backend_dto_boundaries(
    store: WorkflowStore,
    tmp_path: Path,
) -> None:
    service, revision = _authoring_service(
        store,
        tmp_path,
        compiler=CatalogCandidateCompiler(),
    )

    with TestClient(create_workflow_app(service)) as client:
        response = _save_draft(client, revision=revision)

    payload = response.json()
    aggregate = payload["data"]
    candidate = aggregate["candidate"]
    graph = candidate["graph"]
    node_template = graph["node_templates"][0]
    handle_template = graph["handle_templates"][0]
    assert {
        "status": response.status_code,
        "envelope_code": payload["code"],
        "state": aggregate["state"],
        "has_candidate_hash": bool(candidate["candidate_hash"]),
        "workflow_revision": graph["workflow"]["revision"],
        "workflow_tags": graph["workflow"]["tags"],
        "workflow_meta_data": graph["workflow"]["meta_data"],
        "node_json_objects": [
            node_template["goal"],
            node_template["goal_default"],
            node_template["feedback"],
            node_template["result"],
        ],
        "required": handle_template["required"],
        "nullable_fields_present": {
            key
            for entity in (graph["workflow"], node_template, handle_template)
            for key, value in entity.items()
            if value is None
        },
    } == {
        "status": 200,
        "envelope_code": 0,
        "state": "unapplied_graph",
        "has_candidate_hash": True,
        "workflow_revision": revision,
        "workflow_tags": ["stable", 1, None, {"nested": True}],
        "workflow_meta_data": {"round": 11},
        "node_json_objects": [{}, {}, {}, {}],
        "required": False,
        "nullable_fields_present": set(),
    }
