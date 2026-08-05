"""从应用工作流图构造唯一、不可变的执行计划（ExecutionPlan）。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid4

from unilabos.workflow._execution_plan_graph import (
    ExecutionPlanBuildError,
    ExecutionPlanGraphNormalizer,
    executor_kind,
    final_target_data_key,
)
from unilabos.workflow.store import StoreConflict

PLAN_VERSION = 1


class ExecutionPlanBuilder:
    """隐藏拓扑收敛、运行连接点实例化和短期物料需求投影。"""

    def build(
        self,
        graph: Mapping[str, Any],
        *,
        run_mode: str,
        target_node_uuid: str | None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """构造版本化执行计划和首次作业集合。

        参数：``graph`` 是冻结应用图，``run_mode`` 是任务运行模式，
        ``target_node_uuid`` 是单节点模式目标。返回：不含实时库存、预留、执行
        占用或物理结果的计划与作业。异常：图、物料来源（MaterialSource）或
        单节点选择非法时抛 ``ExecutionPlanBuildError``/``StoreConflict``。
        """

        nodes = self._index(graph.get("nodes"), "nodes")
        templates = self._index(graph.get("node_templates", []), "node_templates")
        handles = self._index(graph.get("handle_templates", []), "handle_templates")
        edges = self._objects(graph.get("edges", []), "edges")
        # ``kinds`` 是每个冻结节点的规范执行责任；虚拟节点不创建作业。
        kinds = {
            node_uuid: executor_kind(
                str(
                    (templates.get(node.get("workflow_node_template_uuid")) or {}).get(
                        "node_type"
                    )
                    or node.get("type")
                    or ""
                )
            )
            for node_uuid, node in nodes.items()
        }
        active = {
            node_uuid: node
            for node_uuid, node in nodes.items()
            if node.get("disabled") is not True
            and kinds[node_uuid] not in {"group", "material_source"}
        }
        graph_normalizer = ExecutionPlanGraphNormalizer()
        runtime_handles, runtime_handle_ids = graph_normalizer.runtime_handles(
            active=active,
            handles=handles,
        )
        planned_edges = graph_normalizer.contract_edges(
            nodes=nodes,
            active=active,
            edges=edges,
            handles=handles,
            runtime_handle_ids=runtime_handle_ids,
        )
        requirements = self._material_requirements(
            nodes=nodes,
            active=active,
            kinds=kinds,
            edges=edges,
            handles=handles,
        )
        ordered = graph_normalizer.topological_order(active, planned_edges)
        if run_mode == "single_node":
            if target_node_uuid is None:
                if not ordered:
                    raise StoreConflict("workflow has no enabled nodes")
                target_node_uuid = ordered[0]
            if target_node_uuid not in active:
                raise StoreConflict("single_node target is not enabled")
            ordered = [target_node_uuid]
            planned_edges = []
            runtime_handles = [
                handle
                for handle in runtime_handles
                if handle["node_uuid"] == target_node_uuid
            ]

        planned_nodes: list[dict[str, Any]] = []
        jobs: list[dict[str, Any]] = []
        handles_by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for handle in runtime_handles:
            handles_by_node[handle["node_uuid"]].append(handle)
        for index, node_uuid in enumerate(ordered):
            node = active[node_uuid]
            kind = kinds[node_uuid]
            template = templates.get(node.get("workflow_node_template_uuid")) or {}
            policy = dict(node.get("execution_policy") or {})
            # ``planned_param`` 是任务提交时冻结的动作输入，不是运行时回退视图。
            planned_param = dict(node.get("param") or {})
            node_handles = handles_by_node.get(node_uuid, [])
            planned_node: dict[str, Any] = {
                "uuid": node_uuid,
                "topological_index": index,
                "kind": kind,
                "param": planned_param,
                "execution_policy": policy,
                "inputs": [
                    {
                        "handle_uuid": handle["uuid"],
                        "data_key": final_target_data_key(handle["data_key"]),
                        "type": handle["type"],
                        "required": handle["required"],
                    }
                    for handle in node_handles
                    if handle["io_type"] == "target"
                ],
                "source_handle_uuids": [
                    handle["uuid"]
                    for handle in node_handles
                    if handle["io_type"] == "source"
                ],
            }
            if kind == "device_action":
                planned_node.update(self._device_action_contract(node))
            if node.get("material_uuid") is not None:
                planned_node["material_uuid"] = node["material_uuid"]
            if node.get("script") is not None:
                planned_node["script"] = node["script"]
            if template.get("schema") is not None:
                planned_node["param_schema"] = template["schema"]
            if requirements.get(node_uuid):
                planned_node["material_requirements"] = requirements[node_uuid]
            planned_nodes.append(planned_node)
            jobs.append(
                {
                    "uuid": str(uuid4()),
                    "workflow_node_uuid": node_uuid,
                    "topological_index": index,
                    "executor_kind": kind,
                    "execution_policy": policy,
                    "execution_timeout_seconds": 0,
                    "param": planned_param,
                }
            )
        plan: dict[str, Any] = {
            "version": PLAN_VERSION,
            "run_mode": run_mode,
            "nodes": planned_nodes,
            "edges": planned_edges,
            "handles": runtime_handles,
        }
        if target_node_uuid is not None:
            plan["target_node_uuid"] = target_node_uuid
        return plan, jobs

    def _material_requirements(
        self,
        *,
        nodes: Mapping[str, Mapping[str, Any]],
        active: Mapping[str, Mapping[str, Any]],
        kinds: Mapping[str, str],
        edges: Sequence[Mapping[str, Any]],
        handles: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        """投影 fixed existing 物料来源的短期兼容需求。

        参数：完整节点、活动节点、执行种类、边与连接点来自同一应用图。返回：
        首个启用设备动作至实例物料需求列表。异常：create_new、自动 existing、
        UUID 非法或物料流分叉/循环时抛稳定计划错误。
        """

        outgoing: dict[str, list[str]] = defaultdict(list)
        for edge in edges:
            source_handle = handles.get(str(edge.get("source_handle_uuid") or ""))
            target_handle = handles.get(str(edge.get("target_handle_uuid") or ""))
            if (
                source_handle is not None
                and target_handle is not None
                and source_handle.get("type") == "ResourceSlot"
                and target_handle.get("type") == "ResourceSlot"
            ):
                outgoing[str(edge["source_node_uuid"])].append(
                    str(edge["target_node_uuid"])
                )
        requirements: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for source_uuid, node in nodes.items():
            if kinds[source_uuid] != "material_source" or node.get("disabled") is True:
                continue
            selector = node.get("param")
            if not isinstance(selector, Mapping):
                raise ExecutionPlanBuildError(
                    "invalid_material_source_selector", "物料来源选择器必须是对象"
                )
            if selector.get("mode") != "existing":
                raise ExecutionPlanBuildError(
                    "unsupported_material_source_mode",
                    "短期调度桥只接受 fixed existing 物料来源",
                )
            material_uuid = str(selector.get("material_uuid") or "")
            if not material_uuid:
                raise ExecutionPlanBuildError(
                    "material_source_resolution_required",
                    "existing 物料来源尚未固定具体物料 UUID",
                )
            try:
                material_uuid = str(UUID(material_uuid))
            except ValueError as exc:
                raise ExecutionPlanBuildError(
                    "invalid_material_uuid", "物料来源 UUID 非法"
                ) from exc
            consumer = self._first_device_consumer(
                source_uuid,
                active=active,
                kinds=kinds,
                outgoing=outgoing,
            )
            if consumer is not None and all(
                item["instance_uuid"] != material_uuid
                for item in requirements[consumer]
            ):
                requirements[consumer].append({"instance_uuid": material_uuid})
        return dict(requirements)

    @staticmethod
    def _first_device_consumer(
        source_uuid: str,
        *,
        active: Mapping[str, Mapping[str, Any]],
        kinds: Mapping[str, str],
        outgoing: Mapping[str, Sequence[str]],
    ) -> str | None:
        """沿物料占位符（ResourceSlot）链寻找首个设备动作。

        参数：来源 UUID、活动节点、执行种类和邻接表描述一条冻结物料链。返回：
        首个启用 ``device_action`` UUID 或 ``None``。异常：分叉/循环时抛计划错误。
        """

        current = source_uuid
        visited: set[str] = set()
        while True:
            if current in visited:
                raise ExecutionPlanBuildError(
                    "material_flow_not_linear", "物料占位符链含循环"
                )
            visited.add(current)
            targets = list(outgoing.get(current, ()))
            if len(targets) > 1:
                raise ExecutionPlanBuildError(
                    "material_flow_not_linear", "同一物料不能分叉到多个消费者"
                )
            if not targets:
                return None
            current = targets[0]
            if current in active and kinds[current] == "device_action":
                return current

    @staticmethod
    def _device_action_contract(node: Mapping[str, Any]) -> dict[str, Any]:
        """冻结设备动作执行器和动作合同。

        参数：``node`` 是应用图设备动作节点。返回：``device_id``、动作名与规范
        动作类型；缺失值保留为空供消费编译器稳定失败关闭。异常：元数据形状非法
        时不猜测绑定，等价返回空设备身份。
        """

        metadata = node.get("meta_data")
        unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
        binding = (
            unilab.get("executor_binding") if isinstance(unilab, Mapping) else None
        )
        device_id = None
        if isinstance(binding, Mapping) and binding.get("mode") == "fixed":
            device_id = binding.get("device_id")
        if not device_id:
            device_id = node.get("material_uuid")
        return {
            "device_id": device_id,
            "action_name": node.get("action_name"),
            "action_type": node.get("action_type") or "UniLabJsonCommand",
        }

    @staticmethod
    def _index(value: Any, field: str) -> dict[str, Mapping[str, Any]]:
        """按 UUID 索引对象序列。

        参数：``value`` 是边界数组，``field`` 是诊断字段。返回：身份映射。
        异常：非数组、非对象、空身份或重复身份时抛计划错误。
        """

        objects = ExecutionPlanBuilder._objects(value, field)
        result: dict[str, Mapping[str, Any]] = {}
        for item in objects:
            identity = str(item.get("uuid") or "")
            if not identity or identity in result:
                raise ExecutionPlanBuildError(
                    "invalid_execution_graph", f"{field} 身份缺失或重复"
                )
            result[identity] = item
        return result

    @staticmethod
    def _objects(value: Any, field: str) -> list[Mapping[str, Any]]:
        """校验对象数组。

        参数：``value`` 是边界值，``field`` 是诊断字段。返回：对象列表。
        异常：类型不符时抛 ``ExecutionPlanBuildError``。
        """

        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ExecutionPlanBuildError(
                "invalid_execution_graph", f"{field} 必须是数组"
            )
        if any(not isinstance(item, Mapping) for item in value):
            raise ExecutionPlanBuildError(
                "invalid_execution_graph", f"{field} 成员必须是对象"
            )
        return list(value)


__all__ = [
    "PLAN_VERSION",
    "ExecutionPlanBuildError",
    "ExecutionPlanBuilder",
]
