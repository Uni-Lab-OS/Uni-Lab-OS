"""C1 R2 static expansion 的 lifecycle、诊断、D-064 与零写 RED。"""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.catalog import NodeTemplateImport
from unilabos.workflow.composite import PublishedWorkflowSource

from .c1_r2_static_expansion_fixture import (
    ACTION_READY_SOURCE_UUID,
    ACTION_READY_TARGET_UUID,
    ACTION_TEMPLATE_UUID,
    ACTION_VALUE_SOURCE_UUID,
    ACTION_VALUE_TARGET_UUID,
    CHILD_NODE_UUID,
    CHILD_TEMPLATE_UUID,
    CHILD_WORKFLOW_UUID,
    EXPANDED_CHILD_NODE_UUID,
    GRANDCHILD_NODE_UUID,
    LEAF_WORKFLOW_UUID,
    MATERIAL_TEMPLATE_A_UUID,
    MATERIAL_TEMPLATE_B_UUID,
    MATERIAL_TEMPLATE_C_UUID,
    PARENT_WORKFLOW_UUID,
    SECOND_ACTION_READY_SOURCE_UUID,
    SECOND_ACTION_READY_TARGET_UUID,
    SECOND_ACTION_TEMPLATE_UUID,
    SECOND_ACTION_VALUE_SOURCE_UUID,
    SECOND_ACTION_VALUE_TARGET_UUID,
    SECOND_CHILD_NODE_UUID,
    THIRD_READY_SOURCE_UUID,
    THIRD_READY_TARGET_UUID,
    THIRD_TEMPLATE_UUID,
    THIRD_VALUE_SOURCE_UUID,
    THIRD_VALUE_TARGET_UUID,
    THIRD_WORKFLOW_UUID,
    WorkflowContractFixture,
    action_node,
    composite_metadata,
    create_applied_workflow,
    make_direct_world,
    make_nested_world,
    plain,
    resource_slot_schema,
    scalar_schema,
    workflow_meta,
    workflow_node,
)


def _field(expansion: Any, name: str) -> Any:
    return plain(getattr(expansion, name))


def _assert_diagnostic(expansion: Any, code: str) -> None:
    assert _field(expansion, "invocation_node") is None
    assert _field(expansion, "nodes") == []
    assert _field(expansion, "edges") == []
    assert [item["code"] for item in _field(expansion, "diagnostics")] == [code]


def _assert_zero_parent_or_catalog_write(
    world: Any,
    compile_invalid: Callable[[], Any],
    code: str,
) -> None:
    before_graph = world.store.get_graph(PARENT_WORKFLOW_UUID)
    before_catalog = world.catalog_snapshot()

    expansion = compile_invalid()

    _assert_diagnostic(expansion, code)
    assert world.store.get_graph(PARENT_WORKFLOW_UUID) == before_graph
    assert world.catalog_snapshot() == before_catalog


def _set_workflow_meta(
    world: Any, workflow_uuid: str, meta_data: dict[str, Any]
) -> None:
    with world.store.transaction() as connection:
        connection.execute(
            "UPDATE workflow SET meta_data = ? WHERE uuid = ?",
            (json.dumps(meta_data, sort_keys=True), workflow_uuid),
        )


def _set_node(
    world: Any,
    workflow_uuid: str,
    node_uuid: str,
    *,
    template_uuid: str,
    node_type: str,
    meta_data: dict[str, Any],
) -> None:
    with world.store.transaction() as connection:
        connection.execute(
            """
            UPDATE workflow_node
            SET workflow_node_template_uuid = ?, type = ?, meta_data = ?
            WHERE workflow_uuid = ? AND uuid = ?
            """,
            (
                template_uuid,
                node_type,
                json.dumps(meta_data, sort_keys=True),
                workflow_uuid,
                node_uuid,
            ),
        )


def _replace_child_provenance(world: Any, **changes: Any) -> None:
    imported = world.imports[CHILD_TEMPLATE_UUID]
    template = deepcopy(dict(imported.template))
    template["meta_data"]["unilab"]["workflow_source"].update(changes)
    world.imports[CHILD_TEMPLATE_UUID] = NodeTemplateImport(
        template=template,
        handles=tuple(dict(item) for item in imported.handles),
    )
    world.publish()


