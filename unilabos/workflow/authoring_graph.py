"""工作流创作中间表示到后端形状候选图的纯转换。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from unilabos.workflow.authoring_ast import (
    ActionDeclaration,
    DeviceDeclaration,
    GroupDeclaration,
    WorkflowProgram,
)
from unilabos.workflow.authoring_identity import authoring_edge_uuid
from unilabos.workflow.authoring_kernel import (
    AuthoringCatalogAction,
    AuthoringCatalogError,
    AuthoringCatalogSnapshot,
)
from unilabos.workflow.authoring_material import (
    MaterialAuthoringError,
    MaterialSourceDeclaration,
    build_material_source_node,
)
from unilabos.workflow.material_graph_validation import (
    MaterialGraphValidationError,
    validate_material_graph_projection,
)
from unilabos.workflow.models import CandidateChangeset
from unilabos.workflow.resource_reference import (
    ResourceReferenceResolutionError,
    ResourceReferenceResolver,
    resolve_resource_reference,
)
from unilabos.workflow.workflow_io import (
    WorkflowIOValidationError,
    handle_value_schema,
    schema_is_assignable,
)


class AuthoringGraphError(ValueError):
    """作者程序无法映射到权威目录或候选图。"""

    def __init__(self, code: str, message: str):
        """保存稳定诊断码和中文消息。

        参数说明：``code`` 供接口判断错误类别，``message`` 供用户理解。
        """

        super().__init__(message)
        self.code = code
        self.message = message


def build_candidate_graph(
    *,
    program: WorkflowProgram,
    catalog: AuthoringCatalogSnapshot,
    applied_graph: Mapping[str, Any],
    resource_reference_resolver: ResourceReferenceResolver | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把静态作者程序构造为完整候选图和变更集（Changeset）。

    参数说明：``program`` 是纯 AST 解析结果，``catalog`` 是不可变目录快照，
    ``applied_graph`` 是当前权威图；``resource_reference_resolver`` 是只读库存
    权威（Inventory Authority）资源身份端口。返回最小目录投影候选图和精确变更集；目录
    缺失、连接点不匹配或输出不成立时抛出 ``AuthoringGraphError``。物料图违反
    物料流线性（MaterialFlowLinearity）或资源模板兼容
    （ResourceTemplate Compatibility）时，也会把内部物料图异常转换为
    ``AuthoringGraphError`` 并保留稳定错误码。
    """

    applied = _graph_containers(applied_graph)
    devices = {device.symbol: device for device in program.devices}
    action_catalog: dict[str, AuthoringCatalogAction] = {}
    result_nodes: dict[
        str,
        tuple[ActionDeclaration | MaterialSourceDeclaration, AuthoringCatalogAction],
    ] = {}
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    parent_by_node = dict(program.parent_by_node)
    source_order = {
        node_uuid: index for index, node_uuid in enumerate(program.source_order)
    }
    for declaration in program.groups:
        try:
            group_catalog = catalog.require_action(
                "unilabos.workflow.authoring:group",
                "group",
            )
        except AuthoringCatalogError as error:
            raise AuthoringGraphError(
                "template_catalog_mismatch",
                "工作流创作目录缺少唯一展示分组模板",
            ) from error
        action_catalog[declaration.node_uuid] = group_catalog
        nodes.append(
            _group_node(
                declaration=declaration,
                catalog_action=group_catalog,
                source_order=source_order[declaration.node_uuid],
            )
        )
    for declaration in program.actions:
        if isinstance(declaration, MaterialSourceDeclaration):
            try:
                # ``node`` 与 ``catalog_action`` 分别是候选事实和框架合同。
                node, catalog_action = build_material_source_node(
                    declaration,
                    catalog=catalog,
                    resource_reference_resolver=resource_reference_resolver,
                )
            except MaterialAuthoringError as error:
                raise AuthoringGraphError(error.code, error.message) from error
            action_catalog[declaration.node_uuid] = catalog_action
            result_nodes[declaration.result_name] = (declaration, catalog_action)
            nodes.append(
                _apply_authoring_structure(
                    node,
                    parent_uuid=parent_by_node.get(declaration.node_uuid),
                    source_order=source_order[declaration.node_uuid],
                )
            )
            continue
        device = devices[declaration.device_symbol]
        try:
            catalog_action = catalog.require_action(
                device.class_identity,
                declaration.action_name,
            )
        except AuthoringCatalogError as error:
            raise AuthoringGraphError(
                "template_catalog_mismatch",
                "工作流创作目录缺少唯一动作模板",
            ) from error
        action_catalog[declaration.node_uuid] = catalog_action
        result_nodes[declaration.result_name] = (declaration, catalog_action)
        nodes.append(
            _apply_authoring_structure(
                _candidate_node(
                    declaration=declaration,
                    device=device,
                    catalog_action=catalog_action,
                    resource_reference_resolver=resource_reference_resolver,
                ),
                parent_uuid=parent_by_node.get(declaration.node_uuid),
                source_order=source_order[declaration.node_uuid],
            )
        )

    for declaration in program.actions:
        target_catalog = action_catalog[declaration.node_uuid]
        for argument_name, binding in declaration.arguments:
            if binding.kind != "node_output":
                continue
            source_declaration, source_catalog = result_nodes[binding.result_name or ""]
            source_handle = _require_handle(
                source_catalog,
                key=str(binding.value),
                io_type="source",
            )
            target_handle = _require_handle(
                target_catalog,
                key=argument_name,
                io_type="target",
            )
            edges.append(
                _candidate_edge(
                    workflow_uuid=program.workflow_uuid,
                    source_node_uuid=source_declaration.node_uuid,
                    source_handle_uuid=str(source_handle["uuid"]),
                    target_node_uuid=declaration.node_uuid,
                    target_handle_uuid=str(target_handle["uuid"]),
                )
            )

    # ``order_dependencies`` 只在相邻执行片段没有真实数据边时补 ready 控制边。
    data_pairs = {
        (edge["source_node_uuid"], edge["target_node_uuid"])
        for edge in edges
    }
    for source_node_uuid, target_node_uuid in dict.fromkeys(
        program.order_dependencies
    ):
        if (source_node_uuid, target_node_uuid) in data_pairs:
            continue
        source_handle = _require_handle(
            action_catalog[source_node_uuid],
            key="ready",
            io_type="source",
        )
        target_handle = _require_handle(
            action_catalog[target_node_uuid],
            key="ready",
            io_type="target",
        )
        edges.append(
            _candidate_edge(
                workflow_uuid=program.workflow_uuid,
                source_node_uuid=source_node_uuid,
                source_handle_uuid=str(source_handle["uuid"]),
                target_node_uuid=target_node_uuid,
                target_handle_uuid=str(target_handle["uuid"]),
            )
        )

    workflow = deepcopy(applied["workflow"])
    workflow["uuid"] = program.workflow_uuid
    workflow["name"] = program.display_name
    workflow["description"] = program.description
    workflow_meta = dict(workflow.get("meta_data") or {})
    unilab_meta = dict(workflow_meta.get("unilab") or {})
    unilab_meta.update(
        {
            "authoring_function_name": program.function_name,
            "authoring_result_record_name": (
                program.result_record_name
                or (
                    "".join(
                        part.capitalize() for part in program.function_name.split("_")
                    )
                    + "Result"
                    if program.outputs
                    else None
                )
            ),
            "input_contract": deepcopy(program.input_contract),
            "output_contract": _output_contract(program, result_nodes),
            "output_bindings": _output_bindings(program, result_nodes),
        }
    )
    workflow_meta["unilab"] = unilab_meta
    workflow["meta_data"] = workflow_meta

    used_template_uuids = {
        str(action.template["uuid"]) for action in action_catalog.values()
    }
    node_templates: list[dict[str, Any]] = []
    handle_templates: list[dict[str, Any]] = []
    for template_uuid in sorted(used_template_uuids):
        action = catalog.require_template(template_uuid)
        node_templates.append(action.detached_template())
        handle_templates.extend(action.detached_handles())
    handle_templates.sort(key=lambda item: str(item["uuid"]))

    graph = {
        "workflow": workflow,
        "nodes": sorted(
            nodes,
            key=lambda item: source_order[str(item["uuid"])],
        ),
        "edges": sorted(edges, key=lambda item: str(item["uuid"])),
        "node_templates": node_templates,
        "handle_templates": handle_templates,
    }
    try:
        validate_material_graph_projection(graph)
    except MaterialGraphValidationError as error:
        raise AuthoringGraphError(error.code, error.message) from error
    changeset = candidate_changeset(graph=graph, applied_graph=applied)
    return graph, changeset


