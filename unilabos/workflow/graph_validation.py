"""冻结 Backend 全图 PUT 的本地语义校验。"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

from unilabos.workflow.handle_projection import resource_slot_schema
from unilabos.workflow.json_codec import encode_json, strict_json_equal
from unilabos.workflow.models import WorkflowEdgeWrite, WorkflowNodeWrite
from unilabos.workflow.schema import WorkflowSchemaError, parse_output_contract
from unilabos.workflow.workflow_io import (
    WorkflowIOValidationError,
    validate_workflow_io,
)

_MAX_SCHEMA_DEPTH = 64
_MAX_TIMEOUT_SECONDS = (2**63 - 1) // 1_000_000_000


class GraphValidationError(ValueError):
    """提交的全图不满足冻结 Backend 语义。"""


class MissingTemplateError(GraphValidationError):
    """节点引用的模板不在当前 OS 模板目录中。"""


class CodedGraphValidationError(GraphValidationError):
    """可由 public Workflow seam 稳定暴露的图合同错误。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class MaterialSourceGraphError(CodedGraphValidationError):
    """MaterialSource 的 closed graph 合同错误。"""


def validate_graph(
    *,
    nodes: list[WorkflowNodeWrite],
    edges: list[WorkflowEdgeWrite],
    templates: Mapping[str, dict[str, Any]],
    handles: Mapping[str, dict[str, Any]],
    effective_params: Mapping[str, dict[str, Any]],
    workflow_meta_data: Mapping[str, Any],
    node_meta_data: Mapping[str, dict[str, Any]],
    validate_workflow_io_contract: bool = False,
) -> None:
    """在写事务内校验一份完整替换图。

    参数：
        nodes: 本次完整替换提交中的工作流节点（WorkflowNode）。
        edges: 本次完整替换提交中的工作流边（WorkflowEdge）。
        templates: 当前图权威选择的节点模板目录，以模板 UUID 为键。
        handles: 当前图权威选择的句柄模板目录，以句柄 UUID 为键。
        effective_params: 合并受保护字段后的节点有效参数，以节点 UUID 为键。
        workflow_meta_data: 合并受保护字段后的工作流（Workflow）元数据。
        node_meta_data: 合并受保护字段后的节点元数据，以节点 UUID 为键。
        validate_workflow_io_contract: 是否通过公共工作流 I/O 校验器验证合同。

    返回：
        校验成功时不返回值。

    异常：
        GraphValidationError: 图结构、I/O 或物料合同不成立。
        MissingTemplateError: 节点引用的模板不存在。
    """

    node_by_uuid = {node.uuid: node for node in nodes}
    edge_by_uuid = {edge.uuid: edge for edge in edges}
    if len(node_by_uuid) != len(nodes):
        raise GraphValidationError("工作流节点 UUID 重复")
    if len(edge_by_uuid) != len(edges):
        raise GraphValidationError("工作流边 UUID 重复")

    for node in nodes:
        template_uuid = node.workflow_node_template_uuid
        if template_uuid is not None and template_uuid not in templates:
            raise MissingTemplateError(f"工作流节点模板 {template_uuid} 不存在")
        if node.parent_uuid is not None and node.parent_uuid not in node_by_uuid:
            raise GraphValidationError("父节点不在提交的完整图中")
    _validate_parent_cycles(nodes)
    composite_internal_uuids = _composite_internal_node_uuids(
        nodes,
        templates,
    )
    public_nodes = {
        node_uuid: node
        for node_uuid, node in node_by_uuid.items()
        if node_uuid not in composite_internal_uuids
    }
    validated_io = None
    if validate_workflow_io_contract:
        try:
            validated_io = validate_workflow_io(
                nodes=public_nodes,
                handles=handles,
                workflow_meta_data=workflow_meta_data,
                node_meta_data={
                    node_uuid: node_meta_data[node_uuid] for node_uuid in public_nodes
                },
            )
        except WorkflowIOValidationError as exc:
            raise GraphValidationError("Workflow I/O 合同无效") from exc
    else:
        _validate_output_binding_coverage(workflow_meta_data)
    _validate_material_source_nodes(
        nodes=nodes,
        templates=templates,
        handles=handles,
        effective_params=effective_params,
    )

    for edge in edges:
        if edge.source_node_uuid == edge.target_node_uuid:
            raise GraphValidationError("节点不能连接到自身")
        if (
            edge.source_node_uuid not in node_by_uuid
            or edge.target_node_uuid not in node_by_uuid
        ):
            raise GraphValidationError("边引用了提交图以外的节点")
        _validate_edge_handle(
            node_by_uuid[edge.source_node_uuid],
            edge.source_handle_uuid,
            "source",
            handles,
        )
        _validate_edge_handle(
            node_by_uuid[edge.target_node_uuid],
            edge.target_handle_uuid,
            "target",
            handles,
        )
    bindings_by_node = (
        dict(validated_io.input_bindings)
        if validated_io is not None
        else {
            node.uuid: _validated_input_bindings(
                node,
                node_meta_data[node.uuid],
                workflow_meta_data,
                handles,
                validate_schema_compatibility=False,
            )
            for node in nodes
            if node.uuid not in composite_internal_uuids
        }
    )
    for node_uuid in composite_internal_uuids:
        bindings_by_node[node_uuid] = _validated_private_input_bindings(
            node_by_uuid[node_uuid],
            node_meta_data[node_uuid],
            handles,
        )
    _validate_resource_slot_fan_out(edges=edges, handles=handles)
    _validate_resource_slot_template_compatibility(
        nodes=node_by_uuid,
        edges=edges,
        templates=templates,
        handles=handles,
        effective_params=effective_params,
        input_bindings=bindings_by_node,
        workflow_input_guarantees=_workflow_input_resource_slot_guarantees(
            workflow_meta_data
        ),
    )
    enabled = {
        node.uuid: node
        for node in nodes
        if not node.disabled
        and _node_kind(node, templates) not in {"group", "composite"}
    }
    providers = {node.uuid for node in nodes if not node.disabled}
    enabled_edges: list[WorkflowEdgeWrite] = []
    incoming: dict[tuple[str, str], str] = {}
    connected_inputs: dict[tuple[str, str], str] = {}
    available_data_keys: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        if (
            edge.source_node_uuid not in providers
            or edge.target_node_uuid not in enabled
        ):
            continue
        source_handle = handles[edge.source_handle_uuid]
        target_handle = handles[edge.target_handle_uuid]
        if not _handle_types_compatible(
            source_handle.get("type"),
            target_handle.get("type"),
        ):
            raise GraphValidationError("边两端 Handle 类型不兼容")
        target_input = (edge.target_node_uuid, edge.target_handle_uuid)
        if target_input in incoming:
            raise GraphValidationError("同一目标 Handle 只能有一条入边")
        incoming[target_input] = edge.uuid
        if edge.source_node_uuid in enabled:
            enabled_edges.append(edge)
        if not _dependency_only(source_handle):
            connected_inputs[target_input] = edge.uuid
            available_data_keys[edge.target_node_uuid].append(
                _handle_data_key(target_handle)
            )

    _validate_edge_cycles(enabled, enabled_edges)
    for node_uuid, node in enabled.items():
        param = effective_params[node_uuid]
        bindings = bindings_by_node[node_uuid]
        for handle_uuid in bindings:
            available_data_keys[node_uuid].append(
                _handle_data_key(handles[handle_uuid])
            )
        template_uuid = node.workflow_node_template_uuid
        if template_uuid is not None:
            schema = _parse_schema(templates[template_uuid].get("schema"))
            if schema is not None:
                _validate_schema_value(
                    schema,
                    param,
                    root=schema,
                    path="$",
                    ignore_required=True,
                    depth=0,
                )
                _validate_required_properties(
                    schema,
                    param,
                    root=schema,
                    path="",
                    available={
                        _final_target_data_key(key)
                        for key in available_data_keys[node_uuid]
                        if _final_target_data_key(key)
                    },
                    depth=0,
                )
        _validate_required_handles(
            node,
            param,
            handles.values(),
            connected_inputs,
            bindings,
        )
        _validate_execution_policy(node.execution_policy)
        # D-092: executor 选择属于 Scheduler admission；固定 selector 写入保留
        # executor_binding，未绑定 selector 不得被迫滥用 material_uuid。


