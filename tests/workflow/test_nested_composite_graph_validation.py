"""嵌套组合工作流（Composite Workflow）的公共图校验合同。"""

from __future__ import annotations

from unilabos.workflow.graph_validation import validate_graph
from unilabos.workflow.models import WorkflowNodeWrite

OUTER_UUID = "81000000-0000-4000-8000-000000000001"
INNER_UUID = "81000000-0000-4000-8000-000000000002"
LEAF_UUID = "81000000-0000-4000-8000-000000000003"
OUTER_TEMPLATE = "82000000-0000-4000-8000-000000000001"
INNER_TEMPLATE = "82000000-0000-4000-8000-000000000002"
LEAF_TEMPLATE = "82000000-0000-4000-8000-000000000003"
OUTER_TARGET = "83000000-0000-4000-8000-000000000001"
LEAF_TARGET = "83000000-0000-4000-8000-000000000002"
INNER_TARGET = "83000000-0000-4000-8000-000000000003"


def test_disabled_opaque_composite_does_not_validate_absent_descendants() -> None:
    """禁用组合只保留审阅边界时，不要求其未加载子树仍出现在父执行图。"""

    nodes = [
        WorkflowNodeWrite(
            uuid=OUTER_UUID,
            workflow_node_template_uuid=OUTER_TEMPLATE,
            name="只读 operation 视图",
            type="workflow",
            param={"value": 7},
            disabled=True,
        )
    ]
    templates = {
        OUTER_TEMPLATE: {"uuid": OUTER_TEMPLATE, "node_type": "workflow"},
    }
    handles = {
        OUTER_TARGET: {
            "uuid": OUTER_TARGET,
            "workflow_node_template_uuid": OUTER_TEMPLATE,
            "handle_key": "value",
            "data_key": "value",
            "io_type": "target",
            "type": "integer",
            "required": True,
        }
    }
    node_meta_data = {
        OUTER_UUID: {
            "unilab": {
                "composite": {
                    "target_mappings": {
                        OUTER_TARGET: [
                            {
                                "workflow_node_uuid": LEAF_UUID,
                                "target_handle_uuid": LEAF_TARGET,
                            }
                        ]
                    }
                }
            }
        }
    }

    validate_graph(
        nodes=nodes,
        edges=[],
        templates=templates,
        handles=handles,
        effective_params={OUTER_UUID: {"value": 7}},
        workflow_meta_data={},
        node_meta_data=node_meta_data,
    )


def test_outer_boundary_may_project_to_nested_descendant() -> None:
    """外层输入可投影到孙动作，但不能因此被误判为越过调用边界。"""

    nodes = [
        WorkflowNodeWrite(
            uuid=OUTER_UUID,
            workflow_node_template_uuid=OUTER_TEMPLATE,
            name="外层组合",
            type="workflow",
            param={"value": 7},
        ),
        WorkflowNodeWrite(
            uuid=INNER_UUID,
            workflow_node_template_uuid=INNER_TEMPLATE,
            parent_uuid=OUTER_UUID,
            name="内层组合",
            type="workflow",
            param={},
        ),
        WorkflowNodeWrite(
            uuid=LEAF_UUID,
            workflow_node_template_uuid=LEAF_TEMPLATE,
            parent_uuid=INNER_UUID,
            name="叶动作",
            type="compute",
            param={},
        ),
    ]
    templates = {
        OUTER_TEMPLATE: {"uuid": OUTER_TEMPLATE, "node_type": "workflow"},
        INNER_TEMPLATE: {"uuid": INNER_TEMPLATE, "node_type": "workflow"},
        LEAF_TEMPLATE: {
            "uuid": LEAF_TEMPLATE,
            "node_type": "compute",
            "schema": {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            },
        },
    }
    handles = {
        OUTER_TARGET: {
            "uuid": OUTER_TARGET,
            "workflow_node_template_uuid": OUTER_TEMPLATE,
            "handle_key": "value",
            "data_key": "value",
            "io_type": "target",
            "type": "integer",
            "required": True,
        },
        LEAF_TARGET: {
            "uuid": LEAF_TARGET,
            "workflow_node_template_uuid": LEAF_TEMPLATE,
            "handle_key": "value",
            "data_key": "value",
            "io_type": "target",
            "type": "integer",
            "required": True,
        },
    }
    node_meta_data = {
        OUTER_UUID: {
            "unilab": {
                "composite": {
                    "target_mappings": {
                        OUTER_TARGET: [
                            {
                                "workflow_node_uuid": LEAF_UUID,
                                "target_handle_uuid": LEAF_TARGET,
                            }
                        ]
                    }
                }
            }
        },
        INNER_UUID: {},
        LEAF_UUID: {},
    }

    validate_graph(
        nodes=nodes,
        edges=[],
        templates=templates,
        handles=handles,
        effective_params={OUTER_UUID: {"value": 7}, INNER_UUID: {}, LEAF_UUID: {}},
        workflow_meta_data={},
        node_meta_data=node_meta_data,
    )


