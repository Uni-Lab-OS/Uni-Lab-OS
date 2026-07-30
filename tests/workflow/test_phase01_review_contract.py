"""Phase 01 评审 blocker 的独立公共合同回归测试。

测试通过 WorkflowService、FastAPI app 和 scheduler router 观察行为。模板目录
仅通过公开的 Store transaction seam 注入测试前置数据；断言不查询 SQLite。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from unilabos.app.scheduler.api import (
    create_app as create_scheduler_app,
)
from unilabos.app.scheduler.api import create_scheduler_router
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.models import (
    CandidateCompilation,
    WorkflowEdgeWrite,
    WorkflowNodeWrite,
)
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NODE_A_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-000000000001"
NODE_B_UUID = "bbbbbbbb-bbbb-4bbb-8bbb-000000000002"
EDGE_UUID = "eeeeeeee-eeee-4eee-8eee-000000000001"

TEMPLATE_A_UUID = "10000000-0000-4000-8000-000000000001"
TEMPLATE_B_UUID = "10000000-0000-4000-8000-000000000002"
MISSING_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000099"
RESOURCE_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000001"

A_SOURCE_NUMBER = "30000000-0000-4000-8000-000000000001"
A_SOURCE_STRING = "30000000-0000-4000-8000-000000000002"
A_TARGET_NUMBER = "30000000-0000-4000-8000-000000000003"
B_SOURCE_NUMBER = "30000000-0000-4000-8000-000000000004"
B_TARGET_NUMBER = "30000000-0000-4000-8000-000000000005"

WORKFLOW_RESERVED = {
    "input_contract": {"version": 1, "parameters": []},
    "output_contract": {"version": 1, "outputs": []},
    "output_bindings": {},
}
NODE_RESERVED = {"input_bindings": {}}


@pytest.fixture()
def store(tmp_path: Path) -> WorkflowStore:
    opened = WorkflowStore(tmp_path / "workflow.db")
    yield opened
    opened.close()


@pytest.fixture()
def service(store: WorkflowStore) -> WorkflowService:
    return WorkflowService(store)


def _create_workflow(
    service: WorkflowService,
    *,
    meta_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return service.create_workflow(
        name="review contract",
        tags=[],
        description=None,
        meta_data=meta_data or {},
        workflow_uuid=WORKFLOW_UUID,
    )


def _node(
    node_uuid: str,
    *,
    template_uuid: str | None = None,
    parent_uuid: str | None = None,
    param: dict[str, Any] | None = None,
    meta_data: dict[str, Any] | None = None,
) -> WorkflowNodeWrite:
    return WorkflowNodeWrite(
        uuid=node_uuid,
        workflow_node_template_uuid=template_uuid,
        parent_uuid=parent_uuid,
        name=node_uuid,
        status="idle",
        type="compute",
        pose={},
        param={} if param is None else param,
        execution_policy={},
        meta_data=meta_data or {},
    )


def _edge(
    *,
    source_handle_uuid: str = A_SOURCE_NUMBER,
    target_handle_uuid: str = B_TARGET_NUMBER,
) -> WorkflowEdgeWrite:
    return WorkflowEdgeWrite(
        uuid=EDGE_UUID,
        source_node_uuid=NODE_A_UUID,
        target_node_uuid=NODE_B_UUID,
        source_handle_uuid=source_handle_uuid,
        target_handle_uuid=target_handle_uuid,
        meta_data={},
    )


def _seed_template_catalog(store: WorkflowStore) -> None:
    """注入冻结 Backend 图校验所需的 authority-scoped 模板快照。"""

    timestamp = "2026-07-30T00:00:00Z"
    target_schema = (
        '{"type":"object","properties":{"value":{"type":"number"}},'
        '"required":["value"]}'
    )
    with store.transaction() as connection:
        for template_uuid, name, schema in (
            (TEMPLATE_A_UUID, "source", None),
            (TEMPLATE_B_UUID, "target", target_schema),
        ):
            connection.execute(
                """
                INSERT INTO workflow_node_template(
                    uuid, create_time, update_time, meta_data, authority_id,
                    resource_template_uuid, name, display_name, goal,
                    goal_default, feedback, result, schema, type, node_type
                ) VALUES (?, ?, ?, '{}', 'os-local', ?, ?, ?, '{}', '{}',
                          '{}', '{}', ?, 'action', 'compute')
                """,
                (
                    template_uuid,
                    timestamp,
                    timestamp,
                    RESOURCE_TEMPLATE_UUID,
                    name,
                    name,
                    schema,
                ),
            )
        handles = (
            (
                A_SOURCE_NUMBER,
                TEMPLATE_A_UUID,
                "result",
                "source",
                "number",
                0,
                "result",
            ),
            (
                A_SOURCE_STRING,
                TEMPLATE_A_UUID,
                "text",
                "source",
                "string",
                0,
                "text",
            ),
            (
                A_TARGET_NUMBER,
                TEMPLATE_A_UUID,
                "optional",
                "target",
                "number",
                0,
                "optional",
            ),
            (
                B_SOURCE_NUMBER,
                TEMPLATE_B_UUID,
                "result",
                "source",
                "number",
                0,
                "result",
            ),
            (
                B_TARGET_NUMBER,
                TEMPLATE_B_UUID,
                "value",
                "target",
                "number",
                1,
                "value",
            ),
        )
        for (
            handle_uuid,
            template_uuid,
            key,
            io_type,
            value_type,
            required,
            data_key,
        ) in handles:
            connection.execute(
                """
                INSERT INTO workflow_handle_template(
                    uuid, create_time, update_time, meta_data, authority_id,
                    workflow_node_template_uuid, handle_key, io_type,
                    display_name, type, required, data_key
                ) VALUES (?, ?, ?, '{}', 'os-local', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    handle_uuid,
                    timestamp,
                    timestamp,
                    template_uuid,
                    key,
                    io_type,
                    key,
                    value_type,
                    required,
                    data_key,
                ),
            )


