"""工作流创作（Workflow Authoring）的确定性身份规则。"""

from __future__ import annotations

from uuid import UUID, uuid5

from unilabos.workflow.models import validate_uuid

_COMPOSITE_NODE_PREFIX = "unilabos:c1:node:v1:"


def expanded_node_uuid(invocation_uuid: str, child_node_uuid: str) -> str:
    """把子节点身份确定性派生到一次组合工作流调用命名空间。

    参数：``invocation_uuid`` 是真实调用节点 UUID，``child_node_uuid`` 是已应用
    子工作流中的规范节点 UUID。返回：C1 v1 固定 UUIDv5；非法身份抛出
    ``ValueError``。
    异常：任一身份非规范或为 nil UUID 时抛出 ``ValueError``。
    """

    namespace = UUID(validate_uuid(invocation_uuid))
    child = validate_uuid(child_node_uuid)
    return str(uuid5(namespace, _COMPOSITE_NODE_PREFIX + child))


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
    异常：任一身份非规范或为 nil UUID 时抛出 ``ValueError``。
    """

    normalized_workflow = validate_uuid(workflow_uuid)
    source_node = validate_uuid(source_node_uuid)
    source_handle = validate_uuid(source_handle_uuid)
    target_node = validate_uuid(target_node_uuid)
    target_handle = validate_uuid(target_handle_uuid)
    # C1 v1 与前端共同冻结了该名称字节序；不能改用路径或 JSON 编码。
    name = (
        f"authoring-edge:{source_node}:{source_handle}:"
        f"{target_node}:{target_handle}"
    )
    return str(uuid5(UUID(normalized_workflow), name))


__all__ = ["authoring_edge_uuid", "expanded_node_uuid"]
