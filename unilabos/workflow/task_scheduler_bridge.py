"""把标准工作流任务（WorkflowTask）委托给既有本地调度器。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.store import StoreConflict, WorkflowStore
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection
from unilabos.workflow.workflow_spec_compiler import WorkflowSpecCompiler

logger = logging.getLogger(__name__)


class TaskSchedulerBridgeError(RuntimeError):
    """工作流任务不能安全进入本地调度器时使用的稳定桥接错误。"""


class TaskSchedulerBridge:
    """隐藏任务编译、短期物料门禁、生命周期投影和监听器清理。"""

    def __init__(
        self,
        store: WorkflowStore,
        *,
        scheduler: EdgeScheduler,
        compiler: WorkflowSpecCompiler | None = None,
        projection: TaskRuntimeProjection | None = None,
    ) -> None:
        """装配唯一工作流任务调度桥（TaskSchedulerBridge）。

        参数：``store`` 是标准任务/作业写权威；``scheduler`` 是既有本地调度器；
        ``compiler`` 把冻结执行计划（ExecutionPlan）转换为遗留调度规格；
        ``projection`` 把调度生命周期写回标准事实。返回无。异常：构造不访问
        数据库；库存服务只能从 ``scheduler.inventory_service`` 读取，不能另行注入。
        """

        # ``_store`` 是本桥唯一的工作流任务（WorkflowTask）持久事实来源。
        self._store = store
        # ``_scheduler`` 同时持有唯一允许复用的本地库存权威（Inventory Authority）。
        self._scheduler = scheduler
        self._compiler = compiler or WorkflowSpecCompiler()
        self._projection = projection or TaskRuntimeProjection(store)
        # ``_task_by_job`` 只过滤本桥提交到共享调度器的作业，不承担持久恢复。
        self._task_by_job: dict[str, str] = {}
        # ``_submitted_tasks`` 标识仍可进行准入重试（AdmissionRetry）的本地运行。
        self._submitted_tasks: set[str] = set()
        self._closed = False
        scheduler.add_job_pre_dispatch_listener(self._on_job_pre_dispatch)
        scheduler.add_job_finished_listener(self._on_job_finished)

    def submit(self, task: Mapping[str, Any]) -> dict[str, Any]:
        """提交已经持久化的工作流任务（WorkflowTask）。

        参数：``task`` 是创建事务返回的标准任务投影。返回：调度同步推进后的标准
        任务/作业聚合。异常：桥关闭、任务身份非法、冻结计划编译失败、缺少库存权威
        或派发前投影冲突时失败关闭；失败不会创建新的任务/作业身份。
        """

        if self._closed:
            raise TaskSchedulerBridgeError("工作流任务调度桥已经关闭")
        # ``task_uuid`` 是本地调度运行、遗留预留和标准任务共用的稳定身份。
        task_uuid = self._required_text(task.get("uuid"), field="task.uuid")
        if task_uuid in self._submitted_tasks:
            return self._aggregate(task_uuid)
        jobs: list[dict[str, Any]] = []
        registered = False
        try:
            persisted_task = self._store.get_task(task_uuid)
            # ``jobs`` 是创建事务已经确定的工作流节点作业（WorkflowNodeJob）集合。
            jobs = self._store.list_jobs(task_uuid)
            spec = self._compiler.compile(persisted_task, jobs)
            if (
                spec.material_requirements_by_node()
                and self._scheduler.inventory_service is None
            ):
                raise TaskSchedulerBridgeError(
                    "工作流声明了物料需求，但本地调度器没有装配库存权威"
                )

            for job in jobs:
                # ``job_uuid`` 是监听器回调与标准持久作业之间的稳定路由身份。
                job_uuid = self._required_text(job.get("uuid"), field="jobs[].uuid")
                self._task_by_job[job_uuid] = task_uuid
            self._submitted_tasks.add(task_uuid)
            registered = True
            submission = self._scheduler.submit_workflow(spec)
            # ``scheduler_state`` 是内部等料或运行状态；投影层负责限制 wire 状态。
            scheduler_state = self._required_text(
                submission.get("state"), field="scheduler.state"
            )
            return self._projection.project_submission(task_uuid, scheduler_state)
        except Exception as error:
            if self._crossed_dispatch_boundary(jobs):
                raise TaskSchedulerBridgeError(
                    "工作流任务派发结果不确定，已保留在途执行等待明确结果"
                ) from error
            if registered:
                self._cancel_failed_submission(task_uuid, jobs)
            if isinstance(error, TaskSchedulerBridgeError):
                raise
            raise TaskSchedulerBridgeError(
                "工作流任务无法安全提交到本地调度器"
            ) from error

    def retry_admission(self, task_uuid: str) -> dict[str, Any]:
        """对同一待处理任务触发准入重试（AdmissionRetry）。

        参数：``task_uuid`` 是此前已提交但因物料不足等待的稳定任务身份。返回：
        重排后的标准任务/作业聚合。异常：桥关闭、未知任务或调度重排失败时传播；
        本操作绝不创建新任务、作业或执行尝试身份。
        """

        if self._closed:
            raise TaskSchedulerBridgeError("工作流任务调度桥已经关闭")
        normalized_uuid = self._required_text(task_uuid, field="task_uuid")
        if normalized_uuid not in self._submitted_tasks:
            raise TaskSchedulerBridgeError("工作流任务尚未提交到本地调度器")
        self._scheduler.reschedule()
        return self._aggregate(normalized_uuid)

    def close(self) -> None:
        """幂等注销本桥的调度生命周期监听器。

        参数：无。返回：无；重复调用不重复注销，关闭后清除仅用于回调过滤的内存
        路由，但不修改任何持久任务、作业或物料预留事实。
        """

        if self._closed:
            return
        self._closed = True
        self._scheduler.remove_job_pre_dispatch_listener(self._on_job_pre_dispatch)
        self._scheduler.remove_job_finished_listener(self._on_job_finished)
        self._task_by_job.clear()
        self._submitted_tasks.clear()

    def _on_job_pre_dispatch(self, dispatching: Mapping[str, Any]) -> None:
        """在物理派发前提交标准作业派发意图。

        参数：``dispatching`` 是既有调度器即将越过执行边界的作业摘要。返回无；
        非本桥作业忽略，持久转换冲突原样抛出并阻止执行适配器调用。
        """

        job_uuid = str(dispatching.get("job_id") or "")
        task_uuid = self._task_by_job.get(job_uuid)
        if task_uuid is None:
            return
        dispatch_task_uuid = self._required_text(
            dispatching.get("workflow_id"), field="dispatching.workflow_id"
        )
        if dispatch_task_uuid != task_uuid:
            raise StoreConflict(f"派发作业与任务身份不一致：{job_uuid}")
        self._projection.project_pre_dispatch(
            task_uuid=task_uuid,
            job_uuid=job_uuid,
        )

    def _on_job_finished(
        self,
        job_uuid: str,
        success: bool,
        ret_value: Any,
        suc_type: str,
    ) -> None:
        """把既有调度器明确结果投影为标准任务/作业终态。

        参数：``job_uuid`` 是稳定作业身份；``success`` 是明确成功标志；
        ``ret_value`` 是设备结果；``suc_type`` 是遗留人工处理分类。返回无；非本桥
        作业忽略，投影冲突传播给调度器记录，绝不触发物理重做。
        """

        task_uuid = self._task_by_job.get(job_uuid)
        if task_uuid is None:
            return
        # ``return_info`` 保持标准对象字段；标量结果使用明确包装键。
        return_info = (
            dict(ret_value)
            if isinstance(ret_value, Mapping)
            else ({"return_value": ret_value} if ret_value is not None else {})
        )
        # ``error_info`` 只在明确失败时记录稳定代码与人工决策来源。
        error_info: list[dict[str, Any]] = []
        if not success:
            error_info = [
                {
                    "code": "legacy_edge_scheduler_action_failed",
                    "message": "设备动作执行失败",
                    "suc_type": suc_type,
                }
            ]
        self._projection.project_job_finished(
            job_uuid=job_uuid,
            scheduler_state="success" if success else "failed",
            return_info=return_info,
            error_info=error_info,
        )
        self._task_by_job.pop(job_uuid, None)
        if task_uuid not in self._task_by_job.values():
            self._submitted_tasks.discard(task_uuid)

    def _aggregate(self, task_uuid: str) -> dict[str, Any]:
        """读取一个标准任务/作业聚合。

        参数：``task_uuid`` 是父任务稳定身份。返回：当前任务投影和有序作业列表。
        异常：任务不存在时传播工作流存储（WorkflowStore）异常。
        """

        return {
            "task": self._store.get_task(task_uuid),
            "jobs": self._store.list_jobs(task_uuid),
        }

    def _cancel_failed_submission(
        self,
        task_uuid: str,
        jobs: list[dict[str, Any]],
    ) -> None:
        """封闭一次未越过执行边界的失败提交。

        参数：``task_uuid`` 是旧调度运行身份；``jobs`` 是本次路由的持久作业集合。
        返回无；尽力取消遗留内存运行并清除监听路由，原始异常由调用方保留。
        """

        try:
            self._scheduler.cancel_workflow(task_uuid)
        except Exception:  # 清理失败不能覆盖原始安全错误
            logger.exception("失败的工作流任务提交无法清理遗留调度运行")
        self._submitted_tasks.discard(task_uuid)
        for job in jobs:
            self._task_by_job.pop(str(job.get("uuid") or ""), None)

    def _crossed_dispatch_boundary(self, jobs: list[dict[str, Any]]) -> bool:
        """判断标准作业是否已经越过持久派发边界。

        参数：``jobs`` 是本次提交的既有工作流节点作业（WorkflowNodeJob）集合。
        返回：任一作业已为 ``dispatched`` 或 ``running`` 时为真。异常：存储读取
        故障视为不能证明未派发，保守返回真并禁止取消或清除回调路由。
        """

        try:
            # ``persisted_statuses`` 是物理派发前投影提交后的标准状态集合。
            persisted_statuses = {
                self._store.get_job(str(job.get("uuid") or ""))["status"]
                for job in jobs
            }
        except Exception:  # noqa: BLE001 - 无法证明未派发时必须保守保留在途事实
            return True
        return bool(persisted_statuses & {"dispatched", "running"})

    @staticmethod
    def _required_text(value: Any, *, field: str) -> str:
        """校验桥接必填文本。

        参数：``value`` 是未知输入，``field`` 是稳定诊断字段。返回：去空白文本。
        异常：空值抛出 ``TaskSchedulerBridgeError``。
        """

        normalized = str(value or "").strip()
        if not normalized:
            raise TaskSchedulerBridgeError(f"{field} 不能为空")
        return normalized


__all__ = ["TaskSchedulerBridge", "TaskSchedulerBridgeError"]
