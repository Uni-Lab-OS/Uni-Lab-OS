"""C1 Composite boundary 与 A1 typed Action 的 provider 回归。"""

from unilabos.workflow.graph_validation import validate_graph
from unilabos.workflow.models import WorkflowEdgeWrite, WorkflowNodeWrite

COMPOSITE_NODE_UUID = "91000000-0000-4000-8000-000000000001"
ACTION_NODE_UUID = "91000000-0000-4000-8000-000000000002"
COMPOSITE_TEMPLATE_UUID = "92000000-0000-4000-8000-000000000001"
ACTION_TEMPLATE_UUID = "92000000-0000-4000-8000-000000000002"
SOURCE_HANDLE_UUID = "93000000-0000-4000-8000-000000000001"
TARGET_HANDLE_UUID = "93000000-0000-4000-8000-000000000002"
EDGE_UUID = "94000000-0000-4000-8000-000000000001"


def test_composite_boundary_output_satisfies_typed_action_required_input() -> None:
    nodes = [
        WorkflowNodeWrite(
            uuid=COMPOSITE_NODE_UUID,
            workflow_node_template_uuid=COMPOSITE_TEMPLATE_UUID,
            name="published_child",
            status="idle",
            type="workflow",
        ),
        WorkflowNodeWrite(
            uuid=ACTION_NODE_UUID,
            workflow_node_template_uuid=ACTION_TEMPLATE_UUID,
            name="consume",
            status="idle",
            type="device",
            param={},
        ),
    ]
    templates = {
        COMPOSITE_TEMPLATE_UUID: {
            "uuid": COMPOSITE_TEMPLATE_UUID,
            "type": "workflow",
            "node_type": "workflow",
            "schema": {
                "x-unilabos-workflow-contract": {"version": 1},
            },
        },
        ACTION_TEMPLATE_UUID: {
            "uuid": ACTION_TEMPLATE_UUID,
            "type": "action",
            "node_type": "device",
            "schema": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "object",
                        "properties": {"value": {"type": "number"}},
                        "required": ["value"],
                    },
                    "result": {"type": "object", "properties": {}},
                },
                "required": ["goal", "result"],
                "x-unilabos-action-contract": {"version": 1},
            },
        },
    }
    handles = {
        SOURCE_HANDLE_UUID: {
            "uuid": SOURCE_HANDLE_UUID,
            "workflow_node_template_uuid": COMPOSITE_TEMPLATE_UUID,
            "handle_key": "result",
            "data_key": "result",
            "io_type": "source",
            "type": "number",
            "required": False,
        },
        TARGET_HANDLE_UUID: {
            "uuid": TARGET_HANDLE_UUID,
            "workflow_node_template_uuid": ACTION_TEMPLATE_UUID,
            "handle_key": "value",
            "data_key": "value",
            "io_type": "target",
            "type": "number",
            "required": True,
        },
    }
    edges = [
        WorkflowEdgeWrite(
            uuid=EDGE_UUID,
            source_node_uuid=COMPOSITE_NODE_UUID,
            source_handle_uuid=SOURCE_HANDLE_UUID,
            target_node_uuid=ACTION_NODE_UUID,
            target_handle_uuid=TARGET_HANDLE_UUID,
        )
    ]

    validate_graph(
        nodes=nodes,
        edges=edges,
        templates=templates,
        handles=handles,
        effective_params={node.uuid: node.param or {} for node in nodes},
        workflow_meta_data={},
        node_meta_data={node.uuid: node.meta_data for node in nodes},
    )
