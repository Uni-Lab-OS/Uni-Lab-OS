"""把动作载荷保管声明编译为 Backend 跨 Job 访问区域策略。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, NoReturn

from unilabos.registry.decorators import (
    PAYLOAD_CUSTODY_ACCESS_REGION_KEY,
    PAYLOAD_CUSTODY_SCHEMA_EXTENSION,
    normalize_payload_custody,
)
from unilabos.workflow.models import validate_uuid


class PayloadCustodyCompilationError(ValueError):
    """动作载荷保管声明不能唯一投影为执行策略。"""

    def __init__(self, code: str, message: str):
        """保存稳定错误码和中文诊断消息。"""

        super().__init__(message)
        self.code = code
        self.message = message


def project_payload_custody_access_regions(
    graph: Mapping[str, Any],
) -> dict[str, Any]:
    """在最终候选图上证明保管路径并投影编译器拥有的访问区域。

    参数：``graph`` 是完成组合展开和已应用图固定点合并的五集合候选图。返回：
    不共享容器且已写入访问区域策略的候选图。异常：声明、类型、路径或现有人工
    策略不能无歧义证明时抛出 ``PayloadCustodyCompilationError``。
    """

    if not isinstance(graph, Mapping):
        _fail("payload_custody_invalid", "候选工作流图必须是对象")
    projected = deepcopy(dict(graph))
    nodes = _entity_index(projected.get("nodes"), label="节点")
    templates = _entity_index(projected.get("node_templates"), label="节点模板")
    handles = _entity_index(projected.get("handle_templates"), label="连接点模板")
    handles_by_template: dict[str, list[dict[str, Any]]] = {
        template_uuid: [] for template_uuid in templates
    }
    for handle_uuid, handle in handles.items():
        template_uuid = _required_uuid(
            handle.get("workflow_node_template_uuid"),
            f"连接点 {handle_uuid} 的节点模板",
        )
        if template_uuid not in handles_by_template:
            _fail("payload_custody_invalid", "连接点引用了未知节点模板")
        handles_by_template[template_uuid].append(handle)

    custody_by_template = {
        template_uuid: _template_custody(template)
        for template_uuid, template in templates.items()
    }
    active_acquires: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for node_uuid, node in nodes.items():
        template_uuid = _node_template_uuid(node, node_uuid=node_uuid)
        if template_uuid not in templates:
            _fail("payload_custody_invalid", "候选工作流节点引用未知节点模板")
        custody = custody_by_template.get(template_uuid)
        if (
            custody is not None
            and custody["effect"] == "acquire"
            and not bool(node.get("disabled"))
        ):
            active_acquires.append((node, custody))
        else:
            _remove_reserved_access_region(node)
    if not active_acquires:
        return projected

    outgoing = _outgoing_edges(
        projected.get("edges"),
        nodes=nodes,
        handles=handles,
    )
    regions: list[tuple[dict[str, Any], dict[str, str]]] = []
    for acquire_node, custody in active_acquires:
        acquire_uuid = _required_uuid(acquire_node.get("uuid"), "取得载荷节点")
        holder_uuid = _required_uuid(
            acquire_node.get("material_uuid"),
            f"取得载荷节点 {acquire_uuid} 的执行器物料",
        )
        release_uuid = _trace_release_node(
            acquire_node=acquire_node,
            custody=custody,
            holder_uuid=holder_uuid,
            nodes=nodes,
            templates=templates,
            handles=handles,
            handles_by_template=handles_by_template,
            custody_by_template=custody_by_template,
            outgoing=outgoing,
        )
        regions.append(
            (
                acquire_node,
                {
                    "key": custody["region_key"],
                    "lock_material_uuid": holder_uuid,
                    "release_node_uuid": release_uuid,
                },
            )
        )
    for acquire_node, region in regions:
        _merge_access_region(acquire_node, region)
    return projected


def _trace_release_node(
    *,
    acquire_node: dict[str, Any],
    custody: dict[str, Any],
    holder_uuid: str,
    nodes: Mapping[str, dict[str, Any]],
    templates: Mapping[str, dict[str, Any]],
    handles: Mapping[str, dict[str, Any]],
    handles_by_template: Mapping[str, list[dict[str, Any]]],
    custody_by_template: Mapping[str, dict[str, Any] | None],
    outgoing: Mapping[tuple[str, str], list[dict[str, Any]]],
) -> str:
    """沿唯一 ResourceSlot 透传链查找可信物料转移释放节点。"""

    acquire_uuid = _required_uuid(acquire_node.get("uuid"), "取得载荷节点")
    acquire_template_uuid = _node_template_uuid(acquire_node, node_uuid=acquire_uuid)
    _require_resource_handle(
        handles_by_template,
        template_uuid=acquire_template_uuid,
        handle_key=custody["input"],
        io_type="target",
    )
    current_handle = _require_resource_handle(
        handles_by_template,
        template_uuid=acquire_template_uuid,
        handle_key=custody["output"],
        io_type="source",
    )
    current_node_uuid = acquire_uuid
    deposit_seen = False
    visited: set[tuple[str, str]] = set()
    while True:
        current_handle_uuid = _required_uuid(
            current_handle.get("uuid"), "载荷输出连接点"
        )
        cursor = (current_node_uuid, current_handle_uuid)
        if cursor in visited:
            _fail("payload_custody_invalid", "载荷保管路径形成循环")
        visited.add(cursor)
        candidates = outgoing.get(cursor, [])
        if not candidates:
            _fail(
                "payload_custody_invalid",
                f"取得载荷节点 {acquire_uuid} 缺少可信释放路径",
            )
        if len(candidates) != 1:
            _fail(
                "payload_custody_ambiguous",
                f"取得载荷节点 {acquire_uuid} 的 ResourceSlot 路径发生分叉",
            )
        edge = candidates[0]
        target_uuid = _required_uuid(edge.get("target_node_uuid"), "载荷边目标节点")
        target_node = nodes[target_uuid]
        if bool(target_node.get("disabled")):
            _fail("payload_custody_invalid", "载荷保管路径经过已禁用节点")
        target_template_uuid = _node_template_uuid(target_node, node_uuid=target_uuid)
        target_template = templates[target_template_uuid]
        target_handle_uuid = _required_uuid(
            edge.get("target_handle_uuid"), "载荷边目标连接点"
        )
        target_handle = handles[target_handle_uuid]
        _require_handle_parent(
            target_handle,
            template_uuid=target_template_uuid,
            label="载荷边目标连接点",
        )
        if not _is_resource_slot_handle(target_handle):
            _fail("payload_custody_invalid", "载荷保管路径经过非 ResourceSlot 连接点")

        if _is_material_transfer_template(target_template):
            if not deposit_seen:
                _fail("payload_custody_invalid", "载荷释放前缺少显式 deposit 动作")
            if target_handle.get("handle_key") != "resource":
                _fail(
                    "payload_custody_invalid",
                    "物料转移释放节点必须从 resource 连接点接收载荷",
                )
            return target_uuid

        target_holder_uuid = _required_uuid(
            target_node.get("material_uuid"),
            f"载荷路径节点 {target_uuid} 的执行器物料",
        )
        if target_holder_uuid != holder_uuid:
            _fail("payload_custody_invalid", "载荷保管路径跨越了不同执行器物料")
        target_custody = custody_by_template.get(target_template_uuid)
        if target_custody is not None and target_custody["effect"] == "acquire":
            _fail("payload_custody_invalid", "载荷释放前出现了嵌套 acquire 动作")
        if target_custody is not None and target_custody["effect"] == "deposit":
            if deposit_seen:
                _fail("payload_custody_ambiguous", "载荷保管路径包含多个 deposit 动作")
            if target_custody["region_key"] != custody["region_key"]:
                _fail(
                    "payload_custody_invalid", "acquire 与 deposit 的访问区域键不一致"
                )
            deposit_input = _require_resource_handle(
                handles_by_template,
                template_uuid=target_template_uuid,
                handle_key=target_custody["input"],
                io_type="target",
            )
            if (
                _required_uuid(deposit_input.get("uuid"), "deposit 输入连接点")
                != target_handle_uuid
            ):
                _fail(
                    "payload_custody_invalid", "载荷没有进入 deposit 声明的输入连接点"
                )
            current_handle = _require_resource_handle(
                handles_by_template,
                template_uuid=target_template_uuid,
                handle_key=target_custody["output"],
                io_type="source",
            )
            deposit_seen = True
        else:
            current_handle = _passthrough_output_handle(
                target_node,
                target_handle_uuid=target_handle_uuid,
                template_uuid=target_template_uuid,
                handles=handles,
            )
        current_node_uuid = target_uuid


def _outgoing_edges(
    values: Any,
    *,
    nodes: Mapping[str, dict[str, Any]],
    handles: Mapping[str, dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """索引并校验候选图边的四个端点。"""

    if not isinstance(values, list):
        _fail("payload_custody_invalid", "候选工作流边必须是数组")
    result: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for raw_edge in values:
        if not isinstance(raw_edge, Mapping):
            _fail("payload_custody_invalid", "候选工作流边必须是对象")
        edge = dict(raw_edge)
        source_node_uuid = _required_uuid(edge.get("source_node_uuid"), "边源节点")
        target_node_uuid = _required_uuid(edge.get("target_node_uuid"), "边目标节点")
        source_handle_uuid = _required_uuid(
            edge.get("source_handle_uuid"), "边源连接点"
        )
        target_handle_uuid = _required_uuid(
            edge.get("target_handle_uuid"), "边目标连接点"
        )
        if source_node_uuid not in nodes or target_node_uuid not in nodes:
            _fail("payload_custody_invalid", "候选工作流边引用未知节点")
        if source_handle_uuid not in handles or target_handle_uuid not in handles:
            _fail("payload_custody_invalid", "候选工作流边引用未知连接点")
        _require_handle_parent(
            handles[source_handle_uuid],
            template_uuid=_node_template_uuid(
                nodes[source_node_uuid], node_uuid=source_node_uuid
            ),
            label="边源连接点",
        )
        _require_handle_parent(
            handles[target_handle_uuid],
            template_uuid=_node_template_uuid(
                nodes[target_node_uuid], node_uuid=target_node_uuid
            ),
            label="边目标连接点",
        )
        result.setdefault((source_node_uuid, source_handle_uuid), []).append(edge)
    return result


def _passthrough_output_handle(
    node: Mapping[str, Any],
    *,
    target_handle_uuid: str,
    template_uuid: str,
    handles: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    """按编译器记录的类型证明选择唯一物料透传输出。"""

    metadata = node.get("meta_data")
    unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
    passthroughs = (
        unilab.get("material_passthrough_handles")
        if isinstance(unilab, Mapping)
        else None
    )
    if not isinstance(passthroughs, Mapping):
        _fail("payload_custody_invalid", "载荷路径节点缺少 ResourceSlot 透传证明")
    candidates: list[str] = []
    for raw_source_uuid, raw_target_uuid in passthroughs.items():
        source_uuid = _required_uuid(raw_source_uuid, "透传输出连接点")
        mapped_target_uuid = _required_uuid(raw_target_uuid, "透传输入连接点")
        if mapped_target_uuid == target_handle_uuid:
            candidates.append(source_uuid)
    if len(candidates) != 1:
        _fail("payload_custody_ambiguous", "载荷路径无法选择唯一 ResourceSlot 透传输出")
    source_handle = handles.get(candidates[0])
    if source_handle is None:
        _fail("payload_custody_invalid", "载荷透传证明引用未知输出连接点")
    _require_handle_parent(
        source_handle, template_uuid=template_uuid, label="透传输出连接点"
    )
    if source_handle.get("io_type") != "source" or not _is_resource_slot_handle(
        source_handle
    ):
        _fail("payload_custody_invalid", "载荷透传输出不是 ResourceSlot source 连接点")
    return source_handle


def _template_custody(template: Mapping[str, Any]) -> dict[str, Any] | None:
    """只从规范动作合同扩展读取载荷保管声明。"""

    metadata = template.get("meta_data")
    unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
    schema = (
        unilab.get("action_contract_schema") if isinstance(unilab, Mapping) else None
    )
    raw_custody = (
        schema.get(PAYLOAD_CUSTODY_SCHEMA_EXTENSION)
        if isinstance(schema, Mapping)
        else None
    )
    if raw_custody is None:
        return None
    try:
        return normalize_payload_custody(raw_custody)
    except (TypeError, ValueError) as error:
        _fail("payload_custody_invalid", f"节点模板载荷保管声明无效: {error}")


def _require_resource_handle(
    handles_by_template: Mapping[str, list[dict[str, Any]]],
    *,
    template_uuid: str,
    handle_key: str,
    io_type: str,
) -> dict[str, Any]:
    """取得声明引用的唯一 ResourceSlot 连接点。"""

    candidates = [
        handle
        for handle in handles_by_template.get(template_uuid, [])
        if handle.get("handle_key") == handle_key and handle.get("io_type") == io_type
    ]
    if len(candidates) != 1:
        _fail("payload_custody_ambiguous", "载荷保管声明没有引用唯一连接点")
    if not _is_resource_slot_handle(candidates[0]):
        _fail("payload_custody_invalid", "载荷保管声明必须引用 ResourceSlot 连接点")
    return candidates[0]


def _is_resource_slot_handle(handle: Mapping[str, Any]) -> bool:
    """判断连接点是否携带精确 ResourceSlot 类型证明。"""

    if handle.get("type") == "ResourceSlot":
        return True
    metadata = handle.get("meta_data")
    unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
    schema = unilab.get("value_schema") if isinstance(unilab, Mapping) else None
    return isinstance(schema, Mapping) and schema.get("$slot") == "ResourceSlot"


def _is_material_transfer_template(template: Mapping[str, Any]) -> bool:
    """只以受控执行器类型识别可信物料转移边界。"""

    metadata = template.get("meta_data")
    unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
    candidates = {
        value
        for value in (
            template.get("executor_kind"),
            unilab.get("executor_kind") if isinstance(unilab, Mapping) else None,
        )
        if isinstance(value, str) and value
    }
    return candidates == {"material_transfer"}


def _merge_access_region(node: dict[str, Any], region: dict[str, str]) -> None:
    """保留人工执行策略并严格重算编译器拥有的访问区域。"""

    raw_policy = node.get("execution_policy")
    if raw_policy is None:
        policy: dict[str, Any] = {}
    elif isinstance(raw_policy, Mapping):
        policy = deepcopy(dict(raw_policy))
    else:
        _fail("payload_custody_policy_conflict", "execution_policy 必须是对象")
    existing = policy.get("access_region")
    if existing is not None:
        if not isinstance(existing, Mapping):
            _fail("payload_custody_policy_conflict", "已有 access_region 不是对象")
        if existing.get("key") != PAYLOAD_CUSTODY_ACCESS_REGION_KEY:
            _fail(
                "payload_custody_policy_conflict",
                "人工 access_region 与载荷保管策略冲突",
            )
    policy["access_region"] = dict(region)
    node["execution_policy"] = policy


def _remove_reserved_access_region(node: dict[str, Any]) -> None:
    """从非 acquire 节点删除上一次编译遗留的平台保留区域。"""

    policy = node.get("execution_policy")
    if not isinstance(policy, Mapping):
        return
    region = policy.get("access_region")
    if (
        not isinstance(region, Mapping)
        or region.get("key") != PAYLOAD_CUSTODY_ACCESS_REGION_KEY
    ):
        return
    cleaned = deepcopy(dict(policy))
    cleaned.pop("access_region", None)
    node["execution_policy"] = cleaned


def _entity_index(values: Any, *, label: str) -> dict[str, dict[str, Any]]:
    """按规范 UUID 索引图实体。"""

    if not isinstance(values, list):
        _fail("payload_custody_invalid", f"候选工作流{label}必须是数组")
    result: dict[str, dict[str, Any]] = {}
    for raw_value in values:
        if not isinstance(raw_value, dict):
            _fail("payload_custody_invalid", f"候选工作流{label}必须是对象")
        identity = _required_uuid(raw_value.get("uuid"), label)
        if identity in result:
            _fail("payload_custody_invalid", f"候选工作流{label} UUID 重复")
        result[identity] = raw_value
    return result


def _node_template_uuid(node: Mapping[str, Any], *, node_uuid: str) -> str:
    """读取节点绑定的规范模板 UUID。"""

    return _required_uuid(
        node.get("workflow_node_template_uuid"), f"节点 {node_uuid} 的模板"
    )


def _require_handle_parent(
    handle: Mapping[str, Any],
    *,
    template_uuid: str,
    label: str,
) -> None:
    """确认连接点属于当前路径节点的模板。"""

    parent_uuid = _required_uuid(handle.get("workflow_node_template_uuid"), label)
    if parent_uuid != template_uuid:
        _fail("payload_custody_invalid", f"{label}不属于当前节点模板")


def _required_uuid(value: Any, label: str) -> str:
    """把必填身份转换为规范 UUID。"""

    try:
        return validate_uuid(value)
    except (TypeError, ValueError):
        _fail("payload_custody_invalid", f"{label} UUID 无效")


def _fail(code: str, message: str) -> NoReturn:
    """抛出稳定的载荷保管编译诊断。"""

    raise PayloadCustodyCompilationError(code, message)


__all__ = [
    "PayloadCustodyCompilationError",
    "project_payload_custody_access_regions",
]
