"""从已发布组合节点恢复递归展开所需的静态实参。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from unilabos.workflow._composite_values import CompositeFailure, plain


def recover_node_keyword_arguments(
    node: Mapping[str, Any],
    *,
    edges: Sequence[Mapping[str, Any]],
) -> dict[str, object]:
    """恢复已应用组合节点保存的字面量、父输入和节点输出绑定。

    参数：``node`` 是已应用组合调用节点，``edges`` 是同一已发布快照的完整边；
    工作流输入保存在节点元数据，节点输出实参则以入边为权威。返回：字符串键的
    独立参数字典。异常：参数、边或冻结组合合同形状无效时抛出
    ``CompositeFailure``，避免递归展开时把必填实参静默丢失。
    """

    arguments = node.get("param")
    if not isinstance(arguments, Mapping) or any(
        not isinstance(key, str) for key in arguments
    ):
        raise CompositeFailure(
            "composite_boundary_mapping_invalid",
            "/child/nodes/param",
        )
    result = plain(arguments)
    meta_data = node.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    bindings = unilab.get("input_bindings") if isinstance(unilab, Mapping) else None
    composite = unilab.get("composite") if isinstance(unilab, Mapping) else None
    compatibility = (
        composite.get("contract_compatibility")
        if isinstance(composite, Mapping)
        else None
    )
    inputs = compatibility.get("inputs") if isinstance(compatibility, Mapping) else None
    if bindings is None:
        bindings = {}
    if not isinstance(bindings, Mapping) or not isinstance(inputs, list):
        raise CompositeFailure(
            "composite_boundary_mapping_invalid",
            "/child/nodes/input_bindings",
        )
    names_by_handle = {
        str(item.get("handle_uuid")): str(item.get("name"))
        for item in inputs
        if isinstance(item, Mapping)
        and isinstance(item.get("handle_uuid"), str)
        and isinstance(item.get("name"), str)
    }
    for handle_uuid, binding in bindings.items():
        name = names_by_handle.get(str(handle_uuid))
        parameter = binding.get("parameter") if isinstance(binding, Mapping) else None
        if name is None or not isinstance(parameter, str):
            raise CompositeFailure(
                "composite_boundary_mapping_invalid",
                "/child/nodes/input_bindings",
            )
        result[name] = {"kind": "workflow_input", "parameter": parameter}
    node_uuid = str(node.get("uuid") or "")
    for edge in edges:
        if str(edge.get("target_node_uuid") or "") != node_uuid:
            continue
        name = names_by_handle.get(str(edge.get("target_handle_uuid")))
        if name is None:
            continue
        source_node_uuid = edge.get("source_node_uuid")
        source_handle_uuid = edge.get("source_handle_uuid")
        if (
            name in result
            or not isinstance(source_node_uuid, str)
            or not isinstance(source_handle_uuid, str)
        ):
            raise CompositeFailure(
                "composite_boundary_mapping_invalid",
                "/child/nodes/input_bindings",
            )
        result[name] = {
            "kind": "node_output",
            "workflow_node_uuid": source_node_uuid,
            "source_handle_uuid": source_handle_uuid,
        }
    return result
