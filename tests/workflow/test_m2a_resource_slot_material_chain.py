"""M2A ResourceSlot 物料唯一链的最小 public RED。

本 slice 只冻结两件事：MaterialSource 的同一个 ``material`` output 不能
fan-out，以及普通 Action 可以把同一个 ResourceSlot 从同名 input 顺序传给
同名 output。模板兼容性、普通 Action fan-out 和 runtime 不在本文件范围内。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

import pytest

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.catalog import NodeTemplateImport, TemplateCatalog
from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

from .m2a_material_source_authority_fixture import (
    MOUNT_MATERIAL_UUID,
    default_material_source_authority,
)
from .test_m2a_material_source_vertical_slice import (
    AUTHORITY,
    HOST_RESOURCE_TEMPLATE_UUID,
    MATERIAL_HANDLE_UUID,
    MATERIAL_SOURCE_NODE_UUID,
    PREPARE_NODE_UUID,
    SAMPLE_HANDLE_UUID,
    WORKFLOW_UUID,
    _catalog_imports,
    _StaticResourceTemplateIdentityIndex,
)

SECOND_CONSUMER_NODE_UUID = "20000000-0000-4000-8000-000000000003"
SECOND_MATERIAL_SOURCE_NODE_UUID = "20000000-0000-4000-8000-000000000005"
MIDDLE_NODE_UUID = "20000000-0000-4000-8000-000000000004"
MIDDLE_TEMPLATE_UUID = "30000000-0000-4000-8000-000000000003"
MIDDLE_SAMPLE_TARGET_UUID = "40000000-0000-4000-8000-000000000003"
MIDDLE_SAMPLE_SOURCE_UUID = "40000000-0000-4000-8000-000000000004"


@dataclass
class _ChainContext:
    store: WorkflowStore
    engine: WorkflowAuthoringEngine
    service: WorkflowService
    applied_graph: dict[str, Any]


def _handle(
    handle_uuid: str,
    *,
    io_type: str,
) -> dict[str, Any]:
    return {
        "uuid": handle_uuid,
        "description": "Sequential sample pass-through",
        "meta_data": {"contract": "m2a-material-chain"},
        "handle_key": "sample",
        "io_type": io_type,
        "display_name": "Sample",
        "type": "ResourceSlot",
        "required": io_type == "target",
        "data_source": "executor" if io_type == "target" else "result",
        "data_key": "sample",
    }


def _middle_action_import() -> NodeTemplateImport:
    return NodeTemplateImport(
        template={
            "uuid": MIDDLE_TEMPLATE_UUID,
            "description": "Read barcode and pass the same sample through",
            "meta_data": {"contract": "m2a-material-chain"},
            "resource_template_uuid": HOST_RESOURCE_TEMPLATE_UUID,
            "name": "read_barcode",
            "display_name": "Read Barcode",
            "class": "lab.devices:Reactor",
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
        },
        handles=[
            _handle(MIDDLE_SAMPLE_TARGET_UUID, io_type="target"),
            _handle(MIDDLE_SAMPLE_SOURCE_UUID, io_type="source"),
        ],
    )


@contextmanager
def _opened_context(
    database_path: Path,
    *,
    include_middle_action: bool = False,
) -> Iterator[_ChainContext]:
    store = WorkflowStore(database_path)
    try:
        imports = _catalog_imports()
        if include_middle_action:
            imports.append(_middle_action_import())
        catalog = TemplateCatalog(store)
        catalog.replace(AUTHORITY, imports)
        material_source_authority = default_material_source_authority()
        engine = WorkflowAuthoringEngine(
            catalog=catalog,
            authority=AUTHORITY,
            resource_template_identity_index=(_StaticResourceTemplateIdentityIndex()),
            material_source_authority=material_source_authority,
        )
        service = WorkflowService(
            store,
            compiler=engine,
            material_source_authority=material_source_authority,
        )
        service.create_workflow(
            workflow_uuid=WORKFLOW_UUID,
            name="Material chain",
            tags=[],
            description=None,
            meta_data={},
        )
        yield _ChainContext(
            store=store,
            engine=engine,
            service=service,
            applied_graph=service.get_graph(WORKFLOW_UUID),
        )
    finally:
        store.close()


def _material_source_call() -> str:
    return f'''    # unilab:node_uuid={MATERIAL_SOURCE_NODE_UUID}
    sample = material_source(
        resource_template=corning_96_well_plate,
        mode="create_new",
        mount=resource_ref("{MOUNT_MATERIAL_UUID}"),
        material_uuid=None,
        site=None,
        slot_range=None,
        flow_role=MaterialFlowRole.PRIMARY_SAMPLE,
    )'''


def _source(*, pass_through: bool) -> str:
    if pass_through:
        actions = f"""    # unilab:node_uuid={MIDDLE_NODE_UUID}
    scanned = reactor.read_barcode(sample=sample)
    # unilab:node_uuid={PREPARE_NODE_UUID}
    prepared = reactor.prepare(sample=scanned.sample)"""
        display_name = "Sequential material pass-through"
    else:
        actions = f"""    # unilab:node_uuid={PREPARE_NODE_UUID}
    prepared = reactor.prepare(sample=sample)"""
        display_name = "Single material consumer"
    return f'''from lab.devices import Reactor
from lab.resources import corning_96_well_plate
from unilabos.workflow.authoring import (
    MaterialFlowRole,
    device,
    material_source,
    resource_ref,
    workflow_definition,
)


reactor: Reactor = device()


@workflow_definition(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="{display_name}",
)
def material_chain():
{_material_source_call()}
{actions}
'''


def _compile(context: _ChainContext, source: str) -> CandidateCompilation:
    return context.engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=1,
        python_source=source,
        source_uri="package://lab/workflows/m2a_material_chain.py",
        applied_graph=context.applied_graph,
    )


def test_two_material_sources_round_trip_as_non_executable_declarations(
    tmp_path: Path,
) -> None:
    second_source = _material_source_call().replace(
        MATERIAL_SOURCE_NODE_UUID,
        SECOND_MATERIAL_SOURCE_NODE_UUID,
    ).replace("sample =", "reagent =")
    source = f'''from lab.resources import corning_96_well_plate
from unilabos.workflow.authoring import (
    MaterialFlowRole,
    material_source,
    resource_ref,
    workflow_definition,
)


@workflow_definition(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="Two material sources",
)
def two_material_sources():
{_material_source_call()}
{second_source}
'''

    with _opened_context(tmp_path / "workflow.db") as context:
        compiled = _compile(context, source)

    assert compiled.valid, compiled.diagnostics
    assert compiled.normalized_python_source.count("material_source(") == 2
    assert "with parallel():" not in compiled.normalized_python_source


def _node(graph: dict[str, Any], node_uuid: str) -> dict[str, Any]:
    return next(item for item in graph["nodes"] if item["uuid"] == node_uuid)


def _authoring_edge_uuid(
    *,
    source_node_uuid: str,
    source_handle_uuid: str,
    target_node_uuid: str,
    target_handle_uuid: str,
) -> str:
    return str(
        uuid5(
            UUID(WORKFLOW_UUID),
            "authoring-edge:"
            f"{source_node_uuid}:{source_handle_uuid}:"
            f"{target_node_uuid}:{target_handle_uuid}",
        )
    )


def _material_source_fan_out_graph(
    candidate: dict[str, Any],
) -> dict[str, Any]:
    graph = deepcopy(candidate)
    first_consumer = _node(graph, PREPARE_NODE_UUID)
    second_consumer = deepcopy(first_consumer)
    second_consumer["uuid"] = SECOND_CONSUMER_NODE_UUID
    second_consumer["name"] = "duplicate"
    graph["nodes"].append(second_consumer)

    first_edge = next(
        item
        for item in graph["edges"]
        if item["source_node_uuid"] == MATERIAL_SOURCE_NODE_UUID
        and item["source_handle_uuid"] == MATERIAL_HANDLE_UUID
    )
    second_edge = deepcopy(first_edge)
    second_edge.update(
        {
            "uuid": _authoring_edge_uuid(
                source_node_uuid=MATERIAL_SOURCE_NODE_UUID,
                source_handle_uuid=MATERIAL_HANDLE_UUID,
                target_node_uuid=SECOND_CONSUMER_NODE_UUID,
                target_handle_uuid=SAMPLE_HANDLE_UUID,
            ),
            "target_node_uuid": SECOND_CONSUMER_NODE_UUID,
        }
    )
    graph["edges"].append(second_edge)
    return graph


@pytest.mark.parametrize("public_seam", ["generate_python", "validate"])
def test_material_source_output_fan_out_has_one_stable_engine_diagnostic(
    tmp_path: Path,
    public_seam: str,
) -> None:
    source = _source(pass_through=False)
    with _opened_context(tmp_path / "workflow.db") as context:
        compiled = _compile(context, source)
        assert compiled.valid and compiled.graph is not None
        fan_out = _material_source_fan_out_graph(compiled.graph)

        if public_seam == "generate_python":
            result = context.engine.generate_python(
                workflow_uuid=WORKFLOW_UUID,
                workflow_revision=1,
                graph=fan_out,
                source_uri="package://lab/workflows/m2a_material_chain.py",
            )
        else:
            result = context.engine.validate(
                workflow_uuid=WORKFLOW_UUID,
                workflow_revision=1,
                graph=fan_out,
                python_source=source,
                source_uri="package://lab/workflows/m2a_material_chain.py",
            )

    assert not result.valid
    assert result.graph is None
    assert result.normalized_python_source is None
    assert [item["code"] for item in result.diagnostics] == ["material_flow_fan_out"]


def test_direct_save_rejects_material_source_fan_out_atomically(
    tmp_path: Path,
) -> None:
    with _opened_context(tmp_path / "workflow.db") as context:
        compiled = _compile(context, _source(pass_through=False))
        assert compiled.valid and compiled.graph is not None
        fan_out = _material_source_fan_out_graph(compiled.graph)
        before = context.service.get_graph(WORKFLOW_UUID)

        with pytest.raises(WorkflowError) as caught:
            context.service.save_graph(
                WORKFLOW_UUID,
                revision=1,
                nodes=fan_out["nodes"],
                edges=fan_out["edges"],
            )

        assert caught.value.code == "material_flow_fan_out"
        assert context.service.get_graph(WORKFLOW_UUID) == before
        assert before == context.applied_graph
        assert before["workflow"]["revision"] == 1


def test_action_passes_one_material_forward_as_a_sequential_resource_slot_chain(
    tmp_path: Path,
) -> None:
    source = _source(pass_through=True)
    with _opened_context(
        tmp_path / "workflow.db",
        include_middle_action=True,
    ) as context:
        compiled = _compile(context, source)
        assert compiled.valid, compiled.diagnostics
        assert compiled.graph is not None

        material_edges = [
            item
            for item in compiled.graph["edges"]
            if item["source_handle_uuid"]
            in {MATERIAL_HANDLE_UUID, MIDDLE_SAMPLE_SOURCE_UUID}
        ]
        assert [
            (
                item["source_node_uuid"],
                item["source_handle_uuid"],
                item["target_node_uuid"],
                item["target_handle_uuid"],
            )
            for item in material_edges
        ] == [
            (
                MATERIAL_SOURCE_NODE_UUID,
                MATERIAL_HANDLE_UUID,
                MIDDLE_NODE_UUID,
                MIDDLE_SAMPLE_TARGET_UUID,
            ),
            (
                MIDDLE_NODE_UUID,
                MIDDLE_SAMPLE_SOURCE_UUID,
                PREPARE_NODE_UUID,
                SAMPLE_HANDLE_UUID,
            ),
        ]
        assert all(
            sum(
                item["source_node_uuid"] == source_node_uuid
                and item["source_handle_uuid"] == source_handle_uuid
                for item in material_edges
            )
            == 1
            for source_node_uuid, source_handle_uuid in (
                (MATERIAL_SOURCE_NODE_UUID, MATERIAL_HANDLE_UUID),
                (MIDDLE_NODE_UUID, MIDDLE_SAMPLE_SOURCE_UUID),
            )
        )

        generated = context.engine.generate_python(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=1,
            graph=compiled.graph,
            source_uri="package://lab/workflows/m2a_material_chain.py",
        )
        assert generated.valid, generated.diagnostics
        assert generated.normalized_python_source is not None
        assert "reactor.read_barcode(sample=sample)" in (
            generated.normalized_python_source
        )
        assert "reactor.prepare(sample=scanned.sample)" in (
            generated.normalized_python_source
        )

        recompiled = context.engine.compile(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=1,
            python_source=generated.normalized_python_source,
            source_uri="package://lab/workflows/m2a_material_chain.py",
            applied_graph=compiled.graph,
        )

    assert recompiled.valid, recompiled.diagnostics
    assert recompiled.graph == compiled.graph
    assert (
        "from lab.resources import corning_96_well_plate"
        in generated.normalized_python_source
    )
