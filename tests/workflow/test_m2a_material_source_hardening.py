"""M2A 角色目录、双向 identity 与 selector failure hardening RED。"""

from __future__ import annotations

import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow import authoring as authoring_api
from unilabos.workflow.authoring import MaterialFlowRole
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.catalog import TemplateCatalog
from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

from .m2a_material_source_authority_fixture import (
    INCOMPATIBLE_RESOURCE_TEMPLATE_UUID,
    StaticMaterialSourceAuthority,
    default_material_source_authority,
)
from .test_m2a_material_source_direct_graph import _legal_source
from .test_m2a_material_source_vertical_slice import (
    AUTHORITY,
    MATERIAL_SOURCE_NODE_UUID,
    PLATE_RESOURCE_TEMPLATE_UUID,
    WORKFLOW_UUID,
    _catalog_imports,
    _StaticResourceTemplateIdentityIndex,
)

PROBE_MODULE = "m2a_identity_probe.resources"
PROBE_SOURCE_IDENTITY = f"{PROBE_MODULE}:plate_96"
OTHER_SOURCE_IDENTITY = f"{PROBE_MODULE}:other_plate"


class _CompileNonInverseIdentityIndex:
    def resolve_symbol(self, qualified_name: str) -> str:
        if qualified_name != PROBE_SOURCE_IDENTITY:
            raise KeyError(qualified_name)
        return PLATE_RESOURCE_TEMPLATE_UUID

    def identify_uuid(self, resource_template_uuid: str) -> str:
        if resource_template_uuid != PLATE_RESOURCE_TEMPLATE_UUID:
            raise KeyError(resource_template_uuid)
        return OTHER_SOURCE_IDENTITY


class _GenerateNonInverseIdentityIndex:
    def resolve_symbol(self, qualified_name: str) -> str:
        if qualified_name != PROBE_SOURCE_IDENTITY:
            raise KeyError(qualified_name)
        return INCOMPATIBLE_RESOURCE_TEMPLATE_UUID

    def identify_uuid(self, resource_template_uuid: str) -> str:
        if resource_template_uuid != PLATE_RESOURCE_TEMPLATE_UUID:
            raise KeyError(resource_template_uuid)
        return PROBE_SOURCE_IDENTITY


class _InverseProbeIdentityIndex:
    def resolve_symbol(self, qualified_name: str) -> str:
        if qualified_name != PROBE_SOURCE_IDENTITY:
            raise KeyError(qualified_name)
        return PLATE_RESOURCE_TEMPLATE_UUID

    def identify_uuid(self, resource_template_uuid: str) -> str:
        if resource_template_uuid != PLATE_RESOURCE_TEMPLATE_UUID:
            raise KeyError(resource_template_uuid)
        return PROBE_SOURCE_IDENTITY


@dataclass
class _HardeningContext:
    store: WorkflowStore
    engine: WorkflowAuthoringEngine
    service: WorkflowService
    applied_graph: dict[str, Any]
    authority: StaticMaterialSourceAuthority


@contextmanager
def _opened_context(
    database_path: Path,
    *,
    identity_index: Any,
) -> Iterator[_HardeningContext]:
    store = WorkflowStore(database_path)
    try:
        catalog = TemplateCatalog(store)
        catalog.replace(AUTHORITY, _catalog_imports())
        material_authority = default_material_source_authority()
        engine = WorkflowAuthoringEngine(
            catalog=catalog,
            authority=AUTHORITY,
            resource_template_identity_index=identity_index,
            material_source_authority=material_authority,
        )
        service = WorkflowService(
            store,
            compiler=engine,
            material_source_authority=material_authority,
        )
        service.create_workflow(
            workflow_uuid=WORKFLOW_UUID,
            name="M2A hardening",
            tags=[],
            description=None,
            meta_data={},
        )
        yield _HardeningContext(
            store=store,
            engine=engine,
            service=service,
            applied_graph=service.get_graph(WORKFLOW_UUID),
            authority=material_authority,
        )
    finally:
        store.close()


def _compile(
    context: _HardeningContext,
    source: str,
) -> CandidateCompilation:
    return context.engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=1,
        python_source=source,
        source_uri="package://m2a_identity_probe/workflows/hardening.py",
        applied_graph=context.applied_graph,
    )


