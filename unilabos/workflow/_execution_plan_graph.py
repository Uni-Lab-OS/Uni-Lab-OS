"""执行计划（ExecutionPlan）的纯图归一化内部实现。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid5

from unilabos.workflow.store import StoreConflict


class ExecutionPlanBuildError(StoreConflict):
    """应用图不能安全转换为执行计划（ExecutionPlan）。"""

    def __init__(self, code: str, message: str) -> None:
        """建立计划错误。

        参数：``code`` 是稳定失败码，``message`` 是中文诊断。返回：无。
        异常：无；构造器只保存已经判定的失败关闭结果。
        """

        super().__init__(message)
        self.code = code


class ExecutionPlanGraphNormalizer:
    """收敛虚拟节点、实例化连接点（Handle）并生成确定性拓扑。"""

    def runtime_handles(
        self,
        *,
        active: Mapping[str, Mapping[str, Any]],
        handles: Mapping[str, Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[tuple[str, str], str]]:
        """实例化节点作用域运行连接点（Handle）。

        参数：``active`` 是活动节点，``handles`` 是模板连接点索引。返回：稳定
        排序的运行连接点及 ``(node, template_handle)`` 身份映射。异常：节点或
        模板 UUID 非规范时 ``UUID`` 构造抛 ``ValueError`` 并失败关闭。
        """

        runtime: list[dict[str, Any]] = []
        identities: dict[tuple[str, str], str] = {}
        for node_uuid, node in active.items():
            template_uuid = str(node.get("workflow_node_template_uuid") or "")
            for handle_uuid, handle in handles.items():
                if handle.get("workflow_node_template_uuid") != template_uuid:
                    continue
                # ``runtime_uuid`` 把可复用模板端点变成节点作用域稳定身份。
                runtime_uuid = str(
                    uuid5(UUID(node_uuid), f"runtime-handle:{handle_uuid}")
                )
                identities[(node_uuid, handle_uuid)] = runtime_uuid
                runtime.append(
                    {
                        "uuid": runtime_uuid,
                        "node_uuid": node_uuid,
                        "template_handle_uuid": handle_uuid,
                        "data_source": str(handle.get("data_source") or ""),
                        "handle_key": str(handle.get("handle_key") or ""),
                        "data_key": handle_data_key(handle),
                        "io_type": str(handle.get("io_type") or ""),
                        "type": str(handle.get("type") or ""),
                        "required": bool(handle.get("required")),
                    }
                )
        runtime.sort(key=lambda item: (item["node_uuid"], item["uuid"]))
        return runtime, identities

    def contract_edges(
        self,
        *,
        nodes: Mapping[str, Mapping[str, Any]],
        active: Mapping[str, Mapping[str, Any]],
        edges: Sequence[Mapping[str, Any]],
        handles: Mapping[str, Mapping[str, Any]],
        runtime_handle_ids: Mapping[tuple[str, str], str],
    ) -> list[dict[str, Any]]:
        """收敛虚拟/禁用节点并实例化活动边端点。

        参数：节点、活动节点、原始边、模板连接点和运行身份来自同一冻结图。
        返回：直接活动边与稳定 ``dependency_only`` 旁路边。异常：缺端点、循环
        或连接点引用非法时抛 ``ExecutionPlanBuildError``。
        """

        outgoing: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for edge in edges:
            source = str(edge.get("source_node_uuid") or "")
            target = str(edge.get("target_node_uuid") or "")
            if source not in nodes or target not in nodes:
                raise ExecutionPlanBuildError(
                    "edge_node_identity_mismatch", "工作流边引用快照外节点"
                )
            outgoing[source].append(edge)
        planned: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for source_uuid in active:
            self._walk_contracted_edges(
                source_uuid=source_uuid,
                current_uuid=source_uuid,
                path_edges=(),
                path_nodes=(source_uuid,),
                outgoing=outgoing,
                active=active,
                handles=handles,
                runtime_handle_ids=runtime_handle_ids,
                seen=seen,
                planned=planned,
            )
        planned.sort(
            key=lambda item: (item["source_node_uuid"], item["target_node_uuid"])
        )
        return planned

    def _walk_contracted_edges(
        self,
        *,
        source_uuid: str,
        current_uuid: str,
        path_edges: tuple[str, ...],
        path_nodes: tuple[str, ...],
        outgoing: Mapping[str, Sequence[Mapping[str, Any]]],
        active: Mapping[str, Mapping[str, Any]],
        handles: Mapping[str, Mapping[str, Any]],
        runtime_handle_ids: Mapping[tuple[str, str], str],
        seen: set[tuple[str, str]],
        planned: list[dict[str, Any]],
    ) -> None:
        """递归收敛一个活动节点之后的虚拟路径。

        参数：``source_uuid`` 是起始活动节点，``current_uuid`` 是当前节点，
        ``path_edges``/``path_nodes`` 是当前路径，其他映射提供图与运行端点，
        ``seen``/``planned`` 收集去重结果。返回：无。异常：路径循环时抛稳定
        ``ExecutionPlanBuildError``，不会递归重放物理执行。
        """

        for edge in outgoing.get(current_uuid, ()):
            target_uuid = str(edge["target_node_uuid"])
            if target_uuid in path_nodes:
                raise ExecutionPlanBuildError(
                    "workflow_cycle", "工作流虚拟节点路径含循环"
                )
            # ``next_path_edges`` 冻结旁路边身份；``next_path_nodes`` 用于查环。
            next_path_edges = (*path_edges, str(edge.get("uuid") or ""))
            next_path_nodes = (*path_nodes, target_uuid)
            if target_uuid not in active:
                self._walk_contracted_edges(
                    source_uuid=source_uuid,
                    current_uuid=target_uuid,
                    path_edges=next_path_edges,
                    path_nodes=next_path_nodes,
                    outgoing=outgoing,
                    active=active,
                    handles=handles,
                    runtime_handle_ids=runtime_handle_ids,
                    seen=seen,
                    planned=planned,
                )
                continue
            if current_uuid == source_uuid and not path_edges:
                planned.append(
                    self._direct_edge(
                        edge,
                        handles=handles,
                        runtime_handle_ids=runtime_handle_ids,
                    )
                )
                continue
            pair = (source_uuid, target_uuid)
            if pair in seen:
                continue
            seen.add(pair)
            planned.append(
                {
                    "uuid": str(
                        uuid5(
                            UUID(source_uuid),
                            f"dependency-bypass:{target_uuid}:{':'.join(next_path_edges)}",
                        )
                    ),
                    "source_node_uuid": source_uuid,
                    "target_node_uuid": target_uuid,
                    "source_handle_uuid": "",
                    "target_handle_uuid": "",
                    "source_data_key": "",
                    "target_data_key": "",
                    "source_type": "",
                    "target_type": "",
                    "dependency_only": True,
                }
            )

    @staticmethod
    def _direct_edge(
        edge: Mapping[str, Any],
        *,
        handles: Mapping[str, Mapping[str, Any]],
        runtime_handle_ids: Mapping[tuple[str, str], str],
    ) -> dict[str, Any]:
        """实例化一条直接活动边。

        参数：``edge`` 是模板端点边，``handles`` 提供语义，``runtime_handle_ids``
        提供节点作用域身份。返回：冻结运行边。异常：端点缺失时抛计划错误。
        """

        source = str(edge["source_node_uuid"])
        target = str(edge["target_node_uuid"])
        source_template = str(edge.get("source_handle_uuid") or "")
        target_template = str(edge.get("target_handle_uuid") or "")
        source_handle = handles.get(source_template)
        target_handle = handles.get(target_template)
        if source_handle is None or target_handle is None:
            raise ExecutionPlanBuildError(
                "edge_handle_identity_mismatch", "工作流边引用快照外连接点"
            )
        source_runtime_uuid = runtime_handle_ids.get((source, source_template))
        target_runtime_uuid = runtime_handle_ids.get((target, target_template))
        if source_runtime_uuid is None or target_runtime_uuid is None:
            raise ExecutionPlanBuildError(
                "edge_handle_identity_mismatch", "工作流边端点不属于对应节点模板"
            )
        planned = {
            "uuid": str(edge.get("uuid") or ""),
            "source_node_uuid": source,
            "target_node_uuid": target,
            "source_handle_uuid": source_runtime_uuid,
            "target_handle_uuid": target_runtime_uuid,
            "source_data_key": handle_data_key(source_handle),
            "target_data_key": handle_data_key(target_handle),
            "source_type": str(source_handle.get("type") or ""),
            "target_type": str(target_handle.get("type") or ""),
        }
        if dependency_only(source_handle):
            planned["dependency_only"] = True
        return planned

    @staticmethod
    def topological_order(
        active: Mapping[str, Mapping[str, Any]],
        edges: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        """计算稳定拓扑顺序。

        参数：``active`` 是活动节点，``edges`` 是收敛后依赖。返回：按创建时间与
        UUID 稳定排序的节点身份。异常：存在循环时抛 ``StoreConflict``。
        """

        indegree = {node_uuid: 0 for node_uuid in active}
        outgoing: dict[str, list[str]] = defaultdict(list)
        for edge in edges:
            source = str(edge["source_node_uuid"])
            target = str(edge["target_node_uuid"])
            outgoing[source].append(target)
            indegree[target] += 1

        def stable(identity: str) -> tuple[str, str]:
            """生成稳定拓扑排序键。

            参数：``identity`` 是活动节点 UUID。返回：创建时间与 UUID 元组。
            异常：无；缺失创建时间按空文本排序。
            """

            return str(active[identity].get("create_time") or ""), identity

        available = sorted(
            (key for key, degree in indegree.items() if degree == 0), key=stable
        )
        ordered: list[str] = []
        while available:
            current = available.pop(0)
            ordered.append(current)
            for target in outgoing[current]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    available.append(target)
                    available.sort(key=stable)
        if len(ordered) != len(active):
            raise StoreConflict("workflow graph contains a cycle")
        return ordered


def executor_kind(node_type: str) -> str:
    """规范化节点执行种类。

    参数：``node_type`` 是模板或节点类型。返回：计划记录的执行种类。
    异常：未知类型抛 ``StoreConflict``；编译器稍后限制旧调度器支持范围。
    """

    normalized = node_type.strip().lower()
    aliases = {
        "ilab": "device_action",
        "device": "device_action",
        "action": "device_action",
        "resource_action": "device_action",
        "py_script": "script",
        "transfer": "Transfer",
    }
    kind = aliases.get(normalized, normalized)
    allowed = {
        "device_action",
        "compute",
        "condition",
        "script",
        "group",
        "tool_call",
        "manual_confirm",
        "material_source",
        "Transfer",
    }
    if kind not in allowed:
        raise StoreConflict(f"unsupported workflow node type {node_type!r}")
    return kind


def handle_data_key(handle: Mapping[str, Any]) -> str:
    """读取连接点数据路径。

    参数：``handle`` 是连接点模板。返回：显式 ``data_key`` 或业务键。异常：无。
    """

    return str(handle.get("data_key") or handle.get("handle_key") or "").strip()


def final_target_data_key(data_key: str) -> str:
    """取得目标最终写入键。

    参数：``data_key`` 是可能含 ``@@@`` 的路径。返回：最后一段。异常：无。
    """

    return data_key.split("@@@")[-1].strip()


def dependency_only(handle: Mapping[str, Any]) -> bool:
    """判断来源连接点是否只表达顺序依赖。

    参数：``handle`` 是来源端点。返回：ready 或非 executor 数据源时为真。
    异常：无。
    """

    if str(handle.get("handle_key") or "").strip().lower() == "ready":
        return True
    source = str(handle.get("data_source") or "").strip().lower()
    return bool(source) and source != "executor"


__all__ = [
    "ExecutionPlanBuildError",
    "ExecutionPlanGraphNormalizer",
    "executor_kind",
    "final_target_data_key",
]
