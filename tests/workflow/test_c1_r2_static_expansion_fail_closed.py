"""F06 R2 组合工作流调用（CompositeWorkflowInvocation）失败关闭 RED。"""

from __future__ import annotations

from copy import deepcopy

import pytest

from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.composite import CompositeAuthoring

from .test_c1_r2_static_expansion_contract import (
    ACTION_VALUE_SOURCE_UUID,
    CHILD_NODE_UUID,
    CHILD_WORKFLOW_UUID,
    EXPANDED_CHILD_NODE_UUID,
    INVOCATION_UUID,
    PARENT_WORKFLOW_UUID,
    _world,
    _world_components,
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
