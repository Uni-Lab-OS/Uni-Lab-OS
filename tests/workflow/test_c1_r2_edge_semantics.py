"""C1 R2 子 Edge 控制/展示语义保留的公开 RED。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .c1_r2_static_expansion_fixture import (
    ACTION_READY_SOURCE_UUID,
    ACTION_TEMPLATE_UUID,
    CHILD_NODE_UUID,
    CHILD_WORKFLOW_UUID,
    PARENT_WORKFLOW_UUID,
    SECOND_ACTION_READY_SOURCE_UUID,
    SECOND_ACTION_READY_TARGET_UUID,
    SECOND_ACTION_RESOURCE_TEMPLATE_UUID,
    SECOND_ACTION_TEMPLATE_UUID,
    SECOND_ACTION_VALUE_SOURCE_UUID,
    SECOND_ACTION_VALUE_TARGET_UUID,
    SECOND_CHILD_NODE_UUID,
    STORED_NESTED_EDGE_UUID,
    action_import,
    action_node,
    authoring_edge,
    make_direct_world,
    make_nested_world,
    plain,
    replace_applied_graph,
    scalar_schema,
    workflow_meta,
)

EDGE_DESCRIPTION = "branch semantics"
EDGE_META_DATA = {
    "condition": "x > 0",
    "control": {"kind": "branch", "label": "positive"},
}


def _field(expansion: Any, name: str) -> Any:
    return plain(getattr(expansion, name))


def _snapshot(world: Any) -> dict[str, Any]:
    return {
        "parent": plain(world.store.get_graph(PARENT_WORKFLOW_UUID)),
        "child": plain(world.store.get_graph(CHILD_WORKFLOW_UUID)),
        "catalog": world.catalog_snapshot(),
    }


def _only_edge(expansion: Any) -> dict[str, Any]:
    edges = expansion.edges
    assert len(edges) == 1
    return edges[0]


def test_direct_edge_preserves_semantics_while_remapping_identity_and_endpoints(
    tmp_path: Path,
) -> None:
    world = make_direct_world(tmp_path)
    try:
        world.imports[SECOND_ACTION_TEMPLATE_UUID] = action_import(
            template_uuid=SECOND_ACTION_TEMPLATE_UUID,
            resource_template_uuid=SECOND_ACTION_RESOURCE_TEMPLATE_UUID,
            target_handle_uuid=SECOND_ACTION_VALUE_TARGET_UUID,
            source_handle_uuid=SECOND_ACTION_VALUE_SOURCE_UUID,
            ready_target_uuid=SECOND_ACTION_READY_TARGET_UUID,
            ready_source_uuid=SECOND_ACTION_READY_SOURCE_UUID,
        )
        world.publish()
        world.child = replace_applied_graph(
            world,
            world.child,
            nodes=[
                action_node(node_uuid=CHILD_NODE_UUID),
                action_node(
                    node_uuid=SECOND_CHILD_NODE_UUID,
                    template_uuid=SECOND_ACTION_TEMPLATE_UUID,
                    target_handle_uuid=SECOND_ACTION_VALUE_TARGET_UUID,
                ),
            ],
            edges=[
                authoring_edge(
                    edge_uuid=STORED_NESTED_EDGE_UUID,
                    source_node_uuid=CHILD_NODE_UUID,
                    source_handle_uuid=ACTION_READY_SOURCE_UUID,
                    target_node_uuid=SECOND_CHILD_NODE_UUID,
                    target_handle_uuid=SECOND_ACTION_READY_TARGET_UUID,
                    description=EDGE_DESCRIPTION,
                    meta_data=EDGE_META_DATA,
                )
            ],
            meta_data=workflow_meta(
                input_schema=scalar_schema(),
                output_schema=scalar_schema(),
                output_binding={
                    "kind": "node_output",
                    "workflow_node_uuid": SECOND_CHILD_NODE_UUID,
                    "source_handle_uuid": SECOND_ACTION_VALUE_SOURCE_UUID,
                },
            ),
        )
        before = _snapshot(world)

        expansion = world.compile(keyword_arguments={"value": 1})

        assert _field(expansion, "diagnostics") == []
        edge = _only_edge(expansion)
        nodes_by_template = {
            node["workflow_node_template_uuid"]: node
            for node in _field(expansion, "nodes")
        }
        assert edge["uuid"] != STORED_NESTED_EDGE_UUID
        assert (
            edge["source_node_uuid"] == nodes_by_template[ACTION_TEMPLATE_UUID]["uuid"]
        )
        assert (
            edge["target_node_uuid"]
            == nodes_by_template[SECOND_ACTION_TEMPLATE_UUID]["uuid"]
        )
        assert edge["source_handle_uuid"] == ACTION_READY_SOURCE_UUID
        assert edge["target_handle_uuid"] == SECOND_ACTION_READY_TARGET_UUID
        assert edge["description"] == EDGE_DESCRIPTION
        assert edge["meta_data"] == EDGE_META_DATA
        assert _snapshot(world) == before
    finally:
        world.close()


def test_nested_edge_semantics_are_deep_copied_and_cannot_pollute_store(
    tmp_path: Path,
) -> None:
    world = make_nested_world(
        tmp_path,
        edge_description=EDGE_DESCRIPTION,
        edge_meta_data=EDGE_META_DATA,
    )
    try:
        before = _snapshot(world)

        first = world.compile(keyword_arguments={"value": 1})

        assert _field(first, "diagnostics") == []
        first_edge = _only_edge(first)
        assert first_edge["uuid"] != STORED_NESTED_EDGE_UUID
        assert first_edge["description"] == EDGE_DESCRIPTION
        assert first_edge["meta_data"] == EDGE_META_DATA
        first_edge["meta_data"]["condition"] = "caller mutation"
        first_edge["meta_data"]["control"]["label"] = "caller mutation"

        second = world.compile(keyword_arguments={"value": 1})

        second_edge = _only_edge(second)
        assert second_edge["uuid"] == first_edge["uuid"]
        assert second_edge["description"] == EDGE_DESCRIPTION
        assert second_edge["meta_data"] == EDGE_META_DATA
        assert _snapshot(world) == before
    finally:
        world.close()
