"""Phase 01 第六轮规格评审发现的公共合同回归测试。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
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
CATALOG_FINGERPRINT = f"sha256:{'8' * 64}"

WORKFLOW_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "name",
    "tags",
    "revision",
}
NODE_TEMPLATE_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "resource_template_uuid",
    "name",
    "display_name",
    "goal",
    "goal_default",
    "feedback",
    "result",
    "type",
    "node_type",
}
HANDLE_TEMPLATE_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "workflow_node_template_uuid",
    "handle_key",
    "io_type",
    "display_name",
    "type",
    "required",
}


@pytest.fixture()
def store(tmp_path: Path):
    opened = WorkflowStore(tmp_path / "workflow.db")
    try:
        yield opened
    finally:
        opened.close()


def _create_workflow(service: WorkflowService) -> dict[str, Any]:
    return service.create_workflow(
        name="phase 01 review round 6",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )


def _node(
    node_uuid: str,
    *,
    template_uuid: str | None = None,
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


def _source_only_changeset() -> dict[str, Any]:
    return {
        "kind": "source_only",
        "created_node_uuids": [],
        "updated_node_uuids": [],
        "deleted_node_uuids": [],
        "created_edge_uuids": [],
        "updated_edge_uuids": [],
        "deleted_edge_uuids": [],
        "reserved_metadata_changed": False,
    }


def _delete_graph_changeset(applied_graph: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "graph",
        "created_node_uuids": [],
        "updated_node_uuids": [],
        "deleted_node_uuids": [item["uuid"] for item in applied_graph["nodes"]],
        "created_edge_uuids": [],
        "updated_edge_uuids": [],
        "deleted_edge_uuids": [item["uuid"] for item in applied_graph["edges"]],
        "reserved_metadata_changed": False,
    }


def _compilation(
    *,
    graph: dict[str, Any],
    python_source: str,
    compiler_version: str,
    changeset: dict[str, Any],
) -> CandidateCompilation:
    return CandidateCompilation(
        diagnostics=[],
        graph=graph,
        normalized_python_source=python_source,
        source_map=[],
        changeset=changeset,
        compiler_version=compiler_version,
        template_catalog_fingerprint=CATALOG_FINGERPRINT,
    )


def _empty_graph(applied_graph: dict[str, Any]) -> dict[str, Any]:
    return {
        "workflow": applied_graph["workflow"],
        "nodes": [],
        "edges": [],
        "node_templates": [],
        "handle_templates": [],
    }


def _malformed_catalog_graph(
    applied_graph: dict[str, Any],
) -> dict[str, Any]:
    return {
        **_empty_graph(applied_graph),
        "node_templates": [
            {
                "uuid": SOURCE_TEMPLATE_UUID,
                "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
            }
        ],
    }


class ExtraCatalogCompiler:
    compiler_version = "phase-01-review-round-6-extra"
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
        graph = deepcopy(applied_graph)
        graph["workflow"].update(
            {
                "description": None,
                "frontend_only": "must be discarded",
            }
        )
        for template in graph["node_templates"]:
            template.update(
                {
                    "description": None,
                    "class": None,
                    "schema": None,
                    "icon": None,
                    "header": None,
                    "footer": None,
                    "frontend_only": "must be discarded",
                }
            )
        for handle in graph["handle_templates"]:
            handle.update(
                {
                    "description": None,
                    "data_source": None,
                    "data_key": None,
                    "frontend_only": "must be discarded",
                }
            )
        return _compilation(
            graph=graph,
            python_source=python_source,
            compiler_version=self.compiler_version,
            changeset=_source_only_changeset(),
        )


class EmptyCatalogCompiler:
    compiler_version = "phase-01-review-round-6-empty"
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
        return _compilation(
            graph=_empty_graph(applied_graph),
            python_source=python_source,
            compiler_version=self.compiler_version,
            changeset=_delete_graph_changeset(applied_graph),
        )


class EmptyNodeGraphCompiler:
    compiler_version = "phase-01-review-round-6-empty-node-graph"
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
        graph = _empty_graph(applied_graph)
        graph["node_templates"] = deepcopy(applied_graph["node_templates"])
        graph["handle_templates"] = deepcopy(applied_graph["handle_templates"])
        return _compilation(
            graph=graph,
            python_source=python_source,
            compiler_version=self.compiler_version,
            changeset=_delete_graph_changeset(applied_graph),
        )


class MalformedCatalogCompiler:
    compiler_version = "phase-01-review-round-6-malformed"
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
        return _compilation(
            graph=_malformed_catalog_graph(applied_graph),
            python_source=python_source,
            compiler_version=self.compiler_version,
            changeset=_source_only_changeset(),
        )


class ApplyRevalidationCompiler:
    compiler_version = "phase-01-review-round-6-revalidation"
    template_catalog_fingerprint = CATALOG_FINGERPRINT

    def __init__(self) -> None:
        self.compile_count = 0

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
        self.compile_count += 1
        graph = (
            _empty_graph(applied_graph)
            if self.compile_count == 1
            else _malformed_catalog_graph(applied_graph)
        )
        return _compilation(
            graph=graph,
            python_source=python_source,
            compiler_version=self.compiler_version,
            changeset=_source_only_changeset(),
        )


def _authoring_service(
    store: WorkflowStore,
    tmp_path: Path,
    *,
    compiler: Any,
    with_catalog_graph: bool,
) -> tuple[WorkflowService, int]:
    workflow_service = WorkflowService(store, compiler=compiler)
    _create_workflow(workflow_service)
    revision = 1
    if with_catalog_graph:
        _seed_template_catalog(store)
        workflow_service.save_graph(
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
        revision = 2
    package_root = tmp_path / "package"
    package_root.mkdir()
    workflow_service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase_01_round_6",
        package_root=package_root,
        relative_path="workflows/review.py",
    )
    return workflow_service, revision


def _save_draft(
    client: TestClient,
    *,
    revision: int,
    python_source: str = "build()\n",
) -> Any:
    return client.put(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": python_source,
            "expected_draft_hash": None,
            "expected_workflow_revision": revision,
        },
    )


@pytest.mark.parametrize(
    ("entity_kind", "expected_fields"),
    [
        ("workflow", WORKFLOW_READ_FIELDS),
        ("node_templates", NODE_TEMPLATE_READ_FIELDS),
        ("handle_templates", HANDLE_TEMPLATE_READ_FIELDS),
    ],
    ids=["workflow", "node-template", "handle-template"],
)
def test_candidate_catalog_entities_use_exact_backend_read_dto_fields(
    store: WorkflowStore,
    tmp_path: Path,
    entity_kind: str,
    expected_fields: set[str],
) -> None:
    workflow_service, revision = _authoring_service(
        store,
        tmp_path,
        compiler=ExtraCatalogCompiler(),
        with_catalog_graph=True,
    )

    with TestClient(create_workflow_app(workflow_service)) as client:
        response = _save_draft(client, revision=revision)

    assert response.status_code == 200
    candidate_graph = response.json()["data"]["candidate"]["graph"]
    entity = (
        candidate_graph["workflow"]
        if entity_kind == "workflow"
        else candidate_graph[entity_kind][0]
    )
    assert set(entity) == expected_fields
    assert "frontend_only" not in entity


def test_explicit_empty_candidate_catalog_is_not_filled_from_applied_graph(
    store: WorkflowStore,
    tmp_path: Path,
) -> None:
    workflow_service, revision = _authoring_service(
        store,
        tmp_path,
        compiler=EmptyCatalogCompiler(),
        with_catalog_graph=True,
    )

    with TestClient(create_workflow_app(workflow_service)) as client:
        response = _save_draft(client, revision=revision)

    aggregate = response.json()["data"]
    assert response.status_code == 200
    assert aggregate["state"] == "draft_invalid"
    assert aggregate["candidate"] is None
    assert aggregate["draft"]["diagnostics"] == [
        {
            "severity": "error",
            "code": "candidate_invalid",
            "message": "工作流校验失败，请检查节点、连线和输入输出",
        }
    ]


def test_draft_hydration_failure_is_saved_as_successful_invalid_aggregate(
    store: WorkflowStore,
    tmp_path: Path,
) -> None:
    workflow_service, revision = _authoring_service(
        store,
        tmp_path,
        compiler=MalformedCatalogCompiler(),
        with_catalog_graph=False,
    )
    python_source = "broken candidate projection\n"

    with TestClient(create_workflow_app(workflow_service)) as client:
        response = _save_draft(
            client,
            revision=revision,
            python_source=python_source,
        )
        refreshed = client.get(f"/api/v1/workflows/{WORKFLOW_UUID}/authoring")

    response_data = response.json().get("data") or {}
    response_draft = response_data.get("draft") or {}
    diagnostics = response_draft.get("diagnostics") or []
    diagnostic = diagnostics[0] if diagnostics else {}
    refreshed_data = refreshed.json()["data"]
    assert {
        "status": response.status_code,
        "envelope_code": response.json().get("code"),
        "state": response_data.get("state"),
        "candidate": response_data.get("candidate"),
        "saved_source": response_draft.get("python_source"),
        "has_draft_hash": bool(response_draft.get("draft_hash")),
        "diagnostic": {
            key: diagnostic.get(key) for key in ("severity", "code", "message")
        },
        "refreshed_state": refreshed_data["state"],
        "refreshed_source": refreshed_data["draft"]["python_source"],
    } == {
        "status": 200,
        "envelope_code": 0,
        "state": "draft_invalid",
        "candidate": None,
        "saved_source": python_source,
        "has_draft_hash": True,
        "diagnostic": {
            "severity": "error",
            "code": "candidate_invalid",
            "message": "工作流校验失败，请检查节点、连线和输入输出",
        },
        "refreshed_state": "draft_invalid",
        "refreshed_source": python_source,
    }


def test_apply_revalidation_failure_uses_candidate_invalid_422(
    store: WorkflowStore,
    tmp_path: Path,
) -> None:
    workflow_service, revision = _authoring_service(
        store,
        tmp_path,
        compiler=ApplyRevalidationCompiler(),
        with_catalog_graph=False,
    )

    with TestClient(create_workflow_app(workflow_service)) as client:
        draft_response = _save_draft(client, revision=revision)
        aggregate = draft_response.json()["data"]
        response = client.post(
            f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
            json={
                "expected_draft_hash": aggregate["draft"]["draft_hash"],
                "expected_workflow_revision": revision,
                "expected_candidate_hash": aggregate["candidate"]["candidate_hash"],
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "code": 422,
        "error": {
            "code": "candidate_invalid",
            "message": "工作流校验失败，请检查节点、连线和输入输出",
        },
    }


def test_valid_empty_node_graph_with_authority_catalog_still_applies(
    store: WorkflowStore,
    tmp_path: Path,
) -> None:
    workflow_service, revision = _authoring_service(
        store,
        tmp_path,
        compiler=EmptyNodeGraphCompiler(),
        with_catalog_graph=True,
    )

    with TestClient(create_workflow_app(workflow_service)) as client:
        draft_response = _save_draft(client, revision=revision)
        aggregate = draft_response.json()["data"]
        response = client.post(
            f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
            json={
                "expected_draft_hash": aggregate["draft"]["draft_hash"],
                "expected_workflow_revision": revision,
                "expected_candidate_hash": aggregate["candidate"]["candidate_hash"],
            },
        )

    assert response.status_code == 200
    assert response.json()["data"]["apply_result"]["workflow_revision"] == (
        revision + 1
    )
