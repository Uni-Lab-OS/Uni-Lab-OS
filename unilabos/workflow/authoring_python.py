"""后端形状工作流候选图到规范 Python 源码的确定性生成层。"""

from __future__ import annotations

import keyword
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from unilabos.workflow.authoring_graph import AuthoringGraphError
from unilabos.workflow.authoring_kernel import (
    AuthoringCatalogAction,
    AuthoringCatalogError,
    AuthoringCatalogSnapshot,
)
from unilabos.workflow.authoring_material import (
    MaterialAuthoringError,
    RenderedMaterialSource,
    render_material_source_call,
)
from unilabos.workflow.material_graph_validation import (
    MaterialGraphValidationError,
    validate_material_graph_projection,
)
from unilabos.workflow.models import CandidateSourceMapEntry, validate_uuid
from unilabos.workflow.source_coordinates import utf16_length


@dataclass(frozen=True, slots=True)
class RenderedAuthoringSource:
    """一次确定性源码生成的文本和源码映射。"""

    python_source: str
    source_map: list[dict[str, Any]]


def render_authoring_python(
    *,
    graph: Mapping[str, Any],
    catalog: AuthoringCatalogSnapshot,
) -> RenderedAuthoringSource:
    """把完整候选图渲染为规范作者 Python。

    参数说明：``graph`` 是后端五集合候选图，``catalog`` 是同一编译事务目录
    快照；返回可回编译的规范源码和 UTF-16 源码映射。身份或目录投影不一致时
    抛出 ``AuthoringGraphError``；物料图违反物料流线性
    （MaterialFlowLinearity）或资源模板兼容（ResourceTemplate Compatibility）
    时，也会把内部物料图异常转换为 ``AuthoringGraphError`` 并保留稳定错误码。
    """

    try:
        validate_material_graph_projection(graph)
    except MaterialGraphValidationError as error:
        raise AuthoringGraphError(error.code, error.message) from error
    workflow = graph.get("workflow")
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if (
        not isinstance(workflow, Mapping)
        or not isinstance(nodes, list)
        or not isinstance(edges, list)
    ):
        raise AuthoringGraphError("candidate_invalid", "候选图缺少工作流、节点或边")
    workflow_uuid = validate_uuid(workflow.get("uuid"))
    node_by_uuid = _node_index(nodes)
    catalog_by_node = _catalog_projection(node_by_uuid, catalog)
    ordered_nodes = _topological_nodes(node_by_uuid, edges)
    device_symbols, device_imports = _device_symbols(ordered_nodes, catalog_by_node)
    # ``material_sources`` 冻结每个物料来源节点的 import 与调用表达式。
    material_sources: dict[str, RenderedMaterialSource] = {}
    for node in ordered_nodes:
        node_uuid = str(node["uuid"])
        if not _is_material_source(catalog_by_node[node_uuid]):
            continue
        try:
            material_sources[node_uuid] = render_material_source_call(
                node,
                catalog=catalog,
            )
        except MaterialAuthoringError as error:
            raise AuthoringGraphError(error.code, error.message) from error
    # ``material_imports`` 与设备 import 合并后按限定身份稳定排序。
    material_imports = {
        rendered.resource_import for rendered in material_sources.values()
    }
    input_contract, output_contract, output_bindings = _authoring_metadata(workflow)

    annotations = [
        _render_parameter(item) for item in input_contract.get("parameters", [])
    ]
    output_schemas = {
        item["name"]: item["schema"]
        for item in output_contract.get("outputs", [])
        if isinstance(item, Mapping)
        and isinstance(item.get("name"), str)
        and isinstance(item.get("schema"), Mapping)
    }
    output_annotations = {
        name: _render_schema(dict(output_schemas[name])) for name in output_bindings
    }
    typing_names: set[str] = {"TypedDict"} if output_bindings else set()
    needs_field = False
    needs_resource_slot = False
    for _name, annotation, _default, imports in annotations:
        typing_names.update(imports & {"Annotated", "Literal"})
        needs_field = needs_field or "Field(" in annotation
        needs_resource_slot = needs_resource_slot or "ResourceSlot" in annotation
    for annotation, imports in output_annotations.values():
        typing_names.update(imports & {"Annotated", "Literal"})
        needs_field = needs_field or "Field(" in annotation
        needs_resource_slot = needs_resource_slot or "ResourceSlot" in annotation

    lines: list[str] = []
    if typing_names:
        lines.append(f"from typing import {', '.join(sorted(typing_names))}")
        lines.append("")
    if needs_field:
        lines.append("from pydantic import Field")
    for module, symbol in sorted(device_imports | material_imports):
        lines.append(f"from {module} import {symbol}")
    if needs_resource_slot:
        lines.append("from unilabos.registry.placeholder_type import ResourceSlot")
    marker_imports = "device, workflow"
    if material_sources:
        marker_imports += ", MaterialFlowRole, material_source, resource_ref"
    if not output_bindings:
        marker_imports += ", workflow_output"
    lines.append(f"from unilabos.workflow.authoring import {marker_imports}")
    lines.extend(["", ""])
    result_record_name = _safe_identifier(
        str(
            (workflow.get("meta_data") or {})
            .get("unilab", {})
            .get("authoring_result_record_name")
            or f"{workflow.get('name') or 'Workflow'}Result"
        ),
        fallback="WorkflowResult",
    )
    if output_bindings:
        lines.append(f"class {result_record_name}(TypedDict):")
        for output_name in output_bindings:
            annotation, _imports = output_annotations[output_name]
            lines.append(f"    {output_name}: {annotation}")
        lines.extend(["", ""])
    for selector_key, symbol in device_symbols.items():
        class_identity, device_id = selector_key
        class_name = class_identity.rsplit(":", 1)[1]
        argument = "" if device_id is None else repr(device_id)
        lines.append(f"{symbol}: {class_name} = device({argument})")
    lines.extend(["", ""])
    lines.extend(
        [
            "@workflow(",
            f'    workflow_uuid="{workflow_uuid}",',
            f"    displayname={workflow.get('name')!r},",
        ]
    )
    if workflow.get("description") is not None:
        lines.append(f"    description={workflow.get('description')!r},")
    lines.append(")")
    function_name = _safe_identifier(
        str(
            (workflow.get("meta_data") or {})
            .get("unilab", {})
            .get("authoring_function_name")
            or workflow.get("name")
            or "workflow"
        ),
        fallback="workflow",
    )
    if annotations:
        lines.append(f"def {function_name}(")
        lines.append("    *,")
        for name, annotation, default, _imports in annotations:
            suffix = "" if default is _NO_DEFAULT else f" = {default!r}"
            lines.append(f"    {name}: {annotation}{suffix},")
        return_annotation = f" -> {result_record_name}" if output_bindings else ""
        lines.append(f"){return_annotation}:")
    else:
        return_annotation = f" -> {result_record_name}" if output_bindings else ""
        lines.append(f"def {function_name}(){return_annotation}:")

    incoming = _incoming_bindings(edges)
    source_map: list[dict[str, Any]] = []
    # Python 动作结果变量承载节点间数据依赖，必须唯一且不能被节点展示标题改写。
    result_names: set[str] = set()
    if not ordered_nodes and not output_bindings:
        lines.append('    """空工作流。"""')
    for node in ordered_nodes:
        node_uuid = str(node["uuid"])
        action = catalog_by_node[node_uuid]
        start_line = len(lines) + 1
        result_name = _node_result_name(node)
        if result_name in result_names:
            raise AuthoringGraphError("candidate_invalid", "节点作者结果变量重复")
        result_names.add(result_name)
        metadata_comment = _node_metadata_comment(
            node=node,
            action=action,
        )
        if metadata_comment is not None:
            lines.append(f"    {metadata_comment}")
        lines.append(f"    # unilab:node_uuid={node_uuid}")
        if node_uuid in material_sources:
            call = material_sources[node_uuid].call
        else:
            arguments = _render_action_arguments(
                node=node,
                action=action,
                incoming=incoming,
                node_by_uuid=node_by_uuid,
                catalog_by_node=catalog_by_node,
            )
            selector_key = _selector_key(node, action)
            call = (
                f"{device_symbols[selector_key]}."
                f"{node.get('action_name') or action.template['name']}"
                f"({', '.join(arguments)})"
            )
        lines.append(f"    {result_name} = {call}")
        end_line = len(lines)
        source_map.append(
            CandidateSourceMapEntry(
                workflow_node_uuid=node_uuid,
                start_line=start_line,
                start_column=5,
                end_line=end_line,
                end_column=utf16_length(lines[-1]) + 1,
            ).model_dump()
        )
    if output_bindings:
        # 输出绑定字典由编译器按作者声明顺序建立；保留该顺序才能让输出合同
        # 在 Python→图→Python 往返中达到固定点。
        rendered_outputs = [
            f"{name}={_render_output_binding(binding, node_by_uuid, catalog_by_node)}"
            for name, binding in output_bindings.items()
        ]
        rendered_values = ", ".join(
            f"{name!r}: {expression.split('=', 1)[1]}"
            for name, expression in zip(output_bindings, rendered_outputs, strict=True)
        )
        lines.append(f"    return {{{rendered_values}}}")
    else:
        lines.append("    return workflow_output()")
    return RenderedAuthoringSource(
        python_source="\n".join(lines).rstrip() + "\n",
        source_map=source_map,
    )