def test_missing_soft_deleted_unapplied_and_stale_child_fail_closed(
    tmp_path: Path,
) -> None:
    cases = ("missing", "soft_deleted", "unapplied", "stale")
    for case in cases:
        world = make_direct_world(tmp_path / case)
        try:
            source = world.child.source
            expected = "composite_child_not_found"
            if case == "missing":
                source = PublishedWorkflowSource(
                    workflow_uuid="a1000000-0000-4000-8000-000000000099",
                    definition_fqid="tests.c1_r2.missing",
                    module="tests.c1_r2.missing",
                    symbol="missing",
                    package_catalog_digest="sha256:" + "a" * 64,
                    definition_content_hash="sha256:" + "b" * 64,
                )
                world.resolver.add(source)
            elif case == "soft_deleted":
                world.store.delete_workflow(CHILD_WORKFLOW_UUID)
            else:
                expected = "composite_child_unapplied"
                record = world.store.get_authoring_record(CHILD_WORKFLOW_UUID)
                applied = deepcopy(record["applied_source"])
                if case == "unapplied":
                    applied = None
                else:
                    applied["workflow_revision"] -= 1
                with world.store.transaction() as connection:
                    connection.execute(
                        "UPDATE workflow_authoring SET applied_source = ? "
                        "WHERE workflow_uuid = ?",
                        (
                            None
                            if applied is None
                            else json.dumps(applied, sort_keys=True),
                            CHILD_WORKFLOW_UUID,
                        ),
                    )

            _assert_zero_parent_or_catalog_write(
                world,
                lambda current=world, selected=source: current.compile(
                    source=selected,
                    keyword_arguments={"value": 1},
                ),
                expected,
            )
        finally:
            world.close()


@pytest.mark.parametrize(
    "provenance_change",
    [
        {"definition_content_hash": "sha256:" + "c" * 64},
        {"package_catalog_digest": "sha256:" + "d" * 64},
        {"module": "tests.c1_r2.foreign"},
        {"symbol": "foreign"},
    ],
)
def test_published_template_provenance_must_match_resolved_source(
    tmp_path: Path,
    provenance_change: dict[str, str],
) -> None:
    world = make_direct_world(tmp_path)
    try:
        _replace_child_provenance(world, **provenance_change)

        _assert_zero_parent_or_catalog_write(
            world,
            lambda: world.compile(keyword_arguments={"value": 1}),
            "composite_catalog_mismatch",
        )
    finally:
        world.close()


def _damage_mapping(world: Any, kind: str) -> None:
    graph = world.store.get_graph(CHILD_WORKFLOW_UUID)
    node = deepcopy(graph["nodes"][0])
    node_meta = deepcopy(node["meta_data"])
    workflow_metadata = deepcopy(graph["workflow"]["meta_data"])
    if kind == "foreign_node":
        workflow_metadata["unilab"]["output_bindings"]["result"][
            "workflow_node_uuid"
        ] = "a7000000-0000-4000-8000-000000000001"
        _set_workflow_meta(world, CHILD_WORKFLOW_UUID, workflow_metadata)
        return
    if kind == "missing_handle":
        key = "a7000000-0000-4000-8000-000000000002"
    elif kind == "wrong_direction":
        key = ACTION_VALUE_SOURCE_UUID
    elif kind == "foreign_owner":
        key = SECOND_ACTION_VALUE_TARGET_UUID
    elif kind == "missing_coverage":
        node_meta["unilab"]["input_bindings"] = {}
        key = None
    else:
        raise AssertionError(kind)
    if key is not None:
        node_meta["unilab"]["input_bindings"] = {key: {"parameter": "value"}}
    _set_node(
        world,
        CHILD_WORKFLOW_UUID,
        CHILD_NODE_UUID,
        template_uuid=ACTION_TEMPLATE_UUID,
        node_type="compute",
        meta_data=node_meta,
    )