def _validate_output_binding_coverage(
    workflow_meta_data: Mapping[str, Any],
) -> None:
    """普通 Graph PUT 仍只执行已经冻结的 root coverage 门。"""

    unilab = workflow_meta_data.get("unilab", {})
    if not isinstance(unilab, dict):
        raise GraphValidationError("Workflow meta_data.unilab 必须是对象")
    try:
        output_contract = parse_output_contract(
            unilab.get("output_contract", {"version": 1, "outputs": []})
        ).to_dict()
    except WorkflowSchemaError as exc:
        raise GraphValidationError("output_contract 不符合 Workflow 合同") from exc
    output_bindings = unilab.get("output_bindings", {})
    if not isinstance(output_bindings, dict) or set(output_bindings) != {
        item["name"] for item in output_contract["outputs"]
    }:
        raise GraphValidationError("Workflow output bindings 不完整")


def _validate_parent_cycles(nodes: Iterable[WorkflowNodeWrite]) -> None:
    parents = {
        node.uuid: node.parent_uuid for node in nodes if node.parent_uuid is not None
    }
    for start in parents:
        visited: set[str] = set()
        current: str | None = start
        while current is not None:
            if current in visited:
                raise GraphValidationError("父子关系形成循环")
            visited.add(current)
            current = parents.get(current)


def _composite_internal_node_uuids(
    nodes: Iterable[WorkflowNodeWrite],
    templates: Mapping[str, dict[str, Any]],
) -> set[str]:
    node_by_uuid = {node.uuid: node for node in nodes}
    composite_uuids = {
        node.uuid
        for node in node_by_uuid.values()
        if _is_composite_template(
            templates.get(node.workflow_node_template_uuid or "", {})
        )
    }
    internal: set[str] = set()
    for node_uuid, node in node_by_uuid.items():
        parent = node.parent_uuid
        seen = {node_uuid}
        while parent is not None:
            if parent in seen or parent not in node_by_uuid:
                raise GraphValidationError("Composite parent hierarchy 不完整")
            if parent in composite_uuids:
                internal.add(node_uuid)
                break
            seen.add(parent)
            parent = node_by_uuid[parent].parent_uuid
    return internal


def _is_composite_template(template: Mapping[str, Any]) -> bool:
    schema = template.get("schema")
    extension = (
        schema.get("x-unilabos-workflow-contract")
        if isinstance(schema, Mapping)
        else None
    )
    return (
        template.get("type") == "workflow"
        and template.get("node_type") == "workflow"
        and isinstance(extension, Mapping)
        and extension.get("version") == 1
    )


def _validate_edge_handle(
    node: WorkflowNodeWrite,
    handle_uuid: str,
    io_type: str,
    handles: Mapping[str, dict[str, Any]],
) -> None:
    template_uuid = node.workflow_node_template_uuid
    if template_uuid is None:
        raise GraphValidationError("有连线的节点必须引用节点模板")
    handle = handles.get(handle_uuid)
    if (
        handle is None
        or handle.get("workflow_node_template_uuid") != template_uuid
        or handle.get("io_type") != io_type
    ):
        raise GraphValidationError("Handle 不属于节点模板或方向错误")


def _validate_resource_slot_fan_out(
    *,
    edges: Iterable[WorkflowEdgeWrite],
    handles: Mapping[str, dict[str, Any]],
) -> None:
    """一个物理 ResourceSlot 输出只能沿一条物料链继续传递。"""

    outgoing: dict[tuple[str, str], int] = defaultdict(int)
    for edge in edges:
        source_handle = handles[edge.source_handle_uuid]
        if source_handle.get("type") != "ResourceSlot":
            continue
        source = (edge.source_node_uuid, edge.source_handle_uuid)
        outgoing[source] += 1
        if outgoing[source] > 1:
            raise CodedGraphValidationError(
                "material_flow_fan_out",
                "同一个 ResourceSlot 输出不能同时进入多个下游节点",
            )


