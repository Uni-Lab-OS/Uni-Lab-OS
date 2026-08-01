"""M2A 共享 MaterialSource selector 与 framework aggregate 纵向 RED。

只通过 Authoring Engine、WorkflowService、WorkflowStore 和 TemplateCatalog
公开 seam 观察 direct Graph 行为。本 slice 不查 Site authority、material
fan-out 或 runtime 分配。
"""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.catalog import TemplateCatalog
from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

from .test_m2a_material_source_selector_matrix import (
    FIXED_MATERIAL_UUID,
)
from .test_m2a_material_source_selector_matrix import (
    _source as _selector_source,
)
from .test_m2a_material_source_vertical_slice import (
    AUTHORITY,
    MATERIAL_SOURCE_NODE_UUID,
    MATERIAL_SOURCE_TEMPLATE_UUID,
    MOUNT_MATERIAL_UUID,
    PLATE_RESOURCE_TEMPLATE_UUID,
    WORKFLOW_UUID,
    _catalog_imports,
    _StaticResourceTemplateIdentityIndex,
)

SITE_A_UUID = "60000000-0000-4000-8000-000000000001"
SITE_B_UUID = "60000000-0000-4000-8000-000000000002"
NON_CANONICAL_UUID = "5ABCDEF0-1234-4ABC-8DEF-000000000001"

EXPECTED_DIRECT_SELECTOR = {
    "mode": "create_new",
    "resource_template_uuid": PLATE_RESOURCE_TEMPLATE_UUID,
    "mount": {"uuid": MOUNT_MATERIAL_UUID},
    "material_uuid": None,
    "site": None,
    "slot_range": None,
    "flow_role": "reagent",
}


@dataclass
class _DirectGraphContext:
    store: WorkflowStore
    engine: WorkflowAuthoringEngine
    service: WorkflowService
    source: str
    candidate: dict[str, Any]
    applied_graph: dict[str, Any]


def _legal_source() -> str:
    return _selector_source(
        mode='"create_new"',
        material_uuid="None",
        flow_role="MaterialFlowRole.REAGENT",
    )


def _new_engine(store: WorkflowStore) -> WorkflowAuthoringEngine:
    catalog = TemplateCatalog(store)
    catalog.replace(AUTHORITY, _catalog_imports())
    return WorkflowAuthoringEngine(
        catalog=catalog,
        authority=AUTHORITY,
        resource_template_identity_index=_StaticResourceTemplateIdentityIndex(),
    )


def _compile(
    engine: WorkflowAuthoringEngine,
    *,
    source: str,
    applied_graph: dict[str, Any],
) -> CandidateCompilation:
    return engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=1,
        python_source=source,
        source_uri="package://lab/workflows/m2a_direct_graph.py",
        applied_graph=applied_graph,
    )


@pytest.fixture()
def graph_context(tmp_path: Path) -> Iterator[_DirectGraphContext]:
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        engine = _new_engine(store)
        service = WorkflowService(store, compiler=engine)
        service.create_workflow(
            workflow_uuid=WORKFLOW_UUID,
            name="Assay",
            tags=[],
            description=None,
            meta_data={},
        )
        applied_graph = service.get_graph(WORKFLOW_UUID)
        source = _legal_source()
        compiled = _compile(
            engine,
            source=source,
            applied_graph=applied_graph,
        )
        assert compiled.valid, compiled.diagnostics
        assert compiled.graph is not None
        yield _DirectGraphContext(
            store,
            engine,
            service,
            source,
            compiled.graph,
            applied_graph,
        )
    finally:
        store.close()


def _material_source_node(graph: dict[str, Any]) -> dict[str, Any]:
    return next(
        node for node in graph["nodes"] if node["uuid"] == MATERIAL_SOURCE_NODE_UUID
    )


def _mutated_selector_graph(
    candidate: dict[str, Any],
    case: str,
) -> dict[str, Any]:
    graph = deepcopy(candidate)
    node = _material_source_node(graph)
    param = node["param"]
    if case == "unknown key":
        param["quantity"] = 1
    elif case == "unknown mode":
        param["mode"] = "allocate_later"
    elif case == "create_new with material":
        param["material_uuid"] = FIXED_MATERIAL_UUID
    elif case == "non-canonical UUID":
        param["mount"]["uuid"] = NON_CANONICAL_UUID
    elif case == "nil UUID":
        param["mount"]["uuid"] = "00000000-0000-0000-0000-000000000000"
    elif case == "site and slot_range":
        param["site"] = SITE_A_UUID
        param["slot_range"] = [SITE_B_UUID]
    elif case == "empty slot_range":
        param["slot_range"] = []
    elif case == "duplicate slot_range":
        param["slot_range"] = [SITE_A_UUID, SITE_A_UUID]
    elif case == "unsorted slot_range":
        param["slot_range"] = [SITE_B_UUID, SITE_A_UUID]
    elif case == "invalid flow_role":
        param["flow_role"] = "standard"
    elif case == "top-level material_uuid":
        node["material_uuid"] = FIXED_MATERIAL_UUID
    else:  # pragma: no cover - parameter table is closed below
        raise AssertionError(f"unknown mutation case {case}")
    return graph


