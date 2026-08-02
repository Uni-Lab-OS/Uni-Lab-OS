"""Authoring graph 的稳定 Node/Edge identity 规则。"""

from __future__ import annotations

from uuid import UUID, uuid5

from unilabos.workflow.models import WorkflowEdgeWrite, validate_uuid

_COMPOSITE_NODE_PREFIX = "unilabos:c1:node:v1:"


def expanded_node_uuid(invocation_uuid: str, child_node_uuid: str) -> str:
    """把 canonical child Node identity 派生到一次 Composite invocation。"""

    namespace = UUID(validate_uuid(invocation_uuid))
    child = validate_uuid(child_node_uuid)
    return str(uuid5(namespace, _COMPOSITE_NODE_PREFIX + child))


def authoring_edge(
    workflow_uuid: str,
    source_node_uuid: str,
    target_node_uuid: str,
    source_handle_uuid: str,
    target_handle_uuid: str,
) -> dict[str, object]:
    """生成现有 authoring edge rule 的 Backend-shaped Edge。"""

    workflow = validate_uuid(workflow_uuid)
    source_node = validate_uuid(source_node_uuid)
    target_node = validate_uuid(target_node_uuid)
    source_handle = validate_uuid(source_handle_uuid)
    target_handle = validate_uuid(target_handle_uuid)
    edge_uuid = str(
        uuid5(
            UUID(workflow),
            "authoring-edge:"
            f"{source_node}:{source_handle}:{target_node}:{target_handle}",
        )
    )
    return WorkflowEdgeWrite(
        uuid=edge_uuid,
        source_node_uuid=source_node,
        target_node_uuid=target_node,
        source_handle_uuid=source_handle,
        target_handle_uuid=target_handle,
        meta_data={},
    ).model_dump(exclude_none=True)


__all__ = ["authoring_edge", "expanded_node_uuid"]