def candidate_changeset(
    *,
    graph: Mapping[str, Any],
    applied_graph: Mapping[str, Any],
) -> dict[str, Any]:
    """计算候选图相对已应用图的精确变更集。

    参数说明：两个图均为后端五集合形状；返回经过 ``CandidateChangeset`` 校验
    的规范字典，数组按 UUID 排序，目录投影变化不独立计入生命周期集合。
    """

    candidate = _graph_containers(graph)
    applied = _graph_containers(applied_graph)
    candidate_nodes = _semantic_entities(candidate["nodes"])
    applied_nodes = _semantic_entities(applied["nodes"])
    candidate_edges = _semantic_entities(candidate["edges"])
    applied_edges = _semantic_entities(applied["edges"])
    expected = {
        "created_node_uuids": sorted(set(candidate_nodes) - set(applied_nodes)),
        "updated_node_uuids": sorted(
            identity
            for identity in set(candidate_nodes) & set(applied_nodes)
            if candidate_nodes[identity] != applied_nodes[identity]
        ),
        "deleted_node_uuids": sorted(set(applied_nodes) - set(candidate_nodes)),
        "created_edge_uuids": sorted(set(candidate_edges) - set(applied_edges)),
        "updated_edge_uuids": sorted(
            identity
            for identity in set(candidate_edges) & set(applied_edges)
            if candidate_edges[identity] != applied_edges[identity]
        ),
        "deleted_edge_uuids": sorted(set(applied_edges) - set(candidate_edges)),
    }
    candidate_unilab = (candidate["workflow"].get("meta_data") or {}).get("unilab")
    applied_unilab = (applied["workflow"].get("meta_data") or {}).get("unilab")
    reserved_changed = _canonical(candidate_unilab) != _canonical(applied_unilab)
    graph_changed = reserved_changed or any(expected.values())
    return CandidateChangeset.model_validate(
        {
            "kind": "graph" if graph_changed else "source_only",
            **expected,
            "reserved_metadata_changed": reserved_changed,
        }
    ).model_dump()


