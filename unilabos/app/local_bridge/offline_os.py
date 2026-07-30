"""offline_os — 桥的离线执行核（进程内「仿真 OS」，无真实 OS 连入时的备档）。

真实路径里 task_dag 由**真实 OS 进程**的 ws_client._handle_task_dag→TaskDagRunner 跑，
桥只翻译。但本环境无 Go 后端、亦未必总能拉起真实 OS 进程；OfflineOS 在进程内顶替
OS 面：接桥下发的 F002 task_dag，用 F002 DagExecutor 走依赖偏序、每设备锁保 I3
（同设备串行、不重叠），逐节点回发 F002 job_status。UI 面因而无 OS 也能完整动，
且执行仍走 F002 真实 DagExecutor——不复制走图逻辑（单一事实源）。

接线：OfflineOS.receive 充当 ScheduleSession 的 send 协程（桥→OS 下行入口）；
bind(session) 后经 session.handle_incoming 把 job_status 回喂桥（OS→桥上行）。
节点自动完成（无真实硬件、无 time.sleep）——running 后让出一次事件循环再落终态，
使 running 态可观测；失败/取消由 results 编程或 cancel_task 触发。

契约见 docs/features/F003-local-workflow-bridge/interface-design.md §一（与 schedule_ws 同面）。
"""

from __future__ import annotations

import asyncio
import copy
import logging
import math
from typing import Any

from unilabos.app.local_bridge.schedule_ws import ScheduleSession
from unilabos.scheduler.dag_executor import DagExecutor
from unilabos.scheduler.dag_model import TERMINAL_STATES, DagNode, NodeState, TaskDag
from unilabos.scheduler.debug_controller import (
    DebugCommandError,
    DebugController,
)
from unilabos.scheduler.resource_lock import ResourceLockManager
from unilabos.scheduler.result_store import NodeExecutionResult
from unilabos.runtime.event_store import SQLiteEventJournal
from unilabos.runtime.reconcile import reconcile_unknown_fence

logger = logging.getLogger(__name__)

# NodeState → job_status.status 字面量（schedule_ws._STATUS_TO_NODE_STATE 的逆，供收敛兜底）
_STATE_TO_STATUS: dict[NodeState, str] = {
    NodeState.RUNNING: "running",
    NodeState.SUCCESS: "success",
    NodeState.FAILED: "failed",
    NodeState.CANCELLED: "cancelled",
    NodeState.SKIPPED: "skipped",
}


