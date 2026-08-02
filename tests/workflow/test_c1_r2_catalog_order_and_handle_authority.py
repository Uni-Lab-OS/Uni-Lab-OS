"""C1 R2 真实 Catalog JSON 往返后的 Published Workflow 权威测试。"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.catalog import (
    CatalogAuthority,
    NodeTemplateImport,
    TemplateCatalog,
)
from unilabos.workflow.composite import (
    CompositeAuthoring,
    CompositeCatalogMismatch,
    PublishedWorkflowCatalogPublisher,
    PublishedWorkflowSource,
    project_published_workflow_contract,
)
from unilabos.workflow.models import WorkflowNodeWrite
from unilabos.workflow.store import WorkflowStore

from .c1_r2_static_expansion_fixture import (
    CHILD_WORKFLOW_UUID,
    HOST_RESOURCE_TEMPLATE_UUID,
    LEAF_READY_TARGET_UUID,
    LEAF_TEMPLATE_UUID,
    LEAF_VALUE_TARGET_UUID,
    MATERIAL_TEMPLATE_A_UUID,
    MATERIAL_TEMPLATE_B_UUID,
    MemoryPublishedWorkflowResolver,
    _mark_applied,
    _seed_hierarchical_graph,
    action_import,
    action_node,
    make_nested_resource_world,
    plain,
    resource_slot_schema,
    scalar_schema,
)

ORDER_AUTHORITY = CatalogAuthority(authority_id="os-c1-r2-order", kind="local")
ORDER_HOST_RESOURCE_TEMPLATE_UUID = "81000000-0000-4000-8000-000000000001"
ORDER_LEAF_WORKFLOW_UUID = "81000000-0000-4000-8000-000000000002"
ORDER_OUTER_WORKFLOW_UUID = "81000000-0000-4000-8000-000000000003"
ORDER_PARENT_WORKFLOW_UUID = "81000000-0000-4000-8000-000000000004"
ORDER_INVOCATION_UUID = "81000000-0000-4000-8000-000000000005"

SLOT_ACTION_RESOURCE_UUID = "82000000-0000-4000-8000-000000000001"
SCALAR_ACTION_RESOURCE_UUID = "82000000-0000-4000-8000-000000000002"
SLOT_ACTION_TEMPLATE_UUID = "83000000-0000-4000-8000-000000000001"
SCALAR_ACTION_TEMPLATE_UUID = "83000000-0000-4000-8000-000000000002"
SLOT_ACTION_TARGET_UUID = "84000000-0000-4000-8000-000000000001"
SLOT_ACTION_SOURCE_UUID = "84000000-0000-4000-8000-000000000002"
SLOT_ACTION_READY_TARGET_UUID = "84000000-0000-4000-8000-000000000003"
SLOT_ACTION_READY_SOURCE_UUID = "84000000-0000-4000-8000-000000000004"
SCALAR_ACTION_TARGET_UUID = "84000000-0000-4000-8000-000000000005"
SCALAR_ACTION_SOURCE_UUID = "84000000-0000-4000-8000-000000000006"
SCALAR_ACTION_READY_TARGET_UUID = "84000000-0000-4000-8000-000000000007"
SCALAR_ACTION_READY_SOURCE_UUID = "84000000-0000-4000-8000-000000000008"
SLOT_ACTION_NODE_UUID = "85000000-0000-4000-8000-000000000001"
SCALAR_ACTION_NODE_UUID = "85000000-0000-4000-8000-000000000002"
NESTED_NODE_UUID = "85000000-0000-4000-8000-000000000003"


def _digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def _source(workflow_uuid: str, name: str) -> PublishedWorkflowSource:
    return PublishedWorkflowSource(
        workflow_uuid=workflow_uuid,
        definition_fqid=f"tests.c1_r2.{name}",
        module=f"tests.c1_r2.{name}",
        symbol=name,
        package_catalog_digest=_digest(f"package:{name}"),
        definition_content_hash=_digest(f"definition:{name}"),
    )


def _workflow_meta(
    *,
    inputs: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    output_bindings: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "unilab": {
            "input_contract": {"version": 1, "parameters": inputs},
            "output_contract": {"version": 1, "outputs": outputs},
            "output_bindings": output_bindings,
            "composition_allow_transparent": False,
        }
    }


def _create_applied_workflow(
    store: WorkflowStore,
    *,
    workflow_uuid: str,
    name: str,
    meta_data: dict[str, Any],
    nodes: list[WorkflowNodeWrite],
    hierarchical: bool = False,
) -> None:
    store.create_workflow(
        workflow_uuid=workflow_uuid,
        name=name,
        tags=["c1-r2-order"],
        description=None,
        meta_data=meta_data,
    )
    saved = (
        _seed_hierarchical_graph(store, workflow_uuid, nodes, [])
        if hierarchical
        else store.save_graph(
            workflow_uuid,
            revision=1,
            nodes=nodes,
            edges=[],
        )
    )
    _mark_applied(store, workflow_uuid, saved["workflow"]["revision"])


def _published_template(snapshot: Any, workflow_uuid: str) -> dict[str, Any]:
    return next(
        plain(template)
        for template in snapshot.node_templates
        if template["name"] == f"workflow:{workflow_uuid}"
    )


def _owned_handles(snapshot: Any, template_uuid: str) -> list[dict[str, Any]]:
    return [
        plain(handle)
        for handle in snapshot.handle_templates
        if handle["workflow_node_template_uuid"] == template_uuid
    ]


def _local_import(item: NodeTemplateImport) -> NodeTemplateImport:
    template = plain(item.template)
    template.pop("uuid", None)
    handles = []
    for raw in item.handles:
        handle = plain(raw)
        handle.pop("uuid", None)
        handle.pop("workflow_node_template_uuid", None)
        handles.append(handle)
    return NodeTemplateImport(template=template, handles=tuple(handles))


@dataclass
class _OrderedWorld:
    store: WorkflowStore
    catalog: TemplateCatalog
    resolver: MemoryPublishedWorkflowResolver
    outer_source: PublishedWorkflowSource

    def close(self) -> None:
        self.store.close()


def _make_ordered_world(tmp_path: Path) -> _OrderedWorld:
    store = WorkflowStore(tmp_path / "workflow.db")
    catalog = TemplateCatalog(store)
    resolver = MemoryPublishedWorkflowResolver()
    slot_action = _local_import(
        action_import(
            template_uuid=SLOT_ACTION_TEMPLATE_UUID,
            resource_template_uuid=SLOT_ACTION_RESOURCE_UUID,
            target_handle_uuid=SLOT_ACTION_TARGET_UUID,
            source_handle_uuid=SLOT_ACTION_SOURCE_UUID,
            ready_target_uuid=SLOT_ACTION_READY_TARGET_UUID,
            ready_source_uuid=SLOT_ACTION_READY_SOURCE_UUID,
            value_schema=resource_slot_schema(MATERIAL_TEMPLATE_B_UUID),
        )
    )
    scalar_action = _local_import(
        action_import(
            template_uuid=SCALAR_ACTION_TEMPLATE_UUID,
            resource_template_uuid=SCALAR_ACTION_RESOURCE_UUID,
            target_handle_uuid=SCALAR_ACTION_TARGET_UUID,
            source_handle_uuid=SCALAR_ACTION_SOURCE_UUID,
            ready_target_uuid=SCALAR_ACTION_READY_TARGET_UUID,
            ready_source_uuid=SCALAR_ACTION_READY_SOURCE_UUID,
            value_schema=scalar_schema(),
        )
    )
    leaf_source = _source(ORDER_LEAF_WORKFLOW_UUID, "ordered_leaf")
    outer_source = _source(ORDER_OUTER_WORKFLOW_UUID, "ordered_outer")
    resolver.add(leaf_source)
    resolver.add(outer_source)
    try:
        base = PublishedWorkflowCatalogPublisher(
            catalog=catalog,
            authority=ORDER_AUTHORITY,
            store=store,
            sources=(),
            base_templates=(slot_action, scalar_action),
            host_node_resource_template_uuid=ORDER_HOST_RESOURCE_TEMPLATE_UUID,
        ).publish()
        slot_template = next(
            template
            for template in base.node_templates
            if template["name"] == f"action:{SLOT_ACTION_TEMPLATE_UUID}"
        )
        scalar_template = next(
            template
            for template in base.node_templates
            if template["name"] == f"action:{SCALAR_ACTION_TEMPLATE_UUID}"
        )
        slot_handles = _owned_handles(base, str(slot_template["uuid"]))
        scalar_handles = _owned_handles(base, str(scalar_template["uuid"]))
        slot_handle_by_io = {
            (handle["io_type"], handle["data_key"]): handle["uuid"]
            for handle in slot_handles
        }
        scalar_handle_by_io = {
            (handle["io_type"], handle["data_key"]): handle["uuid"]
            for handle in scalar_handles
        }
        _create_applied_workflow(
            store,
            workflow_uuid=ORDER_LEAF_WORKFLOW_UUID,
            name="ordered_leaf",
            meta_data=_workflow_meta(
                inputs=[
                    {
                        "name": "zeta",
                        "schema": resource_slot_schema(MATERIAL_TEMPLATE_B_UUID),
                        "required": True,
                    },
                    {"name": "alpha", "schema": scalar_schema(), "required": True},
                ],
                outputs=[
                    {
                        "name": "zeta",
                        "schema": resource_slot_schema(MATERIAL_TEMPLATE_B_UUID),
                        "implicit": True,
                    },
                    {"name": "omega", "schema": scalar_schema(), "implicit": False},
                    {"name": "beta", "schema": scalar_schema(), "implicit": False},
                ],
                output_bindings={
                    "zeta": {"kind": "workflow_input", "parameter": "zeta"},
                    "omega": {
                        "kind": "node_output",
                        "workflow_node_uuid": SCALAR_ACTION_NODE_UUID,
                        "source_handle_uuid": scalar_handle_by_io[("source", "result")],
                    },
                    "beta": {
                        "kind": "node_output",
                        "workflow_node_uuid": SCALAR_ACTION_NODE_UUID,
                        "source_handle_uuid": scalar_handle_by_io[("source", "result")],
                    },
                },
            ),
            nodes=[
                action_node(
                    node_uuid=SLOT_ACTION_NODE_UUID,
                    template_uuid=str(slot_template["uuid"]),
                    target_handle_uuid=slot_handle_by_io[("target", "value")],
                    parameter="zeta",
                ),
                action_node(
                    node_uuid=SCALAR_ACTION_NODE_UUID,
                    template_uuid=str(scalar_template["uuid"]),
                    target_handle_uuid=scalar_handle_by_io[("target", "value")],
                    parameter="alpha",
                ),
            ],
        )
        publisher = PublishedWorkflowCatalogPublisher(
            catalog=catalog,
            authority=ORDER_AUTHORITY,
            store=store,
            sources=(leaf_source,),
            base_templates=(slot_action, scalar_action),
            host_node_resource_template_uuid=ORDER_HOST_RESOURCE_TEMPLATE_UUID,
        )
        first = publisher.publish()
        leaf_template = _published_template(first, ORDER_LEAF_WORKFLOW_UUID)
        first_schema = leaf_template["schema"]
        first_extension = first_schema["x-unilabos-workflow-contract"]
        assert list(first_schema["properties"]["goal"]["properties"]) == [
            "alpha",
            "zeta",
        ]
        assert list(first_extension["input_order"]) == ["zeta", "alpha"]
        assert list(first_schema["properties"]["result"]["properties"]) == [
            "beta",
            "omega",
            "zeta",
        ]
        assert list(first_extension["output_order"]) == ["zeta", "omega", "beta"]
        leaf_handles = _owned_handles(first, str(leaf_template["uuid"]))
        handle_by_key = {
            (handle["io_type"], handle["data_key"]): handle["uuid"]
            for handle in leaf_handles
        }
        extension = leaf_template["schema"]["x-unilabos-workflow-contract"]
        nested = WorkflowNodeWrite(
            uuid=NESTED_NODE_UUID,
            workflow_node_template_uuid=str(leaf_template["uuid"]),
            parent_uuid=None,
            name="ordered-leaf",
            status="idle",
            type="workflow",
            pose={},
            param={},
            execution_policy={},
            disabled=False,
            minimized=False,
            meta_data={
                "unilab": {
                    "input_bindings": {
                        handle_by_key[("target", "zeta")]: {"parameter": "zeta"},
                        handle_by_key[("target", "alpha")]: {"parameter": "alpha"},
                    },
                    "composite": {
                        "version": 1,
                        "child_workflow_uuid": ORDER_LEAF_WORKFLOW_UUID,
                        "child_workflow_revision": extension["workflow_revision"],
                        "child_applied_source_hash": extension["applied_source_hash"],
                        "contract_digest": extension["contract_digest"],
                        "composition_allow_transparent": extension[
                            "composition_allow_transparent"
                        ],
                        "target_mappings": {},
                        "source_mappings": {},
                        "structural_mappings": {
                            "entry_targets": [],
                            "completion_sources": [],
                        },
                    },
                }
            },
        )
        _create_applied_workflow(
            store,
            workflow_uuid=ORDER_OUTER_WORKFLOW_UUID,
            name="ordered_outer",
            meta_data=_workflow_meta(
                inputs=[
                    {
                        "name": "zeta",
                        "schema": resource_slot_schema(),
                        "required": True,
                    },
                    {"name": "alpha", "schema": scalar_schema(), "required": True},
                ],
                outputs=[
                    {
                        "name": "zeta",
                        "schema": resource_slot_schema(),
                        "implicit": True,
                    }
                ],
                output_bindings={
                    "zeta": {"kind": "workflow_input", "parameter": "zeta"}
                },
            ),
            nodes=[nested],
            hierarchical=True,
        )
        publisher = PublishedWorkflowCatalogPublisher(
            catalog=catalog,
            authority=ORDER_AUTHORITY,
            store=store,
            sources=(leaf_source, outer_source),
            base_templates=(slot_action, scalar_action),
            host_node_resource_template_uuid=ORDER_HOST_RESOURCE_TEMPLATE_UUID,
        )
        publisher.publish()
        store.create_workflow(
            workflow_uuid=ORDER_PARENT_WORKFLOW_UUID,
            name="ordered_parent",
            tags=[],
            description=None,
            meta_data=_workflow_meta(
                inputs=[
                    {
                        "name": "zeta",
                        "schema": resource_slot_schema(),
                        "required": True,
                    },
                    {"name": "alpha", "schema": scalar_schema(), "required": True},
                ],
                outputs=[],
                output_bindings={},
            ),
        )
        return _OrderedWorld(
            store=store,
            catalog=catalog,
            resolver=resolver,
            outer_source=outer_source,
        )
    except BaseException:
        store.close()
        raise


def test_non_alphabetic_contract_order_survives_catalog_and_nested_compile(
    tmp_path: Path,
) -> None:
    world = _make_ordered_world(tmp_path)
    try:
        with world.catalog.snapshot(ORDER_AUTHORITY) as catalog_snapshot:
            leaf_template = _published_template(
                catalog_snapshot, ORDER_LEAF_WORKFLOW_UUID
            )
            schema = leaf_template["schema"]
            extension = schema["x-unilabos-workflow-contract"]
            assert list(schema["properties"]["goal"]["properties"]) == [
                "alpha",
                "zeta",
            ]
            assert list(extension["input_order"]) == ["zeta", "alpha"]
            assert list(schema["properties"]["result"]["properties"]) == [
                "beta",
                "omega",
                "zeta",
            ]
            assert list(extension["output_order"]) == ["zeta", "omega", "beta"]
        before = {
            "parent": plain(world.store.get_graph(ORDER_PARENT_WORKFLOW_UUID)),
            "outer": plain(world.store.get_graph(ORDER_OUTER_WORKFLOW_UUID)),
            "leaf": plain(world.store.get_graph(ORDER_LEAF_WORKFLOW_UUID)),
        }
        authoring = CompositeAuthoring(
            store=world.store,
            catalog=world.catalog,
            authority=ORDER_AUTHORITY,
            resolver=world.resolver,
        )

        expansion = authoring.compile_invocation(
            parent_workflow_uuid=ORDER_PARENT_WORKFLOW_UUID,
            invocation_uuid=ORDER_INVOCATION_UUID,
            module=world.outer_source.module,
            symbol=world.outer_source.symbol,
            keyword_arguments={
                "zeta": {"kind": "workflow_input", "parameter": "zeta"},
                "alpha": {"kind": "workflow_input", "parameter": "alpha"},
            },
        )

        assert plain(expansion.diagnostics) == []
        effective = plain(expansion.effective_parent_input_contract)
        assert [parameter["name"] for parameter in effective["parameters"]] == [
            "zeta",
            "alpha",
        ]
        assert effective["parameters"][0]["schema"] == resource_slot_schema(
            MATERIAL_TEMPLATE_B_UUID
        )
        assert effective["parameters"][1]["schema"] == scalar_schema()
        assert {
            "parent": plain(world.store.get_graph(ORDER_PARENT_WORKFLOW_UUID)),
            "outer": plain(world.store.get_graph(ORDER_OUTER_WORKFLOW_UUID)),
            "leaf": plain(world.store.get_graph(ORDER_LEAF_WORKFLOW_UUID)),
        } == before
    finally:
        world.close()


def _genuine_outer_projection_snapshot(tmp_path: Path) -> tuple[Any, dict[str, Any]]:
    world = make_nested_resource_world(
        tmp_path,
        parent_schema=resource_slot_schema(),
        direct_schema=resource_slot_schema(),
        leaf_schema=resource_slot_schema(MATERIAL_TEMPLATE_B_UUID),
    )
    return world, plain(
        world.store.get_published_workflow_snapshot(CHILD_WORKFLOW_UUID)
    )


def _mutate_genuine_leaf_authority(snapshot: dict[str, Any], case: str) -> None:
    template = next(
        item
        for item in snapshot["node_templates"]
        if item["uuid"] == LEAF_TEMPLATE_UUID
    )
    handles = [
        item
        for item in snapshot["handle_templates"]
        if item["workflow_node_template_uuid"] == LEAF_TEMPLATE_UUID
    ]
    target = next(item for item in handles if item["uuid"] == LEAF_VALUE_TARGET_UUID)
    ready = next(item for item in handles if item["uuid"] == LEAF_READY_TARGET_UUID)
    extension = template["schema"]["x-unilabos-workflow-contract"]
    provenance = template["meta_data"]["unilab"]["workflow_source"]
    target_unilab = target["meta_data"]["unilab"]
    ready_unilab = ready["meta_data"]["unilab"]
    assert target["type"] == "ResourceSlot"
    assert target["required"] is True
    assert target_unilab["editor_control"] == "material_port"
    assert target_unilab["allowed_resource_template_uuids"] == [
        MATERIAL_TEMPLATE_B_UUID
    ]
    assert ready["type"] == "boolean"
    assert ready["required"] is False
    assert ready_unilab["structural_role"] == "ready"
    if case == "template-name":
        template["name"] = f"{template['name']}-mutated"
    elif case == "template-class":
        template["class"] = f"{template['class']}_mutated"
    elif case == "extension-version":
        extension["version"] = 2
    elif case == "extension-extra-field":
        extension["unexpected"] = True
    elif case == "provenance-module":
        provenance["module"] = f"{provenance['module']}.mutated"
    elif case == "provenance-extra-field":
        provenance["unexpected"] = True
    elif case == "goal-property-missing":
        template["schema"]["properties"]["goal"]["properties"].pop("value")
    elif case == "goal-property-extra":
        template["schema"]["properties"]["goal"]["properties"]["rogue"] = (
            scalar_schema()
        )
    elif case == "result-property-missing":
        template["schema"]["properties"]["result"]["properties"].pop("value")
    elif case == "result-property-extra":
        template["schema"]["properties"]["result"]["properties"]["rogue"] = (
            scalar_schema()
        )
    elif case == "business-type":
        target["type"] = "boolean"
    elif case == "business-required":
        target["required"] = False
    elif case == "business-editor-control":
        target["meta_data"]["unilab"]["editor_control"] = "evil"
    elif case == "business-allowlist-mirror":
        target["meta_data"]["unilab"]["allowed_resource_template_uuids"] = [
            MATERIAL_TEMPLATE_A_UUID
        ]
    elif case == "business-unilab-extra":
        target["meta_data"]["unilab"]["unexpected"] = True
    elif case == "ready-unilab-extra":
        ready["meta_data"]["unilab"]["unexpected"] = True
    elif case == "ready-handle-extra":
        ready["unexpected"] = True
    elif case == "ready-required":
        ready["required"] = True
    else:  # pragma: no cover - 参数表有意保持封闭
        raise AssertionError(case)


GENUINE_SINGLE_FIELD_MUTATIONS = [
    "template-name",
    "template-class",
    "extension-version",
    "extension-extra-field",
    "provenance-module",
    "provenance-extra-field",
    "goal-property-missing",
    "goal-property-extra",
    "result-property-missing",
    "result-property-extra",
    "business-type",
    "business-required",
    "business-editor-control",
    "business-allowlist-mirror",
    "business-unilab-extra",
    "ready-unilab-extra",
    "ready-handle-extra",
    "ready-required",
]


@pytest.mark.parametrize("case", GENUINE_SINGLE_FIELD_MUTATIONS)
def test_genuine_published_workflow_single_field_mutation_loses_relaxation(
    tmp_path: Path,
    case: str,
) -> None:
    world, snapshot = _genuine_outer_projection_snapshot(tmp_path)
    try:
        _mutate_genuine_leaf_authority(snapshot, case)

        with pytest.raises(CompositeCatalogMismatch) as caught:
            project_published_workflow_contract(
                source=world.child.source,
                applied_snapshot=snapshot,
                host_node_resource_template_uuid=HOST_RESOURCE_TEMPLATE_UUID,
            )

        assert caught.value.code == "composite_catalog_mismatch"
        assert caught.value.path == "/published_workflow/io_contract"
    finally:
        world.close()


def test_genuine_catalog_projection_control_keeps_relaxation(tmp_path: Path) -> None:
    world, snapshot = _genuine_outer_projection_snapshot(tmp_path)
    try:
        projected = project_published_workflow_contract(
            source=world.child.source,
            applied_snapshot=deepcopy(snapshot),
            host_node_resource_template_uuid=HOST_RESOURCE_TEMPLATE_UUID,
        )

        assert projected is not None
        assert projected.template["name"] == f"workflow:{CHILD_WORKFLOW_UUID}"
    finally:
        world.close()
