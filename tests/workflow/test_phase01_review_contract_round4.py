"""Phase 01 第四轮规格评审发现的公共合同回归测试。"""

from __future__ import annotations

from copy import deepcopy
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
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
ROOT_NODE_UUID = "20000000-0000-4000-8000-000000000002"
DOWNSTREAM_NODE_UUID = "20000000-0000-4000-8000-000000000001"
EDGE_UUID = "30000000-0000-4000-8000-000000000001"
SOURCE_TEMPLATE_UUID = "40000000-0000-4000-8000-000000000001"
TARGET_TEMPLATE_UUID = "40000000-0000-4000-8000-000000000002"
RESOURCE_TEMPLATE_UUID = "50000000-0000-4000-8000-000000000001"
SOURCE_HANDLE_UUID = "60000000-0000-4000-8000-000000000001"
TARGET_HANDLE_UUID = "60000000-0000-4000-8000-000000000002"
MATERIAL_UUID = "70000000-0000-4000-8000-000000000001"
CATALOG_FINGERPRINT = f"sha256:{'f' * 64}"


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
        name="phase 01 review round 4",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )


def _node(
    node_uuid: str,
    *,
    template_uuid: str | None = None,
    node_type: str = "compute",
    material_uuid: str | None = None,
    execution_policy: dict[str, Any] | None = None,
    script: str | None = None,
) -> WorkflowNodeWrite:
    return WorkflowNodeWrite(
        uuid=node_uuid,
        workflow_node_template_uuid=template_uuid,
        material_uuid=material_uuid,
        name=node_uuid,
        status="idle",
        type=node_type,
        pose={},
        param={},
        execution_policy=execution_policy or {},
        disabled=False,
        minimized=False,
        script=script,
        meta_data={},
    )


def _edge() -> WorkflowEdgeWrite:
    return WorkflowEdgeWrite(
        uuid=EDGE_UUID,
        source_node_uuid=ROOT_NODE_UUID,
        target_node_uuid=DOWNSTREAM_NODE_UUID,
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


class BackendReadGraphCompiler:
    """Return a full Backend read DTO, including nullable optional fields."""

    compiler_version = "phase-01-review-round-4"
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
        graph["workflow"]["description"] = None
        for node in graph["nodes"]:
            node.update(
                {
                    "description": None,
                    "parent_uuid": None,
                    "material_uuid": None,
                    "icon": None,
                    "footer": None,
                    "action_name": None,
                    "action_type": None,
                    "script": None,
                }
            )
        for edge in graph["edges"]:
            edge["description"] = None
        return CandidateCompilation(
            diagnostics=[],
            graph=graph,
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


def _authoring_service_with_read_graph(
    store: WorkflowStore,
    tmp_path: Path,
) -> WorkflowService:
    _seed_template_catalog(store)
    workflow_service = WorkflowService(
        store,
        compiler=BackendReadGraphCompiler(),
    )
    _create_workflow(workflow_service)
    workflow_service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[
            _node(ROOT_NODE_UUID, template_uuid=SOURCE_TEMPLATE_UUID),
            _node(
                DOWNSTREAM_NODE_UUID,
                template_uuid=TARGET_TEMPLATE_UUID,
            ),
        ],
        edges=[_edge()],
    )
    package_root = tmp_path / "package"
    package_root.mkdir()
    workflow_service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase_01_round_4",
        package_root=package_root,
        relative_path="workflows/review.py",
    )
    return workflow_service


def _save_backend_read_candidate(client: TestClient) -> dict[str, Any]:
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


def test_candidate_graph_projects_backend_read_dto_with_omitempty(
    store: WorkflowStore,
    tmp_path: Path,
) -> None:
    workflow_service = _authoring_service_with_read_graph(store, tmp_path)

    with TestClient(create_workflow_app(workflow_service)) as client:
        aggregate = _save_backend_read_candidate(client)

    candidate_graph = aggregate["candidate"]["graph"]
    assert candidate_graph == aggregate["applied_graph"]
    assert {
        "uuid",
        "create_time",
        "update_time",
    } <= candidate_graph["workflow"].keys()
    assert {
        "uuid",
        "workflow_uuid",
        "create_time",
        "update_time",
    } <= candidate_graph["nodes"][0].keys()
    assert {
        "uuid",
        "create_time",
        "update_time",
    } <= candidate_graph["edges"][0].keys()


