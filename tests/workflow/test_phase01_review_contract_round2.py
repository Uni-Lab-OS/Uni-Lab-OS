"""Phase 01 第二轮评审发现的公共合同回归测试。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.models import (
    CandidateCompilation,
    WorkflowEdgeWrite,
    WorkflowNodeWrite,
)
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SOURCE_NODE_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-000000000001"
TARGET_NODE_UUID = "bbbbbbbb-bbbb-4bbb-8bbb-000000000002"
EDGE_UUID = "eeeeeeee-eeee-4eee-8eee-000000000001"

SOURCE_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000001"
TARGET_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000002"
RESOURCE_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000001"
SOURCE_HANDLE_UUID = "30000000-0000-4000-8000-000000000001"
TARGET_HANDLE_UUID = "30000000-0000-4000-8000-000000000002"

CATALOG_FINGERPRINT = f"sha256:{'d' * 64}"


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


def _create_workflow(
    service: WorkflowService,
    *,
    meta_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return service.create_workflow(
        name="phase 01 review round 2",
        tags=[],
        description=None,
        meta_data=meta_data or {},
        workflow_uuid=WORKFLOW_UUID,
    )


def _node(
    node_uuid: str,
    *,
    template_uuid: str,
    param: dict[str, Any] | None = None,
    meta_data: dict[str, Any] | None = None,
    node_type: str = "compute",
) -> WorkflowNodeWrite:
    return WorkflowNodeWrite(
        uuid=node_uuid,
        workflow_node_template_uuid=template_uuid,
        name=node_uuid,
        status="idle",
        type=node_type,
        pose={},
        param={} if param is None else param,
        execution_policy={},
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


def _seed_template(
    store: WorkflowStore,
    *,
    template_uuid: str,
    name: str,
    schema: dict[str, Any] | None = None,
    node_type: str = "compute",
) -> None:
    timestamp = "2026-07-31T00:00:00Z"
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workflow_node_template(
                uuid, create_time, update_time, meta_data, authority_id,
                resource_template_uuid, name, display_name, goal,
                goal_default, feedback, result, schema, type, node_type
            ) VALUES (?, ?, ?, '{}', 'os-local', ?, ?, ?, '{}', '{}',
                      '{}', '{}', ?, 'action', ?)
            """,
            (
                template_uuid,
                timestamp,
                timestamp,
                RESOURCE_TEMPLATE_UUID,
                name,
                name,
                None if schema is None else json.dumps(schema),
                node_type,
            ),
        )


def _seed_handle(
    store: WorkflowStore,
    *,
    handle_uuid: str,
    template_uuid: str,
    handle_key: str,
    io_type: str,
    value_type: str,
    required: bool,
    data_key: str,
) -> None:
    timestamp = "2026-07-31T00:00:00Z"
    with store.transaction() as connection:
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
                handle_key,
                io_type,
                handle_key,
                value_type,
                int(required),
                data_key,
            ),
        )


def _seed_edge_catalog(
    store: WorkflowStore,
    *,
    target_required: bool = True,
    handle_type: str = "number",
) -> None:
    _seed_template(
        store,
        template_uuid=SOURCE_TEMPLATE_UUID,
        name="source",
    )
    _seed_template(
        store,
        template_uuid=TARGET_TEMPLATE_UUID,
        name="target",
    )
    _seed_handle(
        store,
        handle_uuid=SOURCE_HANDLE_UUID,
        template_uuid=SOURCE_TEMPLATE_UUID,
        handle_key="result",
        io_type="source",
        value_type=handle_type,
        required=False,
        data_key="result",
    )
    _seed_handle(
        store,
        handle_uuid=TARGET_HANDLE_UUID,
        template_uuid=TARGET_TEMPLATE_UUID,
        handle_key="value",
        io_type="target",
        value_type=handle_type,
        required=target_required,
        data_key="value",
    )


def _binding(parameter: str) -> dict[str, Any]:
    return {
        "unilab": {
            "input_bindings": {
                TARGET_HANDLE_UUID: {
                    "parameter": parameter,
                }
            }
        }
    }


def _input_contract(parameter_names: list[str]) -> dict[str, Any]:
    return {
        "unilab": {
            "input_contract": {
                "version": 1,
                "parameters": [
                    {
                        "name": name,
                        "schema": {"type": "number"},
                    }
                    for name in parameter_names
                ],
            }
        }
    }