def semantic_graph_equal(left: Any, right: Any) -> bool:
    """比较两个候选图的创作语义而忽略数组顺序和投影时间。

    参数说明：``left`` 和 ``right`` 是待比较对象；结构非法时返回 ``False``，
    合法时比较工作流、节点、边以及目录实体的规范 JSON。
    """

    try:
        return _semantic_graph(left) == _semantic_graph(right)
    except (KeyError, TypeError, ValueError):
        return False


def _group_node(
    *,
    declaration: GroupDeclaration,
    catalog_action: AuthoringCatalogAction,
    source_order: int,
) -> dict[str, Any]:
    """构造一个不参与执行边的展示分组节点（Presentation Group Node）。

    参数说明：``declaration`` 提供稳定节点身份、展示名和并行归属；
    ``catalog_action`` 是唯一框架模板；``source_order`` 是确定性源码顺序。返回：
    后端形状分组节点，其 ``meta_data.unilab`` 足以恢复 ``group/parallel`` 源码；
    异常：目录模板字段缺失时由调用后的候选校验失败关闭。
    """

    template = catalog_action.template
    # ``parallel_scope`` 只关联同一个并行结构内的同级展示分组，不成为执行身份。
    parallel_scope = declaration.parallel_scope
    return {
        "uuid": declaration.node_uuid,
        "workflow_node_template_uuid": str(template["uuid"]),
        "parent_uuid": None,
        "material_uuid": None,
        "name": declaration.title or declaration.name,
        "type": "group",
        "icon": template.get("icon"),
        "pose": {},
        "param": {"name": declaration.name},
        "footer": template.get("footer"),
        "action_name": None,
        "action_type": None,
        "execution_policy": {},
        "disabled": False,
        "minimized": False,
        "script": None,
        "description": (
            declaration.description
            if declaration.description is not None
            else template.get("description")
        ),
        "meta_data": {
            "unilab": {
                "authoring_source_order": source_order,
                "presentation_group": True,
                "parallel_scope": parallel_scope,
                "parallel_order": declaration.parallel_order,
            }
        },
    }