class _NoDefault:
    """区分无默认值与显式 ``None`` 的内部哨兵类型。"""


_NO_DEFAULT = _NoDefault()


def _node_index(nodes: list[Any]) -> dict[str, dict[str, Any]]:
    """建立无重复节点 UUID 索引。

    参数说明：``nodes`` 是候选节点数组；返回 UUID 到普通节点字典的映射，非法
    节点或重复身份抛出 ``AuthoringGraphError``。
    """

    result: dict[str, dict[str, Any]] = {}
    for value in nodes:
        if not isinstance(value, Mapping):
            raise AuthoringGraphError("candidate_invalid", "候选节点必须是对象")
        node = dict(value)
        identity = validate_uuid(node.get("uuid"))
        if identity in result:
            raise AuthoringGraphError("candidate_invalid", "候选节点 UUID 重复")
        result[identity] = node
    return result


def _catalog_projection(
    nodes: Mapping[str, dict[str, Any]],
    catalog: AuthoringCatalogSnapshot,
) -> dict[str, AuthoringCatalogAction]:
    """为每个候选节点解析权威目录动作。

    参数说明：节点必须引用模板 UUID，``catalog`` 是当前快照；返回节点 UUID 到
    目录动作的映射，未知模板失败关闭。
    """

    result: dict[str, AuthoringCatalogAction] = {}
    for node_uuid, node in nodes.items():
        try:
            result[node_uuid] = catalog.require_template(
                str(node["workflow_node_template_uuid"])
            )
        except (KeyError, AuthoringCatalogError) as error:
            raise AuthoringGraphError(
                "template_catalog_mismatch",
                "候选节点引用了当前目录之外的模板",
            ) from error
    return result