def test_apply_accepts_backend_read_dto_fields_from_candidate_graph(
    store: WorkflowStore,
    tmp_path: Path,
) -> None:
    workflow_service = _authoring_service_with_read_graph(store, tmp_path)

    with TestClient(create_workflow_app(workflow_service)) as client:
        aggregate = _save_backend_read_candidate(client)
        candidate = aggregate["candidate"]
        response = client.post(
            f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
            json={"candidate_hash": candidate["candidate_hash"]},
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["apply_result"]["workflow_revision"] == 2
    assert data["authoring"]["applied_graph"]["workflow"]["revision"] == 2


def test_single_node_without_target_selects_first_topological_root(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    _seed_template_catalog(store)
    _create_workflow(service)
    first = service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[
            _node(
                DOWNSTREAM_NODE_UUID,
                template_uuid=TARGET_TEMPLATE_UUID,
            )
        ],
        edges=[],
    )
    second = service.save_graph(
        WORKFLOW_UUID,
        revision=2,
        nodes=[
            _node(ROOT_NODE_UUID, template_uuid=SOURCE_TEMPLATE_UUID),
            _node(
                DOWNSTREAM_NODE_UUID,
                template_uuid=TARGET_TEMPLATE_UUID,
            ),
        ],
        edges=[_edge()],
    )
    first_create_time = first["nodes"][0]["create_time"]
    root_create_time = next(
        node["create_time"]
        for node in second["nodes"]
        if node["uuid"] == ROOT_NODE_UUID
    )
    assert first_create_time < root_create_time

    task = service.create_workflow_task(
        workflow_uuid=WORKFLOW_UUID,
        run_mode="single_node",
        target_node_uuid=None,
        input_value={},
        description=None,
        meta_data={},
    )

    assert {
        "task_target": task.get("target_node_uuid"),
        "plan_target": task["execution_plan"].get("target_node_uuid"),
        "planned_nodes": [node["uuid"] for node in task["execution_plan"]["nodes"]],
    } == {
        "task_target": ROOT_NODE_UUID,
        "plan_target": ROOT_NODE_UUID,
        "planned_nodes": [ROOT_NODE_UUID],
    }


def test_initial_job_does_not_inherit_node_material_or_timeout(
    service: WorkflowService,
) -> None:
    _create_workflow(service)
    service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[
            _node(
                ROOT_NODE_UUID,
                material_uuid=MATERIAL_UUID,
                execution_policy={"execution_timeout_seconds": 17},
            )
        ],
        edges=[],
    )

    task = service.create_workflow_task(
        workflow_uuid=WORKFLOW_UUID,
        run_mode="normal",
        target_node_uuid=None,
        input_value={},
        description=None,
        meta_data={},
    )
    job = service.list_workflow_node_jobs(task["uuid"])[0]

    assert (
        "material_uuid" in job,
        job["execution_timeout_seconds"],
    ) == (False, 0)


def test_empty_run_mode_normalizes_to_normal(
    service: WorkflowService,
) -> None:
    _create_workflow(service)

    task = service.create_workflow_task(
        workflow_uuid=WORKFLOW_UUID,
        run_mode="",
        target_node_uuid=None,
        input_value={},
        description=None,
        meta_data={},
    )

    assert task["run_mode"] == "normal"
    assert task["execution_plan"]["run_mode"] == "normal"