def _apply_authoring_structure(
    node: dict[str, Any],
    *,
    parent_uuid: str | None,
    source_order: int,
) -> dict[str, Any]:
    """把展示父关系与确定性源码顺序加入一个已构造候选节点。

    参数说明：``node`` 是本轮新建、可原位修改的动作或物料来源节点；
    ``parent_uuid`` 是可选展示分组 UUID；``source_order`` 是节点在作者源码中的
    零基顺序。返回：同一节点字典。异常：既有元数据形状非法时抛出 ``TypeError``，
    防止覆盖其他创作事实。
    """

    node["parent_uuid"] = parent_uuid
    meta_data = node.setdefault("meta_data", {})
    unilab = meta_data.setdefault("unilab", {})
    if not isinstance(unilab, dict):
        raise TypeError("候选节点创作元数据必须是对象")
    unilab["authoring_source_order"] = source_order
    return node


def _candidate_node(
    *,
    declaration: ActionDeclaration,
    device: DeviceDeclaration,
    catalog_action: AuthoringCatalogAction,
    resource_reference_resolver: ResourceReferenceResolver | None = None,
) -> dict[str, Any]:
    """构造一个后端写形状节点。

    参数说明：动作声明（ActionDeclaration）提供源码身份；设备声明提供
    执行器绑定（ExecutorBinding）；目录动作（AuthoringCatalogAction）提供
    动作模板（Action Template）和连接点（Handle）定义。返回：不含
    数据库时间字段的节点字典；固定执行器（Fixed Executor）的
    实际设备物料（Material）UUID 同时进入顶层 ``material_uuid`` 和保留
    执行器绑定（ExecutorBinding）元数据；动态执行器绑定（ExecutorBinding）
    保持空值；``resource_reference_resolver`` 把部署业务资源 ID 关闭式解析为
    实际物料 UUID。异常：动作参数连接点或资源身份无法证明时抛出
    ``AuthoringGraphError``。
    """

    params: dict[str, Any] = {}
    input_bindings: dict[str, dict[str, str]] = {}
    # ``resource_refs`` 仅保留规范源码往返需要的部署业务 ID，键使用真实目标
    # 连接点（Handle）UUID；实际物料身份单独进入 ``params``。
    resource_refs: dict[str, dict[str, str]] = {}
    for argument_name, binding in declaration.arguments:
        target_handle = _require_handle(
            catalog_action,
            key=argument_name,
            io_type="target",
        )
        handle_uuid = str(target_handle["uuid"])
        if binding.kind == "literal":
            params[argument_name] = deepcopy(binding.value)
        elif binding.kind == "workflow_input":
            input_bindings[handle_uuid] = {"parameter": str(binding.value)}
        elif binding.kind == "resource_ref":
            try:
                # ``resolved_reference`` 是库存权威证明的实际物料与模板身份。
                resolved_reference = resolve_resource_reference(
                    str(binding.value),
                    resource_reference_resolver,
                )
                _validate_action_resource_reference(
                    resolved_reference,
                    target_handle=target_handle,
                    argument_name=argument_name,
                )
            except ResourceReferenceResolutionError as error:
                raise AuthoringGraphError(
                    "resource_reference_resolution_error",
                    str(error),
                ) from error
            params[argument_name] = {"uuid": resolved_reference["uuid"]}
            resource_refs[handle_uuid] = {"resource_id": str(binding.value)}
    # 作者结果变量是 Python 数据依赖身份，必须与可编辑的节点标题分离保存。
    unilab: dict[str, Any] = {
        "input_bindings": input_bindings,
        "authoring_result_name": declaration.result_name,
    }
    if resource_refs:
        unilab["resource_refs"] = dict(sorted(resource_refs.items()))
    template = catalog_action.template
    if device.device_id is not None:
        unilab["executor_binding"] = {
            "mode": "fixed",
            "device_id": device.device_id,
        }
    # ``device_material_uuid`` 必须是库存权威证明的实际设备物料身份；部署业务
    # ID 只保留在执行器绑定（ExecutorBinding）中供运行时派发。未注入解析端口
    # 时仅兼容作者直接声明规范 UUID，动态 ``device()`` 继续保持空值。
    device_material_uuid = None
    if device.device_id is not None:
        try:
            resolved_device = resolve_resource_reference(
                device.device_id,
                resource_reference_resolver,
            )
        except ResourceReferenceResolutionError as error:
            raise AuthoringGraphError(
                "invalid_executor_binding",
                "固定执行器无法解析为实际设备物料身份",
            ) from error
        # ``expected_device_template_uuid`` 是动作模板声明的设备类型；库存回执若
        # 提供模板身份，必须与它一致，不能把另一类设备绑定到该动作。
        expected_device_template_uuid = template.get("resource_template_uuid")
        resolved_device_template_uuid = resolved_device.get("resource_template_uuid")
        if (
            resolved_device_template_uuid is not None
            and resolved_device_template_uuid != expected_device_template_uuid
        ):
            raise AuthoringGraphError(
                "invalid_executor_binding",
                "固定执行器资源模板与动作模板不一致",
            )
        device_material_uuid = resolved_device["uuid"]
    # 模板标题是未显式覆盖时的节点展示默认值；动作业务名仅作旧目录回退。
    template_title = (
        template.get("display_name") or template.get("name") or declaration.result_name
    )
    return {
        "uuid": declaration.node_uuid,
        "workflow_node_template_uuid": str(template["uuid"]),
        "parent_uuid": None,
        "material_uuid": device_material_uuid,
        "name": declaration.title or template_title,
        "type": str(template.get("node_type") or template.get("type") or "compute"),
        "icon": template.get("icon"),
        "pose": {},
        "param": params,
        "footer": template.get("footer"),
        "action_name": declaration.action_name,
        "action_type": None,
        "execution_policy": {},
        "disabled": False,
        "minimized": False,
        "script": None,
        "description": (
            declaration.description
            if declaration.description is not None
            else template.get("description")
        ),
        "meta_data": {"unilab": unilab},
    }