class GraphCompiler:
    compiler_version = "phase-01-review-round-2"
    template_catalog_fingerprint = CATALOG_FINGERPRINT

    def __init__(
        self,
        *,
        workflow_meta_data: dict[str, Any],
        nodes: list[WorkflowNodeWrite],
        edges: list[WorkflowEdgeWrite],
        normalized_source: str = "build()\n",
    ) -> None:
        self.workflow_meta_data = workflow_meta_data
        self.nodes = nodes
        self.edges = edges
        self.normalized_source = normalized_source

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> CandidateCompilation:
        del python_source, source_uri
        workflow_meta_data = deepcopy(applied_graph["workflow"]["meta_data"])
        workflow_meta_data.pop("unilab", None)
        if "unilab" in self.workflow_meta_data:
            workflow_meta_data["unilab"] = deepcopy(self.workflow_meta_data["unilab"])
        graph = {
            "workflow": {
                **applied_graph["workflow"],
                "uuid": workflow_uuid,
                "revision": workflow_revision,
                "meta_data": workflow_meta_data,
            },
            "nodes": [node.model_dump() for node in self.nodes],
            "edges": [edge.model_dump() for edge in self.edges],
            "node_templates": deepcopy(applied_graph["node_templates"]),
            "handle_templates": deepcopy(applied_graph["handle_templates"]),
        }
        candidate_nodes = {node.uuid: node.model_dump() for node in self.nodes}
        applied_nodes = {
            node["uuid"]: WorkflowNodeWrite.model_validate(node).model_dump()
            for node in applied_graph["nodes"]
        }
        candidate_edges = {edge.uuid: edge.model_dump() for edge in self.edges}
        applied_edges = {
            edge["uuid"]: WorkflowEdgeWrite.model_validate(edge).model_dump()
            for edge in applied_graph["edges"]
        }
        created_node_uuids = sorted(set(candidate_nodes) - set(applied_nodes))
        updated_node_uuids = sorted(
            uuid
            for uuid in set(candidate_nodes) & set(applied_nodes)
            if candidate_nodes[uuid] != applied_nodes[uuid]
        )
        deleted_node_uuids = sorted(set(applied_nodes) - set(candidate_nodes))
        created_edge_uuids = sorted(set(candidate_edges) - set(applied_edges))
        updated_edge_uuids = sorted(
            uuid
            for uuid in set(candidate_edges) & set(applied_edges)
            if candidate_edges[uuid] != applied_edges[uuid]
        )
        deleted_edge_uuids = sorted(set(applied_edges) - set(candidate_edges))
        reserved_metadata_changed = workflow_meta_data.get("unilab") != applied_graph[
            "workflow"
        ]["meta_data"].get("unilab")
        return CandidateCompilation(
            diagnostics=[],
            graph=graph,
            normalized_python_source=self.normalized_source,
            source_map=[],
            changeset={
                "kind": "graph",
                "created_node_uuids": created_node_uuids,
                "updated_node_uuids": updated_node_uuids,
                "deleted_node_uuids": deleted_node_uuids,
                "created_edge_uuids": created_edge_uuids,
                "updated_edge_uuids": updated_edge_uuids,
                "deleted_edge_uuids": deleted_edge_uuids,
                "reserved_metadata_changed": reserved_metadata_changed,
            },
            compiler_version=self.compiler_version,
            template_catalog_fingerprint=self.template_catalog_fingerprint,
        )


class SourceOnlyCompiler:
    compiler_version = "phase-01-review-round-2"
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
        normalized = (
            python_source if python_source.endswith("\n") else f"{python_source}\n"
        )
        return CandidateCompilation(
            diagnostics=[],
            graph=applied_graph,
            normalized_python_source=normalized,
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


def _save_compiler_draft(
    tmp_path: Path,
    *,
    store: WorkflowStore,
    compiler: GraphCompiler | SourceOnlyCompiler,
    initial_meta_data: dict[str, Any] | None = None,
) -> tuple[WorkflowService, dict[str, Any]]:
    service = WorkflowService(store, compiler=compiler)
    if initial_meta_data is None:
        _create_workflow(service)
    else:
        store.create_workflow(
            workflow_uuid=WORKFLOW_UUID,
            name="phase 01 review round 2",
            tags=[],
            description=None,
            meta_data=initial_meta_data,
        )
    revision = 1
    if isinstance(compiler, GraphCompiler) and compiler.nodes:
        service.save_graph(
            WORKFLOW_UUID,
            revision=revision,
            nodes=[
                node.model_copy(
                    update={
                        "param": (
                            {"value": 0}
                            if node.workflow_node_template_uuid == TARGET_TEMPLATE_UUID
                            else {}
                        ),
                        "meta_data": {},
                    }
                )
                for node in compiler.nodes
            ],
            edges=[],
        )
        revision = 2
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase_01_round_2",
        package_root=package_root,
        relative_path="workflows/review.py",
    )
    aggregate = service.save_draft(
        WORKFLOW_UUID,
        python_source="build()",
        expected_draft_hash=None,
        expected_workflow_revision=revision,
    )
    return service, aggregate


