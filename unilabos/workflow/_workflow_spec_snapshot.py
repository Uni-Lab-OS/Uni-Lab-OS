"""工作流规格编译器使用的纯快照校验与物料链遍历。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from unilabos.app.scheduler.models import normalize_node_type


class WorkflowSpecCompilationError(RuntimeError):
    """冻结任务不能安全转换为调度器（Scheduler）输入。"""

    def __init__(self, code: str, message: str) -> None:
        """建立带稳定机器码的编译错误。

        参数：``code`` 是上层映射使用的稳定错误码；``message`` 是中文诊断。
        返回：无。异常：无；构造器只保存已经判定的失败关闭事实。
        """

        super().__init__(message)
        self.code = code


def mapping(value: Any, code: str, field: str) -> Mapping[str, Any]:
    """要求边界值是对象。

    参数：``value`` 是待校验值，``code`` 是失败码，``field`` 是诊断路径。
    返回：原映射。异常：类型不符时抛出 ``WorkflowSpecCompilationError``。
    """

    if not isinstance(value, Mapping):
        raise WorkflowSpecCompilationError(code, f"{field} 必须是对象")
    return value


def mapping_sequence(value: Any, code: str, field: str) -> list[Mapping[str, Any]]:
    """要求边界值是只含对象的有序序列。

    参数：``value`` 是待校验值，``code`` 是失败码，``field`` 是诊断路径。
    返回：隔离后的映射列表。异常：序列或成员类型不符时抛出编译错误。
    """

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise WorkflowSpecCompilationError(code, f"{field} 必须是对象数组")
    return [
        mapping(item, code, f"{field}[{index}]") for index, item in enumerate(value)
    ]


def canonical_uuid(value: Any, code: str, field: str) -> str:
    """校验稳定身份并返回规范小写连字符 UUID。

    参数：``value`` 是身份边界值，``code`` 是失败码，``field`` 是诊断路径。
    返回：规范 UUID 文本。异常：空值、非法或非规范拼写时抛出编译错误。
    """

    text = str(value or "").strip()
    try:
        parsed = UUID(text)
    except (ValueError, AttributeError, TypeError) as exc:
        raise WorkflowSpecCompilationError(code, f"{field} 必须是规范 UUID") from exc
    canonical = str(parsed)
    if parsed.int == 0 or text != canonical:
        raise WorkflowSpecCompilationError(code, f"{field} 必须是规范 UUID")
    return canonical


def index_nodes(
    raw_nodes: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    """校验并索引冻结工作流节点（WorkflowNode）。

    参数：``raw_nodes`` 是快照节点序列。返回：稳定 UUID 索引和原始确定性顺序。
    异常：身份非法或重复时抛出编译错误，禁止覆盖节点事实。
    """

    nodes: dict[str, Mapping[str, Any]] = {}
    ordered_node_uuids: list[str] = []
    for index, node in enumerate(raw_nodes):
        # ``node_uuid`` 是应用图节点身份，也是后续作业的稳定关联键。
        node_uuid = canonical_uuid(
            node.get("uuid"),
            "invalid_node_identity",
            f"workflow_snapshot.nodes[{index}].uuid",
        )
        if node_uuid in nodes:
            raise WorkflowSpecCompilationError(
                "duplicate_node_identity",
                f"工作流快照含重复节点身份：{node_uuid}",
            )
        nodes[node_uuid] = node
        ordered_node_uuids.append(node_uuid)
    return nodes, ordered_node_uuids


def index_jobs(
    jobs: Sequence[Mapping[str, Any]],
    *,
    nodes: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """按节点索引既有工作流节点作业（WorkflowNodeJob）。

    参数：``jobs`` 是作业序列，``nodes`` 是冻结节点索引。返回：节点至唯一作业
    的映射。异常：结构、引用、身份非法或重复时抛出编译错误。
    """

    raw_jobs = mapping_sequence(jobs, "invalid_job_snapshot", "jobs")
    jobs_by_node: dict[str, Mapping[str, Any]] = {}
    seen_job_uuids: set[str] = set()
    for index, job in enumerate(raw_jobs):
        # 两个 UUID 共同证明已有作业属于哪个冻结节点，编译器不得猜测。
        job_uuid = canonical_uuid(
            job.get("uuid"), "invalid_job_identity", f"jobs[{index}].uuid"
        )
        node_uuid = canonical_uuid(
            job.get("workflow_node_uuid"),
            "invalid_job_identity",
            f"jobs[{index}].workflow_node_uuid",
        )
        if job_uuid in seen_job_uuids or node_uuid in jobs_by_node:
            raise WorkflowSpecCompilationError(
                "duplicate_job_identity",
                f"工作流节点作业身份或节点绑定重复：{job_uuid}",
            )
        if node_uuid not in nodes:
            raise WorkflowSpecCompilationError(
                "job_node_identity_mismatch",
                f"工作流节点作业引用快照外节点：{node_uuid}",
            )
        seen_job_uuids.add(job_uuid)
        jobs_by_node[node_uuid] = job
    return jobs_by_node


def index_handles(
    raw_handles: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """按 UUID 索引冻结连接点（Handle）合同。

    参数：``raw_handles`` 是连接点模板。返回：UUID 至连接点映射。异常：身份
    缺失或重复时抛出编译错误，禁止边端点歧义。
    """

    handles: dict[str, Mapping[str, Any]] = {}
    for index, handle in enumerate(raw_handles):
        # ``handle_uuid`` 是边识别物料占位符（ResourceSlot）类型的稳定端点。
        handle_uuid = canonical_uuid(
            handle.get("uuid"),
            "invalid_handle_identity",
            f"handle_templates[{index}].uuid",
        )
        if handle_uuid in handles:
            raise WorkflowSpecCompilationError(
                "duplicate_handle_identity",
                f"工作流快照含重复连接点身份：{handle_uuid}",
            )
        handles[handle_uuid] = handle
    return handles


def material_outgoing_edges(
    edges: Sequence[Mapping[str, Any]],
    *,
    handles: Mapping[str, Mapping[str, Any]],
    nodes: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[str]]:
    """构造只含物料占位符（ResourceSlot）边的邻接表。

    参数：``edges`` 是冻结边，``handles`` 提供端点类型，``nodes`` 限定身份。
    返回：来源至目标节点 UUID 的有序邻接表。异常：非法端点抛出编译错误。
    """

    outgoing: dict[str, list[str]] = defaultdict(list)
    for index, edge in enumerate(edges):
        # 两个节点 UUID 是物料链的持久图端点，不允许引用快照外身份。
        source_uuid = _edge_uuid(edge, "source_node", index)
        target_uuid = _edge_uuid(edge, "target_node", index)
        if source_uuid not in nodes or target_uuid not in nodes:
            raise WorkflowSpecCompilationError(
                "edge_node_identity_mismatch", "工作流边引用快照外节点"
            )
        source_handle_uuid = _edge_uuid(edge, "source_handle", index)
        target_handle_uuid = _edge_uuid(edge, "target_handle", index)
        source_handle = handles.get(source_handle_uuid)
        target_handle = handles.get(target_handle_uuid)
        if source_handle is None or target_handle is None:
            raise WorkflowSpecCompilationError(
                "edge_handle_identity_mismatch", "工作流边引用快照外连接点"
            )
        if (
            source_handle.get("type") == "ResourceSlot"
            and target_handle.get("type") == "ResourceSlot"
        ):
            outgoing[source_uuid].append(target_uuid)
    return dict(outgoing)


def first_enabled_physical_consumer(
    source_uuid: str,
    *,
    nodes: Mapping[str, Mapping[str, Any]],
    outgoing: Mapping[str, Sequence[str]],
) -> str | None:
    """沿线性物料链查找首个启用物理消费者。

    参数：``source_uuid`` 是来源节点，``nodes`` 是节点索引，``outgoing`` 是物料
    邻接表。返回：首个启用 ILab 动作 UUID 或 ``None``。异常：分叉/循环时抛出
    ``material_flow_not_linear``，禁止靠调度锁为非法图排序。
    """

    current_uuid = source_uuid
    visited: set[str] = set()
    while True:
        if current_uuid in visited:
            raise WorkflowSpecCompilationError(
                "material_flow_not_linear", "物料占位符链含循环"
            )
        visited.add(current_uuid)
        targets = list(outgoing.get(current_uuid, ()))
        if len(targets) > 1:
            raise WorkflowSpecCompilationError(
                "material_flow_not_linear",
                "同一物料占位符不能分叉到多个物理消费者",
            )
        if not targets:
            return None
        current_uuid = targets[0]
        current = nodes[current_uuid]
        if (
            current.get("disabled") is not True
            and normalize_node_type(current.get("type")) == "ILab"
            and bool(str(current.get("action_name") or "").strip())
        ):
            return current_uuid


def _edge_uuid(edge: Mapping[str, Any], field: str, index: int) -> str:
    """读取边端点身份并兼容节点 ``*_id`` 旧拼写。

    参数：``edge`` 是冻结边，``field`` 是无后缀端点名，``index`` 是诊断序号。
    返回：规范 UUID。异常：缺失或非法时抛出编译错误。
    """

    value = edge.get(f"{field}_uuid")
    if value is None and field.endswith("_node"):
        value = edge.get(f"{field}_id")
    return canonical_uuid(
        value,
        "invalid_edge_identity",
        f"workflow_snapshot.edges[{index}].{field}_uuid",
    )


__all__ = [
    "WorkflowSpecCompilationError",
    "canonical_uuid",
    "first_enabled_physical_consumer",
    "index_handles",
    "index_jobs",
    "index_nodes",
    "mapping",
    "mapping_sequence",
    "material_outgoing_edges",
]