def _validate_action_resource_reference(
    reference: Mapping[str, str | None],
    *,
    target_handle: Mapping[str, Any],
    argument_name: str,
) -> None:
    """证明动作 ``resource_ref`` 只绑定兼容物料占位符（ResourceSlot）。

    参数：``reference`` 是已解析实际物料身份，``target_handle`` 是动作参数的
    真实目标连接点，``argument_name`` 用于稳定中文诊断。返回：无。异常：目标
    不是物料占位符（ResourceSlot），或资源模板不在允许集合时抛出
    ``AuthoringGraphError``，不能把业务 ID 当普通 JSON 参数放行。
    """

    # ``value_schema`` 是当前目录代际冻结的动作输入值合同。
    value_schema = _handle_schema(target_handle)
    # ``is_resource_slot`` 同时接受规范连接点类型和旧值 Schema 标记；注册表
    # 投影已经把物料参数发布为 ``ResourceSlot``，无需再重复写入 ``$slot``。
    is_resource_slot = (
        target_handle.get("type") == "ResourceSlot"
        or value_schema.get("$slot") == "ResourceSlot"
    )
    if not is_resource_slot:
        raise AuthoringGraphError(
            "resource_reference_resolution_error",
            f"动作参数 {argument_name} 不是物料占位符（ResourceSlot）",
        )
    # ``allowed_templates`` 是该动作输入明确接受的资源模板 UUID 集合；省略表示
    # 不在创作期缩窄模板，但实际物料 UUID 仍已由库存权威验证。
    allowed_templates = value_schema.get("allowed_resource_template_uuids")
    if allowed_templates not in (None, [], ()) and reference.get(
        "resource_template_uuid"
    ) not in allowed_templates:
        raise AuthoringGraphError(
            "resource_reference_resolution_error",
            f"动作参数 {argument_name} 不接受该物料资源模板",
        )