def test_service_normalizes_uppercase_workflow_uuid(
    service: WorkflowService,
) -> None:
    _create_workflow(service)
    service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[_node(NODE_A_UUID)],
        edges=[],
    )

    uppercase_uuid = WORKFLOW_UUID.upper()
    assert service.get_graph(uppercase_uuid)["workflow"]["uuid"] == WORKFLOW_UUID
    updated = service.update_workflow(
        uppercase_uuid,
        name="canonical update",
        tags=[],
        description=None,
        meta_data={},
    )
    assert updated["uuid"] == WORKFLOW_UUID
    task = service.create_workflow_task(
        workflow_uuid=uppercase_uuid,
        run_mode="normal",
        target_node_uuid=None,
        input_value={},
        description=None,
        meta_data={},
    )
    assert task["workflow_uuid"] == WORKFLOW_UUID


def test_service_normalizes_uppercase_task_and_job_uuids(
    service: WorkflowService,
) -> None:
    _create_workflow(service)
    service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[_node(NODE_A_UUID)],
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

    assert service.get_workflow_task(task["uuid"].upper())["uuid"] == task["uuid"]
    assert service.list_workflow_node_jobs(task["uuid"].upper())[0]["uuid"] == (
        job["uuid"]
    )
    assert service.get_workflow_node_job(job["uuid"].upper())["uuid"] == job["uuid"]


def test_api_normalizes_uppercase_uuid_before_service_lookup(
    service: WorkflowService,
) -> None:
    _create_workflow(service)
    service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[_node(NODE_A_UUID)],
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
    with TestClient(
        create_workflow_app(service),
        raise_server_exceptions=False,
    ) as client:
        graph = client.get(
            f"/api/v1/workflows/{WORKFLOW_UUID.upper()}/graph"
        )
        detail = client.get(
            f"/api/v1/workflow-tasks/{task['uuid'].upper()}"
        )

    assert graph.status_code == 200
    assert graph.json()["data"]["workflow"]["uuid"] == WORKFLOW_UUID
    assert detail.status_code == 200
    assert detail.json()["data"]["uuid"] == task["uuid"]


def test_ordinary_workflow_write_preserves_reserved_unilab_metadata(
    store: WorkflowStore,
) -> None:
    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="seeded",
        tags=[],
        description=None,
        meta_data={"unilab": WORKFLOW_RESERVED, "color": "red"},
    )
    service = WorkflowService(store)

    updated = service.update_workflow(
        WORKFLOW_UUID,
        name="presentation update",
        tags=["ui"],
        description="ordinary metadata",
        meta_data={
            "unilab": {"input_contract": "caller overwrite"},
            "color": "blue",
        },
    )

    assert updated["meta_data"]["unilab"] == WORKFLOW_RESERVED
    assert updated["meta_data"]["color"] == "blue"


