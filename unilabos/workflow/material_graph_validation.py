"""工作流物料图（Material Graph）的集中不变量校验。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from unilabos.workflow.workflow_io import (
    ValidatedWorkflowIO,
    WorkflowIOValidationError,
    handle_value_schema,
    schema_is_assignable,
)


class MaterialGraphValidationError(ValueError):
    """物料图（Material Graph）违反稳定领域不变量。"""

    def __init__(self, code: str, message: str):
        """保存稳定机器码与中文诊断。

        参数说明：``code`` 是服务和编译器共同透传的错误码，``message`` 是中文
        用户诊断。返回：无；构造可跨公共接缝转换的领域异常。
        """

        super().__init__(message)
        self.code = code
        self.message = message


def validate_material_graph(
    *,
    nodes: Sequence[Any],
    edges: Sequence[Any],
    templates: Mapping[str, Mapping[str, Any]],
    handles: Mapping[str, Mapping[str, Any]],
    effective_params: Mapping[str, Mapping[str, Any]],
    validated_workflow_io: ValidatedWorkflowIO | None,
) -> None:
    """一次性验证工作流物料图（Material Graph）的安全不变量。

    参数说明：``nodes`` 与 ``edges`` 是完整候选图；``templates`` 与 ``handles``
    是同一快照的目录投影；``effective_params`` 是事务内最终参数；
    ``validated_workflow_io`` 是已验证工作流输入/输出（Workflow I/O）事实。
    本阶段实施物料流线性（MaterialFlowLinearity）与资源模板兼容
    （ResourceTemplate Compatibility），不读取库存（Inventory）、物料实例或
    库位（Site）。返回：无；同一物料占位符（ResourceSlot）输出出现多条物理
    消费边，或生产保证不能赋给消费者时抛出 ``MaterialGraphValidationError``。
    """

    # ``node_by_uuid`` 固定每条物料边的生产节点身份；工作流输入/输出
    # （Workflow I/O）事实留给 F05.2-C，且不让本模块越权查询外部存储。
    del validated_workflow_io
    node_by_uuid = {_field(node, "uuid"): node for node in nodes}
    # ``outgoing_edges`` 按来源节点和来源连接点聚合全部提交边；禁用节点也不能把
    # 非法物料分叉藏进持久图，因此这里不按运行状态过滤。
    outgoing_edges: dict[tuple[str, str], list[str]] = defaultdict(list)
    for edge in edges:
        source_node_uuid = _field(edge, "source_node_uuid")
        source_handle_uuid = _field(edge, "source_handle_uuid")
        handle = handles.get(source_handle_uuid)
        if handle is None or not _is_resource_slot_handle(handle):
            continue
        outgoing_edges[(source_node_uuid, source_handle_uuid)].append(
            _field(edge, "uuid")
        )
    if any(len(edge_uuids) > 1 for edge_uuids in outgoing_edges.values()):
        raise MaterialGraphValidationError(
            "material_flow_fan_out",
            "同一个物料占位符（ResourceSlot）输出不能连接多个物理消费者",
        )
    for edge in edges:
        source_node_uuid = _field(edge, "source_node_uuid")
        source_handle_uuid = _field(edge, "source_handle_uuid")
        target_handle_uuid = _field(edge, "target_handle_uuid")
        source_handle = handles.get(source_handle_uuid)
        target_handle = handles.get(target_handle_uuid)
        if (
            source_handle is None
            or target_handle is None
            or not _is_resource_slot_handle(source_handle)
            or not _is_resource_slot_handle(target_handle)
        ):
            continue
        producer_schema = _producer_schema(
            node=node_by_uuid[source_node_uuid],
            source_handle=source_handle,
            templates=templates,
            handles=handles,
            effective_param=effective_params[source_node_uuid],
        )
        consumer_schema = handle_value_schema(target_handle)
        if not schema_is_assignable(producer_schema, consumer_schema):
            raise MaterialGraphValidationError(
                "material_template_mismatch",
                "物料生产者的资源模板保证不能满足消费者约束",
            )


def validate_material_graph_projection(graph: Mapping[str, Any]) -> None:
    """从后端（Backend）形状候选图调用唯一物料图校验入口。

    参数说明：``graph`` 是含节点、边和模板投影的完整五集合；局部索引只把数组
    转换为 ``validate_material_graph`` 所需形状。返回：无；结构错误保留为普通
    ``TypeError``，领域冲突透传 ``MaterialGraphValidationError``。
    """

    nodes = graph.get("nodes")
    edges = graph.get("edges")
    raw_templates = graph.get("node_templates")
    raw_handles = graph.get("handle_templates")
    if not all(
        isinstance(value, list) for value in (nodes, edges, raw_templates, raw_handles)
    ):
        raise TypeError("候选物料图集合无效")
    templates = _identity_index(raw_templates, label="节点模板")
    handles = _identity_index(raw_handles, label="连接点（Handle）")
    effective_params = {
        _field(node, "uuid"): (
            _field(node, "param") if isinstance(_field(node, "param"), Mapping) else {}
        )
        for node in nodes
    }
    validate_material_graph(
        nodes=nodes,
        edges=edges,
        templates=templates,
        handles=handles,
        effective_params=effective_params,
        validated_workflow_io=None,
    )


def _identity_index(
    values: Sequence[Any],
    *,
    label: str,
) -> dict[str, Mapping[str, Any]]:
    """把目录实体数组转换为稳定 UUID 索引。

    参数说明：``values`` 是待索引对象，``label`` 是中文错误对象名；局部
    ``result`` 保存唯一身份。返回：UUID 到实体映射；非对象或重复身份抛出
    ``TypeError``。
    """

    result: dict[str, Mapping[str, Any]] = {}
    for value in values:
        if not isinstance(value, Mapping):
            raise TypeError(f"{label}投影无效")
        identity = _field(value, "uuid")
        if identity in result:
            raise TypeError(f"{label} UUID 重复")
        result[identity] = value
    return result


def _field(entity: Any, name: str) -> str | Mapping[str, Any] | None:
    """兼容读取 Pydantic 实体和普通图字典字段。

    参数说明：``entity`` 是边、节点或模板实体，``name`` 是代码字段名。返回：
    原始映射字段或对象属性；UUID 字段要求非空字符串，``param`` 可返回映射或
    ``None``，非法形状抛出 ``TypeError``。
    """

    value = entity.get(name) if isinstance(entity, Mapping) else getattr(entity, name)
    if name == "param":
        return value
    if not isinstance(value, str) or not value:
        raise TypeError(f"物料图字段 {name} 无效")
    return value


def _is_resource_slot_handle(handle: Mapping[str, Any]) -> bool:
    """判断连接点（Handle）是否传递物料占位符（ResourceSlot）。

    参数说明：``handle`` 是冻结目录连接点投影。返回：规范值 Schema 含
    ``ResourceSlot`` 时为真；Schema 本身非法时返回假，原图校验器仍负责报告
    通用合同错误，避免错误码被物料分叉覆盖。
    """

    try:
        schema = handle_value_schema(handle).to_dict()
    except WorkflowIOValidationError:
        return False
    return _schema_contains_resource_slot(schema)


def _producer_schema(
    *,
    node: Any,
    source_handle: Mapping[str, Any],
    templates: Mapping[str, Mapping[str, Any]],
    handles: Mapping[str, Mapping[str, Any]],
    effective_param: Mapping[str, Any],
):
    """解析一条物料边的生产端规范保证。

    参数说明：``node`` 是生产节点；``source_handle`` 是实际输出连接点；
    ``templates`` 与 ``handles`` 是同一目录快照；``effective_param`` 是最终节点
    参数。局部 ``resource_template_uuid`` 是物料来源（MaterialSource）选择器的
    精确模板，``passthrough_input`` 是隐式同名透传输入。返回：可传给
    ``schema_is_assignable`` 的规范 Schema；目录关系不完整时抛出 ``TypeError``。
    """

    if _is_material_source_node(node, templates=templates):
        resource_template_uuid = effective_param.get("resource_template_uuid")
        if not isinstance(resource_template_uuid, str) or not resource_template_uuid:
            raise TypeError("物料来源缺少精确资源模板身份")
        return {
            "$slot": "ResourceSlot",
            "allowed_resource_template_uuids": [resource_template_uuid],
        }
    if _is_implicit_passthrough(source_handle):
        passthrough_input = _same_name_input(
            source_handle,
            handles=handles,
        )
        return handle_value_schema(passthrough_input)
    return handle_value_schema(source_handle)


def _is_material_source_node(
    node: Any,
    *,
    templates: Mapping[str, Mapping[str, Any]],
) -> bool:
    """判断生产节点是否为框架物料来源（MaterialSource）。

    参数说明：``node`` 是普通图字典或 Pydantic 节点，``templates`` 是目录索引。
    局部 ``template`` 提供权威节点种类。返回：模板或旧节点类型明确为
    ``material_source`` 时为真。
    """

    template_uuid = _field(node, "workflow_node_template_uuid")
    template = templates.get(template_uuid)
    if template is not None and (
        template.get("node_type") == "material_source"
        or template.get("type") == "material_source"
    ):
        return True
    node_type = node.get("type") if isinstance(node, Mapping) else node.type
    return node_type == "material_source"


def _is_implicit_passthrough(handle: Mapping[str, Any]) -> bool:
    """读取连接点（Handle）的服务端隐式透传标记。

    参数说明：``handle`` 是来源连接点目录投影；局部 ``meta_data`` 与 ``unilab``
    是闭合元数据层。返回：仅精确布尔值 ``True`` 表示隐式透传。
    """

    meta_data = handle.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    return isinstance(unilab, Mapping) and unilab.get("implicit_passthrough") is True


def _same_name_input(
    source_handle: Mapping[str, Any],
    *,
    handles: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    """查找隐式物料输出对应的唯一同名输入连接点。

    参数说明：``source_handle`` 提供父模板和业务键，``handles`` 是完整目录索引；
    局部 ``matches`` 收集同父模板、同 ``handle_key`` 的目标连接点。返回：唯一
    输入连接点；缺失或歧义时抛出 ``TypeError``，禁止猜测物料保证。
    """

    matches = [
        handle
        for handle in handles.values()
        if handle.get("workflow_node_template_uuid")
        == source_handle.get("workflow_node_template_uuid")
        and handle.get("handle_key") == source_handle.get("handle_key")
        and handle.get("io_type") == "target"
    ]
    if len(matches) != 1:
        raise TypeError("隐式物料输出缺少唯一同名输入连接点")
    return matches[0]


def _schema_contains_resource_slot(schema: Mapping[str, Any]) -> bool:
    """递归判断规范值 Schema 是否包含物料占位符。

    参数说明：``schema`` 是已解析普通字典；局部 ``members`` 是可空联合成员。
    返回：根或任一 ``anyOf`` 成员声明 ``ResourceSlot`` 时为真。
    """

    if schema.get("$slot") == "ResourceSlot":
        return True
    members = schema.get("anyOf")
    return isinstance(members, list) and any(
        isinstance(member, Mapping) and _schema_contains_resource_slot(member)
        for member in members
    )


__all__ = [
    "MaterialGraphValidationError",
    "validate_material_graph",
    "validate_material_graph_projection",
]