def _candidate_edge(
    *,
    workflow_uuid: str,
    source_node_uuid: str,
    source_handle_uuid: str,
    target_node_uuid: str,
    target_handle_uuid: str,
) -> dict[str, Any]:
    """构造一条带确定性身份的数据边。

    参数说明：工作流及四个端点 UUID 完整描述边；返回后端写形状字典。
    """

    return {
        "uuid": authoring_edge_uuid(
            workflow_uuid=workflow_uuid,
            source_node_uuid=source_node_uuid,
            source_handle_uuid=source_handle_uuid,
            target_node_uuid=target_node_uuid,
            target_handle_uuid=target_handle_uuid,
        ),
        "source_node_uuid": source_node_uuid,
        "target_node_uuid": target_node_uuid,
        "source_handle_uuid": source_handle_uuid,
        "target_handle_uuid": target_handle_uuid,
        "description": None,
        "meta_data": {},
    }


def _require_handle(
    action: AuthoringCatalogAction,
    *,
    key: str,
    io_type: str,
) -> dict[str, Any]:
    """按业务键和方向取得唯一连接点（Handle）。

    参数说明：``action`` 是目录 aggregate，``key`` 和 ``io_type`` 来自作者绑定；
    返回与不可变目录分离的连接点投影，缺失或歧义抛出
    ``AuthoringGraphError``；递归解冻由目录 aggregate 的公共接口完成。
    """

    # ``detached_handles`` 把目录内嵌套只读映射递归还原为普通 JSON 容器；调用方
    # 后续读取或深拷贝值 Schema 时不会尝试 pickle ``mappingproxy``。
    detached_handles = action.detached_handles()
    matches = [
        handle
        for handle in detached_handles
        if handle.get("handle_key") == key and handle.get("io_type") == io_type
    ]
    if len(matches) != 1:
        raise AuthoringGraphError(
            "template_catalog_mismatch",
            f"动作连接点 {io_type}:{key} 缺失或不唯一",
        )
    return matches[0]


def _output_contract(
    program: WorkflowProgram,
    result_nodes: Mapping[
        str,
        tuple[ActionDeclaration | MaterialSourceDeclaration, AuthoringCatalogAction],
    ],
) -> dict[str, Any]:
    """从输出绑定构造版本 1 工作流输出合同。

    参数说明：``program`` 含输入合同和输出声明，``result_nodes`` 提供节点输出
    连接点（Handle）类型。返回：包含版本和规范输出描述列表的工作流输出合同；
    输出引用缺失或歧义、显式结果记录 Schema 与绑定类型不一致、结果记录字段集
    与返回字典不一致时抛出 ``AuthoringGraphError``，不生成部分合同。
    """

    inputs = {
        item["name"]: item["schema"] for item in program.input_contract["parameters"]
    }
    declared = dict(program.declared_output_schemas)
    outputs: list[dict[str, Any]] = []
    for name, binding in program.outputs:
        if binding.kind == "workflow_input":
            schema = deepcopy(inputs[str(binding.value)])
        else:
            _declaration, action = result_nodes[binding.result_name or ""]
            handle = _require_handle(action, key=str(binding.value), io_type="source")
            schema = _handle_schema(handle)
        if name in declared and not schema_is_assignable(schema, declared[name]):
            raise AuthoringGraphError(
                "invalid_workflow_output",
                f"结果记录字段 {name} 与绑定类型不一致",
            )
        outputs.append({"name": name, "schema": schema, "implicit": False})
    if declared and set(declared) != {item["name"] for item in outputs}:
        raise AuthoringGraphError(
            "invalid_workflow_output",
            "结果记录字段与返回字典不一致",
        )
    return {"version": 1, "outputs": outputs}