class OfflineOS:
    """进程内仿真 OS：接 F002 task_dag，用 DagExecutor 走图并回发 job_status。

    - receive(msg)：作为 ScheduleSession.send 注入——收桥下行 task_dag / cancel_task。
    - bind(session)：绑定回喂目标——经 session.handle_incoming 上行 job_status。
    - results：可编程终态（node_id→NodeState），缺省 SUCCESS；供演示失败路径。
    - model_device_lock：每 device_action_key 一把锁，非 always_free 节点串行（建模 I3）。
    """

    def __init__(
        self,
        results: dict[str, NodeState] | None = None,
        *,
        model_device_lock: bool = True,
        resource_lock_manager: ResourceLockManager | None = None,
        journal: SQLiteEventJournal | None = None,
        node_delay_seconds: float = 0.0,
        device_catalog: dict[str, Any] | None = None,
    ) -> None:
        if not math.isfinite(node_delay_seconds) or node_delay_seconds < 0:
            raise ValueError("node_delay_seconds must be a finite non-negative number")
        self.results = results or {}
        self.model_device_lock = model_device_lock
        self.node_delay_seconds = float(node_delay_seconds)
        self.device_catalog = copy.deepcopy(device_catalog)
        runtime_epoch = (
            resource_lock_manager.runtime_epoch
            if resource_lock_manager is not None
            else journal.runtime_epoch
            if journal is not None
            else "offline"
        )
        self._resource_lock_manager = resource_lock_manager or ResourceLockManager(
            runtime_epoch=runtime_epoch
        )
        self._journal = journal
        self._session: ScheduleSession | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._executors: dict[str, DagExecutor] = {}
        self._dags: dict[str, TaskDag] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._stop_reasons: dict[str, str] = {}
        self.received: list[dict[str, Any]] = []  # 观测量（供断言）
        # 每 device_action_key 的实时/峰值在跑数——供 I3 串行断言（==1 即从不重叠）
        self._key_running: dict[str, int] = {}
        self.max_concurrent_by_key: dict[str, int] = {}

    def bind(self, session: ScheduleSession) -> None:
        """绑定回喂目标 ScheduleSession（job_status 经其 handle_incoming 上行）。"""
        self._session = session

    async def receive(self, msg: dict[str, Any]) -> None:
        """ScheduleSession.send 注入点：分发桥→OS 下行报文（task_dag / cancel_task）。"""
        self.received.append(msg)
        action = msg.get("action", "")
        data = msg.get("data")
        data = data if isinstance(data, dict) else {}
        if action == "task_dag":
            await self._start_task(data)
        elif action == "cancel_task":
            self._cancel_task(data.get("task_id", ""))
        elif action == "reconcile_run":
            await self._reconcile_run(data)
        elif action == "debug_command":
            await self._debug_command(data)
        elif action == "query_device_catalog":
            await self._publish_device_catalog(
                request_id=str(data.get("request_id") or "")
            )
        else:
            logger.debug("[offline_os] 忽略下行 action=%s", action)

    async def _publish_device_catalog(self, *, request_id: str) -> None:
        if self._session is None or self.device_catalog is None:
            return
        snapshot = copy.deepcopy(self.device_catalog)
        if request_id:
            snapshot["request_id"] = request_id
        await self._session.handle_incoming(
            {"action": "device_catalog", "data": snapshot}
        )

    async def _reconcile_run(self, data: dict[str, Any]) -> None:
        result = await reconcile_unknown_fence(
            journal=self._journal,
            lock_manager=self._resource_lock_manager,
            run_id=str(data.get("run_id") or ""),
            lease_id=str(data.get("lease_id") or ""),
            resolution=str(data.get("resolution") or ""),
            actor=str(data.get("actor") or ""),
            reason=str(data.get("reason") or ""),
        )
        if self._session is None:
            return
        ack: dict[str, Any] = {
            "request_id": str(data.get("request_id") or ""),
            "run_id": str(data.get("run_id") or ""),
            "lease_id": str(data.get("lease_id") or ""),
            "status": result.status,
        }
        if result.code:
            ack["code"] = result.code
        if result.node_id:
            ack["node_id"] = result.node_id
        if result.terminal:
            ack["terminal"] = result.terminal
        await self._session.handle_incoming(
            {"action": "reconcile_ack", "data": ack}
        )

    async def _start_task(self, payload: dict[str, Any]) -> None:
        """解析 F002 task_dag，起后台协程用 DagExecutor 走图（不阻塞下发方）。"""
        dag = TaskDag.from_message(payload)
        controller = (
            DebugController(
                run_id=dag.task_id,
                node_ids=set(dag.nodes),
                config=dag.debug,
                on_event=lambda event_type, event_payload: asyncio.ensure_future(
                    self._emit_debug_event(
                        dag.task_id,
                        event_type,
                        event_payload,
                    )
                ),
            )
            if dag.debug
            else None
        )
        executor = DagExecutor(
            dag,
            self._make_submit(dag),
            resource_lock_manager=self._resource_lock_manager,
            journal=self._journal,
            debug_controller=controller,
        )
        self._dags[dag.task_id] = dag
        self._executors[dag.task_id] = executor
        self._cancel_events[dag.task_id] = asyncio.Event()
        self._tasks[dag.task_id] = asyncio.ensure_future(self._run(dag.task_id, executor))
        logger.info("[offline_os] 已受理 task_dag %s（%d 节点）", dag.task_id, len(dag.nodes))

    async def _debug_command(self, data: dict[str, Any]) -> None:
        run_id = str(data.get("run_id") or "")
        request_id = str(data.get("request_id") or "")
        executor = self._executors.get(run_id)
        ack: dict[str, Any] = {
            "request_id": request_id,
            "run_id": run_id,
        }
        if executor is None:
            ack.update({"status": "rejected", "code": "RUN_NOT_ACTIVE"})
        else:
            try:
                projection = await executor.debug_command(
                    str(data.get("command") or ""),
                    data.get("payload")
                    if isinstance(data.get("payload"), dict)
                    else {},
                )
            except (DebugCommandError, ValueError) as exc:
                ack.update({"status": "rejected", "code": str(exc)})
            else:
                command = str(data.get("command") or "").strip().lower()
                if command in {"terminate", "emergency_stop"}:
                    self._stop_reasons[run_id] = command
                    cancel_event = self._cancel_events.get(run_id)
                    if cancel_event is not None:
                        cancel_event.set()
                ack.update({"status": "accepted", "debug": projection})
        if self._session is not None:
            await self._session.handle_incoming(
                {"action": "debug_ack", "data": ack}
            )

    async def _emit_debug_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        if self._session is None:
            return
        await self._session.handle_incoming(
            {
                "action": "debug_event",
                "data": {
                    "run_id": run_id,
                    "type": event_type,
                    "payload": payload,
                },
            }
        )

    def _cancel_task(self, task_id: str) -> None:
        """cancel_task：停止对应 executor 调度后继（未决节点由收敛兜底落 cancelled）。"""
        executor = self._executors.get(task_id)
        if executor is not None:
            executor.cancel()
            self._stop_reasons[task_id] = "cancel_task"
            cancel_event = self._cancel_events.get(task_id)
            if cancel_event is not None:
                cancel_event.set()
            logger.info("[offline_os] 已取消 task_dag %s", task_id)

    async def _run(self, task_id: str, executor: DagExecutor) -> None:
        """走完整张图；结束后对未收到终态 job_status 的节点补发（收敛兜底）。"""
        try:
            snapshot = await executor.run()
        finally:
            self._executors.pop(task_id, None)
            self._tasks.pop(task_id, None)
            self._cancel_events.pop(task_id, None)
        # fail-fast/取消使部分节点未经 submit 即落终态（无 job_status），补发以令桥收敛
        dag = self._dags.get(task_id)
        if dag is None:
            self._stop_reasons.pop(task_id, None)
            return
        for node_id, state in snapshot.items():
            if self._bridge_terminal(task_id, node_id):
                continue
            status = _STATE_TO_STATUS.get(state)
            if status:
                try:
                    await self._emit(dag, dag.nodes[node_id], status)
                except Exception:  # noqa: BLE001 —— 兜底补发失败不得让后台任务异常未被回收
                    logger.exception("[offline_os] 补发 job_status 失败（node=%s）", node_id)
        self._stop_reasons.pop(task_id, None)

    def _bridge_terminal(self, task_id: str, node_id: str) -> bool:
        """桥侧该节点是否已达终态（避免对已收到终态的节点重复补发）。"""
        if self._session is None:
            return True
        state = self._session.node_state(task_id, node_id)
        return state in TERMINAL_STATES

    def _make_submit(self, dag: TaskDag):
        """构 DagExecutor 注入的 submit：每设备锁串行 + 回发 running/终态 job_status。"""

        async def submit(node: DagNode) -> NodeState | NodeExecutionResult:
            key = node.device_action_key
            lock: asyncio.Lock | None = None
            if self.model_device_lock and not node.always_free:
                lock = self._locks.setdefault(key, asyncio.Lock())
                await lock.acquire()
            # 进入运行——记录每 key 并发峰值（同 key 非 free 有锁则恒为 1，即 I3）
            self._key_running[key] = self._key_running.get(key, 0) + 1
            self.max_concurrent_by_key[key] = max(
                self.max_concurrent_by_key.get(key, 0), self._key_running[key]
            )
            try:
                await self._emit(dag, node, "running")
                cancel_event = self._cancel_events[dag.task_id]
                if self.node_delay_seconds > 0:
                    try:
                        await asyncio.wait_for(
                            cancel_event.wait(),
                            timeout=self.node_delay_seconds,
                        )
                    except TimeoutError:
                        pass
                else:
                    await asyncio.sleep(0)  # 让出一次，使 running 态可观测（非 time.sleep）
                if cancel_event.is_set():
                    await self._emit(dag, node, "cancelled")
                    return NodeExecutionResult(
                        state=NodeState.CANCELLED,
                        terminal_info={
                            "physical_state": "confirmed",
                            "reconcile_required": False,
                            "stop_reason": self._stop_reasons.get(
                                dag.task_id,
                                "cancel_task",
                            ),
                        },
                    )
                state = self.results.get(node.node_id, NodeState.SUCCESS)
                await self._emit(dag, node, _STATE_TO_STATUS[state])
                return state
            finally:
                self._key_running[key] -= 1
                if lock is not None:
                    lock.release()

        return submit

    async def _emit(self, dag: TaskDag, node: DagNode, status: str) -> None:
        """回发一条 F002 job_status 给桥（node_id==job_id）。"""
        if self._session is None:
            return
        await self._session.handle_incoming(
            {
                "action": "job_status",
                "data": {
                    "job_id": node.node_id,
                    "task_id": dag.task_id,
                    "device_id": node.device_id,
                    "notebook_id": dag.notebook_id,
                    "action_name": node.action,
                    "status": status,
                    "feedback_data": {},
                    "return_info": None,
                    "timestamp": 0.0,
                },
            }
        )
