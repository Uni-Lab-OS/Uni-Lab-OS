"""F06 R2 组合工作流调用（CompositeWorkflowInvocation）失败关闭 RED。"""

from __future__ import annotations

from copy import deepcopy

import pytest

from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.composite import CompositeAuthoring

from .test_c1_r2_static_expansion_contract import (
    ACTION_VALUE_SOURCE_UUID,
    CHILD_NODE_UUID,
    CHILD_TEMPLATE_UUID,
    CHILD_VALUE_SOURCE_UUID,
    CHILD_VALUE_TARGET_UUID,
    CHILD_WORKFLOW_UUID,
    EXPANDED_CHILD_NODE_UUID,
    INVOCATION_UUID,
    PARENT_WORKFLOW_UUID,
    APPLIED_SOURCE_HASH,
    _world,
    _world_components,
    _nested_world,
)


def _compile(authoring: CompositeAuthoring, **arguments: object):
    """用稳定父图身份编译一次子工作流调用并返回展开结果。"""

    keyword_arguments = arguments.pop("keyword_arguments", {"value": 1})
    return authoring.compile_invocation(
        parent_workflow_uuid=PARENT_WORKFLOW_UUID,
        invocation_uuid=INVOCATION_UUID,
        module="c1_published_lab.workflows.child",
        symbol="prepare_sample",
        keyword_arguments=keyword_arguments,
        **arguments,
    )


def _assert_closed(expansion: object, code: str) -> None:
    """断言组合失败没有泄露任何候选图事实且只返回稳定错误码。"""

    assert getattr(expansion, "invocation_node") is None
    assert getattr(expansion, "nodes") == ()
    assert getattr(expansion, "edges") == ()
    diagnostics = getattr(expansion, "diagnostics")
    assert [item["code"] for item in diagnostics] == [code]


@pytest.mark.parametrize(
    ("damage", "expected"),
    [
        ("missing", "composite_child_not_found"),
        ("unapplied", "composite_child_unapplied"),
        ("stale", "composite_child_unapplied"),
    ],
)
def test_missing_unapplied_and_stale_child_fail_closed(
    damage: str,
    expected: str,
) -> None:
    """缺失、未应用或陈旧的子工作流快照不得产生任何父候选事实。"""

    authoring, provider = _world()
    if damage == "missing":
        provider.snapshots.clear()
    else:
        snapshot = provider.snapshots[CHILD_WORKFLOW_UUID]
        if damage == "unapplied":
            snapshot["applied_source"] = None
        else:
            snapshot["applied_source"]["workflow_revision"] = 6

    _assert_closed(_compile(authoring), expected)
    assert provider.read_count == 1


def test_published_template_provenance_mismatch_fails_closed() -> None:
    """已发布模板的 package 来源与解析结果不一致时拒绝展开。"""

    _authoring, provider, catalog, source_catalog = _world_components()
    templates = [action.detached_template() for action in catalog.actions]
    handles = [
        handle
        for action in catalog.actions
        for handle in action.detached_handles()
    ]
    workflow_template = next(item for item in templates if item["type"] == "workflow")
    workflow_template["meta_data"]["unilab"]["workflow_source"][
        "definition_content_hash"
    ] = "sha256:" + "9" * 64
    damaged_catalog = AuthoringCatalogSnapshot.from_entities(templates, handles)
    authoring = CompositeAuthoring(
        snapshot_provider=provider,
        catalog=damaged_catalog,
        resolver=source_catalog,
    )

    _assert_closed(_compile(authoring), "composite_catalog_mismatch")


@pytest.mark.parametrize(
    "damage",
    ["foreign_node", "missing_handle", "wrong_direction", "missing_coverage"],
)
def test_invalid_boundary_mapping_fails_closed(damage: str) -> None:
    """外部节点、缺失连接点、错误方向或未覆盖输入都被公共校验器拒绝。"""

    authoring, provider = _world()
    snapshot = provider.snapshots[CHILD_WORKFLOW_UUID]
    if damage == "foreign_node":
        snapshot["workflow"]["meta_data"]["unilab"]["output_bindings"]["result"][
            "workflow_node_uuid"
        ] = "a7000000-0000-4000-8000-000000000001"
    else:
        node_meta = snapshot["nodes"][0]["meta_data"]["unilab"]
        if damage == "missing_handle":
            key = "a7000000-0000-4000-8000-000000000002"
        elif damage == "wrong_direction":
            key = ACTION_VALUE_SOURCE_UUID
        else:
            key = None
        node_meta["input_bindings"] = (
            {} if key is None else {key: {"parameter": "value"}}
        )

    _assert_closed(_compile(authoring), "composite_boundary_mapping_invalid")