def _output_bindings(
    program: WorkflowProgram,
    result_nodes: Mapping[
        str,
        tuple[ActionDeclaration | MaterialSourceDeclaration, AuthoringCatalogAction],
    ],
) -> dict[str, dict[str, str]]:
    """把作者输出声明映射为稳定工作流输出绑定。

    参数说明：程序输出引用工作流输入或节点结果；返回按输出名索引的绑定字典。
    """

    bindings: dict[str, dict[str, str]] = {}
    for name, binding in program.outputs:
        if binding.kind == "workflow_input":
            bindings[name] = {"kind": "workflow_input", "parameter": str(binding.value)}
        else:
            declaration, action = result_nodes[binding.result_name or ""]
            handle = _require_handle(action, key=str(binding.value), io_type="source")
            bindings[name] = {
                "kind": "node_output",
                "workflow_node_uuid": declaration.node_uuid,
                "source_handle_uuid": str(handle["uuid"]),
            }
    return bindings


def _handle_schema(handle: Mapping[str, Any]) -> dict[str, Any]:
    """读取连接点（Handle）的规范值 Schema。

    参数说明：优先读取 ``meta_data.unilab.value_schema``，缺失时兼容旧 ``type``；
    返回独立 Schema 字典，未知类型退化为无约束 JSON 对象。
    """

    try:
        # ``handle_value_schema`` 是动作 JSON Schema 到工作流值 Schema 的唯一
        # 适配器，统一处理冻结容器、物料引用、数组、可空和允许集合。
        return handle_value_schema(handle).to_dict()
    except WorkflowIOValidationError as error:
        raise AuthoringGraphError(
            "template_catalog_mismatch",
            "动作连接点值 Schema 无法解析",
        ) from error


def _graph_containers(graph: Mapping[str, Any]) -> dict[str, Any]:
    """复制并验证工作流图五个顶层集合。

    参数说明：``graph`` 必须是映射并含 workflow/nodes/edges/node_templates/
    handle_templates；返回深拷贝，结构非法抛出 ``AuthoringGraphError``。
    """

    required = {"workflow", "nodes", "edges", "node_templates", "handle_templates"}
    if not isinstance(graph, Mapping) or set(graph) != required:
        raise AuthoringGraphError("candidate_invalid", "工作流图必须包含完整五集合")
    copied = deepcopy(dict(graph))
    if not isinstance(copied["workflow"], dict) or any(
        not isinstance(copied[field], list) for field in required - {"workflow"}
    ):
        raise AuthoringGraphError("candidate_invalid", "工作流图集合类型无效")
    return copied


def _semantic_entities(values: list[dict[str, Any]]) -> dict[str, str]:
    """按 UUID 索引实体的稳定创作语义。

    参数说明：``values`` 是节点或边数组；返回 UUID 到规范 JSON 的映射，忽略
    create/update 时间和节点 workflow_uuid 投影字段。
    """

    result: dict[str, str] = {}
    for value in values:
        identity = str(value["uuid"])
        semantic = {
            key: child
            for key, child in value.items()
            if key not in {"create_time", "update_time", "workflow_uuid"}
        }
        result[identity] = _canonical(semantic)
    return result


def _semantic_graph(graph: Mapping[str, Any]) -> str:
    """生成忽略投影时间与数组顺序的候选图规范 JSON。

    参数说明：``graph`` 是后端五集合形状；返回稳定 JSON 字符串。
    """

    value = _graph_containers(graph)
    workflow = {
        key: child
        for key, child in value["workflow"].items()
        if key not in {"create_time", "update_time"}
    }
    payload = {
        "workflow": workflow,
        "nodes": sorted(_semantic_entities(value["nodes"]).values()),
        "edges": sorted(_semantic_entities(value["edges"]).values()),
        "node_templates": sorted(_semantic_entities(value["node_templates"]).values()),
        "handle_templates": sorted(
            _semantic_entities(value["handle_templates"]).values()
        ),
    }
    return _canonical(payload)


def _canonical(value: Any) -> str:
    """把 JSON 值编码为稳定比较字符串。

    参数说明：``value`` 是候选语义；返回排序、紧凑且禁止 NaN 的 JSON。
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "AuthoringGraphError",
    "build_candidate_graph",
    "candidate_changeset",
    "semantic_graph_equal",
]
