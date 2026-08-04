"""M2A 复合工作流调用（CompositeWorkflowInvocation）输入提供者唯一性回归。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from unilabos.workflow.graph_validation import GraphValidationError, validate_graph
from unilabos.workflow.models import WorkflowEdgeWrite, WorkflowNodeWrite

from .c1_r2_static_expansion_fixture import MATERIAL_TEMPLATE_B_UUID
from .test_m2a_composite_input_resource_guarantee import (
    INVOCATION_NODE_UUID,
    _compile,
)

BOUND_MATERIAL_UUID = "82000000-0000-4000-8000-000000000001"


def test_composite_target_rejects_workflow_binding_and_static_param_together(
    tmp_path: Path,
) -> None:
    """证明同一复合目标不能同时接受工作流输入绑定和静态物料参数。"""

    compiled = _compile(
        tmp_path,
        consumer_resource_template_uuid=MATERIAL_TEMPLATE_B_UUID,
    )
    assert compiled.valid, compiled.diagnostics
    assert compiled.graph is not None
    candidate = deepcopy(compiled.graph)

    # 复合工作流调用（CompositeWorkflowInvocation）的公开输入绑定由真实编译器生成；
    # 同名静态物料（Material）参数构成第二个 provider，必须失败关闭。
    invocation = next(
        node for node in candidate["nodes"] if node["uuid"] == INVOCATION_NODE_UUID
    )
    public_bindings = invocation["meta_data"]["unilab"]["input_bindings"]
    assert len(public_bindings) == 1
    assert list(public_bindings.values()) == [{"parameter": "value"}]
    invocation["param"]["value"] = {
        "uuid": BOUND_MATERIAL_UUID,
        "resource_template_uuid": MATERIAL_TEMPLATE_B_UUID,
    }

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

    with pytest.raises(GraphValidationError, match="输入 'value' 存在多个 Provider"):
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
