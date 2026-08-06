"""M2A D-067 ResourceSlot producer/consumer 模板兼容 public RED。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.catalog import NodeTemplateImport, TemplateCatalog
from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

from .m2a_material_source_authority_fixture import (
    INCOMPATIBLE_RESOURCE_TEMPLATE_UUID,
    PLATE_RESOURCE_TEMPLATE_UUID,
    default_material_source_authority,
)
from .test_m2a_material_source_vertical_slice import (
    AUTHORITY,
    MATERIAL_SOURCE_NODE_UUID,
    PREPARE_TEMPLATE_UUID,
    SAMPLE_HANDLE_UUID,
    WORKFLOW_UUID,
    _catalog_imports,
    _StaticResourceTemplateIdentityIndex,
)
from .test_m2a_resource_slot_material_chain import (
    MIDDLE_SAMPLE_SOURCE_UUID,
    MIDDLE_TEMPLATE_UUID,
    _middle_action_import,
    _source,
)


@dataclass
class _CompatibilityContext:
    store: WorkflowStore
    catalog: TemplateCatalog
    engine: WorkflowAuthoringEngine
    service: WorkflowService
    applied_graph: dict[str, Any]


def _set_handle_allowlist(
    handle: dict[str, Any],
    allowlist: tuple[str, ...],
) -> None:
    unilab = handle["meta_data"].setdefault("unilab", {})
    unilab["allowed_resource_template_uuids"] = list(allowlist)


def _imports_with_prepare_allowlist(
    allowlist: tuple[str, ...] | None,
) -> list[NodeTemplateImport]:
    imports = deepcopy(_catalog_imports())
    if allowlist is None:
        return imports
    prepare = next(
        item for item in imports if item.template["uuid"] == PREPARE_TEMPLATE_UUID
    )
    assert isinstance(prepare.handles, list)
    target = next(
        item for item in prepare.handles if item["uuid"] == SAMPLE_HANDLE_UUID
    )
    _set_handle_allowlist(target, allowlist)
    return imports


def _ordinary_action_imports() -> list[NodeTemplateImport]:
    imports = _imports_with_prepare_allowlist((PLATE_RESOURCE_TEMPLATE_UUID,))
    middle = _middle_action_import()
    # 设备模板故意与业务物料模板不同；D-067 不能读取这个字段作为 producer S。
    middle.template["resource_template_uuid"] = INCOMPATIBLE_RESOURCE_TEMPLATE_UUID
    assert isinstance(middle.handles, list)
    source = next(
        item for item in middle.handles if item["uuid"] == MIDDLE_SAMPLE_SOURCE_UUID
    )
    _set_handle_allowlist(source, (PLATE_RESOURCE_TEMPLATE_UUID,))
    imports.append(middle)
    return imports


def _implicit_passthrough_imports() -> list[NodeTemplateImport]:
    imports = _imports_with_prepare_allowlist((PLATE_RESOURCE_TEMPLATE_UUID,))
    middle = _middle_action_import()
    assert isinstance(middle.handles, list)
    source = next(
        item for item in middle.handles if item["uuid"] == MIDDLE_SAMPLE_SOURCE_UUID
    )
    source["meta_data"] = {
        "unilab": {
            "allowed_resource_template_uuids": None,
            "implicit_passthrough": True,
        }
    }
    imports.append(middle)
    return imports


@contextmanager
def _opened_context(
    database_path: Path,
    *,
    imports: list[NodeTemplateImport],
) -> Iterator[_CompatibilityContext]:
    store = WorkflowStore(database_path)
    try:
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
            name="ResourceSlot compatibility",
            tags=[],
            description=None,
            meta_data={},
        )
        yield _CompatibilityContext(
            store=store,
            catalog=catalog,
            engine=engine,
            service=service,
            applied_graph=service.get_graph(WORKFLOW_UUID),
        )
    finally:
        store.close()


def _compile(
    context: _CompatibilityContext,
    source: str,
    *,
    applied_graph: dict[str, Any] | None = None,
) -> CandidateCompilation:
    return context.engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=1,
        python_source=source,
        source_uri="package://lab/workflows/m2a_template_compatibility.py",
        applied_graph=(
            context.applied_graph if applied_graph is None else applied_graph
        ),
    )


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _reproject_catalog(
    graph: dict[str, Any],
    catalog: TemplateCatalog,
) -> dict[str, Any]:
    candidate = deepcopy(graph)
    referenced = {item["workflow_node_template_uuid"] for item in candidate["nodes"]}
    with catalog.snapshot(AUTHORITY) as snapshot:
        candidate["node_templates"] = [
            _plain(item)
            for item in snapshot.node_templates
            if item["uuid"] in referenced
        ]
        candidate["handle_templates"] = [
            _plain(item)
            for item in snapshot.handle_templates
            if item["workflow_node_template_uuid"] in referenced
        ]
    return candidate


def _unconstrained_single_consumer_candidate(database_path: Path) -> dict[str, Any]:
    source = _source(pass_through=False)
    with _opened_context(
        database_path,
        imports=_imports_with_prepare_allowlist(None),
    ) as context:
        compiled = _compile(context, source)
        assert compiled.valid, compiled.diagnostics
        assert compiled.graph is not None
        return deepcopy(compiled.graph)


@pytest.mark.parametrize(
    "target_allowlist",
    [
        pytest.param(None, id="missing-is-universal"),
        pytest.param((), id="empty-is-universal"),
        pytest.param(
            (
                PLATE_RESOURCE_TEMPLATE_UUID,
                INCOMPATIBLE_RESOURCE_TEMPLATE_UUID,
            ),
            id="producer-template-contained",
        ),
    ],
)
def test_material_source_template_guarantee_can_feed_a_compatible_target(
    tmp_path: Path,
    target_allowlist: tuple[str, ...] | None,
) -> None:
    source = _source(pass_through=False)
    with _opened_context(
        tmp_path / "workflow.db",
        imports=_imports_with_prepare_allowlist(target_allowlist),
    ) as context:
        compiled = _compile(context, source)
        assert compiled.valid, compiled.diagnostics
        assert compiled.graph is not None
        material_source = next(
            item
            for item in compiled.graph["nodes"]
            if item["uuid"] == MATERIAL_SOURCE_NODE_UUID
        )
        assert material_source["param"]["resource_template_uuid"] == (
            PLATE_RESOURCE_TEMPLATE_UUID
        )

        generated = context.engine.generate_python(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=1,
            graph=compiled.graph,
            source_uri="package://lab/workflows/m2a_template_compatibility.py",
        )
        assert generated.valid, generated.diagnostics
        assert generated.normalized_python_source is not None
        validated = context.engine.validate(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=1,
            graph=compiled.graph,
            python_source=generated.normalized_python_source,
            source_uri="package://lab/workflows/m2a_template_compatibility.py",
        )

    assert validated.valid, validated.diagnostics
    assert validated.graph == compiled.graph


@pytest.mark.parametrize("public_seam", ["generate_python", "validate"])
def test_material_source_rejects_a_target_that_excludes_its_template(
    tmp_path: Path,
    public_seam: str,
) -> None:
    source = _source(pass_through=False)
    base = _unconstrained_single_consumer_candidate(tmp_path / "builder.db")
    with _opened_context(
        tmp_path / "workflow.db",
        imports=_imports_with_prepare_allowlist((INCOMPATIBLE_RESOURCE_TEMPLATE_UUID,)),
    ) as context:
        incompatible = _reproject_catalog(base, context.catalog)
        if public_seam == "generate_python":
            result = context.engine.generate_python(
                workflow_uuid=WORKFLOW_UUID,
                workflow_revision=1,
                graph=incompatible,
                source_uri="package://lab/workflows/m2a_template_compatibility.py",
            )
        else:
            result = context.engine.validate(
                workflow_uuid=WORKFLOW_UUID,
                workflow_revision=1,
                graph=incompatible,
                python_source=source,
                source_uri="package://lab/workflows/m2a_template_compatibility.py",
            )

    assert not result.valid
    assert result.graph is None
    assert result.normalized_python_source is None
    assert [item["code"] for item in result.diagnostics] == ["material_source_conflict"]


def test_direct_save_rejects_incompatible_material_source_atomically(
    tmp_path: Path,
) -> None:
    base = _unconstrained_single_consumer_candidate(tmp_path / "builder.db")
    with _opened_context(
        tmp_path / "workflow.db",
        imports=_imports_with_prepare_allowlist((INCOMPATIBLE_RESOURCE_TEMPLATE_UUID,)),
    ) as context:
        before = context.service.get_graph(WORKFLOW_UUID)

        with pytest.raises(WorkflowError) as caught:
            context.service.save_graph(
                WORKFLOW_UUID,
                revision=1,
                nodes=base["nodes"],
                edges=base["edges"],
            )

        assert caught.value.code == "material_source_conflict"
        assert context.service.get_graph(WORKFLOW_UUID) == before
        assert before == context.applied_graph
        assert before["workflow"]["revision"] == 1


def test_action_output_contract_not_executor_template_proves_compatibility(
    tmp_path: Path,
) -> None:
    source = _source(pass_through=True)
    with _opened_context(
        tmp_path / "workflow.db",
        imports=_ordinary_action_imports(),
    ) as context:
        compiled = _compile(context, source)
        assert compiled.valid, compiled.diagnostics
        assert compiled.graph is not None

        middle_template = next(
            item
            for item in compiled.graph["node_templates"]
            if item["uuid"] == MIDDLE_TEMPLATE_UUID
        )
        middle_output = next(
            item
            for item in compiled.graph["handle_templates"]
            if item["uuid"] == MIDDLE_SAMPLE_SOURCE_UUID
        )
        final_target = next(
            item
            for item in compiled.graph["handle_templates"]
            if item["uuid"] == SAMPLE_HANDLE_UUID
        )
        assert middle_template["resource_template_uuid"] == (
            INCOMPATIBLE_RESOURCE_TEMPLATE_UUID
        )
        assert middle_output["meta_data"]["unilab"][
            "allowed_resource_template_uuids"
        ] == [PLATE_RESOURCE_TEMPLATE_UUID]
        assert final_target["meta_data"]["unilab"][
            "allowed_resource_template_uuids"
        ] == [PLATE_RESOURCE_TEMPLATE_UUID]

        generated = context.engine.generate_python(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=1,
            graph=compiled.graph,
            source_uri="package://lab/workflows/m2a_template_compatibility.py",
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


def test_implicit_resource_slot_passthrough_preserves_upstream_template_guarantee(
    tmp_path: Path,
) -> None:
    source = _source(pass_through=True)
    with _opened_context(
        tmp_path / "workflow.db",
        imports=_implicit_passthrough_imports(),
    ) as context:
        compiled = _compile(context, source)

    assert compiled.valid, compiled.diagnostics
    assert compiled.graph is not None
