"""Phase 01 第三轮规格评审发现的公共合同回归测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.models import (
    CandidateCompilation,
    WorkflowEdgeWrite,
    WorkflowNodeWrite,
)
from unilabos.workflow.service import (
    WorkflowConflict,
    WorkflowService,
)
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
EARLY_NODE_B_UUID = "20000000-0000-4000-8000-000000000002"
EARLY_NODE_C_UUID = "20000000-0000-4000-8000-000000000003"
LATE_NODE_A_UUID = "20000000-0000-4000-8000-000000000001"
SOURCE_NODE_UUID = EARLY_NODE_B_UUID
TARGET_NODE_UUID = EARLY_NODE_C_UUID
EDGE_UUID = "30000000-0000-4000-8000-000000000001"

SOURCE_TEMPLATE_UUID = "40000000-0000-4000-8000-000000000001"
TARGET_TEMPLATE_UUID = "40000000-0000-4000-8000-000000000002"
RESOURCE_TEMPLATE_UUID = "50000000-0000-4000-8000-000000000001"
SOURCE_HANDLE_UUID = "60000000-0000-4000-8000-000000000001"
TARGET_HANDLE_UUID = "60000000-0000-4000-8000-000000000002"
MISSING_HANDLE_UUID = "60000000-0000-4000-8000-000000000099"

CATALOG_FINGERPRINT = f"sha256:{'e' * 64}"


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
        name="phase 01 review round 3",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )


def _node(
    node_uuid: str,
    *,
    template_uuid: str | None = None,
    name: str | None = None,
    status: str = "idle",
    node_type: str = "compute",
    param: dict[str, Any] | None = None,
    disabled: bool = False,
    meta_data: dict[str, Any] | None = None,
) -> WorkflowNodeWrite:
    return WorkflowNodeWrite(
        uuid=node_uuid,
        workflow_node_template_uuid=template_uuid,
        name=name or node_uuid,
        status=status,
        type=node_type,
        pose={},
        param={} if param is None else param,
        execution_policy={},
        disabled=disabled,
        minimized=False,
        meta_data=meta_data or {},
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


def _seed_template_catalog(
    store: WorkflowStore,
    *,
    source_data_source: str | None = "executor",
    source_data_key: str | None = "measurement",
    target_data_key: str | None = "wrapped@@@temperature",
) -> None:
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
                source_data_source,
                source_data_key,
            ),
            (
                TARGET_HANDLE_UUID,
                TARGET_TEMPLATE_UUID,
                "temperature",
                "target",
                None,
                target_data_key,
            ),
        ):
            connection.execute(
                """
                INSERT INTO workflow_handle_template(
                    uuid, create_time, update_time, meta_data, authority_id,
                    workflow_node_template_uuid, handle_key, io_type,
                    display_name, type, required, data_source, data_key
                ) VALUES (?, ?, ?, '{}', 'os-local', ?, ?, ?, ?, 'number', 0,
                          ?, ?)
                """,
                (
                    values[0],
                    timestamp,
                    timestamp,
                    values[1],
                    values[2],
                    values[3],
                    values[2],
                    values[4],
                    values[5],
                ),
            )


def _input_contract(names: list[str]) -> dict[str, Any]:
    return {
        "unilab": {
            "input_contract": {
                "version": 1,
                "parameters": [
                    {"name": name, "schema": {"type": "number"}} for name in names
                ],
            }
        }
    }


def _binding(
    *,
    handle_uuid: str,
    parameter: str,
) -> dict[str, Any]:
    return {
        "unilab": {
            "input_bindings": {
                handle_uuid: {
                    "parameter": parameter,
                }
            }
        }
    }


class GraphCompiler:
    compiler_version = "phase-01-review-round-3"
    template_catalog_fingerprint = CATALOG_FINGERPRINT

    def __init__(
        self,
        *,
        nodes: list[WorkflowNodeWrite],
        workflow_meta_data: dict[str, Any],
    ) -> None:
        self.nodes = nodes
        self.workflow_meta_data = workflow_meta_data

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
        workflow_meta_data = deepcopy(applied_graph["workflow"]["meta_data"])
        workflow_meta_data.pop("unilab", None)
        if "unilab" in self.workflow_meta_data:
            workflow_meta_data["unilab"] = deepcopy(self.workflow_meta_data["unilab"])
        candidate_nodes = {node.uuid: node.model_dump() for node in self.nodes}
        applied_nodes = {
            node["uuid"]: WorkflowNodeWrite.model_validate(node).model_dump()
            for node in applied_graph["nodes"]
        }
        created_node_uuids = sorted(set(candidate_nodes) - set(applied_nodes))
        updated_node_uuids = sorted(
            uuid
            for uuid in set(candidate_nodes) & set(applied_nodes)
            if candidate_nodes[uuid] != applied_nodes[uuid]
        )
        deleted_node_uuids = sorted(set(applied_nodes) - set(candidate_nodes))
        reserved_metadata_changed = workflow_meta_data.get("unilab") != applied_graph[
            "workflow"
        ]["meta_data"].get("unilab")
        return CandidateCompilation(
            diagnostics=[],
            graph={
                "workflow": {
                    **applied_graph["workflow"],
                    "uuid": workflow_uuid,
                    "revision": workflow_revision,
                    "meta_data": workflow_meta_data,
                },
                "nodes": [node.model_dump() for node in self.nodes],
                "edges": [],
                "node_templates": deepcopy(applied_graph["node_templates"]),
                "handle_templates": deepcopy(applied_graph["handle_templates"]),
            },
            normalized_python_source=python_source,
            source_map=[],
            changeset={
                "kind": "graph",
                "created_node_uuids": created_node_uuids,
                "updated_node_uuids": updated_node_uuids,
                "deleted_node_uuids": deleted_node_uuids,
                "created_edge_uuids": [],
                "updated_edge_uuids": [],
                "deleted_edge_uuids": [],
                "reserved_metadata_changed": reserved_metadata_changed,
            },
            compiler_version=self.compiler_version,
            template_catalog_fingerprint=self.template_catalog_fingerprint,
        )


class EchoSourceCompiler:
    compiler_version = "phase-01-review-round-3"
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
            graph=applied_graph,
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


def _register_source(
    service: WorkflowService,
    tmp_path: Path,
) -> Path:
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase_01_round_3",
        package_root=package_root,
        relative_path="workflows/review.py",
    )
    return package_root / "workflows" / "review.py"


def _save_graph_draft(
    *,
    store: WorkflowStore,
    tmp_path: Path,
    compiler: GraphCompiler,
) -> dict[str, Any]:
    service = WorkflowService(store, compiler=compiler)
    _create_workflow(service)
    revision = 1
    if compiler.nodes:
        service.save_graph(
            WORKFLOW_UUID,
            revision=revision,
            nodes=[
                node.model_copy(update={"meta_data": {}}) for node in compiler.nodes
            ],
            edges=[],
        )
        revision = 2
    _register_source(service, tmp_path)
    aggregate = service.save_draft(
        WORKFLOW_UUID,
        python_source="build()\n",
        expected_draft_hash=None,
        expected_workflow_revision=revision,
    )
    return aggregate


@pytest.mark.parametrize(
    ("handle_uuid", "parameter"),
    [
        (MISSING_HANDLE_UUID, "known"),
        (TARGET_HANDLE_UUID, "missing"),
    ],
    ids=["unknown-target-handle", "unknown-workflow-parameter"],
)
def test_disabled_node_still_validates_nonempty_input_bindings(
    store: WorkflowStore,
    tmp_path: Path,
    handle_uuid: str,
    parameter: str,
) -> None:
    _seed_template_catalog(store)
    compiler = GraphCompiler(
        nodes=[
            _node(
                TARGET_NODE_UUID,
                template_uuid=TARGET_TEMPLATE_UUID,
                disabled=True,
                meta_data=_binding(
                    handle_uuid=handle_uuid,
                    parameter=parameter,
                ),
            )
        ],
        workflow_meta_data=_input_contract(["known"]),
    )

    aggregate = _save_graph_draft(
        store=store,
        tmp_path=tmp_path,
        compiler=compiler,
    )

    assert aggregate["state"] == "draft_invalid"
    assert aggregate["candidate"] is None
    assert aggregate["draft"]["diagnostics"][0]["code"] == "candidate_invalid"


def test_node_without_template_rejects_nonempty_input_bindings(
    store: WorkflowStore,
    tmp_path: Path,
) -> None:
    compiler = GraphCompiler(
        nodes=[
            _node(
                TARGET_NODE_UUID,
                meta_data=_binding(
                    handle_uuid=TARGET_HANDLE_UUID,
                    parameter="known",
                ),
            )
        ],
        workflow_meta_data=_input_contract(["known"]),
    )

    aggregate = _save_graph_draft(
        store=store,
        tmp_path=tmp_path,
        compiler=compiler,
    )

    assert aggregate["state"] == "draft_invalid"
    assert aggregate["candidate"] is None
    assert aggregate["draft"]["diagnostics"][0]["code"] == "candidate_invalid"


def test_draft_put_never_pairs_external_draft_b_with_candidate_a(
    store: WorkflowStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_service = WorkflowService(store, compiler=EchoSourceCompiler())
    _create_workflow(workflow_service)
    source_path = _register_source(workflow_service, tmp_path)
    draft_a = "result = 'draft A'\n"
    draft_b = "result = 'external draft B'\n"
    write_installed = Barrier(2)
    external_write_finished = Barrier(2)
    original_atomic_write = workflow_service._atomic_write

    def pause_after_atomic_write(*args, **kwargs) -> None:
        original_atomic_write(*args, **kwargs)
        write_installed.wait(timeout=5)
        external_write_finished.wait(timeout=5)

    monkeypatch.setattr(
        workflow_service,
        "_atomic_write",
        pause_after_atomic_write,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            workflow_service.save_draft,
            WORKFLOW_UUID,
            python_source=draft_a,
            expected_draft_hash=None,
            expected_workflow_revision=1,
        )
        write_installed.wait(timeout=5)
        source_path.write_text(draft_b, encoding="utf-8")
        external_write_finished.wait(timeout=5)
        try:
            aggregate = pending.result(timeout=5)
        except WorkflowConflict as conflict:
            assert conflict.code == "draft_hash_conflict"
        else:
            candidate = aggregate["candidate"]
            assert candidate is not None
            assert candidate["draft_hash"] == aggregate["draft"]["draft_hash"]
            assert (
                candidate["normalized_python_source"]
                == aggregate["draft"]["python_source"]
            )


def test_shared_graph_revision_conflict_uses_backend_error_code(
    service: WorkflowService,
) -> None:
    _create_workflow(service)
    with TestClient(create_workflow_app(service)) as client:
        first = client.put(
            f"/api/v1/workflows/{WORKFLOW_UUID}/graph",
            json={"revision": 1, "nodes": [], "edges": []},
        )
        conflict = client.put(
            f"/api/v1/workflows/{WORKFLOW_UUID}/graph",
            json={"revision": 1, "nodes": [], "edges": []},
        )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "conflict"


def test_authoring_revision_conflict_keeps_specific_error_code(
    store: WorkflowStore,
    tmp_path: Path,
) -> None:
    workflow_service = WorkflowService(store, compiler=EchoSourceCompiler())
    _create_workflow(workflow_service)
    _register_source(workflow_service, tmp_path)
    aggregate = workflow_service.save_draft(
        WORKFLOW_UUID,
        python_source="build()\n",
        expected_draft_hash=None,
        expected_workflow_revision=1,
    )
    workflow_service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[],
        edges=[],
    )

    with pytest.raises(WorkflowConflict) as failure:
        workflow_service.apply_authoring(
            WORKFLOW_UUID,
            candidate_hash=aggregate["candidate"]["candidate_hash"],
        )

    assert failure.value.code == "workflow_revision_conflict"


def test_workflow_name_must_not_be_blank(service: WorkflowService) -> None:
    with TestClient(create_workflow_app(service)) as client:
        response = client.post(
            "/api/v1/workflows",
            json={
                "name": " \t ",
                "tags": [],
                "meta_data": {},
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_input"


def test_workflow_update_name_must_not_be_blank(
    service: WorkflowService,
) -> None:
    _create_workflow(service)

    with TestClient(create_workflow_app(service)) as client:
        response = client.put(
            f"/api/v1/workflows/{WORKFLOW_UUID}",
            json={
                "name": "\n ",
                "tags": [],
                "meta_data": {},
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_input"


@pytest.mark.parametrize("field", ["name", "status", "type"])
def test_full_graph_node_required_strings_must_not_be_blank(
    service: WorkflowService,
    field: str,
) -> None:
    _create_workflow(service)
    node = {
        "uuid": TARGET_NODE_UUID,
        "name": "node",
        "status": "idle",
        "type": "compute",
        "pose": {},
        "param": {},
        "execution_policy": {},
        "meta_data": {},
    }
    node[field] = " \t "

    with TestClient(create_workflow_app(service)) as client:
        response = client.put(
            f"/api/v1/workflows/{WORKFLOW_UUID}/graph",
            json={"revision": 1, "nodes": [node], "edges": []},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_input"


def test_nonblank_workflow_and_graph_node_are_accepted(
    service: WorkflowService,
) -> None:
    with TestClient(create_workflow_app(service)) as client:
        workflow = client.post(
            "/api/v1/workflows",
            json={
                "name": "valid workflow",
                "tags": [],
                "meta_data": {},
            },
        )
        workflow_uuid = workflow.json()["data"]["uuid"]
        graph = client.put(
            f"/api/v1/workflows/{workflow_uuid}/graph",
            json={
                "revision": 1,
                "nodes": [
                    {
                        "uuid": TARGET_NODE_UUID,
                        "name": "valid node",
                        "status": "idle",
                        "type": "compute",
                        "pose": {},
                        "param": {},
                        "execution_policy": {},
                        "meta_data": {},
                    }
                ],
                "edges": [],
            },
        )

    assert workflow.status_code == 201
    assert graph.status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/workflows?page=0&page_size=101",
        "/api/v1/workflow-tasks?page=0&page_size=101",
    ],
    ids=["workflows", "workflow-tasks"],
)
def test_shared_list_pagination_normalizes_page_and_caps_page_size(
    service: WorkflowService,
    path: str,
) -> None:
    with TestClient(create_workflow_app(service)) as client:
        response = client.get(path)

    assert response.status_code == 200
    assert response.json()["data"]["page"] == 1
    assert response.json()["data"]["page_size"] == 100


@pytest.mark.parametrize(
    "query",
    [
        "status=unknown",
        "cleanup_status=unknown",
    ],
    ids=["task-status", "cleanup-status"],
)
def test_task_list_rejects_unknown_status_enums(
    service: WorkflowService,
    query: str,
) -> None:
    with TestClient(create_workflow_app(service)) as client:
        response = client.get(f"/api/v1/workflow-tasks?{query}")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_input"


def test_task_list_accepts_known_status_enums(
    service: WorkflowService,
) -> None:
    _create_workflow(service)
    task = service.create_workflow_task(
        workflow_uuid=WORKFLOW_UUID,
        run_mode="normal",
        target_node_uuid=None,
        input_value={},
        description=None,
        meta_data={},
    )

    with TestClient(create_workflow_app(service)) as client:
        response = client.get(
            "/api/v1/workflow-tasks?status=pending&cleanup_status=none"
        )

    assert response.status_code == 200
    assert [item["uuid"] for item in response.json()["data"]["items"]] == [task["uuid"]]


def test_normal_task_accepts_target_node_uuid(
    service: WorkflowService,
) -> None:
    _create_workflow(service)
    service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[
            _node(SOURCE_NODE_UUID),
            _node(TARGET_NODE_UUID),
        ],
        edges=[],
    )

    task = service.create_workflow_task(
        workflow_uuid=WORKFLOW_UUID,
        run_mode="normal",
        target_node_uuid=TARGET_NODE_UUID,
        input_value={},
        description=None,
        meta_data={},
    )

    assert task["target_node_uuid"] == TARGET_NODE_UUID
    assert task["execution_plan"]["target_node_uuid"] == TARGET_NODE_UUID
    assert [item["uuid"] for item in task["execution_plan"]["nodes"]] == [
        SOURCE_NODE_UUID,
        TARGET_NODE_UUID,
    ]


def _create_task_with_typed_dependency_edge(
    service: WorkflowService,
    store: WorkflowStore,
) -> dict[str, Any]:
    _seed_template_catalog(
        store,
        source_data_source="result",
        source_data_key="measurement",
        target_data_key="wrapped@@@temperature",
    )
    _create_workflow(service)
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
    return service.create_workflow_task(
        workflow_uuid=WORKFLOW_UUID,
        run_mode="normal",
        target_node_uuid=None,
        input_value={},
        description=None,
        meta_data={},
    )


def test_execution_plan_edges_project_frozen_backend_semantics(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    task = _create_task_with_typed_dependency_edge(service, store)

    assert task["execution_plan"]["edges"] == [
        {
            "uuid": EDGE_UUID,
            "source_node_uuid": SOURCE_NODE_UUID,
            "target_node_uuid": TARGET_NODE_UUID,
            "source_handle_uuid": SOURCE_HANDLE_UUID,
            "target_handle_uuid": TARGET_HANDLE_UUID,
            "dependency_only": True,
            "source_data_key": "measurement",
            "target_data_key": "wrapped@@@temperature",
            "source_type": "number",
            "target_type": "number",
        }
    ]


def test_execution_plan_nodes_project_declared_target_inputs(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    task = _create_task_with_typed_dependency_edge(service, store)
    planned_nodes = {node["uuid"]: node for node in task["execution_plan"]["nodes"]}

    assert planned_nodes[TARGET_NODE_UUID]["inputs"] == [
        {
            "handle_uuid": TARGET_HANDLE_UUID,
            "data_key": "temperature",
            "type": "number",
            "required": False,
        }
    ]


def test_execution_plan_orders_ready_nodes_by_create_time_then_uuid(
    service: WorkflowService,
) -> None:
    _create_workflow(service)
    first_graph = service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[
            _node(EARLY_NODE_C_UUID),
            _node(EARLY_NODE_B_UUID),
        ],
        edges=[],
    )
    second_graph = service.save_graph(
        WORKFLOW_UUID,
        revision=2,
        nodes=[
            _node(EARLY_NODE_C_UUID),
            _node(EARLY_NODE_B_UUID),
            _node(LATE_NODE_A_UUID),
        ],
        edges=[],
    )
    first_create_times = {
        node["uuid"]: node["create_time"] for node in first_graph["nodes"]
    }
    second_create_times = {
        node["uuid"]: node["create_time"] for node in second_graph["nodes"]
    }
    assert (
        first_create_times[EARLY_NODE_B_UUID] == (first_create_times[EARLY_NODE_C_UUID])
    )
    assert (
        second_create_times[EARLY_NODE_B_UUID] < (second_create_times[LATE_NODE_A_UUID])
    )

    task = service.create_workflow_task(
        workflow_uuid=WORKFLOW_UUID,
        run_mode="normal",
        target_node_uuid=None,
        input_value={},
        description=None,
        meta_data={},
    )

    assert [node["uuid"] for node in task["execution_plan"]["nodes"]] == [
        EARLY_NODE_B_UUID,
        EARLY_NODE_C_UUID,
        LATE_NODE_A_UUID,
    ]


def _graph_with_null_template_fields(
    service: WorkflowService,
    store: WorkflowStore,
) -> dict[str, Any]:
    _seed_template_catalog(
        store,
        source_data_source=None,
        source_data_key=None,
        target_data_key=None,
    )
    _create_workflow(service)
    service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[
            _node(
                SOURCE_NODE_UUID,
                template_uuid=SOURCE_TEMPLATE_UUID,
            )
        ],
        edges=[],
    )

    with TestClient(create_workflow_app(service)) as client:
        response = client.get(f"/api/v1/workflows/{WORKFLOW_UUID}/graph")

    assert response.status_code == 200
    return response.json()["data"]


def test_graph_node_template_dto_omits_null_optional_fields(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    graph = _graph_with_null_template_fields(service, store)
    template = graph["node_templates"][0]

    assert {
        "description",
        "class",
        "schema",
        "icon",
        "header",
        "footer",
    }.isdisjoint(template)


def test_graph_handle_template_dto_omits_null_optional_fields(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    graph = _graph_with_null_template_fields(service, store)
    handle = graph["handle_templates"][0]

    assert {"description", "data_source", "data_key"}.isdisjoint(handle)