def _validate_resource_slot_template_compatibility(
    *,
    nodes: Mapping[str, WorkflowNodeWrite],
    edges: Iterable[WorkflowEdgeWrite],
    templates: Mapping[str, dict[str, Any]],
    handles: Mapping[str, dict[str, Any]],
    effective_params: Mapping[str, dict[str, Any]],
    input_bindings: Mapping[str, Mapping[str, Mapping[str, Any]]],
    workflow_input_guarantees: Mapping[str, frozenset[str] | None],
) -> None:
    """证明每条物料流（MaterialFlow）边的生产者模板集合可赋给消费者。

    参数：
        nodes: 以工作流节点（WorkflowNode）UUID 为键的完整节点集合。
        edges: 待验证的完整工作流边（WorkflowEdge）集合。
        templates: 以节点模板 UUID 为键的当前模板目录。
        handles: 以句柄 UUID 为键的当前句柄目录。
        effective_params: 以节点 UUID 为键的有效参数。
        input_bindings: 以节点和目标句柄 UUID 索引的工作流（Workflow）输入绑定。
        workflow_input_guarantees: 工作流（Workflow）输入名对应的资源模板（ResourceTemplate）允许集合；空值表示无约束。

    返回：
        所有物料边都满足模板约束时不返回值。

    异常：
        GraphValidationError: 生产者无法证明满足消费者约束。
        MaterialSourceGraphError: 物料来源（MaterialSource）与消费者模板约束冲突。
    """

    edge_list = tuple(edges)

    for edge in edge_list:
        source_handle = handles[edge.source_handle_uuid]
        target_handle = handles[edge.target_handle_uuid]
        if (
            source_handle.get("type") != "ResourceSlot"
            or target_handle.get("type") != "ResourceSlot"
        ):
            continue
        target_templates = _resource_slot_template_allowlist(target_handle)
        if target_templates is None:
            continue

        source_templates = _resource_slot_producer_guarantee(
            source_node_uuid=edge.source_node_uuid,
            source_handle_uuid=edge.source_handle_uuid,
            nodes=nodes,
            edges=edge_list,
            templates=templates,
            handles=handles,
            effective_params=effective_params,
            input_bindings=input_bindings,
            workflow_input_guarantees=workflow_input_guarantees,
            seen=frozenset(),
        )
        if source_templates is None or not source_templates.issubset(target_templates):
            source_node = nodes[edge.source_node_uuid]
            if _node_kind(source_node, templates) == "material_source":
                raise MaterialSourceGraphError(
                    "material_source_conflict",
                    "MaterialSource 物料模板不被下游 ResourceSlot 接受",
                )
            raise GraphValidationError(
                "ResourceSlot producer 不能证明满足下游物料模板约束"
            )


def _resource_slot_producer_guarantee(
    *,
    source_node_uuid: str,
    source_handle_uuid: str,
    nodes: Mapping[str, WorkflowNodeWrite],
    edges: tuple[WorkflowEdgeWrite, ...],
    templates: Mapping[str, dict[str, Any]],
    handles: Mapping[str, dict[str, Any]],
    effective_params: Mapping[str, dict[str, Any]],
    input_bindings: Mapping[str, Mapping[str, Mapping[str, Any]]],
    workflow_input_guarantees: Mapping[str, frozenset[str] | None],
    seen: frozenset[tuple[str, str]],
) -> frozenset[str] | None:
    """沿物料占位符（ResourceSlot）的隐式透传回溯实际生产者模板保证。

    参数：
        source_node_uuid: 当前待证明的生产者节点 UUID。
        source_handle_uuid: 当前待证明的源句柄 UUID。
        nodes: 以节点 UUID 为键的完整节点集合。
        edges: 当前完整工作流边（WorkflowEdge）集合。
        templates: 以节点模板 UUID 为键的模板目录。
        handles: 以句柄 UUID 为键的句柄目录。
        effective_params: 以节点 UUID 为键的有效参数。
        input_bindings: 以节点和目标句柄 UUID 索引的工作流（Workflow）输入绑定。
        workflow_input_guarantees: 工作流（Workflow）输入名对应的资源模板（ResourceTemplate）允许集合。
        seen: 已访问的节点与源句柄身份，用于拒绝循环证明。

    返回：
        可证明的资源模板 UUID 集合；无约束或无法证明时返回 ``None``。
    """

    identity = (source_node_uuid, source_handle_uuid)
    if identity in seen:
        return None
    source_node = nodes[source_node_uuid]
    if _node_kind(source_node, templates) == "material_source":
        template_uuid = effective_params[source_node.uuid].get("resource_template_uuid")
        return (
            frozenset({template_uuid})
            if isinstance(template_uuid, str)
            else frozenset()
        )

    source_handle = handles[source_handle_uuid]
    unilab = _handle_unilab_metadata(source_handle)
    if unilab.get("implicit_passthrough") is True:
        business_name = str(
            source_handle.get("data_key") or source_handle.get("handle_key") or ""
        )
        template_uuid = source_handle.get("workflow_node_template_uuid")
        target_handles = [
            handle
            for handle in handles.values()
            if handle.get("workflow_node_template_uuid") == template_uuid
            and handle.get("io_type") == "target"
            and str(handle.get("data_key") or handle.get("handle_key") or "")
            == business_name
            and handle.get("type") == "ResourceSlot"
        ]
        if len(target_handles) == 1:
            target_handle_uuid = str(target_handles[0].get("uuid") or "")
            incoming = [
                edge
                for edge in edges
                if edge.target_node_uuid == source_node_uuid
                and edge.target_handle_uuid == target_handle_uuid
            ]
            if len(incoming) == 1:
                upstream = incoming[0]
                guarantee = _resource_slot_producer_guarantee(
                    source_node_uuid=upstream.source_node_uuid,
                    source_handle_uuid=upstream.source_handle_uuid,
                    nodes=nodes,
                    edges=edges,
                    templates=templates,
                    handles=handles,
                    effective_params=effective_params,
                    input_bindings=input_bindings,
                    workflow_input_guarantees=workflow_input_guarantees,
                    seen=seen | {identity},
                )
                if guarantee is not None:
                    return guarantee
            elif not incoming:
                binding = input_bindings.get(source_node_uuid, {}).get(
                    target_handle_uuid
                )
                parameter_name = (
                    binding.get("parameter") if isinstance(binding, Mapping) else None
                )
                if (
                    isinstance(parameter_name, str)
                    and parameter_name in workflow_input_guarantees
                ):
                    return workflow_input_guarantees[parameter_name]
    return _resource_slot_template_allowlist(source_handle)