def _topological_nodes(
    nodes: Mapping[str, dict[str, Any]],
    edges: list[Any],
) -> list[dict[str, Any]]:
    """按依赖和 UUID 确定性排序候选节点。

    参数说明：``nodes`` 是节点索引，``edges`` 是完整边数组；返回拓扑序节点，
    引用未知节点或形成环时抛出 ``AuthoringGraphError``。
    """

    indegree = {identity: 0 for identity in nodes}
    outgoing: dict[str, set[str]] = defaultdict(set)
    for value in edges:
        if not isinstance(value, Mapping):
            raise AuthoringGraphError("candidate_invalid", "候选边必须是对象")
        source = str(value.get("source_node_uuid"))
        target = str(value.get("target_node_uuid"))
        if source not in nodes or target not in nodes:
            raise AuthoringGraphError("candidate_invalid", "候选边引用未知节点")
        if target not in outgoing[source]:
            outgoing[source].add(target)
            indegree[target] += 1
    ready = sorted(identity for identity, count in indegree.items() if count == 0)
    ordered: list[dict[str, Any]] = []
    while ready:
        identity = ready.pop(0)
        ordered.append(nodes[identity])
        for target in sorted(outgoing[identity]):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort()
    if len(ordered) != len(nodes):
        raise AuthoringGraphError("candidate_invalid", "候选图包含依赖环")
    return ordered