def test_ordinary_graph_write_preserves_node_reserved_unilab_metadata(
    store: WorkflowStore,
) -> None:
    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="seeded",
        tags=[],
        description=None,
        meta_data={},
    )
    store.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[
            _node(
                NODE_B_UUID,
                meta_data={"unilab": NODE_RESERVED, "color": "red"},
            )
        ],
        edges=[],
    )
    service = WorkflowService(store)

    graph = service.save_graph(
        WORKFLOW_UUID,
        revision=2,
        nodes=[
            _node(
                NODE_B_UUID,
                meta_data={
                    "unilab": {"input_bindings": "caller overwrite"},
                    "color": "blue",
                },
            )
        ],
        edges=[],
    )

    assert graph["nodes"][0]["meta_data"]["unilab"] == NODE_RESERVED
    assert graph["nodes"][0]["meta_data"]["color"] == "blue"


class ReservedMetadataCompiler:
    compiler_version = "review-contract-v1"
    template_catalog_fingerprint = f"sha256:{'c' * 64}"

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
        graph = {
            "workflow": {
                **applied_graph["workflow"],
                "meta_data": {
                    **applied_graph["workflow"]["meta_data"],
                    "unilab": WORKFLOW_RESERVED,
                },
            },
            "nodes": [
                _node(
                    NODE_B_UUID,
                    meta_data={"unilab": NODE_RESERVED},
                ).model_dump()
            ],
            "edges": [],
            "node_templates": [],
            "handle_templates": [],
        }
        return CandidateCompilation(
            diagnostics=[],
            graph=graph,
            normalized_python_source=(
                python_source
                if python_source.endswith("\n")
                else f"{python_source}\n"
            ),
            source_map=[],
            changeset={
                "kind": "graph",
                "created_node_uuids": [NODE_B_UUID],
                "updated_node_uuids": [],
                "deleted_node_uuids": [],
                "created_edge_uuids": [],
                "updated_edge_uuids": [],
                "deleted_edge_uuids": [],
                "reserved_metadata_changed": True,
            },
            compiler_version=self.compiler_version,
            template_catalog_fingerprint=self.template_catalog_fingerprint,
        )


def test_authoring_apply_commits_candidate_reserved_metadata_with_graph(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    package_root.mkdir()
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store, compiler=ReservedMetadataCompiler())
    try:
        _create_workflow(service, meta_data={"color": "blue"})
        service.register_editable_source(
            workflow_uuid=WORKFLOW_UUID,
            package_id="review_package",
            package_root=package_root,
            relative_path="workflows/reserved.py",
        )
        saved = service.save_draft(
            WORKFLOW_UUID,
            python_source="build()",
            expected_draft_hash=None,
            expected_workflow_revision=1,
        )
        applied = service.apply_authoring(
            WORKFLOW_UUID,
            expected_draft_hash=saved["draft"]["draft_hash"],
            expected_workflow_revision=1,
            expected_candidate_hash=saved["candidate"]["candidate_hash"],
        )
    finally:
        store.close()

    graph = applied["authoring"]["applied_graph"]
    assert applied["apply_result"]["workflow_revision"] == 2
    assert graph["workflow"]["revision"] == 2
    assert graph["workflow"]["meta_data"]["unilab"] == WORKFLOW_RESERVED
    assert graph["nodes"][0]["meta_data"]["unilab"] == NODE_RESERVED


def _invalid_graph(
    case: str,
) -> tuple[list[WorkflowNodeWrite], list[WorkflowEdgeWrite], str]:
    if case == "missing_template":
        return (
            [_node(NODE_A_UUID, template_uuid=MISSING_TEMPLATE_UUID)],
            [],
            "not_found",
        )
    if case == "parent_cycle":
        return (
            [
                _node(NODE_A_UUID, parent_uuid=NODE_B_UUID),
                _node(NODE_B_UUID, parent_uuid=NODE_A_UUID),
            ],
            [],
            "invalid_input",
        )

    source = _node(NODE_A_UUID, template_uuid=TEMPLATE_A_UUID)
    target = _node(
        NODE_B_UUID,
        template_uuid=TEMPLATE_B_UUID,
        param={} if case in {"incompatible", "missing_required"} else {"value": 1},
    )
    if case == "source_handle_owner":
        return [source, target], [_edge(source_handle_uuid=B_SOURCE_NUMBER)], "invalid_input"
    if case == "source_handle_direction":
        return [source, target], [_edge(source_handle_uuid=A_TARGET_NUMBER)], "invalid_input"
    if case == "target_handle_owner":
        return [source, target], [_edge(target_handle_uuid=A_TARGET_NUMBER)], "invalid_input"
    if case == "target_handle_direction":
        return [source, target], [_edge(target_handle_uuid=B_SOURCE_NUMBER)], "invalid_input"
    if case == "incompatible":
        return [source, target], [_edge(source_handle_uuid=A_SOURCE_STRING)], "invalid_input"
    if case == "missing_required":
        return [target], [], "invalid_input"
    if case == "invalid_param":
        target = _node(
            NODE_B_UUID,
            template_uuid=TEMPLATE_B_UUID,
            param={"value": "hot"},
        )
        return [target], [], "invalid_input"
    raise AssertionError(f"unknown graph test case {case}")


