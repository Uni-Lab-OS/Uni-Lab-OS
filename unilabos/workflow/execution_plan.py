"""从应用工作流图构造唯一、不可变的执行计划（ExecutionPlan）。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

from unilabos.workflow._execution_plan_graph import (
    ExecutionPlanBuildError,
    ExecutionPlanGraphNormalizer,
    executor_kind,
    final_target_data_key,
    handle_data_key,
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
        # ``material_sources`` 是由协调器承担的物料来源解析作业，不进入设备派发图。
        material_sources = {
            node_uuid: node
            for node_uuid, node in nodes.items()
            if node.get("disabled") is not True
            and kinds[node_uuid] == "material_source"
        }
        # ``active`` 只包含既有本地调度器能够执行的普通节点；物料来源由任务桥
        # 在普通动作之前统一完成任务物料准入（TaskMaterialAdmission）。组合
        # 工作流调用（Composite Workflow Invocation）仅保留父图层级与边界映射，
        # 其展开内部节点直接归属父工作流任务（WorkflowTask），自身不创建作业。
        active = {
            node_uuid: node
            for node_uuid, node in nodes.items()
            if node.get("disabled") is not True
            and kinds[node_uuid] not in {"group", "material_source", "workflow"}
        }
        # ``planned_graph_nodes`` 同时保留协调责任与普通执行责任，使来源运行连接点
        # 和来源到首消费动作的直连边成为冻结执行计划（ExecutionPlan）事实。
        planned_graph_nodes = {**material_sources, **active}
        graph_normalizer = ExecutionPlanGraphNormalizer()
        runtime_handles, runtime_handle_ids = graph_normalizer.runtime_handles(
            active=planned_graph_nodes,
            handles=handles,
        )
        planned_edges = graph_normalizer.contract_edges(
            nodes=nodes,
            active=planned_graph_nodes,
            edges=edges,
            handles=handles,
            runtime_handle_ids=runtime_handle_ids,
        )
        requirements, material_params = self._fixed_material_inputs(
            nodes=nodes,
            active=active,
            kinds=kinds,
            edges=edges,
            handles=handles,
        )
        graph_order = graph_normalizer.topological_order(
            planned_graph_nodes,
            planned_edges,
        )
        # 来源与普通节点都先遵守冻结图拓扑，再由计划作业序列保证全部协调责任先于
        # 任何物理动作；这样不会让无边来源受创建时间影响而落到动作之后。
        ordered_sources = [
            node_uuid for node_uuid in graph_order if node_uuid in material_sources
        ]
        ordered = [node_uuid for node_uuid in graph_order if node_uuid in active]
        if run_mode == "single_node":
            if target_node_uuid is None:
                candidates = [*ordered_sources, *ordered]
                if not candidates:
                    raise StoreConflict("workflow has no enabled nodes")
                target_node_uuid = candidates[0]
            if target_node_uuid in material_sources:
                ordered_sources = [target_node_uuid]
                ordered = []
            elif target_node_uuid in active:
                ordered = [target_node_uuid]
            else:
                raise StoreConflict("single_node target is not enabled")
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
        # ``planned_order`` 把协调器责任和物理执行责任放进同一持久作业序列。
        planned_order = [*ordered_sources, *ordered]
        for index, node_uuid in enumerate(planned_order):
            node = nodes[node_uuid]
            kind = kinds[node_uuid]
            template = templates.get(node.get("workflow_node_template_uuid")) or {}
            policy = dict(node.get("execution_policy") or {})
            # ``planned_param`` 是任务提交时冻结的动作输入，不是运行时回退视图。
            planned_param = dict(node.get("param") or {})
            # ``fixed_params`` 是固定的 ``existing`` 物料来源（MaterialSource）沿物料占位符链投影的实例引用。
            fixed_params = material_params.get(node_uuid, {})
            planned_param.update(fixed_params)
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
                # ``action_contract`` 来自模板投影保留元数据，而 ``template.schema``
                # 只承载 Backend 规范的 Goal 参数子模式。
                action_contract = self._frozen_action_contract(
                    template,
                    node_uuid=node_uuid,
                )
                planned_node["param_schema"] = action_contract
            if node.get("material_uuid") is not None:
                planned_node["material_uuid"] = node["material_uuid"]
            if node.get("script") is not None:
                planned_node["script"] = node["script"]
            if kind != "device_action" and template.get("schema") is not None:
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

    def _fixed_material_inputs(
        self,
        *,
        nodes: Mapping[str, Mapping[str, Any]],
        active: Mapping[str, Mapping[str, Any]],
        kinds: Mapping[str, str],
        edges: Sequence[Mapping[str, Any]],
        handles: Mapping[str, Mapping[str, Any]],
    ) -> tuple[
        dict[str, list[dict[str, Any]]],
        dict[str, dict[str, dict[str, str]]],
    ]:
        """投影固定的 `existing` 物料来源（MaterialSource）运行输入。

        参数：完整节点、活动节点、执行种类、边与连接点来自同一应用图。返回：
        首个启用设备动作的遗留库存预留（inventory_reservation）
        需求和最终物料引用参数；它不是任务物料预留
        （TaskMaterialReservation）。异常：create_new、自动 existing、UUID
        非法或物料流分叉/循环时抛稳定计划错误。
        """

        # ``outgoing`` 保留每条物料边的目标节点与最终参数键。
        outgoing: dict[str, list[tuple[str, str]]] = defaultdict(list)
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
                    (
                        str(edge["target_node_uuid"]),
                        final_target_data_key(handle_data_key(target_handle)),
                    )
                )
        requirements: dict[str, list[dict[str, Any]]] = defaultdict(list)
        # ``material_params`` 把具体物料身份绑定到首消费动作的参数键。
        material_params: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
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
                    "短期调度桥只接受固定的 existing 物料来源",
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
            # 短期遗留预留（inventory_reservation）归属来源协调责任；普通设备
            # 动作只消费已经准入的稳定物料引用，不再次取得任务级预留。
            requirements[source_uuid].append({"instance_uuid": material_uuid})
            consumer_binding = self._first_device_consumer(
                source_uuid,
                active=active,
                kinds=kinds,
                outgoing=outgoing,
            )
            if consumer_binding is None:
                continue
            consumer, param_key = consumer_binding
            if not param_key:
                raise ExecutionPlanBuildError(
                    "invalid_execution_graph",
                    "物料占位符目标连接点缺少参数键",
                )
            material_reference = {"uuid": material_uuid}
            existing_reference = material_params[consumer].get(param_key)
            if (
                existing_reference is not None
                and existing_reference != material_reference
            ):
                raise ExecutionPlanBuildError(
                    "invalid_execution_graph",
                    "多个固定的 existing 物料来源冲突写入同一动作参数",
                )
            material_params[consumer][param_key] = material_reference
        return dict(requirements), dict(material_params)

    @staticmethod
    def _first_device_consumer(
        source_uuid: str,
        *,
        active: Mapping[str, Mapping[str, Any]],
        kinds: Mapping[str, str],
        outgoing: Mapping[str, Sequence[tuple[str, str]]],
    ) -> tuple[str, str] | None:
        """沿物料占位符（ResourceSlot）链寻找首个设备动作。

        参数：来源 UUID、活动节点、执行种类和带目标参数键的邻接表
        描述一条冻结物料链。返回：首个启用 ``device_action`` UUID
        及它的物料参数键，无消费者时返回 ``None``。异常：分叉/循环时
        抛计划错误。
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
            current, target_param_key = targets[0]
            if current in active and kinds[current] == "device_action":
                return current, target_param_key

    @staticmethod
    def _frozen_action_contract(
        template: Mapping[str, Any], *, node_uuid: str
    ) -> dict[str, Any]:
        """从节点模板保留元数据冻结完整动作合同（Action Contract）。

        参数：``template`` 是应用图冻结的工作流节点模板，``node_uuid`` 是诊断
        使用的动作节点身份。返回：与模板容器隔离的完整动作 Schema envelope。
        异常：保留元数据、完整合同或 ``properties.goal`` 缺失/非对象时抛
        ``ExecutionPlanBuildError``；禁止回退实时注册表或 Goal 子模式。
        """

        # ``metadata``/``unilab`` 定位模板投影保留的 Uni-Lab 执行合同边界。
        metadata = template.get("meta_data")
        unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
        # ``contract`` 是本工作流任务（WorkflowTask）唯一可冻结的完整动作合同。
        contract = (
            unilab.get("action_contract_schema")
            if isinstance(unilab, Mapping)
            else None
        )
        properties = (
            contract.get("properties") if isinstance(contract, Mapping) else None
        )
        # ``goal_schema`` 只用于证明完整合同能被动作物料锁编译器安全消费。
        goal_schema = (
            properties.get("goal") if isinstance(properties, Mapping) else None
        )
        if not isinstance(goal_schema, Mapping):
            raise ExecutionPlanBuildError(
                "invalid_action_contract",
                f"设备动作模板缺少完整动作合同：{node_uuid}",
            )
        return deepcopy(dict(contract))

    @staticmethod
    def _device_action_contract(node: Mapping[str, Any]) -> dict[str, Any]:
        """冻结设备动作执行器和动作合同。

        参数：``node`` 是应用图设备动作节点。返回：显式固定执行器
        （Executor）身份、动作名与规范动作类型。异常：执行器绑定
        （ExecutorBinding）缺失、非 fixed 或设备身份为空时失败关闭；
        ``material_uuid`` 是物料身份，不得作为执行器回退。
        """

        metadata = node.get("meta_data")
        unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
        binding = (
            unilab.get("executor_binding") if isinstance(unilab, Mapping) else None
        )
        device_id = ""
        if isinstance(binding, Mapping) and binding.get("mode") == "fixed":
            device_id = str(binding.get("device_id") or "").strip()
        if not device_id:
            raise ExecutionPlanBuildError(
                "invalid_executor_binding",
                "设备动作缺少显式固定执行器绑定",
            )
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