def _probe_source() -> str:
    return (
        _legal_source()
        .replace(
            "from lab.resources import corning_96_well_plate",
            "from m2a_identity_probe.resources import plate_96",
        )
        .replace(
            "resource_template=corning_96_well_plate",
            "resource_template=plate_96",
        )
    )


def _assert_probe_module_was_not_imported() -> None:
    assert "m2a_identity_probe" not in sys.modules
    assert PROBE_MODULE not in sys.modules


def test_public_chinese_material_flow_role_catalog_is_immutable_and_complete() -> None:
    labels = getattr(authoring_api, "MATERIAL_FLOW_ROLE_LABELS_ZH", None)

    assert isinstance(labels, Mapping)
    assert dict(labels) == {
        "primary_sample": "主样品",
        "aliquot_sample": "分装样品",
        "reagent": "试剂",
        "consumable": "耗材",
    }
    assert {role.value for role in MaterialFlowRole} == set(labels)
    assert all(type(key) is str for key in labels)
    assert "MATERIAL_FLOW_ROLE_LABELS_ZH" in authoring_api.__all__
    with pytest.raises(TypeError):
        labels["primary_sample"] = "changed"  # type: ignore[index]


def test_compile_rejects_a_non_inverse_resource_template_identity_index(
    tmp_path: Path,
) -> None:
    _assert_probe_module_was_not_imported()
    with _opened_context(
        tmp_path / "workflow.db",
        identity_index=_CompileNonInverseIdentityIndex(),
    ) as context:
        result = _compile(context, _probe_source())

    assert not result.valid
    assert result.graph is None
    assert [item["code"] for item in result.diagnostics] == [
        "template_catalog_mismatch"
    ]
    _assert_probe_module_was_not_imported()


def test_generate_rejects_a_non_inverse_resource_template_identity_index(
    tmp_path: Path,
) -> None:
    _assert_probe_module_was_not_imported()
    with _opened_context(
        tmp_path / "workflow.db",
        identity_index=_InverseProbeIdentityIndex(),
    ) as context:
        compiled = _compile(context, _probe_source())
        assert compiled.valid, compiled.diagnostics
        assert compiled.graph is not None
        mismatched_engine = WorkflowAuthoringEngine(
            catalog=TemplateCatalog(context.store),
            authority=AUTHORITY,
            resource_template_identity_index=_GenerateNonInverseIdentityIndex(),
            material_source_authority=context.authority,
        )

        result = mismatched_engine.generate_python(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=1,
            graph=compiled.graph,
            source_uri="package://m2a_identity_probe/workflows/hardening.py",
        )

    assert not result.valid
    assert result.graph is None
    assert [item["code"] for item in result.diagnostics] == [
        "template_catalog_mismatch"
    ]
    _assert_probe_module_was_not_imported()


@pytest.mark.parametrize(
    "malformed_mount",
    [
        pytest.param(None, id="missing-mount"),
        pytest.param("not-an-object", id="mount-is-not-object"),
    ],
)
def test_direct_save_maps_malformed_selector_without_leaking_assertion(
    tmp_path: Path,
    malformed_mount: str | None,
) -> None:
    with _opened_context(
        tmp_path / "workflow.db",
        identity_index=_StaticResourceTemplateIdentityIndex(),
    ) as context:
        compiled = _compile(context, _legal_source())
        assert compiled.valid, compiled.diagnostics
        assert compiled.graph is not None
        graph = deepcopy(compiled.graph)
        material_source = next(
            item for item in graph["nodes"] if item["uuid"] == MATERIAL_SOURCE_NODE_UUID
        )
        if malformed_mount is None:
            material_source["param"].pop("mount")
        else:
            material_source["param"]["mount"] = malformed_mount
        before = context.service.get_graph(WORKFLOW_UUID)

        with pytest.raises(WorkflowError) as caught:
            context.service.save_graph(
                WORKFLOW_UUID,
                revision=1,
                nodes=graph["nodes"],
                edges=graph["edges"],
            )

        assert caught.value.code == "invalid_material_source"
        assert context.service.get_graph(WORKFLOW_UUID) == before
        assert before == context.applied_graph
        assert before["workflow"]["revision"] == 1