def test_nested_boundary_projection_is_parent_first_independent_of_node_order() -> None:
    """任意节点存储顺序下，根输入都应逐层投影到叶动作。"""

    nodes = [
        WorkflowNodeWrite(
            uuid=LEAF_UUID,
            workflow_node_template_uuid=LEAF_TEMPLATE,
            parent_uuid=INNER_UUID,
            name="叶动作",
            type="compute",
            param={},
        ),
        WorkflowNodeWrite(
            uuid=INNER_UUID,
            workflow_node_template_uuid=INNER_TEMPLATE,
            parent_uuid=OUTER_UUID,
            name="内层组合",
            type="workflow",
            param={},
        ),
        WorkflowNodeWrite(
            uuid=OUTER_UUID,
            workflow_node_template_uuid=OUTER_TEMPLATE,
            name="外层组合",
            type="workflow",
            param={"value": 7},
        ),
    ]
    templates = {
        OUTER_TEMPLATE: {"uuid": OUTER_TEMPLATE, "node_type": "workflow"},
        INNER_TEMPLATE: {"uuid": INNER_TEMPLATE, "node_type": "workflow"},
        LEAF_TEMPLATE: {
            "uuid": LEAF_TEMPLATE,
            "node_type": "compute",
            "schema": {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            },
        },
    }
    handles = {
        OUTER_TARGET: {
            "uuid": OUTER_TARGET,
            "workflow_node_template_uuid": OUTER_TEMPLATE,
            "handle_key": "value",
            "data_key": "value",
            "io_type": "target",
            "type": "integer",
            "required": True,
        },
        INNER_TARGET: {
            "uuid": INNER_TARGET,
            "workflow_node_template_uuid": INNER_TEMPLATE,
            "handle_key": "value",
            "data_key": "value",
            "io_type": "target",
            "type": "integer",
            "required": True,
        },
        LEAF_TARGET: {
            "uuid": LEAF_TARGET,
            "workflow_node_template_uuid": LEAF_TEMPLATE,
            "handle_key": "value",
            "data_key": "value",
            "io_type": "target",
            "type": "integer",
            "required": True,
        },
    }
    node_meta_data = {
        OUTER_UUID: {
            "unilab": {
                "composite": {
                    "target_mappings": {
                        OUTER_TARGET: [
                            {
                                "workflow_node_uuid": INNER_UUID,
                                "target_handle_uuid": INNER_TARGET,
                            }
                        ]
                    }
                }
            }
        },
        INNER_UUID: {
            "unilab": {
                "composite": {
                    "target_mappings": {
                        INNER_TARGET: [
                            {
                                "workflow_node_uuid": LEAF_UUID,
                                "target_handle_uuid": LEAF_TARGET,
                            }
                        ]
                    }
                }
            }
        },
        LEAF_UUID: {},
    }

    validate_graph(
        nodes=nodes,
        edges=[],
        templates=templates,
        handles=handles,
        effective_params={OUTER_UUID: {"value": 7}, INNER_UUID: {}, LEAF_UUID: {}},
        workflow_meta_data={},
        node_meta_data=node_meta_data,
    )
