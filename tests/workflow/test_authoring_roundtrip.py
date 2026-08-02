"""Round 02D 顺序、group、parallel 与双向 proof round-trip 合同。"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.models import CandidateChangeset, CandidateSourceMapEntry

from .test_authoring_engine import (
    ANALYZE_NODE_UUID,
    AUTHORITY,
    FINAL_NODE_UUID,
    FINAL_READY_TARGET,
    FINAL_REPORT_SOURCE,
    FINAL_REPORT_TARGET,
    GROUP_A_NODE_UUID,
    GROUP_B_NODE_UUID,
    GROUP_TEMPLATE_UUID,
    PREPARE_NODE_UUID,
    PREPARE_READY_SOURCE,
    PREPARE_READY_TARGET,
    PREPARE_REPORT_SOURCE,
    RESOURCE_TEMPLATE_UUID,
    WORKFLOW_UUID,
    EngineContext,
    _assert_error_result,
    _compile,
    _empty_graph,
    _node_by_uuid,
    _opened_engine,
)

RESOURCE_TEMPLATE_SOURCE_IDENTITY = "lab.resources:plate_96"


class _StaticResourceTemplateIdentityIndex:
    def resolve_symbol(self, qualified_name: str) -> str:
        if qualified_name != RESOURCE_TEMPLATE_SOURCE_IDENTITY:
            raise KeyError(qualified_name)
        return RESOURCE_TEMPLATE_UUID

    def identify_uuid(self, resource_template_uuid: str) -> str:
        if resource_template_uuid != RESOURCE_TEMPLATE_UUID:
            raise KeyError(resource_template_uuid)
        return RESOURCE_TEMPLATE_SOURCE_IDENTITY


@pytest.fixture()
def engine_context(tmp_path: Path) -> Iterator[EngineContext]:
    with _opened_engine(tmp_path / "workflow.db") as context:
        yield context


def _sequential_source() -> str:
    return f'''from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, workflow_definition, workflow_output


reactor: Reactor = device()


@workflow_definition(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="Sequential preparation",
)
def sequential(*, sample: ResourceSlot):
    # unilab:node_uuid={PREPARE_NODE_UUID}
    first = reactor.prepare(sample=sample, cycles=1, note=None)
    # unilab:node_uuid={ANALYZE_NODE_UUID}
    second = reactor.prepare(sample=sample, cycles=2, note=None)
    return workflow_output(report=second.report)
'''


def _group_source() -> str:
    return f'''from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import (
    device,
    group,
    workflow_definition,
    workflow_output,
)


reactor: Reactor = device()


@workflow_definition(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="Grouped preparation",
)
def grouped(*, sample: ResourceSlot):
    # unilab:node_uuid={GROUP_A_NODE_UUID}
    with group(name="Preparation"):
        # unilab:node_uuid={PREPARE_NODE_UUID}
        prepared = reactor.prepare(sample=sample, cycles=1, note=None)
        # unilab:node_uuid={ANALYZE_NODE_UUID}
        analyzed = reactor.analyze(prepared=prepared.prepared, label="grouped")
    return workflow_output(report=analyzed.report)
'''


def _parallel_source() -> str:
    return f'''from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import (
    device,
    group,
    parallel,
    workflow_definition,
    workflow_output,
)


reactor: Reactor = device()


@workflow_definition(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="Parallel preparation",
)
def parallel_preparation(*, sample: ResourceSlot):
    with parallel():
        # unilab:node_uuid={GROUP_A_NODE_UUID}
        with group(name="Branch A"):
            # unilab:node_uuid={PREPARE_NODE_UUID}
            branch_a = reactor.prepare(sample=sample, cycles=1, note=None)
        # unilab:node_uuid={GROUP_B_NODE_UUID}
        with group(name="Branch B"):
            # unilab:node_uuid={ANALYZE_NODE_UUID}
            branch_b = reactor.prepare(sample=sample, cycles=2, note=None)
    # unilab:node_uuid={FINAL_NODE_UUID}
    final = reactor.finalize(report=branch_a.report)
    return workflow_output(report=final.report)
'''


def _sequential_groups_source() -> str:
    return f'''from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import (
    device,
    group,
    workflow_definition,
    workflow_output,
)


reactor: Reactor = device()


@workflow_definition(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="Sequential groups",
)
def sequential_groups(*, sample: ResourceSlot):
    # unilab:node_uuid={GROUP_A_NODE_UUID}
    with group(name="Preparation"):
        # unilab:node_uuid={PREPARE_NODE_UUID}
        prepared = reactor.prepare(sample=sample, cycles=1, note=None)
    # unilab:node_uuid={GROUP_B_NODE_UUID}
    with group(name="Analysis"):
        # unilab:node_uuid={ANALYZE_NODE_UUID}
        analyzed = reactor.analyze(prepared=prepared.prepared, label="sequential")
    return workflow_output(report=analyzed.report)
'''


def _workflow_input_output_source() -> str:
    return f'''from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import workflow_definition, workflow_output


@workflow_definition(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="Pass through",
)
def pass_through(*, sample: ResourceSlot):
    return workflow_output(sample=sample)
'''


def _constrained_workflow_input_source(
    annotation: str,
    *,
    default: str = "",
) -> str:
    return f'''from typing import Annotated

from lab.resources import plate_96
from unilabos.registry.annotations import AllowedResourceTemplates
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import workflow_definition, workflow_output


@workflow_definition(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="Constrained pass through",
)
def constrained_pass_through(
    *,
    sample: {annotation}{default},
):
    return workflow_output(sample=sample)
'''


def _semantic_graph(graph: dict[str, Any]) -> dict[str, Any]:
    """只比较冻结的 write semantics，不把投影时间当业务含义。"""

    workflow = deepcopy(graph["workflow"])
    workflow.pop("create_time", None)
    workflow.pop("update_time", None)
    nodes = []
    for raw in graph["nodes"]:
        node = deepcopy(raw)
        node.pop("create_time", None)
        node.pop("update_time", None)
        nodes.append(node)
    edges = []
    for raw in graph["edges"]:
        edge = deepcopy(raw)
        edge.pop("create_time", None)
        edge.pop("update_time", None)
        edges.append(edge)
    return {
        "workflow": workflow,
        "nodes": sorted(nodes, key=lambda item: item["uuid"]),
        "edges": sorted(edges, key=lambda item: item["uuid"]),
        "node_template_uuids": sorted(item["uuid"] for item in graph["node_templates"]),
        "handle_template_uuids": sorted(
            item["uuid"] for item in graph["handle_templates"]
        ),
    }


def test_source_order_uses_real_ready_handles_without_synthetic_nodes(
    engine_context: EngineContext,
) -> None:
    result = _compile(engine_context.engine, _sequential_source())

    assert result.valid and result.graph is not None
    assert {node["uuid"] for node in result.graph["nodes"]} == {
        PREPARE_NODE_UUID,
        ANALYZE_NODE_UUID,
    }
    assert len(result.graph["edges"]) == 1
    edge = result.graph["edges"][0]
    assert {
        "source_node_uuid": edge["source_node_uuid"],
        "target_node_uuid": edge["target_node_uuid"],
        "source_handle_uuid": edge["source_handle_uuid"],
        "target_handle_uuid": edge["target_handle_uuid"],
    } == {
        "source_node_uuid": PREPARE_NODE_UUID,
        "target_node_uuid": ANALYZE_NODE_UUID,
        "source_handle_uuid": PREPARE_READY_SOURCE,
        "target_handle_uuid": PREPARE_READY_TARGET,
    }


def test_data_edge_suppresses_redundant_source_order_dependency(
    engine_context: EngineContext,
) -> None:
    result = _compile(engine_context.engine, _group_source())

    assert result.valid and result.graph is not None
    action_edges = [
        edge
        for edge in result.graph["edges"]
        if edge["source_node_uuid"] == PREPARE_NODE_UUID
        and edge["target_node_uuid"] == ANALYZE_NODE_UUID
    ]
    assert len(action_edges) == 1
    assert action_edges[0]["source_handle_uuid"] != PREPARE_READY_SOURCE


def test_group_is_a_real_presentation_node_and_not_an_execution_barrier(
    engine_context: EngineContext,
) -> None:
    result = _compile(engine_context.engine, _group_source())

    assert result.valid and result.graph is not None
    group = _node_by_uuid(result.graph, GROUP_A_NODE_UUID)
    prepare = _node_by_uuid(result.graph, PREPARE_NODE_UUID)
    analyze = _node_by_uuid(result.graph, ANALYZE_NODE_UUID)
    assert group["workflow_node_template_uuid"] == GROUP_TEMPLATE_UUID
    assert group["type"] == "group"
    assert prepare["parent_uuid"] == GROUP_A_NODE_UUID
    assert analyze["parent_uuid"] == GROUP_A_NODE_UUID
    assert all(
        GROUP_A_NODE_UUID not in {edge["source_node_uuid"], edge["target_node_uuid"]}
        for edge in result.graph["edges"]
    )
    source_map = [
        CandidateSourceMapEntry.model_validate(item) for item in result.source_map
    ]
    assert {entry.workflow_node_uuid for entry in source_map} == {
        GROUP_A_NODE_UUID,
        PREPARE_NODE_UUID,
        ANALYZE_NODE_UUID,
    }


def test_parallel_is_source_structure_without_fork_or_join_nodes(
    engine_context: EngineContext,
) -> None:
    result = _compile(engine_context.engine, _parallel_source())

    assert result.valid and result.graph is not None
    graph = result.graph
    assert {node["uuid"] for node in graph["nodes"]} == {
        GROUP_A_NODE_UUID,
        GROUP_B_NODE_UUID,
        PREPARE_NODE_UUID,
        ANALYZE_NODE_UUID,
        FINAL_NODE_UUID,
    }
    assert all(node["type"] not in {"fork", "join"} for node in graph["nodes"])
    assert _node_by_uuid(graph, PREPARE_NODE_UUID)["parent_uuid"] == GROUP_A_NODE_UUID
    assert _node_by_uuid(graph, ANALYZE_NODE_UUID)["parent_uuid"] == GROUP_B_NODE_UUID
    assert all(
        group_uuid not in {edge["source_node_uuid"], edge["target_node_uuid"]}
        for group_uuid in (GROUP_A_NODE_UUID, GROUP_B_NODE_UUID)
        for edge in graph["edges"]
    )

    incoming = [
        edge for edge in graph["edges"] if edge["target_node_uuid"] == FINAL_NODE_UUID
    ]
    assert {edge["source_node_uuid"] for edge in incoming} == {
        PREPARE_NODE_UUID,
        ANALYZE_NODE_UUID,
    }
    consumed = next(
        edge for edge in incoming if edge["source_node_uuid"] == PREPARE_NODE_UUID
    )
    dependency = next(
        edge for edge in incoming if edge["source_node_uuid"] == ANALYZE_NODE_UUID
    )
    assert consumed["source_handle_uuid"] == PREPARE_REPORT_SOURCE
    assert consumed["target_handle_uuid"] == FINAL_REPORT_TARGET
    assert dependency["source_handle_uuid"] == PREPARE_READY_SOURCE
    assert dependency["target_handle_uuid"] == FINAL_READY_TARGET

    source_map = [
        CandidateSourceMapEntry.model_validate(item) for item in result.source_map
    ]
    assert len(source_map) == 5
    assert result.normalized_python_source is not None
    assert "with parallel():" in result.normalized_python_source


@pytest.mark.parametrize(
    "source",
    [
        _parallel_source().replace(
            "            branch_b = reactor.prepare",
            "            with parallel():\n                branch_b = reactor.prepare",
        ),
        _parallel_source().replace(
            '        with group(name="Branch B"):',
            '        with group(name="Branch B"):\n            pass',
        ),
        _parallel_source().replace(
            "sample=sample, cycles=2",
            "sample=branch_a.prepared, cycles=2",
        ),
    ],
    ids=["nested-parallel", "non-construct-in-branch", "cross-branch-value"],
)
def test_unrepresentable_parallel_forms_return_diagnostics(
    engine_context: EngineContext,
    source: str,
) -> None:
    _assert_error_result(_compile(engine_context.engine, source))


def test_conditional_ast_remains_outside_round_02d(
    engine_context: EngineContext,
) -> None:
    source = _sequential_source().replace(
        (f"    # unilab:node_uuid={PREPARE_NODE_UUID}\n    first = reactor.prepare("),
        (
            "    if sample is not None:\n"
            f"        # unilab:node_uuid={PREPARE_NODE_UUID}\n"
            "        first = reactor.prepare("
        ),
    )
    ast.parse(source)

    _assert_error_result(_compile(engine_context.engine, source))


def test_workflow_input_can_be_the_explicit_root_output_binding(
    engine_context: EngineContext,
) -> None:
    result = _compile(engine_context.engine, _workflow_input_output_source())

    assert result.valid and result.graph is not None
    unilab = result.graph["workflow"]["meta_data"]["unilab"]
    assert unilab["output_contract"] == {
        "version": 1,
        "outputs": [
            {
                "name": "sample",
                "schema": {"$slot": "ResourceSlot"},
                "implicit": False,
            }
        ],
    }
    assert unilab["output_bindings"] == {
        "sample": {"kind": "workflow_input", "parameter": "sample"}
    }
    assert result.graph["nodes"] == []
    assert result.graph["edges"] == []


@pytest.mark.parametrize(
    ("annotation", "default", "expected_parameter"),
    [
        pytest.param(
            "Annotated[ResourceSlot, AllowedResourceTemplates(plate_96)]",
            "",
            {
                "name": "sample",
                "schema": {
                    "$slot": "ResourceSlot",
                    "allowed_resource_template_uuids": [RESOURCE_TEMPLATE_UUID],
                },
                "required": True,
            },
            id="resource-slot",
        ),
        pytest.param(
            "Annotated[list[ResourceSlot], AllowedResourceTemplates(plate_96)]",
            "",
            {
                "name": "sample",
                "schema": {
                    "type": "array",
                    "items": {
                        "$slot": "ResourceSlot",
                        "allowed_resource_template_uuids": [RESOURCE_TEMPLATE_UUID],
                    },
                },
                "required": True,
            },
            id="resource-slot-list",
        ),
        pytest.param(
            (
                "Annotated[list[ResourceSlot] | None, "
                "AllowedResourceTemplates(plate_96)]"
            ),
            " = None",
            {
                "name": "sample",
                "schema": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "$slot": "ResourceSlot",
                                "allowed_resource_template_uuids": [
                                    RESOURCE_TEMPLATE_UUID
                                ],
                            },
                        },
                        {"type": "null"},
                    ]
                },
                "required": False,
                "default": None,
            },
            id="nullable-resource-slot-list",
        ),
    ],
)
def test_constrained_workflow_input_round_trips_resource_template_identity(
    tmp_path: Path,
    annotation: str,
    default: str,
    expected_parameter: dict[str, Any],
) -> None:
    source = _constrained_workflow_input_source(
        annotation,
        default=default,
    )
    with _opened_engine(
        tmp_path / "constrained-input.db",
        resource_template_identity_index=_StaticResourceTemplateIdentityIndex(),
    ) as context:
        compiled = _compile(context.engine, source)

        assert compiled.valid and compiled.graph is not None, compiled.diagnostics
        unilab = compiled.graph["workflow"]["meta_data"]["unilab"]
        assert unilab["input_contract"] == {
            "version": 1,
            "parameters": [expected_parameter],
        }
        normalized = compiled.normalized_python_source
        assert normalized is not None
        assert "from lab.resources import plate_96" in normalized
        assert (
            "from unilabos.registry.annotations import AllowedResourceTemplates"
            in normalized
        )
        assert f"sample: {annotation}{default}" in normalized

        generated = context.engine.generate_python(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=7,
            graph=compiled.graph,
            source_uri="package://lab/workflows/constrained-input.py",
        )
        assert generated.valid and generated.normalized_python_source is not None
        recompiled = _compile(
            context.engine,
            generated.normalized_python_source,
            graph=compiled.graph,
        )

        assert recompiled.valid and recompiled.graph is not None
        assert _semantic_graph(recompiled.graph) == _semantic_graph(compiled.graph)
        assert generated.normalized_python_source == normalized
        assert recompiled.normalized_python_source == normalized
        assert CandidateChangeset.model_validate(recompiled.changeset).kind == (
            "source_only"
        )


def test_compile_generate_compile_is_a_semantic_fixed_point(
    engine_context: EngineContext,
) -> None:
    compiled = _compile(engine_context.engine, _parallel_source())
    assert compiled.valid and compiled.graph is not None

    generated = engine_context.engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=compiled.graph,
        source_uri="package://lab/workflows/parallel.py",
    )
    assert generated.valid and generated.normalized_python_source is not None
    recompiled = _compile(
        engine_context.engine,
        generated.normalized_python_source,
        graph=compiled.graph,
    )

    assert recompiled.valid and recompiled.graph is not None
    assert _semantic_graph(recompiled.graph) == _semantic_graph(compiled.graph)
    assert recompiled.normalized_python_source == generated.normalized_python_source
    assert CandidateChangeset.model_validate(recompiled.changeset).kind == "source_only"


def test_generate_compile_generate_has_one_deterministic_source(
    engine_context: EngineContext,
) -> None:
    compiled = _compile(engine_context.engine, _group_source())
    assert compiled.valid and compiled.graph is not None

    first = engine_context.engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=compiled.graph,
        source_uri="package://lab/workflows/group.py",
    )
    assert first.valid and first.normalized_python_source is not None
    recompiled = _compile(engine_context.engine, first.normalized_python_source)
    assert recompiled.valid and recompiled.graph is not None
    second = engine_context.engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=recompiled.graph,
        source_uri="package://lab/workflows/group.py",
    )

    assert second.valid
    assert second.normalized_python_source == first.normalized_python_source
    ast.parse(second.normalized_python_source)


def test_generate_keeps_dependent_groups_sequential(
    engine_context: EngineContext,
) -> None:
    compiled = _compile(engine_context.engine, _sequential_groups_source())
    assert compiled.valid and compiled.graph is not None

    generated = engine_context.engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=compiled.graph,
        source_uri="package://lab/workflows/sequential-groups.py",
    )

    assert generated.valid and generated.normalized_python_source is not None
    assert "with parallel():" not in generated.normalized_python_source
    assert generated.normalized_python_source.count("with group(") == 2


def test_generate_is_independent_of_candidate_node_array_order(
    engine_context: EngineContext,
) -> None:
    compiled = _compile(engine_context.engine, _parallel_source())
    assert compiled.valid and compiled.graph is not None
    reordered = deepcopy(compiled.graph)
    reordered["nodes"].reverse()

    generated = engine_context.engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=reordered,
        source_uri="package://lab/workflows/reordered.py",
    )

    assert generated.valid
    assert generated.normalized_python_source == compiled.normalized_python_source


def test_equivalent_author_source_is_source_only_against_applied_graph(
    engine_context: EngineContext,
) -> None:
    compiled = _compile(engine_context.engine, _sequential_source())
    assert compiled.valid and compiled.graph is not None
    reformatted = _sequential_source().replace(
        "first = reactor.prepare(sample=sample, cycles=1, note=None)",
        "first=reactor.prepare( note=None, cycles=1, sample=sample )",
    )

    result = _compile(engine_context.engine, reformatted, graph=compiled.graph)

    assert result.valid
    assert CandidateChangeset.model_validate(result.changeset).kind == "source_only"
    assert result.normalized_python_source == compiled.normalized_python_source


def test_generate_fails_closed_on_unknown_catalog_identity(
    engine_context: EngineContext,
) -> None:
    compiled = _compile(engine_context.engine, _sequential_source())
    assert compiled.valid and compiled.graph is not None
    graph = deepcopy(compiled.graph)
    _node_by_uuid(graph, PREPARE_NODE_UUID)["workflow_node_template_uuid"] = (
        "30000000-0000-4000-8000-000000000099"
    )

    result = engine_context.engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=graph,
        source_uri="package://lab/workflows/sequential.py",
    )

    _assert_error_result(result, code="template_catalog_mismatch")


def test_validate_proves_the_complete_graph_and_source_together(
    engine_context: EngineContext,
) -> None:
    compiled = _compile(engine_context.engine, _parallel_source())
    assert compiled.valid and compiled.graph is not None
    validated = engine_context.engine.validate(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=compiled.graph,
        python_source=compiled.normalized_python_source,
        source_uri="package://lab/workflows/parallel.py",
    )

    assert validated.valid
    assert validated.graph == compiled.graph
    assert validated.normalized_python_source == compiled.normalized_python_source


def test_validate_rejects_graph_with_foreign_authority_catalog_projection(
    engine_context: EngineContext,
) -> None:
    compiled = _compile(engine_context.engine, _sequential_source())
    assert compiled.valid and compiled.graph is not None
    graph = deepcopy(compiled.graph)
    graph["node_templates"][0]["meta_data"] = {"catalog_owner": "forged"}

    result = engine_context.engine.validate(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=graph,
        python_source=compiled.normalized_python_source,
        source_uri="package://lab/workflows/sequential.py",
    )

    _assert_error_result(result, code="template_catalog_mismatch")


def test_generate_rejects_workflow_identity_or_revision_mismatch(
    engine_context: EngineContext,
) -> None:
    compiled = _compile(engine_context.engine, _sequential_source())
    assert compiled.valid and compiled.graph is not None

    wrong_revision = deepcopy(compiled.graph)
    wrong_revision["workflow"]["revision"] = 8
    wrong_identity = deepcopy(compiled.graph)
    wrong_identity["workflow"]["uuid"] = "10000000-0000-4000-8000-000000000002"
    for graph in (wrong_revision, wrong_identity):
        result = engine_context.engine.generate_python(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=7,
            graph=graph,
            source_uri="package://lab/workflows/sequential.py",
        )
        _assert_error_result(result)


def test_catalog_snapshot_used_by_round_trip_is_authority_scoped(
    engine_context: EngineContext,
) -> None:
    compiled = _compile(engine_context.engine, _sequential_source())
    assert compiled.valid and compiled.graph is not None

    with engine_context.engine.catalog_snapshot() as fingerprint:
        assert fingerprint == compiled.template_catalog_fingerprint
        generated = engine_context.engine.generate_python(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=7,
            graph=compiled.graph,
            source_uri="package://lab/workflows/sequential.py",
        )

    assert generated.valid
    assert generated.template_catalog_fingerprint == fingerprint
    assert engine_context.engine.template_catalog_fingerprint == fingerprint
    assert AUTHORITY.authority_id == "backend-primary"


def test_empty_workflow_synthesizes_resource_slot_pass_through(
    engine_context: EngineContext,
) -> None:
    source = _workflow_input_output_source().replace(
        "    return workflow_output(sample=sample)",
        '    """No actions and no outputs."""',
    )

    result = _compile(engine_context.engine, source, graph=_empty_graph())

    assert result.valid and result.graph is not None
    unilab = result.graph["workflow"]["meta_data"]["unilab"]
    assert unilab["output_contract"] == {
        "version": 1,
        "outputs": [
            {
                "name": "sample",
                "schema": {"$slot": "ResourceSlot"},
                "implicit": True,
            }
        ],
    }
    assert unilab["output_bindings"] == {
        "sample": {"kind": "workflow_input", "parameter": "sample"}
    }
    assert result.normalized_python_source is not None
    assert "def pass_through(" in result.normalized_python_source
    assert ") -> None:" in result.normalized_python_source
    assert "workflow_output" not in result.normalized_python_source
    assert result.graph["nodes"] == []
    assert result.graph["edges"] == []
    assert result.source_map == []
    assert set(result.graph) == {
        "workflow",
        "nodes",
        "edges",
        "node_templates",
        "handle_templates",
    }


def test_parallel_final_output_uses_real_node_and_handle_identity(
    engine_context: EngineContext,
) -> None:
    result = _compile(engine_context.engine, _parallel_source())

    assert result.valid and result.graph is not None
    binding = result.graph["workflow"]["meta_data"]["unilab"]["output_bindings"][
        "report"
    ]
    assert binding == {
        "kind": "node_output",
        "workflow_node_uuid": FINAL_NODE_UUID,
        "source_handle_uuid": FINAL_REPORT_SOURCE,
    }
