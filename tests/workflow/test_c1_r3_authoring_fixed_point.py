"""C1 R3 Composite canonical Python 与 breaking pin 的公开 RED。"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.composite import (
    CompositeAuthoring,
    PublishedWorkflowSource,
    project_published_workflow_contract,
)
from unilabos.workflow.models import WorkflowEdgeWrite, WorkflowNodeWrite

from .c1_r2_static_expansion_fixture import (
    AUTHORITY,
    HOST_RESOURCE_TEMPLATE_UUID,
    INVOCATION_UUID,
    MATERIAL_TEMPLATE_B_UUID,
    PARENT_WORKFLOW_UUID,
    make_direct_world,
    resource_slot_schema,
    scalar_schema,
)
from .m2a_material_source_authority_fixture import (
    StaticMaterialSourceAuthority,
    material_record,
)

CHILD_MODULE = "tests.c1_r2.child"
CHILD_SYMBOL = "child"
RESOURCE_SYMBOL = "tests.c1_r3.resources:material_b"
OTHER_CONTRACT_DIGEST = "sha256:" + "f" * 64
FIXED_COMPOSITE_RESOURCE_UUID = "52000000-0000-4000-8000-000000000001"
FIXED_COMPOSITE_RESOURCE_ID = "fixed_composite_warehouse"


class _NamedResourceAuthority(StaticMaterialSourceAuthority):
    def resolve_material_ref(
        self,
        resource_id: str,
        *,
        uow: object | None = None,
    ) -> Any:
        if resource_id != FIXED_COMPOSITE_RESOURCE_ID:
            raise LookupError(resource_id)
        return self.get_material(FIXED_COMPOSITE_RESOURCE_UUID, uow=uow)


class _ResourceTemplateIdentityIndex:
    """为 normalized Python 提供唯一的测试 ResourceTemplate identity。"""

    def resolve_symbol(self, qualified_name: str) -> str:
        if qualified_name != RESOURCE_SYMBOL:
            raise LookupError(qualified_name)
        return MATERIAL_TEMPLATE_B_UUID

    def identify_uuid(self, resource_template_uuid: str) -> str:
        if resource_template_uuid != MATERIAL_TEMPLATE_B_UUID:
            raise LookupError(resource_template_uuid)
        return RESOURCE_SYMBOL


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _semantic_graph(graph: dict[str, Any]) -> dict[str, Any]:
    return {
        "workflow": {
            "uuid": graph["workflow"]["uuid"],
            "name": graph["workflow"]["name"],
            "description": graph["workflow"].get("description"),
            "meta_data": _plain(graph["workflow"]["meta_data"]),
        },
        "nodes": sorted(
            (
                WorkflowNodeWrite.model_validate(node).model_dump()
                for node in graph["nodes"]
            ),
            key=lambda node: node["uuid"],
        ),
        "edges": sorted(
            (
                WorkflowEdgeWrite.model_validate(edge).model_dump()
                for edge in graph["edges"]
            ),
            key=lambda edge: edge["uuid"],
        ),
        "node_templates": sorted(
            (_plain(template) for template in graph["node_templates"]),
            key=lambda template: template["uuid"],
        ),
        "handle_templates": sorted(
            (_plain(handle) for handle in graph["handle_templates"]),
            key=lambda handle: handle["uuid"],
        ),
    }


def _source() -> str:
    return f'''from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import workflow_definition
from {CHILD_MODULE} import {CHILD_SYMBOL}


@workflow_definition(
    workflow_uuid="{PARENT_WORKFLOW_UUID}",
    displayname="parent",
)
def parent(*, value: ResourceSlot) -> None:
    # unilab:node_uuid={INVOCATION_UUID}
    result = {CHILD_SYMBOL}(value=value)
'''


def _opened_engine(
    tmp_path: Path,
    *,
    material_source_authority: object | None = None,
) -> tuple[Any, WorkflowAuthoringEngine]:
    world = make_direct_world(
        tmp_path,
        input_schema=resource_slot_schema(MATERIAL_TEMPLATE_B_UUID),
        output_schema=resource_slot_schema(MATERIAL_TEMPLATE_B_UUID),
    )
    composite = CompositeAuthoring(
        store=world.store,
        catalog=world.catalog,
        authority=AUTHORITY,
        resolver=world.resolver,
    )
    engine = WorkflowAuthoringEngine(
        catalog=world.catalog,
        authority=AUTHORITY,
        resource_template_identity_index=_ResourceTemplateIdentityIndex(),
        composite_authoring=composite,
        material_source_authority=material_source_authority,
    )
    return world, engine


def _compile(
    engine: WorkflowAuthoringEngine,
    source: str,
    graph: dict[str, Any],
) -> Any:
    return engine.compile(
        workflow_uuid=PARENT_WORKFLOW_UUID,
        workflow_revision=1,
        python_source=source,
        source_uri="package://tests/c1_r3/workflows/parent.py",
        applied_graph=graph,
    )


def test_absolute_published_workflow_call_is_a_canonical_fixed_point(
    tmp_path: Path,
) -> None:
    world, engine = _opened_engine(tmp_path)
    try:
        assert CHILD_MODULE not in sys.modules

        compiled = _compile(
            engine, _source(), world.store.get_graph(PARENT_WORKFLOW_UUID)
        )

        assert compiled.valid and compiled.graph is not None, compiled.diagnostics
        normalized = compiled.normalized_python_source
        assert normalized is not None
        assert f"from {CHILD_MODULE} import {CHILD_SYMBOL}" in normalized
        assert f"result = {CHILD_SYMBOL}(value=value)" in normalized
        assert "FixtureAction" not in normalized
        assert CHILD_MODULE not in sys.modules
        assert (
            compiled.template_catalog_fingerprint
            == world.catalog_snapshot()["fingerprint"]
        )
        assert [entry["workflow_node_uuid"] for entry in compiled.source_map] == [
            INVOCATION_UUID
        ]

        invocation = next(
            node for node in compiled.graph["nodes"] if node["uuid"] == INVOCATION_UUID
        )
        internal = [
            node for node in compiled.graph["nodes"] if node["uuid"] != INVOCATION_UUID
        ]
        assert internal and all(
            node["parent_uuid"] == INVOCATION_UUID for node in internal
        )
        composite = invocation["meta_data"]["unilab"]["composite"]
        assert composite["child_workflow_uuid"] == world.child.source.workflow_uuid
        assert (
            composite["contract_digest"] == world.child.contract_pin["contract_digest"]
        )
        assert composite["target_mappings"]
        assert composite["source_mappings"]
        assert composite["structural_mappings"]["entry_targets"]
        assert composite["structural_mappings"]["completion_sources"]
        parameter = compiled.graph["workflow"]["meta_data"]["unilab"]["input_contract"][
            "parameters"
        ][0]
        assert parameter["schema"] == resource_slot_schema(MATERIAL_TEMPLATE_B_UUID)

        recompiled = _compile(engine, normalized, compiled.graph)

        assert recompiled.valid and recompiled.graph is not None, recompiled.diagnostics
        assert _semantic_graph(recompiled.graph) == _semantic_graph(compiled.graph)
        assert recompiled.normalized_python_source == normalized
        assert recompiled.source_map == compiled.source_map
        assert recompiled.template_catalog_fingerprint == (
            compiled.template_catalog_fingerprint
        )
        assert CHILD_MODULE not in sys.modules
    finally:
        world.close()


def test_published_workflow_resource_ref_is_preserved_as_canonical_source(
    tmp_path: Path,
) -> None:
    material_source_authority = _NamedResourceAuthority(
        materials=(
            material_record(
                FIXED_COMPOSITE_RESOURCE_UUID,
                resource_template_uuid=MATERIAL_TEMPLATE_B_UUID,
            ),
        ),
        sites=(),
    )
    world, engine = _opened_engine(
        tmp_path,
        material_source_authority=material_source_authority,
    )
    source = f'''from unilabos.workflow.authoring import resource_ref, workflow_definition
from {CHILD_MODULE} import {CHILD_SYMBOL}


@workflow_definition(
    workflow_uuid="{PARENT_WORKFLOW_UUID}",
    displayname="fixed-resource parent",
)
def fixed_resource_parent() -> None:
    # unilab:node_uuid={INVOCATION_UUID}
    result = {CHILD_SYMBOL}(value=resource_ref("{FIXED_COMPOSITE_RESOURCE_ID}"))
'''
    try:
        compiled = _compile(
            engine,
            source,
            world.store.get_graph(PARENT_WORKFLOW_UUID),
        )

        assert compiled.valid and compiled.graph is not None, compiled.diagnostics
        invocation = next(
            node for node in compiled.graph["nodes"] if node["uuid"] == INVOCATION_UUID
        )
        target_handle = next(
            handle
            for handle in compiled.graph["handle_templates"]
            if handle["workflow_node_template_uuid"]
            == invocation["workflow_node_template_uuid"]
            and handle["io_type"] == "target"
            and handle["data_key"] == "value"
        )
        assert invocation["param"]["value"] == {
            "uuid": FIXED_COMPOSITE_RESOURCE_UUID,
            "resource_template_uuid": MATERIAL_TEMPLATE_B_UUID,
        }
        assert invocation["meta_data"]["unilab"]["resource_refs"] == {
            target_handle["uuid"]: {"resource_id": FIXED_COMPOSITE_RESOURCE_ID}
        }
        normalized = compiled.normalized_python_source
        assert normalized is not None
        assert (
            f'{CHILD_SYMBOL}(value=resource_ref(\'{FIXED_COMPOSITE_RESOURCE_ID}\'))'
            in normalized
        )

        recompiled = _compile(engine, normalized, compiled.graph)

        assert recompiled.valid and recompiled.graph is not None, recompiled.diagnostics
        assert _semantic_graph(recompiled.graph) == _semantic_graph(compiled.graph)
        assert recompiled.normalized_python_source == normalized
    finally:
        world.close()


def test_publishing_parent_ignores_expanded_child_private_input_bindings(
    tmp_path: Path,
) -> None:
    world = make_direct_world(
        tmp_path,
        input_schema=scalar_schema(),
        output_schema=scalar_schema(),
    )
    composite = CompositeAuthoring(
        store=world.store,
        catalog=world.catalog,
        authority=AUTHORITY,
        resolver=world.resolver,
    )
    engine = WorkflowAuthoringEngine(
        catalog=world.catalog,
        authority=AUTHORITY,
        composite_authoring=composite,
    )
    source = f'''from unilabos.workflow.authoring import workflow_definition
from {CHILD_MODULE} import {CHILD_SYMBOL}


@workflow_definition(
    workflow_uuid="{PARENT_WORKFLOW_UUID}",
    displayname="literal parent",
)
def literal_parent() -> None:
    # unilab:node_uuid={INVOCATION_UUID}
    result = {CHILD_SYMBOL}(value=1.0)
'''
    parent_source = PublishedWorkflowSource(
        workflow_uuid=PARENT_WORKFLOW_UUID,
        definition_fqid="tests.c1_r3.literal_parent",
        module="tests.c1_r3",
        symbol="literal_parent",
        package_catalog_digest="sha256:" + "a" * 64,
        definition_content_hash="sha256:" + "b" * 64,
    )
    try:
        compiled = _compile(
            engine,
            source,
            world.store.get_graph(PARENT_WORKFLOW_UUID),
        )
        assert compiled.valid and compiled.graph is not None, compiled.diagnostics
        applied_snapshot = deepcopy(compiled.graph)
        applied_snapshot["workflow"]["revision"] = 1
        applied_snapshot["applied_source"] = {
            "workflow_revision": 1,
            "source_hash": "sha256:" + "c" * 64,
        }

        projected = project_published_workflow_contract(
            source=parent_source,
            applied_snapshot=applied_snapshot,
            host_node_resource_template_uuid=HOST_RESOURCE_TEMPLATE_UUID,
        )

        assert projected is not None
    finally:
        world.close()


def test_breaking_child_pin_fails_closed_at_the_authoring_compile_seam(
    tmp_path: Path,
) -> None:
    world, engine = _opened_engine(tmp_path)
    try:
        compiled = _compile(
            engine, _source(), world.store.get_graph(PARENT_WORKFLOW_UUID)
        )
        assert compiled.valid and compiled.graph is not None, compiled.diagnostics
        stale = deepcopy(compiled.graph)
        invocation = next(
            node for node in stale["nodes"] if node["uuid"] == INVOCATION_UUID
        )
        pin = invocation["meta_data"]["unilab"]["composite"]
        assert pin["contract_digest"] != OTHER_CONTRACT_DIGEST
        pin["contract_digest"] = OTHER_CONTRACT_DIGEST

        rejected = _compile(engine, _source(), stale)

        assert not rejected.valid
        assert rejected.graph is None
        assert [item["code"] for item in rejected.diagnostics] == [
            "composite_contract_stale"
        ]
    finally:
        world.close()
