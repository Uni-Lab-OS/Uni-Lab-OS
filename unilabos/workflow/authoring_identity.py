"""工作流创作（Workflow Authoring）的确定性身份规则。"""

from __future__ import annotations

from uuid import UUID, uuid5

from unilabos.workflow.models import validate_uuid


def authoring_edge_uuid(
    *,
    workflow_uuid: str,
    source_node_uuid: str,
    source_handle_uuid: str,
    target_node_uuid: str,
    target_handle_uuid: str,
) -> str:
    """为一条创作边（Authoring Edge）生成确定性 UUIDv5。

    参数说明：``workflow_uuid`` 是身份命名空间；其余四个 UUID 是源节点、
    源连接点（Handle）、目标节点和目标连接点身份。返回值对相同端点稳定，
    任一端点变化都会产生不同 UUID；非法或 nil UUID 会抛出 ``ValueError``。
    """

    normalized_workflow = validate_uuid(workflow_uuid)
    endpoint_identity = "/".join(
        (
            validate_uuid(source_node_uuid),
            validate_uuid(source_handle_uuid),
            validate_uuid(target_node_uuid),
            validate_uuid(target_handle_uuid),
        )
    )
    return str(uuid5(UUID(normalized_workflow), f"workflow-edge/{endpoint_identity}"))


__all__ = ["authoring_edge_uuid"]
