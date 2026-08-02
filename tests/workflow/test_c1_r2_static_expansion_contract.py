"""C1 R2 CompositeAuthoring 公开静态展开 RED。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .c1_r2_static_expansion_fixture import (
    ACTION_READY_SOURCE_UUID,
    ACTION_READY_TARGET_UUID,
    ACTION_TEMPLATE_UUID,
    ACTION_VALUE_SOURCE_UUID,
    ACTION_VALUE_TARGET_UUID,
    CHILD_NODE_UUID,
    CHILD_READY_SOURCE_UUID,
    CHILD_READY_TARGET_UUID,
    CHILD_TEMPLATE_UUID,
    CHILD_VALUE_SOURCE_UUID,
    CHILD_VALUE_TARGET_UUID,
    EXPANDED_CHILD_NODE_UUID,
    EXPANDED_EDGE_UUID,
    EXPANDED_GRANDCHILD_NODE_UUID,
    INVOCATION_UUID,
    SECOND_ACTION_READY_SOURCE_UUID,
    SECOND_ACTION_READY_TARGET_UUID,
    SECOND_ACTION_RESOURCE_TEMPLATE_UUID,
    SECOND_ACTION_TEMPLATE_UUID,
    SECOND_ACTION_VALUE_SOURCE_UUID,
    SECOND_ACTION_VALUE_TARGET_UUID,
    SECOND_CHILD_NODE_UUID,
    action_import,
    action_node,
    make_direct_world,
    make_nested_world,
    plain,
    replace_applied_graph,
    scalar_schema,
    workflow_meta,
)


def _field(expansion: Any, name: str) -> Any:
    return plain(getattr(expansion, name))


def _by_uuid(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item["uuid"]): item for item in items}


def test_direct_invocation_returns_complete_hierarchical_expansion_and_pin(
    tmp_path: Path,
) -> None:
    world = make_direct_world(tmp_path)
    try:
        expansion = world.compile(keyword_arguments={"value": 7.5})

        assert _field(expansion, "diagnostics") == []
        invocation = _field(expansion, "invocation_node")
        assert invocation["uuid"] == INVOCATION_UUID
        assert invocation["workflow_node_template_uuid"] == CHILD_TEMPLATE_UUID
        assert invocation["parent_uuid"] is None
        assert invocation["param"] == {"value": 7.5}

        nodes = _field(expansion, "nodes")
        assert len(nodes) == 1
        assert nodes[0]["uuid"] == EXPANDED_CHILD_NODE_UUID
        assert nodes[0]["parent_uuid"] == INVOCATION_UUID
        assert nodes[0]["workflow_node_template_uuid"] == ACTION_TEMPLATE_UUID
        assert _field(expansion, "edges") == []

        expected_pin = {
            "child_workflow_uuid": world.child.source.workflow_uuid,
            "child_workflow_revision": world.child.contract_pin["workflow_revision"],
            "child_applied_source_hash": world.child.contract_pin[
                "applied_source_hash"
            ],
            "contract_digest": world.child.contract_pin["contract_digest"],
            "composition_allow_transparent": False,
        }
        assert _field(expansion, "contract_pin") == expected_pin
        composite = invocation["meta_data"]["unilab"]["composite"]
        assert {key: composite[key] for key in ("version", *expected_pin)} == {
            "version": 1,
            **expected_pin,
        }

        template_ids = {item["uuid"] for item in _field(expansion, "node_templates")}
        handle_ids = {item["uuid"] for item in _field(expansion, "handle_templates")}
        assert template_ids == {ACTION_TEMPLATE_UUID, CHILD_TEMPLATE_UUID}
        assert {
            ACTION_VALUE_TARGET_UUID,
            ACTION_VALUE_SOURCE_UUID,
            ACTION_READY_TARGET_UUID,
            ACTION_READY_SOURCE_UUID,
            CHILD_VALUE_TARGET_UUID,
            CHILD_VALUE_SOURCE_UUID,
            CHILD_READY_TARGET_UUID,
            CHILD_READY_SOURCE_UUID,
        } <= handle_ids
    finally:
        world.close()


def test_business_and_structural_mappings_are_separate_canonical_and_sorted(
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
                action_node(
                    node_uuid=SECOND_CHILD_NODE_UUID,
                    template_uuid=SECOND_ACTION_TEMPLATE_UUID,
                    target_handle_uuid=SECOND_ACTION_VALUE_TARGET_UUID,
                ),
                action_node(node_uuid=CHILD_NODE_UUID),
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

        expansion = world.compile(keyword_arguments={"value": 3})
        target_mappings = _field(expansion, "target_mappings")
        source_mappings = _field(expansion, "source_mappings")
        structural = _field(expansion, "structural_mappings")

        assert list(target_mappings) == [CHILD_VALUE_TARGET_UUID]
        assert target_mappings[CHILD_VALUE_TARGET_UUID] == sorted(
            target_mappings[CHILD_VALUE_TARGET_UUID],
            key=lambda item: (
                item["workflow_node_uuid"],
                item["target_handle_uuid"],
            ),
        )
        assert all(
            item["target_handle_uuid"] != ACTION_READY_TARGET_UUID
            for item in target_mappings[CHILD_VALUE_TARGET_UUID]
        )
        assert source_mappings == {
            CHILD_VALUE_SOURCE_UUID: {
                "kind": "node_output",
                "workflow_node_uuid": EXPANDED_CHILD_NODE_UUID,
                "source_handle_uuid": ACTION_VALUE_SOURCE_UUID,
            }
        }
        assert structural["entry_targets"] == sorted(
            structural["entry_targets"],
            key=lambda item: (
                item["workflow_node_uuid"],
                item["target_handle_uuid"],
            ),
        )
        assert structural["completion_sources"] == sorted(
            structural["completion_sources"],
            key=lambda item: (
                item["workflow_node_uuid"],
                item["source_handle_uuid"],
            ),
        )
        assert set(target_mappings).isdisjoint(
            {CHILD_READY_TARGET_UUID, CHILD_READY_SOURCE_UUID}
        )
        assert set(source_mappings).isdisjoint(
            {CHILD_READY_TARGET_UUID, CHILD_READY_SOURCE_UUID}
        )
        composite = _field(expansion, "invocation_node")["meta_data"]["unilab"][
            "composite"
        ]
        assert composite["target_mappings"] == target_mappings
        assert composite["source_mappings"] == source_mappings
        assert composite["structural_mappings"] == structural
    finally:
        world.close()


def test_ready_only_child_preserves_entry_and_completion_without_business_values(
    tmp_path: Path,
) -> None:
    world = make_direct_world(tmp_path, ready_only=True)
    try:
        expansion = world.compile()

        assert _field(expansion, "diagnostics") == []
        assert _field(expansion, "target_mappings") == {}
        assert _field(expansion, "source_mappings") == {}
        assert _field(expansion, "structural_mappings") == {
            "entry_targets": [
                {
                    "workflow_node_uuid": EXPANDED_CHILD_NODE_UUID,
                    "target_handle_uuid": ACTION_READY_TARGET_UUID,
                }
            ],
            "completion_sources": [
                {
                    "workflow_node_uuid": EXPANDED_CHILD_NODE_UUID,
                    "source_handle_uuid": ACTION_READY_SOURCE_UUID,
                }
            ],
        }
    finally:
        world.close()


def test_two_invocations_share_contract_templates_but_not_expanded_node_identity(
    tmp_path: Path,
) -> None:
    world = make_direct_world(tmp_path)
    other_invocation_uuid = "11111111-1111-4111-8111-111111111112"
    try:
        first = world.compile(keyword_arguments={"value": 1})
        second = world.compile(
            invocation_uuid=other_invocation_uuid,
            keyword_arguments={"value": 1},
        )

        first_node = _field(first, "nodes")[0]
        second_node = _field(second, "nodes")[0]
        assert first_node["uuid"] == EXPANDED_CHILD_NODE_UUID
        assert second_node["uuid"] != first_node["uuid"]
        assert (
            first_node["workflow_node_template_uuid"]
            == second_node["workflow_node_template_uuid"]
        )
        assert {item["uuid"] for item in _field(first, "handle_templates")} == {
            item["uuid"] for item in _field(second, "handle_templates")
        }
    finally:
        world.close()


def test_nested_child_uses_derived_invocation_namespace_and_root_edge_rule(
    tmp_path: Path,
) -> None:
    world = make_nested_world(tmp_path)
    try:
        expansion = world.compile(keyword_arguments={"value": 9})

        nodes = _by_uuid(_field(expansion, "nodes"))
        assert set(nodes) == {
            EXPANDED_CHILD_NODE_UUID,
            EXPANDED_GRANDCHILD_NODE_UUID,
        }
        assert nodes[EXPANDED_CHILD_NODE_UUID]["parent_uuid"] == INVOCATION_UUID
        assert nodes[EXPANDED_GRANDCHILD_NODE_UUID]["parent_uuid"] == (
            EXPANDED_CHILD_NODE_UUID
        )
        edges = _field(expansion, "edges")
        assert len(edges) == 1
        assert {
            key: edges[0][key]
            for key in (
                "uuid",
                "source_node_uuid",
                "target_node_uuid",
                "source_handle_uuid",
                "target_handle_uuid",
            )
        } == {
            "uuid": EXPANDED_EDGE_UUID,
            "source_node_uuid": EXPANDED_CHILD_NODE_UUID,
            "target_node_uuid": EXPANDED_GRANDCHILD_NODE_UUID,
            "source_handle_uuid": ACTION_VALUE_SOURCE_UUID,
            "target_handle_uuid": SECOND_ACTION_VALUE_TARGET_UUID,
        }
        nested_composite = nodes[EXPANDED_CHILD_NODE_UUID]["meta_data"]["unilab"][
            "composite"
        ]
        assert nested_composite["structural_mappings"] == {
            "entry_targets": [
                {
                    "workflow_node_uuid": EXPANDED_GRANDCHILD_NODE_UUID,
                    "target_handle_uuid": SECOND_ACTION_READY_TARGET_UUID,
                }
            ],
            "completion_sources": [
                {
                    "workflow_node_uuid": EXPANDED_GRANDCHILD_NODE_UUID,
                    "source_handle_uuid": SECOND_ACTION_READY_SOURCE_UUID,
                }
            ],
        }
    finally:
        world.close()
