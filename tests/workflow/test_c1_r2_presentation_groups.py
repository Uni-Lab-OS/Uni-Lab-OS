"""C1 R2 展示分组保持可见但绝不成为可执行节点的公开 RED。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .c1_r2_static_expansion_fixture import (
    ACTION_READY_SOURCE_UUID,
    ACTION_READY_TARGET_UUID,
    ACTION_TEMPLATE_UUID,
    ACTION_VALUE_SOURCE_UUID,
    CHILD_NODE_UUID,
    CHILD_WORKFLOW_UUID,
    GROUP_NODE_UUID,
    GROUP_TEMPLATE_UUID,
    LEAF_TEMPLATE_UUID,
    LEAF_WORKFLOW_UUID,
    PARENT_WORKFLOW_UUID,
    SECOND_ACTION_READY_SOURCE_UUID,
    SECOND_ACTION_READY_TARGET_UUID,
    SECOND_ACTION_TEMPLATE_UUID,
    action_node,
    group_node,
    make_direct_world,
    make_nested_world,
    plain,
    presentation_group_import,
    replace_applied_graph,
    scalar_schema,
    workflow_meta,
)


def _field(expansion: Any, name: str) -> Any:
    return plain(getattr(expansion, name))


def _snapshot(world: Any, *, nested: bool = False) -> dict[str, Any]:
    result = {
        "parent": plain(world.store.get_graph(PARENT_WORKFLOW_UUID)),
        "child": plain(world.store.get_graph(CHILD_WORKFLOW_UUID)),
        "catalog": world.catalog_snapshot(),
    }
    if nested:
        result["leaf"] = plain(world.store.get_graph(LEAF_WORKFLOW_UUID))
    return result


def _assert_group_has_no_ready_handle(expansion: Any) -> None:
    group_handle_uuids = {
        handle["uuid"]
        for handle in _field(expansion, "handle_templates")
        if handle["workflow_node_template_uuid"] == GROUP_TEMPLATE_UUID
    }
    assert group_handle_uuids == set()
    structural = _field(expansion, "structural_mappings")
    group_node_uuids = {
        node["uuid"] for node in _field(expansion, "nodes") if node["type"] == "group"
    }
    referenced_nodes = {
        item["workflow_node_uuid"]
        for entries in structural.values()
        for item in entries
    }
    assert referenced_nodes.isdisjoint(group_node_uuids)


def test_group_and_child_hierarchy_are_preserved_but_structural_dag_ignores_group(
    tmp_path: Path,
) -> None:
    world = make_direct_world(tmp_path)
    try:
        world.imports[GROUP_TEMPLATE_UUID] = presentation_group_import()
        world.publish()
        world.child = replace_applied_graph(
            world,
            world.child,
            nodes=[
                group_node(node_uuid=GROUP_NODE_UUID),
                action_node(node_uuid=CHILD_NODE_UUID, parent_uuid=GROUP_NODE_UUID),
            ],
            edges=[],
            meta_data=workflow_meta(
                input_schema=scalar_schema(),
                output_schema=scalar_schema(),
                output_binding={
                    "kind": "node_output",
                    "workflow_node_uuid": CHILD_NODE_UUID,
                    "source_handle_uuid": ACTION_VALUE_SOURCE_UUID,
                },
            ),
        )
        before = _snapshot(world)

        expansion = world.compile(keyword_arguments={"value": 1})

        assert _field(expansion, "diagnostics") == []
        nodes = _field(expansion, "nodes")
        group = next(node for node in nodes if node["type"] == "group")
        action = next(
            node
            for node in nodes
            if node["workflow_node_template_uuid"] == ACTION_TEMPLATE_UUID
        )
        assert group["workflow_node_template_uuid"] == GROUP_TEMPLATE_UUID
        assert action["parent_uuid"] == group["uuid"]
        assert _field(expansion, "structural_mappings") == {
            "entry_targets": [
                {
                    "workflow_node_uuid": action["uuid"],
                    "target_handle_uuid": ACTION_READY_TARGET_UUID,
                }
            ],
            "completion_sources": [
                {
                    "workflow_node_uuid": action["uuid"],
                    "source_handle_uuid": ACTION_READY_SOURCE_UUID,
                }
            ],
        }
        _assert_group_has_no_ready_handle(expansion)
        assert _snapshot(world) == before
    finally:
        world.close()


def test_nested_group_stays_under_nested_invocation_but_nested_ready_maps_use_action(
    tmp_path: Path,
) -> None:
    world = make_nested_world(tmp_path, leaf_group=True)
    try:
        before = _snapshot(world, nested=True)

        expansion = world.compile(keyword_arguments={"value": 1})

        assert _field(expansion, "diagnostics") == []
        nodes = _field(expansion, "nodes")
        nested_invocation = next(
            node
            for node in nodes
            if node["workflow_node_template_uuid"] == LEAF_TEMPLATE_UUID
        )
        group = next(node for node in nodes if node["type"] == "group")
        action = next(
            node
            for node in nodes
            if node["workflow_node_template_uuid"] == SECOND_ACTION_TEMPLATE_UUID
        )
        assert group["parent_uuid"] == nested_invocation["uuid"]
        assert action["parent_uuid"] == group["uuid"]
        nested_structural = nested_invocation["meta_data"]["unilab"]["composite"][
            "structural_mappings"
        ]
        assert nested_structural == {
            "entry_targets": [
                {
                    "workflow_node_uuid": action["uuid"],
                    "target_handle_uuid": SECOND_ACTION_READY_TARGET_UUID,
                }
            ],
            "completion_sources": [
                {
                    "workflow_node_uuid": action["uuid"],
                    "source_handle_uuid": SECOND_ACTION_READY_SOURCE_UUID,
                }
            ],
        }
        _assert_group_has_no_ready_handle(expansion)
        assert _snapshot(world, nested=True) == before
    finally:
        world.close()
