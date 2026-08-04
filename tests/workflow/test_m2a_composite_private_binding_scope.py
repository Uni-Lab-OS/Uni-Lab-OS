"""M2A 复合工作流（Composite Workflow）私有输入绑定作用域回归。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.graph_validation import GraphValidationError, validate_graph
from unilabos.workflow.models import WorkflowEdgeWrite, WorkflowNodeWrite

from .c1_r2_static_expansion_fixture import (
    ACTION_VALUE_SOURCE_UUID,
    MATERIAL_TEMPLATE_A_UUID,
    MATERIAL_TEMPLATE_B_UUID,
    resource_slot_schema,
)
from .test_m2a_composite_input_resource_guarantee import (
    CONSUMER_NODE_UUID,
    INVOCATION_NODE_UUID,
    _compile,
)

INCOMPATIBLE_MATERIAL_UUID = "81000000-0000-4000-8000-000000000001"


def _private_binding_collision_candidate(tmp_path: Path) -> dict[str, Any]:
    """从真实编译产物构造子输入名与无关父输入同名的复合图。

    参数：
        tmp_path: 隔离工作流（Workflow）数据库所在的临时目录。

    返回：
        保留真实复合展开层级、但让调用参数由不兼容物料（Material）提供的候选图。
    """

    compiled = _compile(
        tmp_path,
        consumer_resource_template_uuid=MATERIAL_TEMPLATE_B_UUID,
    )
    assert compiled.valid, compiled.diagnostics
    assert compiled.graph is not None
    candidate = deepcopy(compiled.graph)

    # 根复合调用（CompositeWorkflowInvocation）改由模板 A 的固定物料提供；
    # 父工作流（Workflow）中同名的模板 B 输入故意保留为无关合同事实。
    invocation = next(
        node for node in candidate["nodes"] if node["uuid"] == INVOCATION_NODE_UUID
    )
    invocation["param"] = {
        "value": {
            "uuid": INCOMPATIBLE_MATERIAL_UUID,
            "resource_template_uuid": MATERIAL_TEMPLATE_A_UUID,
        }
    }
    invocation["meta_data"]["unilab"]["input_bindings"] = {}

    # 展开后的首个子动作（Action）仍保存 child-local ``value`` 私有绑定；
    # 它与父输入同名，但不引用父工作流输入合同。
    internal_source = next(
        node
        for node in candidate["nodes"]
        if node.get("parent_uuid") == INVOCATION_NODE_UUID
    )
    private_bindings = internal_source["meta_data"]["unilab"]["input_bindings"]
    assert {binding["parameter"] for binding in private_bindings.values()} == {
        "value"
    }

    # 将真实子动作输出声明为无约束隐式透传，并把既有下游消费者纳入同一复合层级。
    source_handle = next(
        handle
        for handle in candidate["handle_templates"]
        if handle["uuid"] == ACTION_VALUE_SOURCE_UUID
    )
    source_handle.update(
        {
            "handle_key": "value",
            "data_key": "value",
            "type": "ResourceSlot",
        }
    )
    source_handle["meta_data"]["unilab"].update(
        {
            "value_schema": resource_slot_schema(),
            "allowed_resource_template_uuids": None,
            "implicit_passthrough": True,
        }
    )
    consumer = next(
        node for node in candidate["nodes"] if node["uuid"] == CONSUMER_NODE_UUID
    )
    consumer["parent_uuid"] = INVOCATION_NODE_UUID
    edge = candidate["edges"][0]
    edge["source_node_uuid"] = internal_source["uuid"]
    edge["source_handle_uuid"] = ACTION_VALUE_SOURCE_UUID
    return candidate


def test_composite_private_binding_cannot_inherit_same_named_parent_guarantee(
    tmp_path: Path,
) -> None:
    """证明 child-local 同名绑定不能把不兼容物料伪装成受限消费者可接受。"""

    candidate = _private_binding_collision_candidate(tmp_path)
    nodes = [
        WorkflowNodeWrite.model_validate(node) for node in candidate["nodes"]
    ]
    edges = [
        WorkflowEdgeWrite.model_validate(edge) for edge in candidate["edges"]
    ]
    templates = {
        template["uuid"]: template for template in candidate["node_templates"]
    }
    handles = {
        handle["uuid"]: handle for handle in candidate["handle_templates"]
    }

    with pytest.raises(
        GraphValidationError,
        match="ResourceSlot producer 不能证明满足下游物料模板约束",
    ):
        validate_graph(
            nodes=nodes,
            edges=edges,
            templates=templates,
            handles=handles,
            effective_params={node.uuid: node.param or {} for node in nodes},
            workflow_meta_data=candidate["workflow"]["meta_data"],
            node_meta_data={node.uuid: node.meta_data for node in nodes},
            validate_workflow_io_contract=True,
        )