def _device_symbols(
    nodes: list[dict[str, Any]],
    catalog_by_node: Mapping[str, AuthoringCatalogAction],
) -> tuple[dict[tuple[str, str | None], str], set[tuple[str, str]]]:
    """为候选节点分配确定性设备选择器局部名。

    参数说明：节点顺序和目录映射共同确定设备类及固定设备身份；返回选择器键到
    局部名映射，以及需要导入的 ``(module, class)`` 集合。
    """

    keys: set[tuple[str, str | None]] = set()
    imports: set[tuple[str, str]] = set()
    for node in nodes:
        action = catalog_by_node[str(node["uuid"])]
        if _is_material_source(action):
            continue
        class_identity, device_id = _selector_key(node, action)
        module, class_name = class_identity.rsplit(":", 1)
        imports.add((module, class_name))
        keys.add((class_identity, device_id))
    result: dict[tuple[str, str | None], str] = {}
    used: set[str] = set()
    for index, key in enumerate(
        sorted(keys, key=lambda item: (item[0], item[1] or "")), start=1
    ):
        base = _safe_identifier(key[0].rsplit(":", 1)[1], fallback="device").lower()
        symbol = base
        suffix = 2
        while symbol in used:
            symbol = f"{base}_{suffix}"
            suffix += 1
        used.add(symbol)
        result[key] = symbol
    return result, imports


def _selector_key(
    node: Mapping[str, Any],
    action: AuthoringCatalogAction,
) -> tuple[str, str | None]:
    """读取节点的设备类与可选固定设备身份。

    参数说明：``node`` 携带执行器绑定，``action`` 携带设备类；返回选择器键，
    非法绑定抛出 ``AuthoringGraphError``。
    """

    class_identity = action.template.get("class")
    if not isinstance(class_identity, str) or ":" not in class_identity:
        raise AuthoringGraphError("template_catalog_mismatch", "目录模板缺少设备类")
    unilab = (node.get("meta_data") or {}).get("unilab", {})
    binding = unilab.get("executor_binding") if isinstance(unilab, Mapping) else None
    if binding is None:
        return class_identity, None
    if not isinstance(binding, Mapping) or binding.get("mode") != "fixed":
        raise AuthoringGraphError("candidate_invalid", "节点执行器绑定无效")
    device_id = binding.get("device_id")
    if not isinstance(device_id, str) or not device_id:
        raise AuthoringGraphError("candidate_invalid", "固定设备身份无效")
    return class_identity, device_id