def _apply_compiler_candidate(
    tmp_path: Path,
    *,
    store: WorkflowStore,
    compiler: GraphCompiler | SourceOnlyCompiler,
    initial_meta_data: dict[str, Any] | None = None,
) -> tuple[WorkflowService, dict[str, Any]]:
    service, aggregate = _save_compiler_draft(
        tmp_path,
        store=store,
        compiler=compiler,
        initial_meta_data=initial_meta_data,
    )
    candidate = aggregate["candidate"]
    assert candidate is not None
    if aggregate["draft"]["python_source"] != candidate["normalized_python_source"]:
        aggregate = service.save_draft(
            WORKFLOW_UUID,
            python_source=candidate["normalized_python_source"],
            expected_draft_hash=aggregate["draft"]["draft_hash"],
            expected_workflow_revision=aggregate["workflow_revision"],
        )
        candidate = aggregate["candidate"]
        assert candidate is not None
    applied = service.apply_authoring(
        WORKFLOW_UUID,
        candidate_hash=candidate["candidate_hash"],
    )
    return service, applied


def test_required_handle_accepts_one_workflow_input_binding(
    store: WorkflowStore,
    tmp_path: Path,
) -> None:
    _seed_edge_catalog(store)
    compiler = GraphCompiler(
        workflow_meta_data=_input_contract(["temperature"]),
        nodes=[
            _node(
                TARGET_NODE_UUID,
                template_uuid=TARGET_TEMPLATE_UUID,
                meta_data=_binding("temperature"),
            )
        ],
        edges=[],
    )

    _, applied = _apply_compiler_candidate(
        tmp_path,
        store=store,
        compiler=compiler,
    )

    assert applied["apply_result"]["workflow_revision"] == 3
    bindings = applied["authoring"]["applied_graph"]["nodes"][0]["meta_data"]
    assert bindings == _binding("temperature")


@pytest.mark.parametrize(
    ("providers", "with_binding"),
    [
        ({"param", "edge"}, False),
        ({"edge"}, True),
        ({"param"}, True),
    ],
    ids=["param-plus-edge", "binding-plus-edge", "binding-plus-param"],
)
def test_same_target_handle_rejects_ambiguous_providers(
    store: WorkflowStore,
    tmp_path: Path,
    providers: set[str],
    with_binding: bool,
) -> None:
    _seed_edge_catalog(store)
    compiler = GraphCompiler(
        workflow_meta_data=(_input_contract(["temperature"]) if with_binding else {}),
        nodes=[
            *(
                [
                    _node(
                        SOURCE_NODE_UUID,
                        template_uuid=SOURCE_TEMPLATE_UUID,
                    )
                ]
                if "edge" in providers
                else []
            ),
            _node(
                TARGET_NODE_UUID,
                template_uuid=TARGET_TEMPLATE_UUID,
                param={"value": 21} if "param" in providers else {},
                meta_data=(_binding("temperature") if with_binding else {}),
            ),
        ],
        edges=[_edge()] if "edge" in providers else [],
    )

    _, aggregate = _save_compiler_draft(
        tmp_path,
        store=store,
        compiler=compiler,
    )

    assert aggregate["state"] == "draft_invalid"
    assert aggregate["candidate"] is None
    assert aggregate["draft"]["diagnostics"][0]["code"] == "candidate_invalid"


@pytest.mark.parametrize(
    "contract_parameters",
    [[], ["temperature", "temperature"]],
    ids=["missing-parameter", "duplicate-parameter"],
)
def test_input_binding_references_exactly_one_contract_parameter(
    store: WorkflowStore,
    tmp_path: Path,
    contract_parameters: list[str],
) -> None:
    _seed_edge_catalog(store, target_required=False)
    compiler = GraphCompiler(
        workflow_meta_data=_input_contract(contract_parameters),
        nodes=[
            _node(
                TARGET_NODE_UUID,
                template_uuid=TARGET_TEMPLATE_UUID,
                meta_data=_binding("temperature"),
            )
        ],
        edges=[],
    )

    _, aggregate = _save_compiler_draft(
        tmp_path,
        store=store,
        compiler=compiler,
    )

    assert aggregate["state"] == "draft_invalid"
    assert aggregate["candidate"] is None
    assert aggregate["draft"]["diagnostics"][0]["code"] == "candidate_invalid"


