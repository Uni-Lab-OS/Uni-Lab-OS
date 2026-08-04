"""把标准设备单动作运行（DeviceActionRun）转换为旧 ``WorkflowSpec``。"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

from unilabos.app.scheduler.models import WorkflowNode, WorkflowSpec
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.device_action_run_store import DeviceActionRunStore
from unilabos.workflow.store import StoreConflict, WorkflowStore

logger = logging.getLogger(__name__)

MaterialResolver = Callable[[str], Mapping[str, Any] | None]


class DeviceActionRunBridgeError(RuntimeError):
    """设备单动作聚合无法安全转换为旧调度输入。"""


class DeviceActionRunWorkflowSpecBridge:
    """以标准 Task/Job 为外部身份，将单节点执行委托给旧调度器。"""

    def __init__(
        self,
        store: WorkflowStore,
        *,
        scheduler: EdgeScheduler,
        material_resolver: MaterialResolver,
    ) -> None:
        """装配兼容桥并注册旧调度器生命周期监听器。

        参数：``store`` 是标准工作流写模型；``scheduler`` 是本地旧调度器；
        ``material_resolver`` 按设备物料 UUID 返回明确 Edge 本地设备身份。返回无。
        """

        self._store = store
        self._run_store = DeviceActionRunStore(store)
        self._scheduler = scheduler
        self._material_resolver = material_resolver
        # ``submitted_jobs`` 只用于过滤同一旧调度器中的非桥接工作流，不承担恢复。
        self._submitted_jobs: set[str] = set()
        self._closed = False
        scheduler.add_job_pre_dispatch_listener(self._on_job_pre_dispatch)
        scheduler.add_job_finished_listener(self._on_job_finished)

    def close(self) -> None:
        """注销兼容桥监听器；返回无，重复关闭幂等。"""

        if self._closed:
            return
        self._closed = True
        self._scheduler.remove_job_pre_dispatch_listener(self._on_job_pre_dispatch)
        self._scheduler.remove_job_finished_listener(self._on_job_finished)
        self._submitted_jobs.clear()

    def submit(self, aggregate: Mapping[str, Any]) -> None:
        """把已经持久化的设备单动作 Task/Job 转换为一个 ``WorkflowSpec``。

        参数：``aggregate`` 必须含 Backend 形状的 ``task``、``job`` 和 ``created``。
        返回无；只有首次创建可提交，转换失败时抛出 ``DeviceActionRunBridgeError``。
        """

        if self._closed:
            raise DeviceActionRunBridgeError("设备单动作执行桥已经关闭")
        if aggregate.get("created") is not True:
            return
        task = self._object(aggregate.get("task"), field="task")
        job = self._object(aggregate.get("job"), field="job")
        task_uuid = self._required_text(task.get("uuid"), field="task.uuid")
        job_uuid = self._required_text(job.get("uuid"), field="job.uuid")
        if job.get("workflow_task_uuid") != task_uuid:
            raise DeviceActionRunBridgeError("设备单动作 Task/Job 身份不一致")
        spec = self._workflow_spec(task=task, job=job)
        self._submitted_jobs.add(job_uuid)
        try:
            self._scheduler.submit_workflow(spec)
        except BaseException:
            # ``submit_workflow`` 会先登记旧运行再重排；派发前事务失败时必须把该
            # 运行转为取消终态，否则下一次任意重排可能绕过本次请求偷偷派发。
            self._scheduler.cancel_workflow(task_uuid)
            self._submitted_jobs.discard(job_uuid)
            raise

    def _workflow_spec(
        self,
        *,
        task: Mapping[str, Any],
        job: Mapping[str, Any],
    ) -> WorkflowSpec:
        """构造只含一个节点的旧调度输入。

        参数：``task`` 提供冻结执行快照，``job`` 提供唯一执行身份和最终参数。返回
        使用 Task UUID 作为临时 Workflow 身份的 ``WorkflowSpec``。
        """

        task_uuid = self._required_text(task.get("uuid"), field="task.uuid")
        job_uuid = self._required_text(job.get("uuid"), field="job.uuid")
        node_uuid = self._required_text(
            job.get("workflow_node_uuid"),
            field="job.workflow_node_uuid",
        )
        device_material_uuid = self._required_text(
            job.get("material_uuid"),
            field="job.material_uuid",
        )
        device_material = self._material_resolver(device_material_uuid)
        if device_material is None:
            raise DeviceActionRunBridgeError("设备物料不存在或不可用")
        device_id = self._edge_local_id(device_material)
        snapshot = self._object(
            task.get("workflow_snapshot"),
            field="task.workflow_snapshot",
        )
        nodes = snapshot.get("nodes")
        if not isinstance(nodes, list):
            raise DeviceActionRunBridgeError("设备单动作冻结快照缺少 nodes")
        matches = [
            self._object(node, field="task.workflow_snapshot.nodes[]")
            for node in nodes
            if isinstance(node, Mapping) and str(node.get("uuid") or "") == node_uuid
        ]
        if len(matches) != 1:
            raise DeviceActionRunBridgeError("设备单动作冻结快照没有唯一目标节点")
        frozen_node = matches[0]
        action_name = self._required_text(
            frozen_node.get("action_name"),
            field="snapshot.action_name",
        )
        action_type = self._required_text(
            frozen_node.get("action_type"),
            field="snapshot.action_type",
        )
        param = self._object(job.get("param"), field="job.param")
        return WorkflowSpec(
            workflow_id=task_uuid,
            task_id=task_uuid,
            nodes=[
                WorkflowNode(
                    id=node_uuid,
                    job_id=job_uuid,
                    device_id=device_id,
                    action_name=action_name,
                    action_type=action_type,
                    param=dict(param),
                    node_type=str(frozen_node.get("type") or "ILab"),
                )
            ],
        )

    def _on_job_pre_dispatch(self, dispatching: Mapping[str, Any]) -> None:
        """在物理派发前提交标准 Task/Job 派发意图。

        参数：``dispatching`` 是旧调度器即将派发的摘要。返回无；非本桥提交的作业
        忽略；持久化失败会阻止物理派发。
        """

        job_uuid = str(dispatching.get("job_id") or "")
        if job_uuid not in self._submitted_jobs:
            return
        task_uuid = self._required_text(
            dispatching.get("workflow_id"),
            field="dispatching.workflow_id",
        )
        self._run_store.mark_dispatched(
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
        """把旧调度器终态结果投影回标准 Task/Job。

        参数：``job_uuid`` 是标准作业身份；``success`` 是旧成功标记；
        ``ret_value`` 是设备结果；``suc_type`` 是旧人工决策结果。返回无。
        """

        if job_uuid not in self._submitted_jobs:
            return
        # ``return_info`` 保持 Backend 对象字段；标量旧返回值放入明确包装字段。
        return_info = (
            dict(ret_value)
            if isinstance(ret_value, Mapping)
            else ({"return_value": ret_value} if ret_value is not None else {})
        )
        error_info: list[Any] = []
        if not success:
            error_info = [
                {
                    "code": "legacy_edge_scheduler_action_failed",
                    "message": "设备动作执行失败",
                    "suc_type": suc_type,
                }
            ]
        try:
            self._run_store.complete(
                job_uuid=job_uuid,
                success=success,
                return_info=return_info,
                error_info=error_info,
            )
        except StoreConflict:
            logger.exception("设备单动作终态无法写回标准 Task/Job")
            raise
        finally:
            self._submitted_jobs.discard(job_uuid)

    @staticmethod
    def _edge_local_id(material: Mapping[str, Any]) -> str:
        """从设备物料摘要读取明确 Edge 本地设备身份。

        参数：``material`` 是库存权威摘要。返回 ``edge_local_id``；字段缺失时关闭
        失败，禁止从物料名称、条码或 UUID 猜测设备路由。
        """

        direct = str(material.get("edge_local_id") or "").strip()
        if direct:
            return direct
        meta_data = material.get("meta_data")
        if isinstance(meta_data, str):
            try:
                meta_data = json.loads(meta_data)
            except ValueError:
                meta_data = None
        if isinstance(meta_data, Mapping):
            local_id = str(meta_data.get("edge_local_id") or "").strip()
            if local_id:
                return local_id
        raise DeviceActionRunBridgeError("设备物料缺少明确 edge_local_id")

    @staticmethod
    def _object(value: Any, *, field: str) -> Mapping[str, Any]:
        """校验桥接输入对象；参数是未知值和诊断字段名，返回只读映射。"""

        if not isinstance(value, Mapping):
            raise DeviceActionRunBridgeError(f"{field} 必须是对象")
        return value

    @staticmethod
    def _required_text(value: Any, *, field: str) -> str:
        """校验桥接必填文本；参数是未知值和诊断字段名，返回去空白字符串。"""

        text = str(value or "").strip()
        if not text:
            raise DeviceActionRunBridgeError(f"{field} 不能为空")
        return text


__all__ = [
    "DeviceActionRunBridgeError",
    "DeviceActionRunWorkflowSpecBridge",
]