def _authoring_metadata(
    workflow: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """读取候选工作流的输入合同和输出绑定。

    参数说明：``workflow`` 是工作流投影；返回两个普通字典，缺少保留元数据时
    使用空合同，非法类型失败关闭。
    """

    meta_data = workflow.get("meta_data") or {}
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    if not isinstance(unilab, Mapping):
        return (
            {"version": 1, "parameters": []},
            {"version": 1, "outputs": []},
            {},
        )
    input_contract = unilab.get("input_contract", {"version": 1, "parameters": []})
    output_contract = unilab.get("output_contract", {"version": 1, "outputs": []})
    output_bindings = unilab.get("output_bindings", {})
    if (
        not isinstance(input_contract, Mapping)
        or not isinstance(output_contract, Mapping)
        or not isinstance(output_bindings, Mapping)
    ):
        raise AuthoringGraphError("candidate_invalid", "工作流创作元数据无效")
    return dict(input_contract), dict(output_contract), dict(output_bindings)


def _render_parameter(
    descriptor: Mapping[str, Any],
) -> tuple[str, str, Any, set[str]]:
    """把输入合同参数渲染为函数参数片段。

    参数说明：``descriptor`` 是版本 1 参数描述；返回名称、注解、默认值和所需
    typing 名称集合，非法描述抛出 ``AuthoringGraphError``。
    """

    name = descriptor.get("name")
    schema = descriptor.get("schema")
    if not isinstance(name, str) or not isinstance(schema, Mapping):
        raise AuthoringGraphError("candidate_invalid", "工作流输入合同无效")
    annotation, imports = _render_schema(dict(schema))
    default = descriptor.get("default", _NO_DEFAULT)
    return name, annotation, default, imports


def _render_schema(schema: dict[str, Any]) -> tuple[str, set[str]]:
    """把规范值 Schema 渲染为静态 Python 注解。

    参数说明：``schema`` 是工作流版本 1 值 Schema；返回注解文本和所需 typing
    名称，当前合同之外的 Schema 失败关闭。
    """

    if schema.get("$slot") == "ResourceSlot":
        return "ResourceSlot", set()
    if "anyOf" in schema:
        members = schema["anyOf"]
        if (
            not isinstance(members, list)
            or len(members) != 2
            or members[1] != {"type": "null"}
        ):
            raise AuthoringGraphError("candidate_invalid", "nullable Schema 无效")
        base, imports = _render_schema(dict(members[0]))
        return f"{base} | None", imports
    value_type = schema.get("type")
    names = {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "object": "dict[str, object]",
    }
    if "enum" in schema:
        values = schema.get("enum")
        if not isinstance(values, list) or not values:
            raise AuthoringGraphError("candidate_invalid", "枚举 Schema 无效")
        return f"Literal[{', '.join(repr(item) for item in values)}]", {"Literal"}
    if value_type not in names:
        raise AuthoringGraphError("candidate_invalid", "暂不支持的输入 Schema")
    annotation = names[value_type]
    field_arguments: list[str] = []
    for schema_key, field_key in (
        ("minimum", "ge"),
        ("maximum", "le"),
        ("minLength", "min_length"),
        ("maxLength", "max_length"),
    ):
        if schema_key in schema:
            field_arguments.append(f"{field_key}={schema[schema_key]!r}")
    if field_arguments:
        return f"Annotated[{annotation}, Field({', '.join(field_arguments)})]", {
            "Annotated"
        }
    return annotation, set()


def _incoming_bindings(
    edges: list[Any],
) -> dict[tuple[str, str], tuple[str, str]]:
    """按目标节点和目标连接点索引数据边来源。

    参数说明：``edges`` 是完整候选边；返回目标二元组到源二元组映射，重复目标
    失败关闭。
    """

    result: dict[tuple[str, str], tuple[str, str]] = {}
    for value in edges:
        if not isinstance(value, Mapping):
            raise AuthoringGraphError("candidate_invalid", "候选边必须是对象")
        target = (
            str(value.get("target_node_uuid")),
            str(value.get("target_handle_uuid")),
        )
        source = (
            str(value.get("source_node_uuid")),
            str(value.get("source_handle_uuid")),
        )
        if target in result:
            raise AuthoringGraphError("candidate_invalid", "目标连接点存在多条入边")
        result[target] = source
    return result


def _render_action_arguments(
    *,
    node: Mapping[str, Any],
    action: AuthoringCatalogAction,
    incoming: Mapping[tuple[str, str], tuple[str, str]],
    node_by_uuid: Mapping[str, dict[str, Any]],
    catalog_by_node: Mapping[str, AuthoringCatalogAction],
) -> list[str]:
    """渲染一个动作（Action）调用的确定性命名参数。

    参数说明：``node`` 与 ``action`` 提供节点事实和不可变动作合同（Action
    Contract），``incoming`` 提供按目标连接点（Handle）索引的稳定入边，
    ``node_by_uuid`` 与 ``catalog_by_node`` 分别解析源工作流节点（WorkflowNode）
    及其目录动作（Catalog Action）和输出连接点。返回：按业务键排序的
    ``name=value`` 片段；只渲染遗留 ``executor`` 或第 2 版动作合同 ``goal``
    输入，结构依赖不成为动作参数。异常：连接点身份、输入绑定或必填参数无法
    证明时抛出 ``AuthoringGraphError``，不得按节点顺序或名称猜测。
    """

    # ``node_uuid`` 是待渲染工作流节点（WorkflowNode）的稳定身份，用于精确
    # 查询以目标连接点（Handle）为端点的入边。
    node_uuid = str(node["uuid"])
    # ``params`` 是没有工作流输入或上游边提供者时可使用的节点静态参数事实。
    params = node.get("param") or {}
    # ``unilab`` 与 ``input_bindings`` 保存工作流输入到动作输入连接点（Handle）
    # 的稳定绑定，不允许从参数名称反向猜测绑定。
    unilab = (node.get("meta_data") or {}).get("unilab", {})
    input_bindings = (
        unilab.get("input_bindings", {}) if isinstance(unilab, Mapping) else {}
    )
    # ``rendered`` 按动作合同（Action Contract）业务键顺序收集最终命名参数。
    rendered: list[str] = []
    # ``target_handles`` 只包含动作数据输入；ready 等结构连接点（Handle）不得
    # 被渲染成设备动作（Action）参数。
    target_handles = sorted(
        (
            handle
            for handle in action.handles
            if handle.get("io_type") == "target"
            and str(handle.get("data_source") or "executor").lower()
            in {"executor", "goal"}
        ),
        key=lambda item: str(item.get("handle_key")),
    )
    for handle in target_handles:
        # ``handle_uuid`` 是动作输入连接点（Handle）的稳定身份；``key`` 是动作
        # 合同（Action Contract）冻结的业务参数名。
        handle_uuid = str(handle["uuid"])
        key = str(handle["handle_key"])
        # ``expression`` 只接受工作流输入绑定、精确入边或静态参数三种可证明
        # 来源；空值表示当前没有合法提供者。
        expression: str | None = None
        if handle_uuid in input_bindings:
            binding = input_bindings[handle_uuid]
            if not isinstance(binding, Mapping) or not isinstance(
                binding.get("parameter"), str
            ):
                raise AuthoringGraphError("candidate_invalid", "节点输入绑定无效")
            expression = str(binding["parameter"])
        elif (node_uuid, handle_uuid) in incoming:
            # ``source_node_uuid`` 与 ``source_handle_uuid`` 是候选边冻结的源端点
            # 身份，不能替换成节点顺序或展示名称。
            source_node_uuid, source_handle_uuid = incoming[(node_uuid, handle_uuid)]
            # ``source_node`` 与 ``source_action`` 共同解析源结果变量和真实输出
            # 连接点（Handle），保持物料来源（MaterialSource）与普通动作共用路径。
            source_node = node_by_uuid[source_node_uuid]
            source_action = catalog_by_node[source_node_uuid]
            # ``source_handle`` 必须由边上的稳定 UUID 在源目录聚合中唯一命中。
            source_handle = next(
                (
                    item
                    for item in source_action.handles
                    if str(item["uuid"]) == source_handle_uuid
                ),
                None,
            )
            if source_handle is None:
                raise AuthoringGraphError(
                    "candidate_invalid", "数据边源连接点不在目录中"
                )
            # ``source_name`` 是源节点冻结的作者结果变量；只有物料来源
            # （MaterialSource）唯一输出直接引用变量本身，普通动作引用具名结果。
            source_name = _node_result_name(source_node)
            expression = (
                source_name
                if _is_material_source(source_action)
                and source_handle.get("handle_key") == "material"
                else f"{source_name}.{source_handle['handle_key']}"
            )
        elif key in params:
            expression = repr(params[key])
        if expression is not None:
            rendered.append(f"{key}={expression}")
        elif bool(handle.get("required")):
            raise AuthoringGraphError("candidate_invalid", f"动作缺少必填参数 {key}")
    return rendered


def _render_output_binding(
    binding: Any,
    node_by_uuid: Mapping[str, dict[str, Any]],
    catalog_by_node: Mapping[str, AuthoringCatalogAction],
) -> str:
    """渲染一个工作流输出表达式。

    参数说明：``binding`` 是保留元数据中的输出绑定，另两个索引解析节点结果；
    返回 Python 表达式，非法身份失败关闭。
    """

    if not isinstance(binding, Mapping):
        raise AuthoringGraphError("candidate_invalid", "工作流输出绑定无效")
    if binding.get("kind") == "workflow_input":
        parameter = binding.get("parameter")
        if not isinstance(parameter, str):
            raise AuthoringGraphError("candidate_invalid", "工作流输入输出绑定无效")
        return parameter
    if binding.get("kind") != "node_output":
        raise AuthoringGraphError("candidate_invalid", "未知工作流输出绑定类型")
    node_uuid = validate_uuid(binding.get("workflow_node_uuid"))
    handle_uuid = validate_uuid(binding.get("source_handle_uuid"))
    node = node_by_uuid[node_uuid]
    action = catalog_by_node[node_uuid]
    handle = next(
        (item for item in action.handles if str(item["uuid"]) == handle_uuid),
        None,
    )
    if handle is None or handle.get("io_type") != "source":
        raise AuthoringGraphError("candidate_invalid", "工作流输出连接点无效")
    result_name = _node_result_name(node)
    if _is_material_source(action) and handle.get("handle_key") == "material":
        return result_name
    return f"{result_name}.{handle['handle_key']}"


def _is_material_source(action: AuthoringCatalogAction) -> bool:
    """判断目录 aggregate 是否为框架物料来源（MaterialSource）。

    参数说明：``action`` 是节点模板与连接点（Handle）的不可变聚合。返回：
    仅当模板类型和节点类型同时为 ``material_source`` 时为真。
    """

    return (
        action.template.get("type") == "material_source"
        and action.template.get("node_type") == "material_source"
    )


def _node_result_name(node: Mapping[str, Any]) -> str:
    """读取与节点展示标题分离的 Python 动作结果变量。

    参数说明：``node`` 是候选工作流节点（WorkflowNode）；返回可用于生成数据
    依赖表达式的 Python 标识符。新图优先读取 ``authoring_result_name``，旧图才
    从节点名称兼容推导；伪造或不可规范化的显式身份失败关闭。
    """

    metadata = node.get("meta_data") or {}
    unilab = metadata.get("unilab", {}) if isinstance(metadata, Mapping) else {}
    explicit = (
        unilab.get("authoring_result_name") if isinstance(unilab, Mapping) else None
    )
    if explicit is not None:
        if not isinstance(explicit, str) or not explicit:
            raise AuthoringGraphError("candidate_invalid", "节点作者结果变量无效")
        normalized = _safe_identifier(explicit, fallback="result")
        if normalized != explicit:
            raise AuthoringGraphError(
                "candidate_invalid", "节点作者结果变量不是稳定标识符"
            )
        return explicit
    return _safe_identifier(str(node.get("name") or "result"), fallback="result")


def _node_metadata_comment(
    *,
    node: Mapping[str, Any],
    action: AuthoringCatalogAction,
) -> str | None:
    """把节点标题和描述渲染为规范单行展示注释。

    参数说明：``node`` 提供当前展示字段，``action`` 提供动作模板（Action
    Template）的默认显示名和描述；当节点展示字段等于模板默认值时返回
    ``None``，否则返回 ``# [标题]: 描述``。无法无损表示的换行、右方括号或
    空描述会拒绝生成，避免源码往返静默改变候选图。
    """

    title = node.get("name")
    description = node.get("description")
    # 动作模板显示名是人类可读默认值，动作业务名只用于兼容旧目录。
    template_title = action.template.get("display_name") or action.template.get("name")
    template_description = action.template.get("description")
    if title == template_title and description == template_description:
        return None
    if not isinstance(title, str) or not title.strip():
        raise AuthoringGraphError("candidate_invalid", "节点展示标题不能为空")
    if not isinstance(description, str) or not description.strip():
        raise AuthoringGraphError("candidate_invalid", "自定义节点展示必须包含描述")
    normalized_title = title.strip()
    normalized_description = description.strip()
    if "]" in normalized_title or "\n" in normalized_title or "\r" in normalized_title:
        raise AuthoringGraphError("candidate_invalid", "节点展示标题不能写入单行注释")
    if "\n" in normalized_description or "\r" in normalized_description:
        raise AuthoringGraphError("candidate_invalid", "节点展示描述不能写入单行注释")
    return f"# [{normalized_title}]: {normalized_description}"


def _safe_identifier(value: str, *, fallback: str) -> str:
    """把展示文本规范为安全 Python 局部名称。

    参数说明：``value`` 是候选名称，``fallback`` 是清洗为空时的替代；返回非
    关键字标识符，不执行任何源码。
    """

    normalized = re.sub(r"\W+", "_", value, flags=re.UNICODE).strip("_")
    if not normalized or normalized[0].isdigit():
        normalized = fallback
    if keyword.iskeyword(normalized):
        normalized = f"{normalized}_value"
    return normalized


__all__ = ["RenderedAuthoringSource", "render_authoring_python"]
