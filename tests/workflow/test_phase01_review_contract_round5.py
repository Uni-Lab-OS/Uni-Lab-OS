"""Phase 01 第五轮规格评审发现的公共合同回归测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import (
    create_workflow_app,
    install_workflow_api,
)
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
CATALOG_FINGERPRINT = f"sha256:{'9' * 64}"


@pytest.fixture()
def store(tmp_path: Path):
    opened = WorkflowStore(tmp_path / "workflow.db")
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture()
def service(store: WorkflowStore) -> WorkflowService:
    return WorkflowService(store)


def _create_workflow(service: WorkflowService) -> dict[str, Any]:
    return service.create_workflow(
        name="phase 01 review round 5",
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
        description=None,
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


class WriteShapeGraphCompiler:
    """Return valid graph semantics using only public write DTO entities."""

    compiler_version = "phase-01-review-round-5"
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
                "nodes": [
                    _node(
                        SOURCE_NODE_UUID,
                        template_uuid=SOURCE_TEMPLATE_UUID,
                    ).model_dump(),
                    _node(
                        TARGET_NODE_UUID,
                        template_uuid=TARGET_TEMPLATE_UUID,
                    ).model_dump(),
                ],
                "edges": [_edge().model_dump()],
                "node_templates": applied_graph["node_templates"],
                "handle_templates": applied_graph["handle_templates"],
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


def _authoring_service(
    store: WorkflowStore,
    tmp_path: Path,
) -> WorkflowService:
    _seed_template_catalog(store)
    workflow_service = WorkflowService(
        store,
        compiler=WriteShapeGraphCompiler(),
    )
    _create_workflow(workflow_service)
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
    package_root = tmp_path / "package"
    package_root.mkdir()
    workflow_service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase_01_round_5",
        package_root=package_root,
        relative_path="workflows/review.py",
    )
    return workflow_service


def _save_write_shape_candidate(client: TestClient) -> dict[str, Any]:
    response = client.put(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": "build()\n",
            "expected_draft_hash": None,
            "expected_workflow_revision": 2,
        },
    )
    assert response.status_code == 200
    return response.json()["data"]


@pytest.mark.parametrize(
    ("collection", "required_fields", "optional_fields"),
    [
        (
            "nodes",
            {"workflow_uuid", "create_time", "update_time"},
            {
                "description",
                "parent_uuid",
                "material_uuid",
                "icon",
                "footer",
                "action_name",
                "action_type",
                "script",
            },
        ),
        (
            "edges",
            {
                "uuid",
                "create_time",
                "update_time",
                "meta_data",
                "source_node_uuid",
                "target_node_uuid",
                "source_handle_uuid",
                "target_handle_uuid",
            },
            {"description"},
        ),
    ],
    ids=["node", "edge"],
)
def test_write_shape_candidate_entities_are_hydrated_as_backend_read_dto(
    store: WorkflowStore,
    tmp_path: Path,
    collection: str,
    required_fields: set[str],
    optional_fields: set[str],
) -> None:
    workflow_service = _authoring_service(store, tmp_path)

    with TestClient(create_workflow_app(workflow_service)) as client:
        aggregate = _save_write_shape_candidate(client)

    candidate_entity = aggregate["candidate"]["graph"][collection][0]
    applied_entity = aggregate["applied_graph"][collection][0]
    assert {
        "entity": candidate_entity,
        "has_required_fields": required_fields <= candidate_entity.keys(),
        "omits_null_fields": optional_fields.isdisjoint(candidate_entity),
    } == {
        "entity": applied_entity,
        "has_required_fields": True,
        "omits_null_fields": True,
    }


def test_apply_accepts_server_hydrated_write_shape_candidate(
    store: WorkflowStore,
    tmp_path: Path,
) -> None:
    workflow_service = _authoring_service(store, tmp_path)

    with TestClient(create_workflow_app(workflow_service)) as client:
        aggregate = _save_write_shape_candidate(client)
        candidate = aggregate["candidate"]
        response = client.post(
            f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
            json={"candidate_hash": candidate["candidate_hash"]},
        )

    assert response.status_code == 200
    assert response.json()["data"]["apply_result"]["workflow_revision"] == 2


@pytest.mark.parametrize("run_mode", ["NORMAL", " normal "])
def test_task_run_mode_rejects_non_exact_spellings_without_persisting(
    service: WorkflowService,
    run_mode: str,
) -> None:
    _create_workflow(service)

    with TestClient(create_workflow_app(service)) as client:
        response = client.post(
            "/api/v1/workflow-tasks",
            json={
                "workflow_uuid": WORKFLOW_UUID,
                "run_mode": run_mode,
                "input": {},
                "meta_data": {},
            },
        )
        listed = client.get("/api/v1/workflow-tasks")

    error = response.json().get("error") or {}
    assert {
        "status": response.status_code,
        "error_code": error.get("code"),
        "persisted_tasks": listed.json()["data"]["total"],
    } == {
        "status": 400,
        "error_code": "invalid_input",
        "persisted_tasks": 0,
    }


@pytest.mark.parametrize(
    ("run_mode", "expected"),
    [
        ("", "normal"),
        ("normal", "normal"),
        ("step", "step"),
        ("single_node", "single_node"),
    ],
)
def test_task_run_mode_accepts_only_frozen_exact_values(
    service: WorkflowService,
    run_mode: str,
    expected: str,
) -> None:
    _create_workflow(service)
    service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[_node(SOURCE_NODE_UUID)],
        edges=[],
    )

    with TestClient(create_workflow_app(service)) as client:
        response = client.post(
            "/api/v1/workflow-tasks",
            json={
                "workflow_uuid": WORKFLOW_UUID,
                "run_mode": run_mode,
                "input": {},
                "meta_data": {},
            },
        )

    assert response.status_code == 201
    assert response.json()["data"]["run_mode"] == expected


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/workflows-extra",
        "/api/v1/workflow-tasks-extra",
        "/api/v1/workflow-node-jobs-extra",
    ],
)
def test_workflow_lookalike_routes_keep_native_fastapi_validation(
    service: WorkflowService,
    path: str,
) -> None:
    app = FastAPI()

    def unrelated(count: int) -> dict[str, int]:
        return {"count": count}

    app.add_api_route(path, unrelated, methods=["GET"])
    install_workflow_api(app, service)

    with TestClient(app) as client:
        response = client.get(f"{path}?count=not-an-integer")

    assert response.status_code == 422
    assert isinstance(response.json().get("detail"), list)
    assert "error" not in response.json()