@pytest.mark.parametrize(
    "case",
    [
        "missing_template",
        "parent_cycle",
        "source_handle_owner",
        "source_handle_direction",
        "target_handle_owner",
        "target_handle_direction",
        "incompatible",
        "missing_required",
        "invalid_param",
    ],
)
def test_full_graph_put_rejects_frozen_backend_semantic_violations(
    service: WorkflowService,
    store: WorkflowStore,
    case: str,
) -> None:
    _seed_template_catalog(store)
    _create_workflow(service)
    nodes, edges, expected_code = _invalid_graph(case)

    with pytest.raises(WorkflowError) as failure:
        service.save_graph(
            WORKFLOW_UUID,
            revision=1,
            nodes=nodes,
            edges=edges,
        )

    assert failure.value.code == expected_code
    unchanged = service.get_graph(WORKFLOW_UUID)
    assert unchanged["workflow"]["revision"] == 1
    assert unchanged["nodes"] == []
    assert unchanged["edges"] == []


def test_full_graph_put_accepts_required_input_from_edge_or_static_param(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    _seed_template_catalog(store)
    _create_workflow(service)

    supplied_by_edge = service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[
            _node(NODE_A_UUID, template_uuid=TEMPLATE_A_UUID),
            _node(NODE_B_UUID, template_uuid=TEMPLATE_B_UUID, param={}),
        ],
        edges=[_edge()],
    )
    assert supplied_by_edge["workflow"]["revision"] == 2

    supplied_by_param = service.save_graph(
        WORKFLOW_UUID,
        revision=2,
        nodes=[
            _node(
                NODE_B_UUID,
                template_uuid=TEMPLATE_B_UUID,
                param={"value": 21},
            )
        ],
        edges=[],
    )
    assert supplied_by_param["workflow"]["revision"] == 3


def test_independent_scheduler_defaults_hide_execution_shaped_workflows() -> None:
    router_paths = {route.path for route in create_scheduler_router(lambda: None).routes}
    app_paths = set(create_scheduler_app().openapi()["paths"])

    for paths in (router_paths, app_paths):
        assert "/api/v1/workflows" not in paths
        assert "/api/v1/workflows/{workflow_id}" not in paths
        assert "/api/v1/workflows/{workflow_id}/cancel" not in paths
        assert "/api/v1/health" in paths


@pytest.mark.parametrize(
    "relative_path",
    [
        "scripts/demo.py",
        "workflows/demo.txt",
        "workflows/nested/demo.py",
    ],
)
def test_authoring_registration_rejects_non_workflow_python_paths(
    service: WorkflowService,
    tmp_path: Path,
    relative_path: str,
) -> None:
    _create_workflow(service)
    package_root = tmp_path / "registered_package"
    package_root.mkdir()
    (package_root / "package.yaml").write_text(
        "name: registered_package\n",
        encoding="utf-8",
    )

    with pytest.raises(WorkflowError) as failure:
        service.register_editable_source(
            workflow_uuid=WORKFLOW_UUID,
            package_id="registered_package",
            package_root=package_root,
            relative_path=relative_path,
        )

    assert failure.value.code == "invalid_input"


def test_authoring_registration_accepts_package_workflow_python_path(
    service: WorkflowService,
    tmp_path: Path,
) -> None:
    _create_workflow(service)
    package_root = tmp_path / "registered_package"
    package_root.mkdir()

    registration = service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="registered_package",
        package_root=package_root,
        relative_path="workflows/demo.py",
    )

    assert registration["source_uri"] == (
        "package://registered_package/workflows/demo.py"
    )
