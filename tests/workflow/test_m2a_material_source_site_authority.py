"""M2A MaterialSource Site static authority 与 Python 往返 RED。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.catalog import TemplateCatalog
from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

from .m2a_material_source_authority_fixture import (
    DEFAULT_MATERIALS,
    DEFAULT_SITES,
    DELETED_SITE_UUID,
    FIXED_MATERIAL_UUID,
    FOREIGN_SITE_UUID,
    INCOMPATIBLE_RESOURCE_TEMPLATE_UUID,
    MISSING_SITE_UUID,
    MOUNT_MATERIAL_UUID,
    PLATE_RESOURCE_TEMPLATE_UUID,
    SITE_A_UUID,
    SITE_B_UUID,
    SITE_C_UUID,
    StaticMaterialSourceAuthority,
    default_material_source_authority,
)
from .test_m2a_material_source_selector_matrix import _source as _selector_source
from .test_m2a_material_source_vertical_slice import (
    AUTHORITY,
    MATERIAL_SOURCE_NODE_UUID,
    WORKFLOW_UUID,
    _catalog_imports,
    _StaticResourceTemplateIdentityIndex,
)


@dataclass
class _SiteContext:
    store: WorkflowStore
    engine: WorkflowAuthoringEngine
    service: WorkflowService
    applied_graph: dict[str, Any]


def test_public_static_authority_port_declares_the_three_read_methods() -> None:
    api = import_module("unilabos.workflow.material_source")
    port = api.MaterialSourceStaticAuthority

    for method_name in ("get_material", "get_site", "list_sites"):
        assert callable(getattr(port, method_name))


def test_fake_authority_contains_the_frozen_direct_site_facts() -> None:
    authority = default_material_source_authority()

    assert authority.get_material(MOUNT_MATERIAL_UUID).deleted_at is None
    assert (
        authority.get_material(FIXED_MATERIAL_UUID).resource_template_uuid
        == PLATE_RESOURCE_TEMPLATE_UUID
    )
    sites = authority.list_sites(MOUNT_MATERIAL_UUID)
    assert [item.uuid for item in sites] == [SITE_A_UUID, SITE_B_UUID, SITE_C_UUID]
    assert sites[0].occupied_material_uuid == FIXED_MATERIAL_UUID
    assert sites[0].allowed_resource_template_uuids == (PLATE_RESOURCE_TEMPLATE_UUID,)
    assert sites[1].allowed_resource_template_uuids == ()
    assert sites[2].allowed_resource_template_uuids == (
        INCOMPATIBLE_RESOURCE_TEMPLATE_UUID,
    )


@contextmanager
def _opened_context(
    database_path: Path,
    *,
    material_source_authority: StaticMaterialSourceAuthority,
) -> Iterator[_SiteContext]:
    store = WorkflowStore(database_path)
    try:
        catalog = TemplateCatalog(store)
        catalog.replace(AUTHORITY, _catalog_imports())
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
            name="Assay",
            tags=[],
            description=None,
            meta_data={},
        )
        yield _SiteContext(
            store,
            engine,
            service,
            service.get_graph(WORKFLOW_UUID),
        )
    finally:
        store.close()


def _compile(
    context: _SiteContext,
    source: str,
    *,
    applied_graph: dict[str, Any] | None = None,
) -> CandidateCompilation:
    return context.engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=1,
        python_source=source,
        source_uri="package://lab/workflows/m2a_site_authority.py",
        applied_graph=(
            context.applied_graph if applied_graph is None else applied_graph
        ),
    )


def _source(
    *,
    mode: str = "existing",
    material_uuid: str | None = FIXED_MATERIAL_UUID,
    site: str | None = None,
    slot_range: tuple[str, ...] | None = None,
) -> str:
    material_expression = "None" if material_uuid is None else f'"{material_uuid}"'
    site_expression = "None" if site is None else f'"{site}"'
    range_expression = (
        "None"
        if slot_range is None
        else "[" + ", ".join(f'"{item}"' for item in slot_range) + "]"
    )
    return _selector_source(
        mode=f'"{mode}"',
        material_uuid=material_expression,
        flow_role="MaterialFlowRole.REAGENT",
        site=site_expression,
        slot_range=range_expression,
    )


def _material_source_param(graph: dict[str, Any]) -> dict[str, Any]:
    return next(
        item["param"]
        for item in graph["nodes"]
        if item["uuid"] == MATERIAL_SOURCE_NODE_UUID
    )


def _assert_python_round_trip(
    context: _SiteContext,
    compiled: CandidateCompilation,
) -> None:
    assert compiled.valid, compiled.diagnostics
    assert compiled.graph is not None
    generated = context.engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=1,
        graph=compiled.graph,
        source_uri="package://lab/workflows/m2a_site_authority.py",
    )
    assert generated.valid, generated.diagnostics
    assert generated.normalized_python_source is not None
    recompiled = _compile(
        context,
        generated.normalized_python_source,
        applied_graph=compiled.graph,
    )
    assert recompiled.valid, recompiled.diagnostics
    assert recompiled.graph == compiled.graph


@pytest.mark.parametrize(
    ("site", "slot_range"),
    [
        pytest.param(SITE_A_UUID, None, id="exact-site"),
        pytest.param(None, (SITE_A_UUID, SITE_B_UUID), id="candidate-range"),
        pytest.param(None, None, id="all-compatible-sites"),
    ],
)
def test_python_site_selection_round_trips_exact_range_and_all(
    tmp_path: Path,
    site: str | None,
    slot_range: tuple[str, ...] | None,
) -> None:
    with _opened_context(
        tmp_path / "workflow.db",
        material_source_authority=default_material_source_authority(),
    ) as context:
        compiled = _compile(
            context,
            _source(site=site, slot_range=slot_range),
        )

        assert compiled.valid, compiled.diagnostics
        assert compiled.graph is not None
        assert _material_source_param(compiled.graph) == {
            "mode": "existing",
            "resource_template_uuid": PLATE_RESOURCE_TEMPLATE_UUID,
            "mount": {"uuid": MOUNT_MATERIAL_UUID},
            "material_uuid": FIXED_MATERIAL_UUID,
            "site": site,
            "slot_range": list(slot_range) if slot_range is not None else None,
            "flow_role": "reagent",
        }
        _assert_python_round_trip(context, compiled)


def test_create_new_accepts_an_occupied_compatible_site_statically(
    tmp_path: Path,
) -> None:
    authority = default_material_source_authority()
    assert authority.get_site(SITE_A_UUID).occupied_material_uuid is not None

    with _opened_context(
        tmp_path / "workflow.db",
        material_source_authority=authority,
    ) as context:
        compiled = _compile(
            context,
            _source(mode="create_new", material_uuid=None, site=SITE_A_UUID),
        )

        assert compiled.valid, compiled.diagnostics
        assert compiled.graph is not None
        assert _material_source_param(compiled.graph)["site"] == SITE_A_UUID
        _assert_python_round_trip(context, compiled)


def _authority_failure_case(
    case: str,
) -> tuple[StaticMaterialSourceAuthority, str, str]:
    materials = list(DEFAULT_MATERIALS)
    sites = list(DEFAULT_SITES)
    source = _source()
    expected_code = "material_source_conflict"

    if case == "missing mount":
        materials = [item for item in materials if item.uuid != MOUNT_MATERIAL_UUID]
        expected_code = "not_found"
    elif case == "deleted mount":
        materials = [
            replace(item, deleted_at="2026-08-02T01:00:00Z")
            if item.uuid == MOUNT_MATERIAL_UUID
            else item
            for item in materials
        ]
        expected_code = "not_found"
    elif case == "missing exact site":
        source = _source(site=MISSING_SITE_UUID)
        expected_code = "not_found"
    elif case == "deleted exact site":
        source = _source(site=DELETED_SITE_UUID)
        expected_code = "not_found"
    elif case == "site owned by another mount":
        source = _source(site=FOREIGN_SITE_UUID)
    elif case == "exact site incompatible":
        source = _source(site=SITE_C_UUID, material_uuid=None)
    elif case == "range has no compatible site":
        source = _source(slot_range=(SITE_C_UUID,), material_uuid=None)
    elif case == "all sites incompatible":
        sites = [item for item in sites if item.uuid == SITE_C_UUID]
        source = _source(material_uuid=None)
    elif case == "fixed material template mismatch":
        materials = [
            replace(
                item,
                resource_template_uuid=INCOMPATIBLE_RESOURCE_TEMPLATE_UUID,
            )
            if item.uuid == FIXED_MATERIAL_UUID
            else item
            for item in materials
        ]
        source = _source(site=SITE_A_UUID)
    elif case == "fixed material outside range":
        source = _source(slot_range=(SITE_B_UUID,))
    else:  # pragma: no cover - closed case table below
        raise AssertionError(f"unknown authority case {case}")
    return (
        StaticMaterialSourceAuthority(materials=materials, sites=sites),
        source,
        expected_code,
    )


@pytest.mark.parametrize(
    "case",
    [
        "missing mount",
        "deleted mount",
        "missing exact site",
        "deleted exact site",
        "site owned by another mount",
        "exact site incompatible",
        "range has no compatible site",
        "all sites incompatible",
        "fixed material template mismatch",
        "fixed material outside range",
    ],
)
def test_compile_returns_stable_static_authority_failure(
    tmp_path: Path,
    case: str,
) -> None:
    authority, source, expected_code = _authority_failure_case(case)
    with _opened_context(
        tmp_path / "workflow.db",
        material_source_authority=authority,
    ) as context:
        result = _compile(context, source)

    assert not result.valid
    assert result.graph is None
    assert [item["code"] for item in result.diagnostics] == [expected_code]


@pytest.mark.parametrize(
    ("public_seam", "failure_case", "expected_code"),
    [
        ("generate_python", "missing mount", "not_found"),
        ("validate", "exact site incompatible", "material_source_conflict"),
    ],
)
def test_graph_engine_seams_apply_the_same_static_authority_contract(
    tmp_path: Path,
    public_seam: str,
    failure_case: str,
    expected_code: str,
) -> None:
    with _opened_context(
        tmp_path / "workflow.db",
        material_source_authority=default_material_source_authority(),
    ) as context:
        compiled = _compile(context, _source(material_uuid=None))
        assert compiled.valid and compiled.graph is not None

        failing_authority, _, _ = _authority_failure_case(failure_case)
        failing_engine = WorkflowAuthoringEngine(
            catalog=TemplateCatalog(context.store),
            authority=AUTHORITY,
            resource_template_identity_index=(_StaticResourceTemplateIdentityIndex()),
            material_source_authority=failing_authority,
        )
        graph = deepcopy(compiled.graph)
        if failure_case == "exact site incompatible":
            selector = _material_source_param(graph)
            selector["site"] = SITE_C_UUID
            selector["slot_range"] = None
        if public_seam == "generate_python":
            result = failing_engine.generate_python(
                workflow_uuid=WORKFLOW_UUID,
                workflow_revision=1,
                graph=graph,
                source_uri="package://lab/workflows/m2a_site_authority.py",
            )
        else:
            result = failing_engine.validate(
                workflow_uuid=WORKFLOW_UUID,
                workflow_revision=1,
                graph=graph,
                python_source=compiled.normalized_python_source,
                source_uri="package://lab/workflows/m2a_site_authority.py",
            )

    assert not result.valid
    assert result.graph is None
    assert [item["code"] for item in result.diagnostics] == [expected_code]


def test_direct_save_authority_failure_is_atomic(
    tmp_path: Path,
) -> None:
    with _opened_context(
        tmp_path / "workflow.db",
        material_source_authority=default_material_source_authority(),
    ) as context:
        compiled = _compile(context, _source(material_uuid=None))
        assert compiled.valid and compiled.graph is not None
        graph = deepcopy(compiled.graph)
        _material_source_param(graph)["site"] = MISSING_SITE_UUID
        before = context.service.get_graph(WORKFLOW_UUID)

        with pytest.raises(WorkflowError) as caught:
            context.service.save_graph(
                WORKFLOW_UUID,
                revision=1,
                nodes=graph["nodes"],
                edges=graph["edges"],
            )

        assert caught.value.code == "not_found"
        assert context.service.get_graph(WORKFLOW_UUID) == before
        assert before["workflow"]["revision"] == 1
