"""把冻结工作流任务（WorkflowTask）纯编译为旧调度器工作流规格。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from unilabos.app.scheduler.inventory.domain import MaterialRequirement
from unilabos.app.scheduler.models import (
    Handle,
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
    normalize_node_type,
)
from unilabos.workflow._workflow_spec_snapshot import (
    WorkflowSpecCompilationError,
    canonical_uuid,
    first_enabled_physical_consumer,
    index_handles,
    index_jobs,
    index_nodes,
    mapping,
    mapping_sequence,
    material_outgoing_edges,
)


class WorkflowSpecCompiler:
    """封装冻结任务到 ``WorkflowSpec`` 的全部确定性转换规则。"""

    def compile(
        self,
        task_snapshot: Mapping[str, Any],
        jobs: Sequence[Mapping[str, Any]],
    ) -> WorkflowSpec:
        """编译已持久化身份的工作流任务（WorkflowTask）。

        参数是任务不可变应用图和已有作业；返回 ``WorkflowSpec``；结构、身份、
        物料来源（MaterialSource）或物料流非法时抛编译错误，且不执行 I/O。
        """
        task = mapping(task_snapshot, "invalid_task_snapshot", "task_snapshot")
        # ``task_uuid`` 是旧调度运行必须复用的工作流任务稳定身份。
        task_uuid = canonical_uuid(
            task.get("uuid"), "invalid_task_identity", "task_snapshot.uuid"
        )
        graph = mapping(
            task.get("workflow_snapshot"),
            "invalid_workflow_snapshot",
            "task_snapshot.workflow_snapshot",
        )
        raw_nodes = mapping_sequence(
            graph.get("nodes"),
            "invalid_workflow_snapshot",
            "task_snapshot.workflow_snapshot.nodes",
        )
        raw_edges = mapping_sequence(
            graph.get("edges", []),
            "invalid_workflow_snapshot",
            "task_snapshot.workflow_snapshot.edges",
        )
        raw_handles = mapping_sequence(
            graph.get("handle_templates", graph.get("handles", [])),
            "invalid_workflow_snapshot",
            "task_snapshot.workflow_snapshot.handle_templates",
        )
        nodes, ordered_node_uuids = index_nodes(raw_nodes)
        jobs_by_node = index_jobs(jobs, nodes=nodes)
        handles_by_uuid = index_handles(raw_handles)
        requirements_by_node = self._material_requirements(
            nodes=nodes,
            edges=raw_edges,
            handles=handles_by_uuid,
        )

        compiled_nodes: list[WorkflowNode] = []
        for node_uuid in ordered_node_uuids:
            node = nodes[node_uuid]
            if node.get("type") == "material_source" or node.get("disabled") is True:
                continue
            job = jobs_by_node.get(node_uuid)
            if job is None:
                raise WorkflowSpecCompilationError(
                    "missing_workflow_node_job",
                    f"启用工作流节点缺少持久作业身份：{node_uuid}",
                )
            # ``job_uuid`` 是已有作业身份；编译器绝不生成替代 UUID。
            job_uuid = canonical_uuid(
                job.get("uuid"), "invalid_job_identity", f"jobs[{node_uuid}].uuid"
            )
            # ``final_param`` 优先采用作业已冻结参数；没有独立作业参数时才使用
            # 同一任务快照中的节点参数，并复制以隔离调用者后续修改。
            raw_param = job.get("param", node.get("param"))
            if raw_param is not None and not isinstance(raw_param, Mapping):
                raise WorkflowSpecCompilationError(
                    "invalid_job_param", "工作流节点最终参数必须是对象"
                )
            final_param = dict(raw_param or {})
            compiled_nodes.append(
                WorkflowNode(
                    id=node_uuid,
                    job_id=job_uuid,
                    device_id=str(
                        node.get("device_id") or node.get("material_uuid") or ""
                    ).strip(),
                    action_name=str(node.get("action_name") or "").strip(),
                    action_type=str(node.get("action_type") or "").strip(),
                    param=final_param,
                    node_type=normalize_node_type(node.get("type")),
                    disabled=False,
                    material_requirements=list(requirements_by_node.get(node_uuid, ())),
                )
            )

        # ``active_node_uuids`` 是本次旧调度运行唯一允许推进的节点集合。
        active_node_uuids = {node.id for node in compiled_nodes}
        compiled_edges = self._compile_edges(
            raw_edges,
            active_node_uuids=active_node_uuids,
            handles=handles_by_uuid,
        )
        compiled_handles = self._compile_handles(
            raw_handles,
            nodes=nodes,
            active_node_uuids=active_node_uuids,
        )
        return WorkflowSpec(
            workflow_id=task_uuid,
            task_id=task_uuid,
            nodes=compiled_nodes,
            edges=compiled_edges,
            handles=compiled_handles,
            priority=task.get("priority", 1.0),
            lab_id=str(task.get("lab_id") or "").strip(),
        )

    def _material_requirements(
        self,
        *,
        nodes: Mapping[str, Mapping[str, Any]],
        edges: Sequence[Mapping[str, Any]],
        handles: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, list[MaterialRequirement]]:
        """投影首个消费者的短期物料需求。

        参数是同一冻结图的节点、边和连接点；返回逐节点需求；不支持的来源模式、
        未固定 UUID 或非线性物料流抛错，且不代表正式任务物料预留。
        """
        outgoing = material_outgoing_edges(edges, handles=handles, nodes=nodes)
        requirements: dict[str, list[MaterialRequirement]] = defaultdict(list)
        seen_by_consumer: dict[str, set[str]] = defaultdict(set)
        for source_uuid, source in nodes.items():
            if (
                source.get("type") != "material_source"
                or source.get("disabled") is True
            ):
                continue
            selector = mapping(
                source.get("param"),
                "invalid_material_source_selector",
                f"material_source[{source_uuid}].param",
            )
            mode = str(selector.get("mode") or "").strip()
            if mode != "existing":
                raise WorkflowSpecCompilationError(
                    "unsupported_material_source_mode",
                    "短期调度桥只接受已固定 UUID 的 existing 物料来源",
                )
            material_value = selector.get("material_uuid")
            if material_value is None or not str(material_value).strip():
                raise WorkflowSpecCompilationError(
                    "material_source_resolution_required",
                    "existing 物料来源尚未由物料权威固定具体物料 UUID",
                )
            # ``material_uuid`` 是短期整图物料预留必须使用的规范实例身份。
            material_uuid = canonical_uuid(
                material_value,
                "invalid_material_uuid",
                f"material_source[{source_uuid}].param.material_uuid",
            )
            consumer_uuid = first_enabled_physical_consumer(
                source_uuid,
                nodes=nodes,
                outgoing=outgoing,
            )
            if (
                consumer_uuid is None
                or material_uuid in seen_by_consumer[consumer_uuid]
            ):
                continue
            requirements[consumer_uuid].append(
                MaterialRequirement(instance_uuid=material_uuid)
            )
            seen_by_consumer[consumer_uuid].add(material_uuid)
        return dict(requirements)

    def _compile_edges(
        self,
        raw_edges: Sequence[Mapping[str, Any]],
        *,
        active_node_uuids: set[str],
        handles: Mapping[str, Mapping[str, Any]],
    ) -> list[WorkflowEdge]:
        """编译活动节点依赖。

        参数是冻结边、活动节点和连接点；返回调度边；身份/端点非法时抛错。
        """
        compiled: list[WorkflowEdge] = []
        for index, edge in enumerate(raw_edges):
            source_uuid = canonical_uuid(
                edge.get("source_node_uuid", edge.get("source_node_id")),
                "invalid_edge_identity",
                f"workflow_snapshot.edges[{index}].source_node_uuid",
            )
            target_uuid = canonical_uuid(
                edge.get("target_node_uuid", edge.get("target_node_id")),
                "invalid_edge_identity",
                f"workflow_snapshot.edges[{index}].target_node_uuid",
            )
            if (
                source_uuid not in active_node_uuids
                or target_uuid not in active_node_uuids
            ):
                continue
            edge_uuid = canonical_uuid(
                edge.get("uuid"),
                "invalid_edge_identity",
                f"workflow_snapshot.edges[{index}].uuid",
            )
            source_handle_uuid = canonical_uuid(
                edge.get("source_handle_uuid"),
                "invalid_edge_identity",
                f"workflow_snapshot.edges[{index}].source_handle_uuid",
            )
            target_handle_uuid = canonical_uuid(
                edge.get("target_handle_uuid"),
                "invalid_edge_identity",
                f"workflow_snapshot.edges[{index}].target_handle_uuid",
            )
            source_handle = handles.get(source_handle_uuid)
            target_handle = handles.get(target_handle_uuid)
            if source_handle is None or target_handle is None:
                raise WorkflowSpecCompilationError(
                    "edge_handle_identity_mismatch",
                    "活动工作流边引用快照外连接点",
                )
            compiled.append(
                WorkflowEdge(
                    uuid=edge_uuid,
                    source_node_id=source_uuid,
                    target_node_id=target_uuid,
                    source_handle_uuid=source_handle_uuid,
                    target_handle_uuid=target_handle_uuid,
                    source_handle_key=str(
                        source_handle.get("handle_key") or ""
                    ).strip(),
                    target_handle_key=str(
                        target_handle.get("handle_key") or ""
                    ).strip(),
                )
            )
        return compiled

    def _compile_handles(
        self,
        raw_handles: Sequence[Mapping[str, Any]],
        *,
        nodes: Mapping[str, Mapping[str, Any]],
        active_node_uuids: set[str],
    ) -> list[Handle]:
        """投影活动节点连接点。

        参数是模板、节点和活动集合；返回保留数据路径的连接点；无适用异常。
        """
        nodes_by_template: dict[str, list[str]] = defaultdict(list)
        for node_uuid, node in nodes.items():
            if node_uuid not in active_node_uuids:
                continue
            template_uuid = str(node.get("workflow_node_template_uuid") or "").strip()
            if template_uuid:
                nodes_by_template[template_uuid].append(node_uuid)
        compiled: list[Handle] = []
        for raw_handle in raw_handles:
            template_uuid = str(
                raw_handle.get("workflow_node_template_uuid") or ""
            ).strip()
            explicit_node_uuid = str(raw_handle.get("node_id") or "").strip()
            owner_node_uuids = (
                [explicit_node_uuid]
                if explicit_node_uuid in active_node_uuids
                else nodes_by_template.get(template_uuid, [])
            )
            for owner_node_uuid in owner_node_uuids:
                compiled.append(
                    Handle(
                        uuid=str(raw_handle.get("uuid") or "").strip(),
                        data_source=str(raw_handle.get("data_source") or "").strip(),
                        handle_key=str(raw_handle.get("handle_key") or "").strip(),
                        data_key=str(raw_handle.get("data_key") or "").strip(),
                        node_id=owner_node_uuid,
                        io_type=str(raw_handle.get("io_type") or "").strip(),
                    )
                )
        return compiled


__all__ = ["WorkflowSpecCompilationError", "WorkflowSpecCompiler"]