def _workflow_input_resource_slot_guarantees(
    workflow_meta_data: Mapping[str, Any],
) -> dict[str, frozenset[str] | None]:
    """从有效工作流（Workflow）输入合同提取物料占位符（ResourceSlot）模板保证。

    参数：
        workflow_meta_data: 包含服务端所有输入合同的工作流（Workflow）元数据。

    返回：
        以工作流（Workflow）输入名为键的资源模板（ResourceTemplate）允许集合；空值表示该输入无模板约束。

    说明：
        非物料输入不进入结果。合同形状仍由公共 I/O 校验器负责，本函数只读取
        已验证或受保护的合同事实，不自行创造输入绑定。
    """

    unilab = workflow_meta_data.get("unilab")
    input_contract = (
        unilab.get("input_contract") if isinstance(unilab, Mapping) else None
    )
    parameters = (
        input_contract.get("parameters")
        if isinstance(input_contract, Mapping)
        else None
    )
    if not isinstance(parameters, list):
        return {}

    guarantees: dict[str, frozenset[str] | None] = {}
    for parameter in parameters:
        if not isinstance(parameter, Mapping):
            continue
        parameter_name = parameter.get("name")
        schema = parameter.get("schema")
        if not isinstance(parameter_name, str) or not isinstance(schema, Mapping):
            continue
        slot_schema = resource_slot_schema(schema)
        if slot_schema is None:
            continue
        guarantees[parameter_name] = _validated_resource_template_allowlist(
            slot_schema.get("allowed_resource_template_uuids")
        )
    return guarantees


def _handle_unilab_metadata(handle: Mapping[str, Any]) -> Mapping[str, Any]:
    meta_data = handle.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    return unilab if isinstance(unilab, Mapping) else {}


def _resource_slot_template_allowlist(
    handle: Mapping[str, Any],
) -> frozenset[str] | None:
    """读取并校验句柄声明的资源模板（ResourceTemplate）允许集合。

    参数：
        handle: 当前工作流句柄模板。

    返回：
        规范资源模板（ResourceTemplate）UUID 集合；字段缺失或为空时返回 ``None``。

    异常：
        GraphValidationError: 允许列表不是规范且唯一的 UUID 数组。
    """

    meta_data = handle.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    raw = (
        unilab.get("allowed_resource_template_uuids")
        if isinstance(unilab, Mapping)
        else None
    )
    return _validated_resource_template_allowlist(raw)


def _validated_resource_template_allowlist(
    raw: Any,
) -> frozenset[str] | None:
    """把一个资源模板（ResourceTemplate）允许列表校验并冻结为集合。

    参数：
        raw: 来自工作流合同或句柄元数据的允许列表原值。

    返回：
        规范资源模板（ResourceTemplate）UUID 集合；缺失或空列表表示无约束并返回 ``None``。

    异常：
        GraphValidationError: 原值不是规范且唯一的 UUID 数组。
    """

    if raw is None or raw == [] or raw == ():
        return None
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        raise GraphValidationError(
            "ResourceSlot allowed_resource_template_uuids 必须是 UUID 数组"
        )
    result: set[str] = set()
    for value in raw:
        if not isinstance(value, str):
            raise GraphValidationError(
                "ResourceSlot allowed_resource_template_uuids 必须是 UUID 数组"
            )
        try:
            parsed = UUID(value)
        except (AttributeError, ValueError):
            raise GraphValidationError(
                "ResourceSlot allowed_resource_template_uuids 包含无效 UUID"
            ) from None
        if parsed.int == 0 or str(parsed) != value or value in result:
            raise GraphValidationError(
                "ResourceSlot allowed_resource_template_uuids 不是规范唯一 UUID 数组"
            )
        result.add(value)
    return frozenset(result)


def _node_kind(
    node: WorkflowNodeWrite,
    templates: Mapping[str, dict[str, Any]],
) -> str:
    raw_kind = node.type
    if node.workflow_node_template_uuid is not None:
        raw_kind = templates[node.workflow_node_template_uuid].get(
            "node_type",
            "",
        )
    aliases = {
        "device": "device_action",
        "device_action": "device_action",
        "resource_action": "device_action",
        "ilab": "device_action",
        "compute": "compute",
        "condition": "condition",
        "script": "script",
        "py_script": "script",
        "group": "group",
        "tool_call": "tool_call",
        "manual_confirm": "manual_confirm",
        "material_source": "material_source",
        "workflow": "composite",
    }
    kind = aliases.get(str(raw_kind).strip().lower())
    if kind is None:
        raise GraphValidationError(f"不支持的节点执行类型 {raw_kind!r}")
    return kind


