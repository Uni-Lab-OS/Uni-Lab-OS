"""协调物料来源解析作业与 gaojing 短期整图预留。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from unilabos.app.scheduler.inventory.domain import (
    InsufficientStock,
    MaterialRequirement,
)
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection

MaterialSourceResolutionStatus = Literal["not_required", "blocked", "admitted"]


class MaterialSourceResolutionError(RuntimeError):
    """物料来源不能安全完成短期任务物料准入时使用的稳定错误。"""


@dataclass(frozen=True, slots=True)
class MaterialSourceResolution:
    """一次任务级物料来源协调结果。"""

    status: MaterialSourceResolutionStatus


class MaterialSourceResolutionCoordinator:
    """隐藏来源集合校验、整图预留和逐来源结果投影。"""

    def __init__(
        self,
        *,
        inventory: Any,
        projection: TaskRuntimeProjection,
    ) -> None:
        """绑定唯一短期库存权威与任务运行投影。

        参数：``inventory`` 是本地库存权威（Inventory Authority），可为 ``None``；
        ``projection`` 是工作流任务（WorkflowTask）/作业写边界。返回无。异常：
        构造不访问外部状态，缺库存只在任务确有来源时失败关闭。
        """

        # ``_inventory`` 必须与 EdgeScheduler 使用同一实例，禁止第二库存权威。
        self._inventory = inventory
        self._projection = projection

    def reconcile(
        self,
        task: Mapping[str, Any],
        jobs: Sequence[Mapping[str, Any]],
    ) -> MaterialSourceResolution:
        """协调一次可重放的短期任务物料准入（TaskMaterialAdmission）。

        参数：``task`` 提供冻结执行计划（ExecutionPlan）和稳定任务身份；``jobs``
        是创建事务持久化的完整工作流节点作业（WorkflowNodeJob）集合。返回：无
        来源、受阻或已准入的闭集结果。异常：计划、作业、选择器或库存装配不完整
        时抛 ``MaterialSourceResolutionError``/``StoreConflict``；只有
        ``InsufficientStock`` 被解释为可重试受阻。
        """

        task_uuid = self._required_text(task.get("uuid"), field="task.uuid")
        plan = task.get("execution_plan")
        if not isinstance(plan, Mapping):
            raise MaterialSourceResolutionError("工作流任务缺少冻结执行计划")
        raw_nodes = plan.get("nodes")
        if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes)):
            raise MaterialSourceResolutionError("执行计划节点必须是数组")
        # ``source_nodes`` 是本轮全有或全无预留共同覆盖的全部启用来源责任。
        source_nodes = [
            node
            for node in raw_nodes
            if isinstance(node, Mapping) and node.get("kind") == "material_source"
        ]
        if not source_nodes:
            return MaterialSourceResolution(status="not_required")
        if self._inventory is None:
            raise MaterialSourceResolutionError(
                "工作流声明了物料来源，但本地调度器没有装配库存权威"
            )

        source_node_uuids = {
            self._required_text(node.get("uuid"), field="material_source.uuid")
            for node in source_nodes
        }
        # ``source_jobs`` 证明每个来源都有且只有一个协调器所有的持久执行责任。
        source_jobs = [
            job for job in jobs if job.get("executor_kind") == "material_source"
        ]
        source_job_nodes = {
            self._required_text(
                job.get("workflow_node_uuid"),
                field="material_source_job.workflow_node_uuid",
            )
            for job in source_jobs
        }
        if (
            len(source_jobs) != len(source_node_uuids)
            or source_job_nodes != source_node_uuids
        ):
            raise MaterialSourceResolutionError("物料来源与解析作业集合不一致")

        requirements: dict[str, list[MaterialRequirement]] = {}
        bindings: dict[str, dict[str, str]] = {}
        for node in source_nodes:
            node_uuid = str(node["uuid"])
            raw_requirements = node.get("material_requirements")
            if not isinstance(raw_requirements, Sequence) or isinstance(
                raw_requirements, (str, bytes)
            ):
                raise MaterialSourceResolutionError("物料来源预留需求必须是数组")
            requirements[node_uuid] = [
                MaterialRequirement.from_dict(dict(requirement))
                for requirement in raw_requirements
                if isinstance(requirement, Mapping)
            ]
            if (
                len(requirements[node_uuid]) != len(raw_requirements)
                or not requirements[node_uuid]
            ):
                raise MaterialSourceResolutionError("物料来源预留需求不能为空")
            selector = node.get("param")
            if not isinstance(selector, Mapping):
                raise MaterialSourceResolutionError("物料来源选择器必须是对象")
            bindings[node_uuid] = {
                "uuid": self._required_text(
                    selector.get("material_uuid"),
                    field="material_source.material_uuid",
                ),
                "resource_template_uuid": self._required_text(
                    selector.get("resource_template_uuid"),
                    field="material_source.resource_template_uuid",
                ),
            }

        try:
            # gaojing ``reserve_workflow`` 在一个 SQLite 事务内完成整集合预留，
            # 任一来源不足会整体回滚；同一任务/来源/attempt 重放保持幂等。
            self._inventory.reserve_workflow(task_uuid, requirements)
        except InsufficientStock:
            self._projection.project_material_source_blocked(task_uuid)
            return MaterialSourceResolution(status="blocked")
        self._projection.project_material_source_admission(task_uuid, bindings)
        return MaterialSourceResolution(status="admitted")

    def release_terminal_reservations(self, task_uuid: str, *, reason: str) -> None:
        """幂等释放终态任务仍活跃的短期物料预留。

        参数：``task_uuid`` 是已完成准入的工作流任务（WorkflowTask）身份，
        ``reason`` 是稳定终态清理原因。返回：无。异常：库存权威缺失或释放失败时
        原样传播，禁止把仍活跃的预留静默当作已清理。
        """

        if self._inventory is None:
            raise MaterialSourceResolutionError(
                "工作流声明了物料来源，但本地调度器没有装配库存权威"
            )
        self._inventory.release_workflow(task_uuid, reason=reason)

    @staticmethod
    def _required_text(value: Any, *, field: str) -> str:
        """校验协调器使用的必填文本。

        参数：``value`` 是未知边界值；``field`` 是中文诊断路径。返回：去空白
        文本。异常：空值抛 ``MaterialSourceResolutionError``。
        """

        normalized = str(value or "").strip()
        if not normalized:
            raise MaterialSourceResolutionError(f"{field} 不能为空")
        return normalized


__all__ = [
    "MaterialSourceResolution",
    "MaterialSourceResolutionCoordinator",
    "MaterialSourceResolutionError",
    "MaterialSourceResolutionStatus",
]