@pytest.mark.parametrize(
    ("submitted", "expected"),
    [
        ("  labeled task  ", "labeled task"),
        (" \t\n ", None),
    ],
    ids=["trimmed", "blank-omitted"],
)
def test_task_description_uses_backend_optional_text_normalization(
    service: WorkflowService,
    submitted: str,
    expected: str | None,
) -> None:
    _create_workflow(service)

    task = service.create_workflow_task(
        workflow_uuid=WORKFLOW_UUID,
        run_mode="normal",
        target_node_uuid=None,
        input_value={},
        description=submitted,
        meta_data={},
    )

    if expected is None:
        assert "description" not in task
    else:
        assert task["description"] == expected


def test_node_type_matching_is_case_insensitive_for_task_planning(
    service: WorkflowService,
) -> None:
    _create_workflow(service)
    service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[_node(ROOT_NODE_UUID, node_type="Compute")],
        edges=[],
    )

    task = service.create_workflow_task(
        workflow_uuid=WORKFLOW_UUID,
        run_mode="normal",
        target_node_uuid=None,
        input_value={},
        description=None,
        meta_data={},
    )

    assert task["execution_plan"]["nodes"][0]["kind"] == "compute"


def test_script_task_is_rejected_without_configured_script_executor(
    service: WorkflowService,
) -> None:
    _create_workflow(service)
    service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[
            _node(
                ROOT_NODE_UUID,
                node_type="script",
                script="result = 1",
            )
        ],
        edges=[],
    )

    with pytest.raises(WorkflowError) as failure:
        service.create_workflow_task(
            workflow_uuid=WORKFLOW_UUID,
            run_mode="normal",
            target_node_uuid=None,
            input_value={},
            description=None,
            meta_data={},
        )

    assert failure.value.code == "invalid_input"


def test_installing_workflow_api_preserves_unrelated_validation_errors(
    service: WorkflowService,
) -> None:
    app = FastAPI()

    @app.get("/unrelated")
    def unrelated(count: int) -> dict[str, int]:
        return {"count": count}

    install_workflow_api(app, service)

    with TestClient(app) as client:
        response = client.get("/unrelated?count=not-an-integer")

    assert response.status_code == 422
    assert isinstance(response.json().get("detail"), list)
    assert "error" not in response.json()


@pytest.mark.parametrize(
    "request_kind",
    ["workflow", "graph", "task"],
)
def test_shared_backend_requests_ignore_unknown_json_fields(
    service: WorkflowService,
    request_kind: str,
) -> None:
    with TestClient(create_workflow_app(service)) as client:
        if request_kind == "workflow":
            response = client.post(
                "/api/v1/workflows",
                json={
                    "name": "unknown fields",
                    "tags": [],
                    "meta_data": {},
                    "frontend_only": True,
                },
            )
        elif request_kind == "graph":
            _create_workflow(service)
            response = client.put(
                f"/api/v1/workflows/{WORKFLOW_UUID}/graph",
                json={
                    "revision": 1,
                    "nodes": [
                        {
                            "uuid": ROOT_NODE_UUID,
                            "name": "node",
                            "status": "idle",
                            "type": "compute",
                            "pose": {},
                            "param": {},
                            "execution_policy": {},
                            "meta_data": {},
                            "frontend_only": True,
                        }
                    ],
                    "edges": [],
                    "frontend_only": True,
                },
            )
        else:
            _create_workflow(service)
            response = client.post(
                "/api/v1/workflow-tasks",
                json={
                    "workflow_uuid": WORKFLOW_UUID,
                    "run_mode": "normal",
                    "input": {},
                    "meta_data": {},
                    "frontend_only": True,
                },
            )

    assert response.status_code in {200, 201}
    assert response.json()["code"] == 0


def test_authoring_apply_request_remains_closed(
    service: WorkflowService,
) -> None:
    _create_workflow(service)

    with TestClient(create_workflow_app(service)) as client:
        response = client.post(
            f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
            json={
                "candidate_hash": f"sha256:{'b' * 64}",
                "frontend_only": True,
            },
        )

    assert response.status_code == 400
    assert response.json() == {
        "code": 400,
        "error": {
            "code": "invalid_input",
            "message": "提交内容格式不正确",
        },
    }