def _validate_material_source_nodes(
    *,
    nodes: Iterable[WorkflowNodeWrite],
    templates: Mapping[str, dict[str, Any]],
    handles: Mapping[str, dict[str, Any]],
    effective_params: Mapping[str, dict[str, Any]],
) -> None:
    for node in nodes:
        template_uuid = node.workflow_node_template_uuid
        template = templates.get(template_uuid or "", {})
        is_material_source = node.type == "material_source" or any(
            (
                template.get("class") == "unilabos.workflow.authoring:material_source",
                template.get("name") == "material_source",
                template.get("type") == "material_source",
                template.get("node_type") == "material_source",
            )
        )
        if not is_material_source:
            continue
        material_handles = [
            handle
            for handle in handles.values()
            if handle.get("workflow_node_template_uuid") == template_uuid
        ]
        if (
            template.get("class") != "unilabos.workflow.authoring:material_source"
            or template.get("name") != "material_source"
            or template.get("type") != "material_source"
            or template.get("node_type") != "material_source"
            or node.type != "material_source"
            or node.action_name is not None
            or len(material_handles) != 1
        ):
            raise MaterialSourceGraphError(
                "template_catalog_mismatch",
                "MaterialSource framework template 不符合合同",
            )
        handle = material_handles[0]
        if (
            handle.get("handle_key") != "material"
            or handle.get("io_type") != "source"
            or handle.get("type") != "ResourceSlot"
            or handle.get("required") is not False
            or handle.get("data_source") != "executor"
            or handle.get("data_key") != "material"
        ):
            raise MaterialSourceGraphError(
                "template_catalog_mismatch",
                "MaterialSource framework Handle 不符合合同",
            )
        if node.material_uuid is not None:
            raise MaterialSourceGraphError(
                "invalid_material_source",
                "MaterialSource 顶层 material_uuid 必须为 null",
            )
        _validate_material_source_selector(effective_params[node.uuid])


def _validate_material_source_selector(param: Mapping[str, Any]) -> None:
    expected_keys = {
        "mode",
        "resource_template_uuid",
        "mount",
        "material_uuid",
        "site",
        "slot_range",
        "flow_role",
    }
    if set(param) != expected_keys:
        raise MaterialSourceGraphError(
            "invalid_material_source",
            "MaterialSource selector 必须是 closed object",
        )
    mode = param.get("mode")
    if mode not in {"existing", "create_new"}:
        raise MaterialSourceGraphError(
            "invalid_material_source",
            "MaterialSource mode 不在闭合目录中",
        )
    _require_canonical_uuid(
        param.get("resource_template_uuid"),
        "resource_template_uuid",
    )
    mount = param.get("mount")
    if not isinstance(mount, dict) or set(mount) != {"uuid"}:
        raise MaterialSourceGraphError(
            "invalid_material_source",
            "MaterialSource mount 必须是 closed ResourceSlot",
        )
    _require_canonical_uuid(mount.get("uuid"), "mount.uuid")
    material_uuid = param.get("material_uuid")
    if mode == "create_new" and material_uuid is not None:
        raise MaterialSourceGraphError(
            "invalid_material_source",
            "create_new 禁止指定 material_uuid",
        )
    if material_uuid is not None:
        _require_canonical_uuid(material_uuid, "material_uuid")
    site = param.get("site")
    slot_range = param.get("slot_range")
    if site is not None and slot_range is not None:
        raise MaterialSourceGraphError(
            "invalid_material_source",
            "site 与 slot_range 互斥",
        )
    if site is not None:
        _require_canonical_uuid(site, "site")
    if slot_range is not None:
        if not isinstance(slot_range, list) or not slot_range:
            raise MaterialSourceGraphError(
                "invalid_material_source",
                "slot_range 必须是非空 Site UUID 数组",
            )
        canonical_range = [
            _require_canonical_uuid(value, "slot_range") for value in slot_range
        ]
        if len(set(canonical_range)) != len(canonical_range):
            raise MaterialSourceGraphError(
                "invalid_material_source",
                "slot_range 不能包含重复 Site UUID",
            )
        if canonical_range != sorted(canonical_range):
            raise MaterialSourceGraphError(
                "invalid_material_source",
                "slot_range 必须按 Site UUID 规范排序",
            )
    if param.get("flow_role") not in {
        "primary_sample",
        "aliquot_sample",
        "reagent",
        "consumable",
    }:
        raise MaterialSourceGraphError(
            "invalid_material_source",
            "flow_role 不在闭合目录中",
        )


def _require_canonical_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise MaterialSourceGraphError(
            "invalid_material_source",
            f"MaterialSource {field} 必须是 canonical non-nil UUID",
        )
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError):
        raise MaterialSourceGraphError(
            "invalid_material_source",
            f"MaterialSource {field} 必须是 canonical non-nil UUID",
        ) from None
    if parsed.int == 0 or str(parsed) != value:
        raise MaterialSourceGraphError(
            "invalid_material_source",
            f"MaterialSource {field} 必须是 canonical non-nil UUID",
        )
    return value