def test_parent_argument_cannot_reference_expanded_private_handle() -> None:
    """父工作流参数不能绕过调用边界直连展开后的内部私有连接点。"""

    authoring, provider = _world()
    before = deepcopy(provider.snapshots)
    expansion = _compile(
        authoring,
        keyword_arguments={
            "value": {
                "kind": "node_output",
                "workflow_node_uuid": EXPANDED_CHILD_NODE_UUID,
                "source_handle_uuid": ACTION_VALUE_SOURCE_UUID,
            }
        },
    )

    _assert_closed(expansion, "composite_external_private_edge")
    assert provider.snapshots == before
    assert provider.snapshots[CHILD_WORKFLOW_UUID]["nodes"][0]["uuid"] == (
        CHILD_NODE_UUID
    )


def test_self_nested_workflow_reference_uses_stable_cycle_diagnostic() -> None:
    """已应用子图再次调用自身时，以统一递归诊断关闭失败。"""

    authoring, provider, catalog, _source_catalog = _world_components()
    snapshot = provider.snapshots[CHILD_WORKFLOW_UUID]
    workflow_action = next(
        action for action in catalog.actions if action.template["type"] == "workflow"
    )
    node = snapshot["nodes"][0]
    node.update(
        {
            "workflow_node_template_uuid": CHILD_TEMPLATE_UUID,
            "type": "workflow",
            "param": {"value": 1},
            "meta_data": {
                "unilab": {
                    "input_bindings": {
                        CHILD_VALUE_TARGET_UUID: {"parameter": "value"}
                    }
                }
            },
        }
    )
    snapshot["workflow"]["meta_data"]["unilab"]["output_bindings"]["result"] = {
        "kind": "node_output",
        "workflow_node_uuid": CHILD_NODE_UUID,
        "source_handle_uuid": CHILD_VALUE_SOURCE_UUID,
    }
    snapshot["node_templates"] = [workflow_action.detached_template()]
    snapshot["handle_templates"] = workflow_action.detached_handles()

    _assert_closed(_compile(authoring), "composite_recursive_reference")


def test_cross_workflow_cycle_uses_same_stable_diagnostic() -> None:
    """子工作流经叶工作流回指祖先时复用统一递归诊断。"""

    authoring, provider = _nested_world()
    _unused, _provider, base_catalog, _resolver = _world_components()
    child_action = next(
        action
        for action in base_catalog.actions
        if action.template["uuid"] == CHILD_TEMPLATE_UUID
    )
    leaf_snapshot = provider.snapshots[
        "a1000000-0000-4000-8000-000000000002"
    ]
    leaf_node = leaf_snapshot["nodes"][0]
    leaf_node.update(
        {
            "workflow_node_template_uuid": CHILD_TEMPLATE_UUID,
            "type": "workflow",
            "param": {"value": 1},
            "meta_data": {
                "unilab": {
                    "input_bindings": {
                        CHILD_VALUE_TARGET_UUID: {"parameter": "value"}
                    },
                    "composite": {
                        "version": 1,
                        "child_workflow_uuid": CHILD_WORKFLOW_UUID,
                        "child_workflow_revision": 7,
                        "child_applied_source_hash": APPLIED_SOURCE_HASH,
                        "contract_digest": (
                            "sha256:689aaac733eba27d13279d242a71fc3c8bc41f0c"
                            "144d41261dc160a52b46a1cf"
                        ),
                        "composition_allow_transparent": False,
                    },
                }
            },
        }
    )
    leaf_snapshot["workflow"]["meta_data"]["unilab"]["output_bindings"][
        "result"
    ] = {
        "kind": "node_output",
        "workflow_node_uuid": leaf_node["uuid"],
        "source_handle_uuid": CHILD_VALUE_SOURCE_UUID,
    }
    leaf_snapshot["node_templates"] = [child_action.detached_template()]
    leaf_snapshot["handle_templates"] = child_action.detached_handles()

    _assert_closed(_compile(authoring), "composite_recursive_reference")


def test_nested_applied_pin_mismatch_fails_closed() -> None:
    """嵌套调用保存的应用 pin 与当前发布合同时不得静默漂移。"""

    authoring, provider = _nested_world()
    child_snapshot = provider.snapshots[CHILD_WORKFLOW_UUID]
    child_snapshot["nodes"][0]["meta_data"]["unilab"]["composite"][
        "child_applied_source_hash"
    ] = "sha256:" + "6" * 64

    _assert_closed(_compile(authoring), "composite_catalog_mismatch")
