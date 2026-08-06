"""C1 R2 D-064 嵌套 ResourceSlot 约束传导的公开 RED。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from .c1_r2_static_expansion_fixture import (
    CHILD_WORKFLOW_UUID,
    LEAF_TEMPLATE_UUID,
    LEAF_VALUE_TARGET_UUID,
    LEAF_WORKFLOW_UUID,
    MATERIAL_TEMPLATE_A_UUID,
    MATERIAL_TEMPLATE_B_UUID,
    MATERIAL_TEMPLATE_C_UUID,
    PARENT_WORKFLOW_UUID,
    make_nested_resource_world,
    plain,
    resource_slot_schema,
    resource_slot_wrapper,
)


def _field(expansion: Any, name: str) -> Any:
    return plain(getattr(expansion, name))


def _world_snapshot(world: Any) -> dict[str, Any]:
    return {
        "parent": plain(world.store.get_graph(PARENT_WORKFLOW_UUID)),
        "direct": plain(world.store.get_graph(CHILD_WORKFLOW_UUID)),
        "leaf": plain(world.store.get_graph(LEAF_WORKFLOW_UUID)),
        "catalog": world.catalog_snapshot(),
    }


def _slot_schema(schema: dict[str, Any]) -> dict[str, Any]:
    if schema.get("$slot") == "ResourceSlot":
        return schema
    if schema.get("type") == "array":
        return _slot_schema(schema["items"])
    for member in schema.get("anyOf", []):
        if isinstance(member, dict) and member.get("type") != "null":
            return _slot_schema(member)
    raise AssertionError(f"schema 不含 ResourceSlot：{schema!r}")


def _compile_nested(world: Any) -> Any:
    return world.compile(
        keyword_arguments={"value": {"kind": "workflow_input", "parameter": "value"}}
    )


def test_publisher_accepts_legal_resource_slot_narrowing_at_nested_binding(
    tmp_path: Path,
) -> None:
    """D-064 收窄是编译期交集，并非非法 I/O。

    这里独立冻结当前发布 blocker：direct child input 是全集，而它真实的嵌套
    input_binding 指向 leaf ``{B}``。
    """

    world = make_nested_resource_world(
        tmp_path,
        parent_schema=resource_slot_schema(),
        direct_schema=resource_slot_schema(),
        leaf_schema=resource_slot_schema(MATERIAL_TEMPLATE_B_UUID),
    )
    try:
        nested = next(
            node
            for node in world.store.get_graph(CHILD_WORKFLOW_UUID)["nodes"]
            if node["workflow_node_template_uuid"] == LEAF_TEMPLATE_UUID
        )
        assert nested["param"] == {}
        assert nested["meta_data"]["unilab"]["input_bindings"] == {
            LEAF_VALUE_TARGET_UUID: {"parameter": "value"}
        }
    finally:
        world.close()


def test_nested_leaf_constraint_reaches_top_effective_parent_contract(
    tmp_path: Path,
) -> None:
    world = make_nested_resource_world(
        tmp_path,
        parent_schema=resource_slot_schema(),
        direct_schema=resource_slot_schema(),
        leaf_schema=resource_slot_schema(MATERIAL_TEMPLATE_B_UUID),
    )
    try:
        before = _world_snapshot(world)

        expansion = _compile_nested(world)

        assert _field(expansion, "diagnostics") == []
        effective = _field(expansion, "effective_parent_input_contract")
        assert effective["parameters"][0]["schema"] == resource_slot_schema(
            MATERIAL_TEMPLATE_B_UUID
        )
        nested = next(
            node
            for node in _field(expansion, "nodes")
            if node["workflow_node_template_uuid"] == LEAF_TEMPLATE_UUID
        )
        assert nested["param"] == {}
        assert nested["meta_data"]["unilab"]["input_bindings"] == {
            LEAF_VALUE_TARGET_UUID: {"parameter": "value"}
        }
        assert _world_snapshot(world) == before
    finally:
        world.close()


def test_all_explicit_resource_slot_layers_are_intersected(
    tmp_path: Path,
) -> None:
    world = make_nested_resource_world(
        tmp_path,
        parent_schema=resource_slot_schema(
            MATERIAL_TEMPLATE_A_UUID,
            MATERIAL_TEMPLATE_B_UUID,
            MATERIAL_TEMPLATE_C_UUID,
        ),
        direct_schema=resource_slot_schema(
            MATERIAL_TEMPLATE_B_UUID,
            MATERIAL_TEMPLATE_C_UUID,
        ),
        leaf_schema=resource_slot_schema(MATERIAL_TEMPLATE_B_UUID),
    )
    try:
        before = _world_snapshot(world)

        expansion = _compile_nested(world)

        assert _field(expansion, "diagnostics") == []
        effective = _field(expansion, "effective_parent_input_contract")
        assert effective["parameters"][0]["schema"] == resource_slot_schema(
            MATERIAL_TEMPLATE_B_UUID
        )
        assert _world_snapshot(world) == before
    finally:
        world.close()


@pytest.mark.parametrize(
    ("parent_schema", "direct_schema", "leaf_schema"),
    [
        (
            resource_slot_schema(MATERIAL_TEMPLATE_A_UUID),
            resource_slot_schema(MATERIAL_TEMPLATE_B_UUID),
            resource_slot_schema(MATERIAL_TEMPLATE_B_UUID),
        ),
        (
            resource_slot_schema(MATERIAL_TEMPLATE_A_UUID, MATERIAL_TEMPLATE_B_UUID),
            resource_slot_schema(MATERIAL_TEMPLATE_A_UUID),
            resource_slot_schema(MATERIAL_TEMPLATE_B_UUID),
        ),
    ],
    ids=["empty-at-top-boundary", "empty-at-nested-boundary"],
)
def test_empty_intersection_at_any_layer_fails_closed_with_stable_root_pointer(
    tmp_path: Path,
    parent_schema: dict[str, Any],
    direct_schema: dict[str, Any],
    leaf_schema: dict[str, Any],
) -> None:
    world = make_nested_resource_world(
        tmp_path,
        parent_schema=parent_schema,
        direct_schema=direct_schema,
        leaf_schema=leaf_schema,
    )
    try:
        before = _world_snapshot(world)

        expansion = _compile_nested(world)

        assert _field(expansion, "diagnostics") == [
            {
                "code": "composite_resource_constraint_empty",
                "path": "/keyword_arguments/value/schema",
                "severity": "error",
                "message": "Composite authoring contract validation failed",
            }
        ]
        assert _field(expansion, "invocation_node") is None
        assert _field(expansion, "nodes") == []
        assert _field(expansion, "edges") == []
        assert _world_snapshot(world) == before
    finally:
        world.close()


@pytest.mark.parametrize(
    ("collection", "nullable"),
    [(True, False), (True, True)],
    ids=["list-resource-slot", "nullable-list-resource-slot"],
)
def test_nested_constraint_preserves_resource_slot_wrapper_shape(
    tmp_path: Path,
    collection: bool,
    nullable: bool,
) -> None:
    parent_schema = resource_slot_wrapper(
        MATERIAL_TEMPLATE_A_UUID,
        MATERIAL_TEMPLATE_B_UUID,
        MATERIAL_TEMPLATE_C_UUID,
        collection=collection,
        nullable=nullable,
    )
    direct_schema = resource_slot_wrapper(
        MATERIAL_TEMPLATE_B_UUID,
        MATERIAL_TEMPLATE_C_UUID,
        collection=collection,
        nullable=nullable,
    )
    leaf_schema = resource_slot_wrapper(
        MATERIAL_TEMPLATE_B_UUID,
        collection=collection,
        nullable=nullable,
    )
    world = make_nested_resource_world(
        tmp_path,
        parent_schema=parent_schema,
        direct_schema=direct_schema,
        leaf_schema=leaf_schema,
    )
    try:
        before = _world_snapshot(world)

        expansion = _compile_nested(world)

        assert _field(expansion, "diagnostics") == []
        effective = _field(expansion, "effective_parent_input_contract")
        schema = effective["parameters"][0]["schema"]
        assert schema == leaf_schema
        assert _slot_schema(schema)["allowed_resource_template_uuids"] == [
            MATERIAL_TEMPLATE_B_UUID
        ]
        assert _world_snapshot(world) == before
    finally:
        world.close()