def _validate_edge_cycles(
    enabled: Mapping[str, WorkflowNodeWrite],
    edges: Iterable[WorkflowEdgeWrite],
) -> None:
    indegree = {node_uuid: 0 for node_uuid in enabled}
    outgoing: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        indegree[edge.target_node_uuid] += 1
        outgoing[edge.source_node_uuid].append(edge.target_node_uuid)
    ready = [node_uuid for node_uuid, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        current = ready.pop()
        visited += 1
        for target in outgoing[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if visited != len(enabled):
        raise GraphValidationError("工作流图形成循环")


def _handle_types_compatible(source: Any, target: Any) -> bool:
    source_type = str(source or "").strip().lower()
    target_type = str(target or "").strip().lower()
    return (
        source_type == target_type
        or source_type in {"", "any"}
        or target_type in {"", "any"}
    )


def _handle_data_key(handle: Mapping[str, Any]) -> str:
    return str(handle.get("data_key") or handle.get("handle_key") or "").strip()


def _final_target_data_key(data_key: str) -> str:
    return data_key.split("@@@")[-1].strip()


def _dependency_only(handle: Mapping[str, Any]) -> bool:
    if str(handle.get("handle_key") or "").strip().lower() == "ready":
        return True
    data_source = str(handle.get("data_source") or "").strip()
    return data_source.lower() == "dependency"


def _validate_required_handles(
    node: WorkflowNodeWrite,
    param: Mapping[str, Any],
    handles: Iterable[dict[str, Any]],
    incoming: Mapping[tuple[str, str], str],
    bindings: Mapping[str, dict[str, Any]],
) -> None:
    template_uuid = node.workflow_node_template_uuid
    if template_uuid is None:
        return
    for handle in handles:
        if (
            handle.get("workflow_node_template_uuid") != template_uuid
            or handle.get("io_type") != "target"
        ):
            continue
        data_key = _final_target_data_key(_handle_data_key(handle))
        has_default = data_key in param and param[data_key] is not None
        has_edge = (node.uuid, str(handle["uuid"])) in incoming
        has_binding = str(handle["uuid"]) in bindings
        provider_count = sum((has_default, has_edge, has_binding))
        if provider_count > 1:
            raise GraphValidationError(f"输入 {data_key!r} 存在多个 Provider")
        if handle.get("required") and provider_count != 1:
            raise GraphValidationError(f"缺少必填输入 {data_key!r}")
        if has_default and not declared_handle_type_matches(
            param[data_key],
            handle.get("type"),
        ):
            raise GraphValidationError(f"输入 {data_key!r} 的类型不正确")


def _validated_input_bindings(
    node: WorkflowNodeWrite,
    meta_data: Mapping[str, Any],
    workflow_meta_data: Mapping[str, Any],
    handles: Mapping[str, dict[str, Any]],
    *,
    validate_schema_compatibility: bool,
) -> dict[str, dict[str, Any]]:
    """兼容普通 Graph PUT；Authoring 路径使用公共 I/O validator。"""

    unilab = meta_data.get("unilab", {})
    if not isinstance(unilab, dict):
        raise GraphValidationError("Node meta_data.unilab 必须是对象")
    raw_bindings = unilab.get("input_bindings", {})
    if not isinstance(raw_bindings, dict):
        raise GraphValidationError("input_bindings 必须是对象")
    if not raw_bindings:
        return {}
    if node.workflow_node_template_uuid is None:
        raise GraphValidationError("无模板节点不能声明 input_bindings")

    workflow_unilab = workflow_meta_data.get("unilab", {})
    if not isinstance(workflow_unilab, dict):
        raise GraphValidationError("Workflow meta_data.unilab 必须是对象")
    input_contract = workflow_unilab.get("input_contract", {})
    if not isinstance(input_contract, dict):
        raise GraphValidationError("input_contract 必须是对象")
    parameters = input_contract.get("parameters", [])
    if not isinstance(parameters, list):
        raise GraphValidationError("input_contract.parameters 必须是数组")
    parameter_entries = [item for item in parameters if isinstance(item, dict)]

    result: dict[str, dict[str, Any]] = {}
    for handle_uuid, raw_binding in raw_bindings.items():
        handle = handles.get(handle_uuid)
        if (
            handle is None
            or handle.get("workflow_node_template_uuid")
            != node.workflow_node_template_uuid
            or handle.get("io_type") != "target"
        ):
            raise GraphValidationError("input_binding 未引用本节点的目标 Handle")
        if not isinstance(raw_binding, dict) or set(raw_binding) != {"parameter"}:
            raise GraphValidationError("input_binding 必须是闭合对象")
        parameter = raw_binding.get("parameter")
        if not isinstance(parameter, str) or not parameter:
            raise GraphValidationError("input_binding.parameter 无效")
        matches = [item for item in parameter_entries if item.get("name") == parameter]
        if len(matches) != 1:
            raise GraphValidationError("input_binding 必须唯一引用 Workflow 参数")
        parameter_schema = matches[0].get("schema")
        if validate_schema_compatibility and (
            not isinstance(parameter_schema, dict)
            or not workflow_schema_matches_handle_type(
                parameter_schema,
                handle.get("type"),
            )
        ):
            raise GraphValidationError("input_binding 与 Workflow 参数类型不兼容")
        result[handle_uuid] = dict(raw_binding)
    return result


def _validated_private_input_bindings(
    node: WorkflowNodeWrite,
    meta_data: Mapping[str, Any],
    handles: Mapping[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """校验 Composite 内部 binding 形状；child-local 参数已由展开器验证。"""

    unilab = meta_data.get("unilab", {})
    if not isinstance(unilab, dict):
        raise GraphValidationError("Node meta_data.unilab 必须是对象")
    raw_bindings = unilab.get("input_bindings", {})
    if not isinstance(raw_bindings, dict):
        raise GraphValidationError("input_bindings 必须是对象")
    result: dict[str, dict[str, Any]] = {}
    for handle_uuid, raw_binding in raw_bindings.items():
        handle = handles.get(handle_uuid)
        if (
            node.workflow_node_template_uuid is None
            or handle is None
            or handle.get("workflow_node_template_uuid")
            != node.workflow_node_template_uuid
            or handle.get("io_type") != "target"
        ):
            raise GraphValidationError("input_binding 未引用本节点的目标 Handle")
        if not isinstance(raw_binding, dict) or set(raw_binding) != {"parameter"}:
            raise GraphValidationError("input_binding 必须是闭合对象")
        parameter = raw_binding.get("parameter")
        if not isinstance(parameter, str) or not parameter:
            raise GraphValidationError("input_binding.parameter 无效")
        result[handle_uuid] = dict(raw_binding)
    return result


def _validate_execution_policy(policy: Mapping[str, Any]) -> None:
    if "execution_timeout_seconds" not in policy:
        return
    value = policy["execution_timeout_seconds"]
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_TIMEOUT_SECONDS
    ):
        raise GraphValidationError("execution_timeout_seconds 必须是非负整数")


def _parse_schema(raw_schema: Any) -> Any:
    if raw_schema is None:
        return None
    if isinstance(raw_schema, dict):
        schema = raw_schema
    else:
        if not isinstance(raw_schema, str) or raw_schema.strip() == "":
            return None
        try:
            schema = json.loads(raw_schema)
        except (TypeError, ValueError) as exc:
            raise GraphValidationError("节点参数 JSON Schema 无效") from exc
    if not isinstance(schema, (dict, bool)):
        raise GraphValidationError("节点参数 JSON Schema 必须是对象或布尔值")
    if isinstance(schema, dict):
        extension = schema.get("x-unilabos-action-contract")
        if isinstance(extension, dict):
            properties = schema.get("properties")
            goal = properties.get("goal") if isinstance(properties, dict) else None
            if extension.get("version") != 1 or not isinstance(goal, dict):
                raise GraphValidationError("Action 参数 JSON Schema 无效")
            return goal
    return schema


def _resolve_ref(root: Any, reference: str) -> Any:
    if reference == "#":
        return root
    if not reference.startswith("#/"):
        raise GraphValidationError("仅支持本地 JSON Schema 引用")
    current = root
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            raise GraphValidationError("JSON Schema 引用不存在")
        current = current[part]
    return current


def _validate_schema_value(
    schema: Any,
    value: Any,
    *,
    root: Any,
    path: str,
    ignore_required: bool,
    depth: int,
) -> None:
    if depth > _MAX_SCHEMA_DEPTH:
        raise GraphValidationError("JSON Schema 校验深度超限")
    if schema is True:
        return
    if schema is False or not isinstance(schema, dict):
        raise GraphValidationError(f"{path} 不满足 JSON Schema")
    if "$ref" in schema:
        _validate_schema_value(
            _resolve_ref(root, schema["$ref"]),
            value,
            root=root,
            path=path,
            ignore_required=ignore_required,
            depth=depth + 1,
        )
    for child in schema.get("allOf", []):
        _validate_schema_value(
            child,
            value,
            root=root,
            path=path,
            ignore_required=ignore_required,
            depth=depth + 1,
        )
    for keyword in ("anyOf", "oneOf"):
        if keyword not in schema:
            continue
        matches = 0
        for child in schema[keyword]:
            try:
                _validate_schema_value(
                    child,
                    value,
                    root=root,
                    path=path,
                    ignore_required=ignore_required,
                    depth=depth + 1,
                )
            except GraphValidationError:
                continue
            matches += 1
        if matches == 0 or (keyword == "oneOf" and matches != 1):
            raise GraphValidationError(f"{path} 不满足 {keyword}")
    if "not" in schema:
        try:
            _validate_schema_value(
                schema["not"],
                value,
                root=root,
                path=path,
                ignore_required=ignore_required,
                depth=depth + 1,
            )
        except GraphValidationError:
            pass
        else:
            raise GraphValidationError(f"{path} 命中禁止的 JSON Schema")

    declared_types = schema.get("type")
    if declared_types is not None:
        if not isinstance(declared_types, list):
            declared_types = [declared_types]
        if not any(_json_type_matches(value, item) for item in declared_types):
            raise GraphValidationError(f"{path} 的类型不满足 JSON Schema")
    if "const" in schema and not _json_equal(value, schema["const"]):
        raise GraphValidationError(f"{path} 不等于 JSON Schema const")
    if "enum" in schema and not any(
        _json_equal(value, item) for item in schema["enum"]
    ):
        raise GraphValidationError(f"{path} 不在 JSON Schema enum 中")

    if isinstance(value, dict):
        if len(value) < schema.get("minProperties", 0):
            raise GraphValidationError(f"{path} 少于 minProperties")
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            raise GraphValidationError(f"{path} 多于 maxProperties")
        if not ignore_required:
            for name in schema.get("required", []):
                if name not in value:
                    raise GraphValidationError(f"{path} 缺少必填属性 {name!r}")
        properties = schema.get("properties", {})
        for name, child_value in value.items():
            if name in properties:
                _validate_schema_value(
                    properties[name],
                    child_value,
                    root=root,
                    path=f"{path}.{name}",
                    ignore_required=ignore_required,
                    depth=depth + 1,
                )
            elif schema.get("additionalProperties") is False:
                raise GraphValidationError(f"{path} 含未声明属性 {name!r}")
            elif isinstance(schema.get("additionalProperties"), dict):
                _validate_schema_value(
                    schema["additionalProperties"],
                    child_value,
                    root=root,
                    path=f"{path}.{name}",
                    ignore_required=ignore_required,
                    depth=depth + 1,
                )
    if isinstance(value, list) and "items" in schema:
        for index, child_value in enumerate(value):
            _validate_schema_value(
                schema["items"],
                child_value,
                root=root,
                path=f"{path}[{index}]",
                ignore_required=ignore_required,
                depth=depth + 1,
            )
    _validate_scalar_constraints(schema, value, path)


def _validate_scalar_constraints(
    schema: Mapping[str, Any],
    value: Any,
    path: str,
) -> None:
    if isinstance(value, str):
        length = len(value)
        if length < schema.get("minLength", 0):
            raise GraphValidationError(f"{path} 短于 minLength")
        if "maxLength" in schema and length > schema["maxLength"]:
            raise GraphValidationError(f"{path} 长于 maxLength")
        if "pattern" in schema:
            try:
                matched = re.search(schema["pattern"], value)
            except re.error as exc:
                raise GraphValidationError("JSON Schema pattern 无效") from exc
            if matched is None:
                raise GraphValidationError(f"{path} 不匹配 pattern")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise GraphValidationError(f"{path} 少于 minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise GraphValidationError(f"{path} 多于 maxItems")
        if schema.get("uniqueItems") and len(
            {_canonical_json(item) for item in value}
        ) != len(value):
            raise GraphValidationError(f"{path} 含重复数组项")
    if _is_number(value):
        if "minimum" in schema and value < schema["minimum"]:
            raise GraphValidationError(f"{path} 小于 minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise GraphValidationError(f"{path} 大于 maximum")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise GraphValidationError(f"{path} 不大于 exclusiveMinimum")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            raise GraphValidationError(f"{path} 不小于 exclusiveMaximum")
        if "multipleOf" in schema:
            quotient = value / schema["multipleOf"]
            if not math.isclose(quotient, round(quotient)):
                raise GraphValidationError(f"{path} 不是 multipleOf 的倍数")


def _validate_required_properties(
    schema: Any,
    value: Any,
    *,
    root: Any,
    path: str,
    available: set[str],
    depth: int,
) -> None:
    if depth > _MAX_SCHEMA_DEPTH or isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        return
    if "$ref" in schema:
        _validate_required_properties(
            _resolve_ref(root, schema["$ref"]),
            value,
            root=root,
            path=path,
            available=available,
            depth=depth + 1,
        )
    for child in schema.get("allOf", []):
        _validate_required_properties(
            child,
            value,
            root=root,
            path=path,
            available=available,
            depth=depth + 1,
        )
    for keyword in ("anyOf", "oneOf"):
        if keyword not in schema:
            continue
        failures = 0
        for child in schema[keyword]:
            try:
                _validate_required_properties(
                    child,
                    value,
                    root=root,
                    path=path,
                    available=available,
                    depth=depth + 1,
                )
            except GraphValidationError:
                failures += 1
        if failures == len(schema[keyword]):
            raise GraphValidationError(f"{path or '$'} 缺少必填属性")
    object_value = value if isinstance(value, dict) else {}
    for name in schema.get("required", []):
        child_path = f"{path}.{name}" if path else name
        if name not in object_value and child_path not in available:
            raise GraphValidationError(f"缺少 JSON Schema 必填属性 {child_path!r}")
    for name, child_schema in schema.get("properties", {}).items():
        if name not in object_value:
            continue
        child_path = f"{path}.{name}" if path else name
        _validate_required_properties(
            child_schema,
            object_value[name],
            root=root,
            path=child_path,
            available=available,
            depth=depth + 1,
        )


def _json_type_matches(value: Any, declared_type: Any) -> bool:
    expected = str(declared_type).strip().lower()
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            or isinstance(value, float)
            and math.isfinite(value)
            and value.is_integer()
        )
    if expected == "number":
        return _is_number(value)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False


_HANDLE_SCALAR_TYPES = {
    "str": "string",
    "string": "string",
    "int": "integer",
    "integer": "integer",
    "float": "number",
    "double": "number",
    "number": "number",
    "bool": "boolean",
    "boolean": "boolean",
    "dict": "object",
    "map": "object",
    "object": "object",
    "json": "object",
    "null": "null",
}


def _handle_type_shape(declared_type: Any) -> tuple[str, str | None] | None:
    raw = str(declared_type or "").strip().lower()
    if raw in {"", "any", "default"}:
        return "any", None
    if raw == "resourceslot":
        return "slot", None
    if raw in {"array", "list"}:
        return "array", None
    if raw.startswith("list[") and raw.endswith("]"):
        item = raw[5:-1].strip()
        if item == "resourceslot":
            return "array", "slot"
        normalized_item = _HANDLE_SCALAR_TYPES.get(item)
        return ("array", normalized_item) if normalized_item is not None else None
    normalized = _HANDLE_SCALAR_TYPES.get(raw)
    return ("scalar", normalized) if normalized is not None else None


def _resource_slot_reference_matches(value: Any) -> bool:
    return type(value) is dict and type(value.get("uuid")) is str


def _resource_slot_handle_value_matches(value: Any) -> bool:
    if _resource_slot_reference_matches(value):
        return True
    return (
        type(value) is list
        and bool(value)
        and all(_resource_slot_reference_matches(item) for item in value)
    )


def _handle_item_matches(value: Any, item_type: str) -> bool:
    if item_type == "slot":
        return _resource_slot_reference_matches(value)
    return _json_type_matches(value, item_type)


def declared_handle_type_matches(value: Any, declared_type: Any) -> bool:
    """按 Catalog Handle 的完整 v1 vocabulary 判断一个 provider 值。"""

    if value is None:
        return True
    shape = _handle_type_shape(declared_type)
    if shape is None or shape[0] == "any":
        return True
    kind, item_type = shape
    if kind == "slot":
        return _resource_slot_handle_value_matches(value)
    if kind == "scalar":
        assert item_type is not None
        return _json_type_matches(value, item_type)
    if type(value) is not list:
        return False
    if item_type is None:
        return True
    return all(_handle_item_matches(item, item_type) for item in value)


def workflow_schema_matches_handle_type(
    schema: Mapping[str, Any],
    declared_type: Any,
) -> bool:
    """证明 v1 Workflow schema 可为一个 Catalog Handle 供应非空值。"""

    shape = _handle_type_shape(declared_type)
    if shape is None or shape[0] == "any":
        return True
    if "anyOf" in schema:
        members = schema.get("anyOf")
        if type(members) is not list or not members:
            return False
        schema = members[0]
    kind, item_type = shape
    if kind == "slot":
        return schema.get("$slot") == "ResourceSlot"
    if kind == "scalar":
        assert item_type is not None
        actual = schema.get("type")
        return actual == item_type or (item_type == "number" and actual == "integer")
    if schema.get("type") != "array":
        return False
    if item_type is None:
        return True
    items = schema.get("items")
    if type(items) is not dict:
        return False
    if item_type == "slot":
        return items.get("$slot") == "ResourceSlot"
    actual_item = items.get("type")
    return actual_item == item_type or (
        item_type == "number" and actual_item == "integer"
    )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _canonical_json(value: Any) -> bytes:
    return encode_json(value, sort_keys=True)


def _json_equal(left: Any, right: Any) -> bool:
    return strict_json_equal(left, right)


__all__ = [
    "CodedGraphValidationError",
    "GraphValidationError",
    "MaterialSourceGraphError",
    "MissingTemplateError",
    "declared_handle_type_matches",
    "validate_graph",
    "workflow_schema_matches_handle_type",
]
