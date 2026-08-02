"""EdgeScheduler：Edge 侧执行态调度器（推进的唯一入口）。

重排触发点（硬性约定，二者都强制全量 reschedule）：

1. **每个工作流提交**（``submit_workflow``）
2. **每个子 action 完成**（``on_job_finished``，含成功/失败）

每次 reschedule：

    收集所有 RUNNING 工作流的 ready 节点
      → TaskOrderer 排序（本地 stub 或 HTTP 调 uni-lab-scheduler）
      → 按序下发；device_action_key 被占用的节点跳过，等下一次触发
      → 下发前解析父节点传参（gjson/sjson + ``@@@`` 语义）

不做一次性拓扑序：ready 集合每次触发点都重新计算、重新排序。

Material admission/release only serves durable WorkflowTasks. Legacy
``WorkflowSpec.material_requirements`` is rejected instead of guessed or
silently dispatched.

物料锁（``@action(lock_resource=[...])``，注入 lock_resource_resolver 时启用）：

- 下发前用 resolver 取该动作声明的 ResourceSlot 参数名，从已解析参数里提取
  资源标识生成锁键；与在执行 job 的锁键冲突 → 本轮跳过（等释放后的重排）
- job 完成 / 工作流取消时释放
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid as uuid_mod
from collections import deque
from collections.abc import Callable
from typing import Any

from unilabos.app.scheduler.dag_state import WorkflowRun
from unilabos.app.scheduler.dispatch import (
    Dispatcher,
    RecordingDispatcher,
    build_job_start_payload,
)
from unilabos.app.scheduler.estimation import DurationEstimator
from unilabos.app.scheduler.inventory.domain import (
    InventoryError,
    TaskMaterialAdmissionCommand,
    TaskMaterialAdmissionResult,
    TaskMaterialAdmissionSource,
    TaskMaterialReleaseCommand,
    TaskMaterialReleaseResult,
)
from unilabos.app.scheduler.models import (
    DispatchedJob,
    ReadyTask,
    WorkflowSpec,
    WorkflowState,
    priority_weight,
)
from unilabos.app.scheduler.ordering import (
    OrderingContext,
    StableLocalOrderer,
    TaskOrderer,
)
from unilabos.app.scheduler.param_resolver import ParamResolveError

logger = logging.getLogger(__name__)

# ResourceSlot 参数值里可作为资源标识的字段（按优先级取第一个非空）
_RESOURCE_ID_FIELDS = ("unilabos_uuid", "uuid", "id", "name")


def _extract_resource_ids(value: Any) -> set[str]:
    """从 action 参数值提取资源标识（锁键素材）。

    支持形态：字符串（uuid/名称）、dict（ResourceSlot 原始入参，含
    unilabos_uuid/uuid/id/name 任一字段，或嵌套 ``data.unilabos_uuid``）、
    以及它们的 list/tuple。取不到标识的值直接忽略（宁可漏锁不误锁）。
    """
    ids: set[str] = set()
    if value is None:
        return ids
    if isinstance(value, str):
        if value:
            ids.add(value)
        return ids
    if isinstance(value, (list, tuple, set)):
        for item in value:
            ids |= _extract_resource_ids(item)
        return ids
    if isinstance(value, dict):
        nested = value.get("data")
        if isinstance(nested, dict) and nested.get("unilabos_uuid"):
            ids.add(str(nested["unilabos_uuid"]))
            return ids
        for field_name in _RESOURCE_ID_FIELDS:
            field_value = value.get(field_name)
            if isinstance(field_value, str) and field_value:
                ids.add(field_value)
                return ids
        return ids
    return ids


class EdgeScheduler:
    def __init__(
        self,
        orderer: TaskOrderer | None = None,
        dispatcher: Dispatcher | None = None,
        external_busy_keys: set[str] | None = None,
        busy_key_provider: Callable[[], set[str]] | None = None,
        workflow_state_listener: Callable[[str, str], None] | None = None,
        inventory: Any = None,
        lock_resource_resolver: Callable[[str, str], list[str]] | None = None,
        estimator: DurationEstimator | None = None,
        timeline_capacity: int = 400,
        monitor: Any = None,
        history: Any = None,
        workflow_tasks: Any = None,
        admission_fault_hook: Callable[[str], None] | None = None,
    ):
        self._orderer = orderer or StableLocalOrderer()
        self._dispatcher = dispatcher or RecordingDispatcher()
        self._lock = threading.RLock()
        self._material_saga_lock = threading.Lock()

        self._workflows: dict[str, WorkflowRun] = {}
        # job_id -> DispatchedJob（完成回调路由 + 资源锁）
        self._inflight: dict[str, DispatchedJob] = {}
        # 外部注入的锁（例如 DeviceActionManager 已占用的设备），可选
        self._external_busy_keys = (
            external_busy_keys if external_busy_keys is not None else set()
        )
        # 实时锁视图提供者（微后端 busy_device_action_keys），可选
        self._busy_key_provider = busy_key_provider
        # 工作流终态通知（success/failed/canceled 各通知一次；锁外触发）
        self._workflow_state_listener = workflow_state_listener
        self._notified_workflows: set[str] = set()
        self._reschedule_count = 0
        # Canonical InventoryService used only by durable WorkflowTask sagas.
        self._inventory = inventory
        # 物料/资源锁：resolver(device_id, action_name) -> @action(lock_resource=[...])
        # 声明的参数名列表；None = 物料锁关闭
        self._lock_resource_resolver = lock_resource_resolver
        # job_id -> 该 job 持有的资源锁键（job 完成/取消时释放）
        self._job_resource_locks: dict[str, set[str]] = {}
        # 时长预估器（declared / historical / auto 三种 mode，内含两种计算模式）
        self._estimator = estimator or DurationEstimator()
        # 泳道图时间线：已完结 job 的起止记录（环形缓冲）
        self._timeline: deque[dict[str, Any]] = deque(maxlen=timeline_capacity)
        # 实时监控总线（duck-typed emit(channel, type, data)）；None = 关闭
        self._monitor = monitor
        # 工作流执行历史（WorkflowHistoryStore，独立 SQLite）；None = 不落盘
        self._history = history
        # 新 WorkflowTask kernel 的持久投影 port；不参与 legacy WorkflowRun DAG。
        self._workflow_tasks = workflow_tasks
        self._admission_fault_hook = admission_fault_hook

    def _emit_monitor(
        self, channel: str, event_type: str, data: dict[str, Any]
    ) -> None:
        if self._monitor is None:
            return
        try:
            self._monitor.emit(channel, event_type, data)
        except Exception:  # noqa: BLE001, S110 - 监控故障不影响调度
            pass

    def _safe_history(self, method: str, *args: Any, **kwargs: Any) -> None:
        """写执行历史；持久化故障不影响调度。"""
        if self._history is None:
            return
        try:
            getattr(self._history, method)(*args, **kwargs)
        except Exception:
            logger.exception("[EdgeScheduler] history.%s failed", method)

    def set_workflow_state_listener(self, listener: Callable[[str, str], None]) -> None:
        self._workflow_state_listener = listener

    def reconcile_task_admission(
        self,
        task_uuid: str,
    ) -> TaskMaterialAdmissionResult | None:
        """Drive one replay-safe workflow.db ↔ inventory.db admission saga."""

        with self._material_saga_lock:
            return self._reconcile_task_admission_locked(task_uuid)

    def _reconcile_task_admission_locked(
        self,
        task_uuid: str,
    ) -> TaskMaterialAdmissionResult | None:
        """Serialize one complete cross-database admission command."""

        if self._workflow_tasks is None or self._inventory is None:
            raise RuntimeError("Workflow Task Material coordination is not configured")
        task = self._workflow_tasks.get_workflow_task(task_uuid)
        snapshot = task.get("workflow_snapshot")
        if not isinstance(snapshot, dict):
            raise TypeError("Workflow Task snapshot is invalid")
        nodes = snapshot.get("nodes")
        if not isinstance(nodes, list):
            raise TypeError("Workflow Task snapshot nodes are invalid")
        encoded_snapshot = json.dumps(
            snapshot,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        snapshot_fingerprint = f"sha256:{hashlib.sha256(encoded_snapshot).hexdigest()}"
        sources: list[TaskMaterialAdmissionSource] = []
        for node in sorted(
            (
                item
                for item in nodes
                if isinstance(item, dict) and item.get("type") == "material_source"
            ),
            key=lambda item: str(item.get("uuid") or ""),
        ):
            param = node.get("param")
            if not isinstance(param, dict):
                raise TypeError("MaterialSource snapshot parameter is invalid")
            slot_range = param.get("slot_range")
            candidate_site_uuids = (
                tuple(slot_range) if isinstance(slot_range, list) else ()
            )
            sources.append(
                TaskMaterialAdmissionSource(
                    material_source_node_uuid=str(node.get("uuid") or ""),
                    mode=str(param.get("mode") or ""),
                    resource_template_uuid=str(
                        param.get("resource_template_uuid") or ""
                    ),
                    mount=dict(param.get("mount") or {}),
                    material_uuid=(
                        str(param["material_uuid"])
                        if param.get("material_uuid") is not None
                        else None
                    ),
                    site_uuid=(
                        str(param["site"]) if param.get("site") is not None else None
                    ),
                    candidate_site_uuids=candidate_site_uuids,
                    flow_role=str(param.get("flow_role") or ""),
                )
            )
        if not sources:
            return None
        canonical_task_uuid = str(task["uuid"])
        command_uuid = str(
            uuid_mod.uuid5(
                uuid_mod.UUID(canonical_task_uuid),
                f"material-admission:{snapshot_fingerprint}",
            )
        )
        command = TaskMaterialAdmissionCommand(
            schema_version=1,
            command_uuid=command_uuid,
            idempotency_key=(
                f"workflow-task:{canonical_task_uuid}:material-admission:"
                f"{snapshot_fingerprint}"
            ),
            workflow_task_uuid=canonical_task_uuid,
            workflow_snapshot_fingerprint=snapshot_fingerprint,
            sources=tuple(sources),
        )
        result = self._inventory.admit_task(command)
        self._inject_admission_fault("after_inventory_commit")
        self._workflow_tasks.project_material_admission(result)
        self._inject_admission_fault("after_workflow_projection")
        self._inventory.acknowledge(result.outbox_sequence)
        return result

    def reconcile_pending_task_admissions(self) -> tuple[str, ...]:
        """Replay pending Task admissions in durable creation order at startup."""

        if self._workflow_tasks is None or self._inventory is None:
            raise RuntimeError("Workflow Task Material coordination is not configured")
        pending: list[dict[str, Any]] = []
        reconciled: list[str] = []
        page = 1
        while True:
            tasks = self._workflow_tasks.list_workflow_tasks(
                page=page,
                page_size=100,
                status="pending",
            )
            items = tasks.get("items")
            if not isinstance(items, list):
                raise TypeError("Workflow Task list projection is invalid")
            pending.extend(items)
            if page * 100 >= int(tasks.get("total") or 0):
                break
            page += 1
        for task in sorted(
            pending,
            key=lambda item: (
                str(item.get("create_time") or ""),
                str(item.get("uuid") or ""),
            ),
        ):
            task_uuid = str(task.get("uuid") or "")
            if self.reconcile_task_admission(task_uuid) is not None:
                reconciled.append(task_uuid)
        return tuple(reconciled)

    def _inject_admission_fault(self, stage: str) -> None:
        hook = self._admission_fault_hook
        if hook is not None:
            hook(stage)

    def can_dispatch_task_materials(self, task_uuid: str) -> bool:
        """Fail-closed proof used before WorkflowTask Job dispatch admission."""

        if self._workflow_tasks is None or self._inventory is None:
            return False
        projection = self._workflow_tasks.get_material_admission(task_uuid)
        if not isinstance(projection, dict) or projection.get("status") != "admitted":
            return False
        reservation_uuid = projection.get("reservation_uuid")
        if not isinstance(reservation_uuid, str) or not reservation_uuid:
            return False
        try:
            return bool(
                self._inventory.has_active_task_reservation(
                    task_uuid,
                    reservation_uuid,
                )
            )
        except InventoryError:
            return False

    def reconcile_task_release(
        self,
        task_uuid: str,
        reason: str,
    ) -> TaskMaterialReleaseResult:
        """Drive one replay-safe terminal Task Material release saga."""

        with self._material_saga_lock:
            return self._reconcile_task_release_locked(task_uuid, reason)

    def _reconcile_task_release_locked(
        self,
        task_uuid: str,
        reason: str,
    ) -> TaskMaterialReleaseResult:
        """Serialize one complete cross-database release command."""

        if self._workflow_tasks is None or self._inventory is None:
            raise RuntimeError("Workflow Task Material coordination is not configured")
        task = self._workflow_tasks.get_workflow_task(task_uuid)
        canonical_task_uuid = str(task["uuid"])
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("Material release reason must not be blank")
        command_uuid = str(
            uuid_mod.uuid5(
                uuid_mod.UUID(canonical_task_uuid),
                f"material-release:{normalized_reason}",
            )
        )
        command = TaskMaterialReleaseCommand(
            schema_version=1,
            command_uuid=command_uuid,
            idempotency_key=(
                f"workflow-task:{canonical_task_uuid}:material-release:"
                f"{normalized_reason}"
            ),
            workflow_task_uuid=canonical_task_uuid,
            reason=normalized_reason,
        )
        result = self._inventory.release_task(command)
        self._inject_admission_fault("after_inventory_release_commit")
        self._workflow_tasks.project_material_release(result)
        self._inject_admission_fault("after_workflow_release_projection")
        self._inventory.acknowledge(result.outbox_sequence)
        return result

    # ── 触发点 1：任务进来 ────────────────────────────────────

    def submit_workflow(self, spec: WorkflowSpec) -> dict[str, Any]:
        """提交工作流并立即重排。返回本次下发结果。

        Legacy material requirements are not an admission contract and fail closed.
        """
        with self._lock:
            if spec.workflow_id in self._workflows:
                raise ValueError(f"workflow {spec.workflow_id} already submitted")
            if spec.material_requirements_by_node():
                raise ValueError(
                    "WorkflowSpec material requirements are retired; "
                    "use WorkflowTask Material admission"
                )
            run = WorkflowRun(spec)  # 构图 + 环检测，失败直接抛
            self._workflows[spec.workflow_id] = run

            logger.info(
                "[EdgeScheduler] workflow %s submitted (%d nodes, state=%s), reschedule",
                spec.workflow_id,
                len(spec.nodes),
                run.state.value,
            )
            self._emit_monitor(
                "scheduler",
                "workflow_submitted",
                {
                    "workflow_id": spec.workflow_id,
                    "nodes": len(spec.nodes),
                    "state": run.state.value,
                    "priority": str(spec.priority),
                },
            )
            self._safe_history("record_submitted", spec, run.state.value)
            dispatched = self._reschedule_locked()
            notifications = self._collect_terminal_notifications()
        self._fire_notifications(notifications)
        return {
            "workflow_id": spec.workflow_id,
            "state": run.state.value,
            "dispatched": dispatched,
        }

    # ── 触发点 2：子 action 完成 ──────────────────────────────

    def on_job_finished(
        self,
        job_id: str,
        success: bool,
        ret_value: Any = None,
        suc_type: str = "normal",
    ) -> dict[str, Any]:
        """job 完成回调（成功或失败）：写回结果 → 清依赖 → 强制重排。

        ``suc_type`` 来自设备侧异常决策（registry.action_policy）：
        normal / skip / operator_intervention。skip 表示动作报错后人工选择
        跳过——节点按成功推进，但其已消费物料隔离待复核。
        """
        with self._lock:
            job = self._inflight.pop(job_id, None)
            self._job_resource_locks.pop(job_id, None)
            if job is None:
                logger.warning("[EdgeScheduler] unknown job finished: %s", job_id)
                return {"dispatched": []}

            # 泳道图时间线：记录实际起止 + 喂给历史统计（EMA）+ 历史库落盘
            self._record_timeline(
                job, success=success, suc_type=suc_type, ret_value=ret_value
            )

            run = self._workflows.get(job.workflow_id)
            if run is None:
                return {"dispatched": []}

            if success:
                run.mark_finished(job.node_id, ret_value)
            else:
                run.mark_failed(job.node_id)
                # 失败工作流的未下发节点不再推进；已下发的等它们各自回调
                logger.warning(
                    "[EdgeScheduler] node %s failed, workflow %s stops advancing",
                    job.node_id,
                    job.workflow_id,
                )

            logger.info(
                "[EdgeScheduler] job %s (wf=%s node=%s success=%s) finished, reschedule",
                job_id[:8],
                job.workflow_id,
                job.node_id,
                success,
            )
            dispatched = self._reschedule_locked()
            result = {
                "workflow_id": job.workflow_id,
                "workflow_state": run.state.value,
                "dispatched": dispatched,
            }
            notifications = self._collect_terminal_notifications()
        self._fire_notifications(notifications)
        return result

    # ── 重排核心 ─────────────────────────────────────────────

    def reschedule(self) -> list[dict[str, Any]]:
        """手动触发重排（API 暴露；正常推进依赖两个自动触发点）。"""
        with self._lock:
            return self._reschedule_locked()

    def _reschedule_locked(self) -> list[dict[str, Any]]:
        self._reschedule_count += 1

        ready: list[ReadyTask] = []
        for run in self._workflows.values():
            if run.state is not WorkflowState.RUNNING:
                continue
            weight = priority_weight(run.spec.priority)
            for node in run.ready_nodes():
                ready.append(
                    ReadyTask(
                        workflow_id=run.spec.workflow_id,
                        node=node,
                        priority_weight=weight,
                        submitted_at=run.spec.submitted_at,
                    )
                )

        if not ready:
            return []

        busy = self._busy_keys()
        held_resource_locks = self._held_resource_locks()
        ordered = self._orderer.order(ready, OrderingContext(set(busy)))

        dispatched: list[dict[str, Any]] = []
        for task in ordered:
            key = task.node.device_action_key
            # manual_confirm 是 always-free 特殊节点：不占设备动作锁，也不受其阻塞
            manual_confirm = task.node.is_manual_confirm()
            if not manual_confirm and key in busy:
                # 设备/动作被占用：本轮跳过，等占用 job 完成的那次重排再下发
                continue

            run = self._workflows[task.workflow_id]
            try:
                resolved_args = run.resolve_params(task.node.id)
            except ParamResolveError as exc:
                logger.error(
                    "[EdgeScheduler] param resolve failed for wf=%s node=%s: %s",
                    task.workflow_id,
                    task.node.id,
                    exc,
                )
                run.mark_failed(task.node.id)
                continue

            # 物料锁：@action(lock_resource=[...]) 声明的资源被在执行 job 占用 → 本轮跳过
            lock_keys = self._resource_lock_keys(task.node, resolved_args)
            if lock_keys & held_resource_locks:
                logger.info(
                    "[EdgeScheduler] node %s waits for resource lock(s) %s (wf=%s)",
                    task.node.id,
                    sorted(lock_keys & held_resource_locks),
                    task.workflow_id,
                )
                continue

            job_id = uuid_mod.uuid4().hex
            payload = build_job_start_payload(
                job_id=job_id,
                task_id=run.spec.task_id,
                workflow_id=task.workflow_id,
                node_id=task.node.id,
                device_id=task.node.device_id,
                action_name=task.node.action_name,
                action_type=task.node.action_type,
                action_args=resolved_args,
            )
            # 预估基于 sjson 覆写后的 resolved 参数：父节点经 gjson/sjson 传下来的
            # 实际值（如 time）直接决定声明式预估结果
            estimated_s, estimate_source = self._estimator.estimate(key, resolved_args)
            if not manual_confirm:
                self._dispatcher.dispatch(payload)
            # manual_confirm 不进执行器：job 停驻在 inflight，
            # 由 POST /jobs/{job_id}/finish（人工确认）走统一完成路径
            run.mark_dispatched(task.node.id)
            self._inflight[job_id] = DispatchedJob(
                job_id=job_id,
                workflow_id=task.workflow_id,
                node_id=task.node.id,
                device_action_key=key,
                device_id=task.node.device_id,
                action_name=task.node.action_name,
                estimated_s=estimated_s,
                estimate_source=estimate_source,
            )
            if not manual_confirm:
                busy.add(key)
            if lock_keys:
                self._job_resource_locks[job_id] = lock_keys
                held_resource_locks |= lock_keys
            dispatched.append(
                {
                    "job_id": job_id,
                    "workflow_id": task.workflow_id,
                    "node_id": task.node.id,
                    "device_action_key": key,
                    "estimated_s": round(estimated_s, 3),
                    "estimate_source": estimate_source,
                }
            )
            self._emit_monitor(
                "action",
                "job_dispatched",
                {
                    "job_id": job_id,
                    "workflow_id": task.workflow_id,
                    "node_id": task.node.id,
                    "device_id": task.node.device_id,
                    "action_name": task.node.action_name,
                    "device_action_key": key,
                    "estimated_s": round(estimated_s, 3),
                    "estimate_source": estimate_source,
                    "manual_confirm": manual_confirm,
                },
            )
            if not manual_confirm:
                self._emit_monitor(
                    "device",
                    "device_busy",
                    {
                        "device_id": task.node.device_id,
                        "action_name": task.node.action_name,
                        "device_action_key": key,
                        "job_id": job_id,
                        "workflow_id": task.workflow_id,
                    },
                )

        if ready:
            self._emit_monitor(
                "scheduler",
                "reschedule",
                {
                    "round": self._reschedule_count,
                    "ready": len(ready),
                    "dispatched": len(dispatched),
                },
            )
        return dispatched

    # 终态集合与云端 workflow_task 一致；TIMEOUT 当前由云端判定，列入以备
    # Edge 后续本地超时（词汇不再变更）。
    _TERMINAL_STATES = (
        WorkflowState.SUCCESS,
        WorkflowState.FAILED,
        WorkflowState.CANCELED,
        WorkflowState.TIMEOUT,
    )

    def _collect_terminal_notifications(self) -> list[tuple[str, str]]:
        """收集未处理过的终态工作流（须在锁内调用；通知/释放在锁外做）。"""
        pending: list[tuple[str, str]] = []
        for wid, run in self._workflows.items():
            if (
                run.state not in self._TERMINAL_STATES
                or wid in self._notified_workflows
            ):
                continue
            self._notified_workflows.add(wid)
            pending.append((wid, run.state.value))
            self._emit_monitor(
                "scheduler",
                "workflow_state",
                {"workflow_id": wid, "state": run.state.value},
            )
            self._safe_history("record_state", wid, run.state.value)
        return pending

    def _fire_notifications(self, notifications: list[tuple[str, str]]) -> None:
        for wid, state in notifications:
            if self._workflow_state_listener is None:
                continue
            try:
                self._workflow_state_listener(wid, state)
            except Exception:
                logger.exception("[EdgeScheduler] workflow state listener failed")

    # ── 物料/资源锁 ──────────────────────────────────────────

    def _held_resource_locks(self) -> set[str]:
        held: set[str] = set()
        for keys in self._job_resource_locks.values():
            held |= keys
        return held

    def _resource_lock_keys(self, node: Any, resolved_args: dict[str, Any]) -> set[str]:
        """节点的资源锁键集合只来自 explicit action lock_resource。"""
        keys: set[str] = set()
        if self._lock_resource_resolver is not None:
            try:
                param_names = (
                    self._lock_resource_resolver(node.device_id, node.action_name) or []
                )
            except Exception:
                logger.exception("[EdgeScheduler] lock_resource resolver failed")
                param_names = []
            for name in param_names:
                for rid in _extract_resource_ids(resolved_args.get(name)):
                    keys.add(f"res:{rid}")
        return keys

    def _busy_keys(self) -> set[str]:
        busy = set(self._external_busy_keys)
        if self._busy_key_provider is not None:
            try:
                busy |= set(self._busy_key_provider())
            except Exception:
                logger.exception("[EdgeScheduler] busy_key_provider failed")
        for job in self._inflight.values():
            busy.add(job.device_action_key)
        return busy

    # ── 泳道图时间线 ─────────────────────────────────────────

    def _record_timeline(
        self,
        job: DispatchedJob,
        success: bool,
        suc_type: str = "normal",
        state: str = "",
        ret_value: Any = None,
    ) -> None:
        """job 完结（成功/失败/取消）时记录时间线并喂历史统计（须在锁内调用）。"""
        ended_at = time.time()
        actual_s = max(0.0, ended_at - job.dispatched_at)
        if not state:
            state = "success" if success else "failed"
        # 只有正常成功的样本才进入历史统计（skip/失败/取消的时长不代表真实执行）
        if success and suc_type == "normal":
            self._estimator.observe(job.device_action_key, actual_s)
        entry = {
            "job_id": job.job_id,
            "workflow_id": job.workflow_id,
            "node_id": job.node_id,
            "device_id": job.device_id,
            "action_name": job.action_name,
            "device_action_key": job.device_action_key,
            "started_at": job.dispatched_at,
            "ended_at": ended_at,
            "actual_s": round(actual_s, 3),
            "estimated_s": round(job.estimated_s, 3),
            "estimate_source": job.estimate_source,
            "state": state,
            "suc_type": suc_type,
        }
        self._timeline.append(entry)
        # 历史库落盘（独立 SQLite；含截断后的返回值，供审计/回放）
        self._safe_history("record_job", entry, ret_value)
        self._emit_monitor(
            "action",
            "job_finished",
            {
                "job_id": job.job_id,
                "workflow_id": job.workflow_id,
                "node_id": job.node_id,
                "device_id": job.device_id,
                "action_name": job.action_name,
                "device_action_key": job.device_action_key,
                "state": state,
                "suc_type": suc_type,
                "actual_s": round(actual_s, 3),
                "estimated_s": round(job.estimated_s, 3),
            },
        )
        self._emit_monitor(
            "device",
            "device_idle",
            {
                "device_id": job.device_id,
                "action_name": job.action_name,
                "device_action_key": job.device_action_key,
                "job_id": job.job_id,
            },
        )

    def timeline(self, window_s: float = 3600.0) -> dict[str, Any]:
        """泳道图数据：执行中 job + 窗口内已完结 job + 预估器状态。

        泳道由前端按 device_id（或 device_action_key）分组；running 条目
        用 started_at + estimated_s 画预估终点，completed 条目画实际区间。
        """
        now = time.time()
        cutoff = now - max(window_s, 0.0)
        with self._lock:
            running = [
                {
                    "job_id": j.job_id,
                    "workflow_id": j.workflow_id,
                    "node_id": j.node_id,
                    "device_id": j.device_id,
                    "action_name": j.action_name,
                    "device_action_key": j.device_action_key,
                    "started_at": j.dispatched_at,
                    "elapsed_s": round(max(0.0, now - j.dispatched_at), 3),
                    "estimated_s": round(j.estimated_s, 3),
                    "estimate_source": j.estimate_source,
                }
                for j in self._inflight.values()
            ]
            completed = [e for e in self._timeline if e["ended_at"] >= cutoff]
            return {
                "now": now,
                "window_s": window_s,
                "running": running,
                "completed": completed,
                "estimator": {
                    "mode": self._estimator.mode,
                    "default_s": self._estimator.default_s,
                    "stats": self._estimator.stats(),
                },
            }

    def device_status(self) -> list[dict[str, Any]]:
        """设备占用视图（监控面板）：busy 来自 inflight，idle 来自时间线痕迹。"""
        now = time.time()
        with self._lock:
            devices: dict[str, dict[str, Any]] = {}
            # 时间线里出现过的设备默认 idle（带最近一次动作）
            for entry in self._timeline:
                dev = entry["device_id"] or entry["device_action_key"]
                cur = devices.get(dev)
                if cur is None or entry["ended_at"] > cur.get("last_seen", 0):
                    devices[dev] = {
                        "device_id": dev,
                        "status": "idle",
                        "last_action": entry["action_name"],
                        "last_state": entry["state"],
                        "last_seen": entry["ended_at"],
                    }
            # 在执行 job 的设备置 busy
            for j in self._inflight.values():
                dev = j.device_id or j.device_action_key
                devices[dev] = {
                    "device_id": dev,
                    "status": "busy",
                    "action_name": j.action_name,
                    "job_id": j.job_id,
                    "workflow_id": j.workflow_id,
                    "started_at": j.dispatched_at,
                    "elapsed_s": round(max(0.0, now - j.dispatched_at), 3),
                    "estimated_s": round(j.estimated_s, 3),
                    "estimate_source": j.estimate_source,
                    "last_seen": now,
                }
            return sorted(devices.values(), key=lambda d: d["device_id"])

    # ── 查询 ─────────────────────────────────────────────────

    def workflow_snapshot(self, workflow_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._workflows.get(workflow_id)
            if run is None:
                return None
            snap = run.snapshot()
            # 叠加在执行 job_id：前端对 manual_confirm 节点凭它调 /jobs/{id}/finish
            nodes = snap.get("nodes", {})
            for job_id, job in self._inflight.items():
                if job.workflow_id == workflow_id and job.node_id in nodes:
                    nodes[job.node_id]["job_id"] = job_id
            return snap

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "workflows": {
                    wid: run.snapshot() for wid, run in self._workflows.items()
                },
                "inflight_jobs": {
                    job_id: {
                        "workflow_id": j.workflow_id,
                        "node_id": j.node_id,
                        "device_action_key": j.device_action_key,
                        "resource_locks": sorted(
                            self._job_resource_locks.get(job_id, set())
                        ),
                        "started_at": j.dispatched_at,
                        "estimated_s": round(j.estimated_s, 3),
                        "estimate_source": j.estimate_source,
                    }
                    for job_id, j in self._inflight.items()
                },
                "reschedule_count": self._reschedule_count,
            }

    def cancel_workflow(self, workflow_id: str) -> bool:
        with self._lock:
            run = self._workflows.get(workflow_id)
            if run is None:
                return False
            run.cancel()
            removed = [
                job_id
                for job_id, j in self._inflight.items()
                if j.workflow_id == workflow_id
            ]
            for job_id in removed:
                job = self._inflight.pop(job_id, None)
                self._job_resource_locks.pop(job_id, None)
                if job is not None:
                    self._record_timeline(job, success=False, state="canceled")
            notifications = self._collect_terminal_notifications()
        self._fire_notifications(notifications)
        return True


__all__ = ["EdgeScheduler"]