@pytest.mark.parametrize(
    "damage",
    [
        "foreign_node",
        "missing_handle",
        "wrong_direction",
        "foreign_owner",
        "missing_coverage",
    ],
)
def test_foreign_missing_wrong_direction_or_uncovered_mapping_is_rejected(
    tmp_path: Path,
    damage: str,
) -> None:
    world = make_direct_world(tmp_path)
    try:
        if damage == "foreign_owner":
            second = make_direct_world(tmp_path / "foreign-template-source")
            try:
                foreign_import = second.imports[ACTION_TEMPLATE_UUID]
                handles = []
                for raw in foreign_import.handles:
                    handle = dict(raw)
                    if handle["handle_key"] == "value":
                        handle["uuid"] = SECOND_ACTION_VALUE_TARGET_UUID
                    elif handle["handle_key"] == "result":
                        handle["uuid"] = SECOND_ACTION_VALUE_SOURCE_UUID
                    elif handle["io_type"] == "target":
                        handle["uuid"] = SECOND_ACTION_READY_TARGET_UUID
                    else:
                        handle["uuid"] = SECOND_ACTION_READY_SOURCE_UUID
                    handle["workflow_node_template_uuid"] = SECOND_ACTION_TEMPLATE_UUID
                    handles.append(handle)
                template = dict(foreign_import.template)
                template.update(
                    {
                        "uuid": SECOND_ACTION_TEMPLATE_UUID,
                        "resource_template_uuid": (
                            "a2000000-0000-4000-8000-000000000003"
                        ),
                        "name": "foreign-action",
                    }
                )
                world.imports[SECOND_ACTION_TEMPLATE_UUID] = NodeTemplateImport(
                    template=template,
                    handles=tuple(handles),
                )
            finally:
                second.close()
            world.publish()
        _damage_mapping(world, damage)

        _assert_zero_parent_or_catalog_write(
            world,
            lambda: world.compile(keyword_arguments={"value": 1}),
            "composite_boundary_mapping_invalid",
        )
    finally:
        world.close()


def test_parent_argument_cannot_reference_expanded_private_handle(
    tmp_path: Path,
) -> None:
    world = make_direct_world(tmp_path)
    try:
        _assert_zero_parent_or_catalog_write(
            world,
            lambda: world.compile(
                keyword_arguments={
                    "value": {
                        "kind": "node_output",
                        "workflow_node_uuid": EXPANDED_CHILD_NODE_UUID,
                        "source_handle_uuid": ACTION_VALUE_SOURCE_UUID,
                    }
                }
            ),
            "composite_external_private_edge",
        )
    finally:
        world.close()


def _rewrite_as_composite(
    world: Any,
    *,
    owner_workflow_uuid: str,
    owner_node_uuid: str,
    target: WorkflowContractFixture,
) -> None:
    node = workflow_node(
        node_uuid=owner_node_uuid,
        template_uuid=target.template_uuid,
        input_handle_uuid=target.handles.get(("value", "target")),
        parameter="value",
        composite=composite_metadata(
            target,
            target_node_uuid=owner_node_uuid,
            target_handle_uuid=ACTION_VALUE_TARGET_UUID,
            source_node_uuid=owner_node_uuid,
            source_handle_uuid=ACTION_VALUE_SOURCE_UUID,
            entry_node_uuid=owner_node_uuid,
            entry_handle_uuid=ACTION_READY_TARGET_UUID,
            completion_node_uuid=owner_node_uuid,
            completion_handle_uuid=ACTION_READY_SOURCE_UUID,
        ),
    )
    _set_node(
        world,
        owner_workflow_uuid,
        owner_node_uuid,
        template_uuid=target.template_uuid,
        node_type="workflow",
        meta_data=node.meta_data,
    )
    owner_graph = world.store.get_graph(owner_workflow_uuid)
    owner_meta = deepcopy(owner_graph["workflow"]["meta_data"])
    owner_meta["unilab"]["output_bindings"] = {
        "result": {
            "kind": "node_output",
            "workflow_node_uuid": owner_node_uuid,
            "source_handle_uuid": target.handles[("result", "source")],
        }
    }
    _set_workflow_meta(world, owner_workflow_uuid, owner_meta)