INVALID_SELECTOR_CASES = [
    "unknown key",
    "unknown mode",
    "create_new with material",
    "non-canonical UUID",
    "nil UUID",
    "site and slot_range",
    "empty slot_range",
    "duplicate slot_range",
    "unsorted slot_range",
    "invalid flow_role",
    "top-level material_uuid",
]


@pytest.mark.parametrize("public_seam", ["generate_python", "validate"])
@pytest.mark.parametrize("case", INVALID_SELECTOR_CASES)
def test_direct_graph_invalid_selector_has_one_stable_engine_diagnostic(
    graph_context: _DirectGraphContext,
    public_seam: str,
    case: str,
) -> None:
    graph = _mutated_selector_graph(graph_context.candidate, case)

    if public_seam == "generate_python":
        result = graph_context.engine.generate_python(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=1,
            graph=graph,
            source_uri="package://lab/workflows/m2a_direct_graph.py",
        )
    else:
        result = graph_context.engine.validate(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=1,
            graph=graph,
            python_source=graph_context.source,
            source_uri="package://lab/workflows/m2a_direct_graph.py",
        )

    assert not result.valid
    assert result.graph is None
    assert result.normalized_python_source is None
    assert [item["code"] for item in result.diagnostics] == ["invalid_material_source"]


@pytest.mark.parametrize("case", ["unknown key", "top-level material_uuid"])
def test_direct_save_rejects_invalid_selector_without_changing_graph(
    graph_context: _DirectGraphContext,
    case: str,
) -> None:
    graph = _mutated_selector_graph(graph_context.candidate, case)
    before = graph_context.service.get_graph(WORKFLOW_UUID)

    with pytest.raises(WorkflowError) as caught:
        graph_context.service.save_graph(
            WORKFLOW_UUID,
            revision=1,
            nodes=graph["nodes"],
            edges=graph["edges"],
        )

    assert caught.value.code == "invalid_material_source"
    assert graph_context.service.get_graph(WORKFLOW_UUID) == before
    assert before == graph_context.applied_graph
    assert before["workflow"]["revision"] == 1


@pytest.mark.parametrize(
    "aggregate_case",
    [
        "wrong class",
        "wrong name",
        "wrong type",
        "wrong node_type",
        "missing material handle",
    ],
)
def test_framework_aggregate_mismatch_is_catalog_diagnostic(
    tmp_path: Path,
    aggregate_case: str,
) -> None:
    imports = deepcopy(_catalog_imports())
    framework = next(
        item
        for item in imports
        if item.template["uuid"] == MATERIAL_SOURCE_TEMPLATE_UUID
    )
    if aggregate_case == "wrong class":
        framework.template["class"] = "lab.framework:WrongMaterialSource"
    elif aggregate_case == "wrong name":
        framework.template["name"] = "wrong_material_source"
    elif aggregate_case == "wrong type":
        framework.template["type"] = "compute"
    elif aggregate_case == "wrong node_type":
        framework.template["node_type"] = "compute"
    elif aggregate_case == "missing material handle":
        assert isinstance(framework.handles, list)
        framework.handles.clear()

    store = WorkflowStore(tmp_path / "aggregate.db")
    try:
        catalog = TemplateCatalog(store)
        catalog.replace(AUTHORITY, imports)
        engine = WorkflowAuthoringEngine(
            catalog=catalog,
            authority=AUTHORITY,
            resource_template_identity_index=(_StaticResourceTemplateIdentityIndex()),
        )
        store.create_workflow(
            workflow_uuid=WORKFLOW_UUID,
            name="Assay",
            tags=[],
            description=None,
            meta_data={},
        )

        result = _compile(
            engine,
            source=_legal_source(),
            applied_graph=store.get_graph(WORKFLOW_UUID),
        )

        assert not result.valid
        assert result.graph is None
        assert [item["code"] for item in result.diagnostics] == [
            "template_catalog_mismatch"
        ]
    finally:
        store.close()


def test_legal_direct_selector_saves_and_reads_back_without_drift(
    graph_context: _DirectGraphContext,
) -> None:
    saved = graph_context.service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=graph_context.candidate["nodes"],
        edges=graph_context.candidate["edges"],
    )
    persisted = graph_context.service.get_graph(WORKFLOW_UUID)

    assert saved == persisted
    assert persisted["workflow"]["revision"] == 2
    node = _material_source_node(persisted)
    assert node.get("material_uuid") is None
    assert node["param"] == EXPECTED_DIRECT_SELECTOR