@pytest.mark.parametrize(
    "param",
    [
        {"first": 1},
        {"first": 1, "second": 2, "third": 3},
    ],
    ids=["below-minProperties", "above-maxProperties"],
)
def test_graph_param_schema_enforces_property_count_bounds(
    service: WorkflowService,
    store: WorkflowStore,
    param: dict[str, int],
) -> None:
    _seed_template(
        store,
        template_uuid=TARGET_TEMPLATE_UUID,
        name="bounded object",
        schema={
            "type": "object",
            "minProperties": 2,
            "maxProperties": 2,
        },
    )
    _create_workflow(service)

    with pytest.raises(WorkflowError) as failure:
        service.save_graph(
            WORKFLOW_UUID,
            revision=1,
            nodes=[
                _node(
                    TARGET_NODE_UUID,
                    template_uuid=TARGET_TEMPLATE_UUID,
                    param=param,
                )
            ],
            edges=[],
        )

    assert failure.value.code == "invalid_input"


def test_graph_param_schema_accepts_property_count_at_bounds(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    _seed_template(
        store,
        template_uuid=TARGET_TEMPLATE_UUID,
        name="bounded object",
        schema={
            "type": "object",
            "minProperties": 2,
            "maxProperties": 2,
        },
    )
    _create_workflow(service)

    graph = service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[
            _node(
                TARGET_NODE_UUID,
                template_uuid=TARGET_TEMPLATE_UUID,
                param={"first": 1, "second": 2},
            )
        ],
        edges=[],
    )

    assert graph["workflow"]["revision"] == 2


def test_unknown_handle_type_keeps_backend_tolerant_value_semantics(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    _seed_edge_catalog(store, handle_type="liquid")
    _create_workflow(service)

    graph = service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[
            _node(
                TARGET_NODE_UUID,
                template_uuid=TARGET_TEMPLATE_UUID,
                param={"value": {"chemical": "water", "volume_ml": 5}},
            )
        ],
        edges=[],
    )

    assert graph["nodes"][0]["param"]["value"]["chemical"] == "water"


def test_task_plan_and_job_use_template_node_type(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    _seed_template(
        store,
        template_uuid=TARGET_TEMPLATE_UUID,
        name="approval",
        node_type="manual_confirm",
    )
    _create_workflow(service)
    service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[
            _node(
                TARGET_NODE_UUID,
                template_uuid=TARGET_TEMPLATE_UUID,
                node_type="compute",
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
    jobs = service.list_workflow_node_jobs(task["uuid"])

    assert task["execution_plan"]["nodes"][0]["kind"] == "manual_confirm"
    assert jobs[0]["executor_kind"] == "manual_confirm"


def test_apply_event_tokens_match_normalized_authoring_aggregate(
    store: WorkflowStore,
    tmp_path: Path,
) -> None:
    service, applied = _apply_compiler_candidate(
        tmp_path,
        store=store,
        compiler=SourceOnlyCompiler(),
    )

    event = service.list_events(after_id=0)["items"][-1]
    aggregate = applied["authoring"]
    event_tokens = (
        event["data"]["workflow_revision"],
        event["data"]["draft_hash"],
        event["data"]["candidate_hash"],
    )
    aggregate_tokens = (
        aggregate["workflow_revision"],
        aggregate["draft"]["draft_hash"],
        (
            aggregate["candidate"]["candidate_hash"]
            if aggregate["candidate"] is not None
            else None
        ),
    )

    assert event["data"]["cause"] == "applied"
    assert event_tokens == aggregate_tokens


def test_graph_candidate_can_delete_workflow_unilab_metadata_completely(
    store: WorkflowStore,
    tmp_path: Path,
) -> None:
    compiler = GraphCompiler(
        workflow_meta_data={"color": "blue"},
        nodes=[],
        edges=[],
    )

    _, applied = _apply_compiler_candidate(
        tmp_path,
        store=store,
        compiler=compiler,
        initial_meta_data={
            "color": "red",
            "unilab": _input_contract(["temperature"])["unilab"],
        },
    )

    workflow_meta = applied["authoring"]["applied_graph"]["workflow"]["meta_data"]
    assert workflow_meta == {"color": "red"}