def test_self_nested_and_cross_workflow_cycles_share_one_stable_diagnostic(
    tmp_path: Path,
) -> None:
    for case in ("self", "nested", "cross"):
        world = make_nested_world(tmp_path / case)
        try:
            if case == "self":
                _rewrite_as_composite(
                    world,
                    owner_workflow_uuid=CHILD_WORKFLOW_UUID,
                    owner_node_uuid=CHILD_NODE_UUID,
                    target=world.child,
                )
            elif case == "nested":
                leaf = world.contracts[LEAF_WORKFLOW_UUID]
                _rewrite_as_composite(
                    world,
                    owner_workflow_uuid=LEAF_WORKFLOW_UUID,
                    owner_node_uuid=GRANDCHILD_NODE_UUID,
                    target=leaf,
                )
            else:
                third = create_applied_workflow(
                    world,
                    workflow_uuid=THIRD_WORKFLOW_UUID,
                    module="tests.c1_r2.third",
                    symbol="third",
                    template_uuid=THIRD_TEMPLATE_UUID,
                    boundary_handle_uuids={
                        ("value", "target"): THIRD_VALUE_TARGET_UUID,
                        ("result", "source"): THIRD_VALUE_SOURCE_UUID,
                        ("ready", "target"): THIRD_READY_TARGET_UUID,
                        ("ready", "source"): THIRD_READY_SOURCE_UUID,
                    },
                    nodes=[
                        action_node(
                            node_uuid=SECOND_CHILD_NODE_UUID,
                            template_uuid=SECOND_ACTION_TEMPLATE_UUID,
                            target_handle_uuid=SECOND_ACTION_VALUE_TARGET_UUID,
                        )
                    ],
                    edges=[],
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
                world.contracts[THIRD_WORKFLOW_UUID] = third
                leaf = world.contracts[LEAF_WORKFLOW_UUID]
                _rewrite_as_composite(
                    world,
                    owner_workflow_uuid=LEAF_WORKFLOW_UUID,
                    owner_node_uuid=GRANDCHILD_NODE_UUID,
                    target=third,
                )
                _rewrite_as_composite(
                    world,
                    owner_workflow_uuid=THIRD_WORKFLOW_UUID,
                    owner_node_uuid=SECOND_CHILD_NODE_UUID,
                    target=world.child,
                )

            _assert_zero_parent_or_catalog_write(
                world,
                lambda current=world: current.compile(keyword_arguments={"value": 1}),
                "composite_recursive_reference",
            )
        finally:
            world.close()


def _set_parent_input_schema(world: Any, schema: dict[str, Any]) -> None:
    _set_workflow_meta(
        world,
        PARENT_WORKFLOW_UUID,
        workflow_meta(
            input_schema=schema,
            output_schema=None,
            output_binding=None,
        ),
    )


@pytest.mark.parametrize(
    ("child_allowlist", "parent_allowlist", "expected"),
    [
        ((MATERIAL_TEMPLATE_A_UUID,), (), [MATERIAL_TEMPLATE_A_UUID]),
        ((), (MATERIAL_TEMPLATE_A_UUID,), [MATERIAL_TEMPLATE_A_UUID]),
        (
            (MATERIAL_TEMPLATE_A_UUID, MATERIAL_TEMPLATE_B_UUID),
            (MATERIAL_TEMPLATE_B_UUID, MATERIAL_TEMPLATE_C_UUID),
            [MATERIAL_TEMPLATE_B_UUID],
        ),
        ((), (), None),
    ],
)
def test_resource_slot_omission_is_universal_and_explicit_constraints_intersect(
    tmp_path: Path,
    child_allowlist: tuple[str, ...],
    parent_allowlist: tuple[str, ...],
    expected: list[str] | None,
) -> None:
    child_schema = resource_slot_schema(*child_allowlist)
    world = make_direct_world(
        tmp_path,
        input_schema=child_schema,
        output_schema=child_schema,
    )
    try:
        _set_parent_input_schema(
            world,
            resource_slot_schema(*parent_allowlist),
        )

        expansion = world.compile(
            keyword_arguments={
                "value": {"kind": "workflow_input", "parameter": "value"}
            }
        )

        assert _field(expansion, "diagnostics") == []
        effective = _field(expansion, "effective_parent_input_contract")
        schema = effective["parameters"][0]["schema"]
        if expected is None:
            assert "allowed_resource_template_uuids" not in schema
        else:
            assert schema["allowed_resource_template_uuids"] == expected
    finally:
        world.close()


def test_empty_resource_slot_intersection_is_diagnostic_and_writes_nothing(
    tmp_path: Path,
) -> None:
    child_schema = resource_slot_schema(MATERIAL_TEMPLATE_A_UUID)
    world = make_direct_world(
        tmp_path,
        input_schema=child_schema,
        output_schema=child_schema,
    )
    try:
        _set_parent_input_schema(
            world,
            resource_slot_schema(MATERIAL_TEMPLATE_B_UUID),
        )

        _assert_zero_parent_or_catalog_write(
            world,
            lambda: world.compile(
                keyword_arguments={
                    "value": {"kind": "workflow_input", "parameter": "value"}
                }
            ),
            "composite_resource_constraint_empty",
        )
    finally:
        world.close()
