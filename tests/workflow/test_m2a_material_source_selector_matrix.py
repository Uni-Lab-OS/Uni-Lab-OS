"""M2A MaterialSource mode/role selector 的第二个纵向 RED。

本 slice 只通过 ``WorkflowAuthoringEngine`` 的公开 compile/generate seam
观察 selector 矩阵与稳定诊断。不涉及 Site authority、material fan-out、
WorkflowService Apply/direct save 或 runtime 物料分配。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.catalog import TemplateCatalog
from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.store import WorkflowStore

from .m2a_material_source_authority_fixture import (
    default_material_source_authority,
)
from .test_m2a_material_source_vertical_slice import (
    AUTHORITY,
    MATERIAL_SOURCE_NODE_UUID,
    MOUNT_MATERIAL_UUID,
    PLATE_RESOURCE_TEMPLATE_UUID,
    PREPARE_NODE_UUID,
    WORKFLOW_UUID,
    _catalog_imports,
    _StaticResourceTemplateIdentityIndex,
)

FIXED_MATERIAL_UUID = "51000000-0000-4000-8000-000000000001"


@dataclass
class _EngineContext:
    store: WorkflowStore
    engine: WorkflowAuthoringEngine
    applied_graph: dict[str, Any]


@pytest.fixture()
def engine_context(tmp_path: Path) -> Iterator[_EngineContext]:
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        catalog = TemplateCatalog(store)
        catalog.replace(AUTHORITY, _catalog_imports())
        store.create_workflow(
            workflow_uuid=WORKFLOW_UUID,
            name="Assay",
            tags=[],
            description=None,
            meta_data={},
        )
        engine = WorkflowAuthoringEngine(
            catalog=catalog,
            authority=AUTHORITY,
            resource_template_identity_index=(_StaticResourceTemplateIdentityIndex()),
            material_source_authority=default_material_source_authority(),
        )
        yield _EngineContext(store, engine, store.get_graph(WORKFLOW_UUID))
    finally:
        store.close()


def _source(
    *,
    mode: str,
    material_uuid: str,
    flow_role: str,
    site: str = "None",
    slot_range: str = "None",
    omitted_field: str | None = None,
    extra_field: str | None = None,
) -> str:
    fields = [
        "resource_template=corning_96_well_plate",
        f"mode={mode}",
        f'mount=resource_ref("{MOUNT_MATERIAL_UUID}")',
        f"material_uuid={material_uuid}",
        f"site={site}",
        f"slot_range={slot_range}",
        f"flow_role={flow_role}",
    ]
    if omitted_field is not None:
        fields = [item for item in fields if not item.startswith(f"{omitted_field}=")]
    if extra_field is not None:
        fields.append(extra_field)
    arguments = ",\n".join(f"        {field}" for field in fields)
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
    displayname="Assay",
)
def assay_workflow():
    # unilab:node_uuid={MATERIAL_SOURCE_NODE_UUID}
    assay_plate = material_source(
{arguments},
    )
    # unilab:node_uuid={PREPARE_NODE_UUID}
    prepared = reactor.prepare(sample=assay_plate)
'''


def _compile(
    context: _EngineContext,
    source: str,
    *,
    applied_graph: dict[str, Any] | None = None,
) -> CandidateCompilation:
    return context.engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=1,
        python_source=source,
        source_uri="package://lab/workflows/m2a_selector_matrix.py",
        applied_graph=(
            context.applied_graph if applied_graph is None else applied_graph
        ),
    )


def _material_source_param(graph: dict[str, Any]) -> dict[str, Any]:
    node = next(
        item for item in graph["nodes"] if item["uuid"] == MATERIAL_SOURCE_NODE_UUID
    )
    assert node.get("material_uuid") is None
    return node["param"]


def _assert_round_trip(
    context: _EngineContext,
    compiled: CandidateCompilation,
    *,
    role_member: str,
) -> None:
    assert compiled.valid, compiled.diagnostics
    assert compiled.graph is not None
    generated = context.engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=1,
        graph=compiled.graph,
        source_uri="package://lab/workflows/m2a_selector_matrix.py",
    )
    assert generated.valid, generated.diagnostics
    assert generated.normalized_python_source is not None
    assert f"flow_role=MaterialFlowRole.{role_member}" in (
        generated.normalized_python_source
    )
    recompiled = _compile(
        context,
        generated.normalized_python_source,
        applied_graph=compiled.graph,
    )
    assert recompiled.valid, recompiled.diagnostics
    assert recompiled.graph == compiled.graph


@pytest.mark.parametrize(
    ("role_member", "wire_value"),
    [
        ("ALIQUOT_SAMPLE", "aliquot_sample"),
        ("REAGENT", "reagent"),
        ("CONSUMABLE", "consumable"),
    ],
)
def test_existing_fixed_material_round_trips_each_remaining_flow_role(
    engine_context: _EngineContext,
    role_member: str,
    wire_value: str,
) -> None:
    compiled = _compile(
        engine_context,
        _source(
            mode='"existing"',
            material_uuid=f'"{FIXED_MATERIAL_UUID}"',
            flow_role=f"MaterialFlowRole.{role_member}",
        ),
    )

    assert compiled.valid, compiled.diagnostics
    assert compiled.graph is not None
    assert _material_source_param(compiled.graph) == {
        "mode": "existing",
        "resource_template_uuid": PLATE_RESOURCE_TEMPLATE_UUID,
        "mount": {"uuid": MOUNT_MATERIAL_UUID},
        "material_uuid": FIXED_MATERIAL_UUID,
        "site": None,
        "slot_range": None,
        "flow_role": wire_value,
    }
    _assert_round_trip(engine_context, compiled, role_member=role_member)


def test_create_new_without_material_round_trips_canonical_selector(
    engine_context: _EngineContext,
) -> None:
    compiled = _compile(
        engine_context,
        _source(
            mode='"create_new"',
            material_uuid="None",
            flow_role="MaterialFlowRole.REAGENT",
        ),
    )

    assert compiled.valid, compiled.diagnostics
    assert compiled.graph is not None
    assert _material_source_param(compiled.graph) == {
        "mode": "create_new",
        "resource_template_uuid": PLATE_RESOURCE_TEMPLATE_UUID,
        "mount": {"uuid": MOUNT_MATERIAL_UUID},
        "material_uuid": None,
        "site": None,
        "slot_range": None,
        "flow_role": "reagent",
    }
    _assert_round_trip(engine_context, compiled, role_member="REAGENT")


@pytest.mark.parametrize(
    ("case", "source"),
    [
        (
            "unknown mode",
            _source(
                mode='"allocate_later"',
                material_uuid="None",
                flow_role="MaterialFlowRole.PRIMARY_SAMPLE",
            ),
        ),
        (
            "create_new with fixed material",
            _source(
                mode='"create_new"',
                material_uuid=f'"{FIXED_MATERIAL_UUID}"',
                flow_role="MaterialFlowRole.PRIMARY_SAMPLE",
            ),
        ),
        (
            "free-string role",
            _source(
                mode='"existing"',
                material_uuid="None",
                flow_role='"reagent"',
            ),
        ),
        (
            "unknown enum member",
            _source(
                mode='"existing"',
                material_uuid="None",
                flow_role="MaterialFlowRole.UNKNOWN",
            ),
        ),
        (
            "missing field",
            _source(
                mode='"existing"',
                material_uuid="None",
                flow_role="MaterialFlowRole.PRIMARY_SAMPLE",
                omitted_field="slot_range",
            ),
        ),
        (
            "extra field",
            _source(
                mode='"existing"',
                material_uuid="None",
                flow_role="MaterialFlowRole.PRIMARY_SAMPLE",
                extra_field="quantity=1",
            ),
        ),
    ],
    ids=lambda value: value if isinstance(value, str) and "\n" not in value else None,
)
def test_invalid_selector_matrix_returns_stable_diagnostic(
    engine_context: _EngineContext,
    case: str,
    source: str,
) -> None:
    del case

    result = _compile(engine_context, source)

    assert not result.valid
    assert result.graph is None
    assert result.normalized_python_source is None
    assert [item["code"] for item in result.diagnostics] == ["invalid_material_source"]
