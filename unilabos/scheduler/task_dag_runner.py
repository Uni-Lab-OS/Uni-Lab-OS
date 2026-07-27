"""把整张 task_dag 接到现有 per-node job_start 执行栈的桥接层。

生产的执行栈是**回调/队列驱动**：ws_client._handle_job_start 构造 JobInfo →
DeviceActionManager.enqueue_job（每设备锁/幂等/串行 I3）→ HostNode.send_goal；
完成经另一线程的 publish_job_status 终态回调回流。而 DagExecutor 需要的是
``submit(node) -> awaitable(NodeState)``。TaskDagRunner 做这层适配：

- submit(node)：在事件循环上建一个 future 登记进 pending，调用注入的
  ``on_start_node``（真正的入队 + 视情况 send_goal 副作用），await 该 future。
- notify_terminal(job_id, status)：由 publish_job_status 在终态时**跨线程**回调，
  经 loop.call_soon_threadsafe 解析对应 future 为 NodeState（node_id 即 job_id）。
- cancel()：停止 DagExecutor 调度后继，并把未决 future 一律解析为 CANCELLED，
  避免被取消的设备任务永不回终态而使 run() 悬挂。

DagExecutor 通过注入的共享 ResourceLockManager 负责统一业务资源 admission；
DeviceActionManager 只保留兼容队列、幂等和通信安全串行。本层不复制锁逻辑。见
docs/features/F002-os-local-dag-executor/interface-design.md §三。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Optional

from unilabos.scheduler.dag_executor import DagExecutor, DagWalk, OnTerminalFn
from unilabos.scheduler.dag_model import DagNode, NodeState, TaskDag
from unilabos.scheduler.resource_lock import ResourceLockManager
from unilabos.scheduler.result_store import NodeExecutionResult, ResultEnvelope
from unilabos.scheduler.debug_controller import DebugController
from unilabos.runtime.event_store import SQLiteEventJournal

logger = logging.getLogger(__name__)

# 入队 + 视情况 send_goal 的副作用（ws 侧提供，复用 _handle_job_start 路径）。
StartNodeFn = Callable[[DagNode], None]
# 走图终止（失败/取消）后清理仍在设备侧运行的本 task 任务（ws 侧提供，
# 复用 DeviceActionManager.cancel_jobs_by_task_id）。
CancelRemainingFn = Callable[[], None]


def _status_to_state(status: str) -> NodeState:
    """把 ws 的 job_status 字符串映射为完整 NodeState。"""

    normalized = status.strip().lower()
    aliases = {
        "succeeded": NodeState.SUCCESS,
        "success": NodeState.SUCCESS,
        "failed": NodeState.FAILED,
        "cancelled": NodeState.CANCELLED,
        "canceled": NodeState.CANCELLED,
        "skipped": NodeState.SKIPPED,
    }
    return aliases.get(normalized, NodeState.FAILED)


class TaskDagRunner:
    """单张 task_dag 的驱动器：桥接 DagExecutor 与回调式 per-node 执行栈。"""

    def __init__(
        self,
        dag: TaskDag,
        on_start_node: StartNodeFn,
        *,
        on_node_terminal: Optional[OnTerminalFn] = None,
        on_cancel_remaining: Optional[CancelRemainingFn] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        walk: Optional[DagWalk] = None,
        resource_lock_manager: Optional[ResourceLockManager] = None,
        journal: Optional[SQLiteEventJournal] = None,
        debug_controller: Optional[DebugController] = None,
    ) -> None:
        self.dag = dag
        self._on_start_node = on_start_node
        self._on_cancel_remaining = on_cancel_remaining
        self._loop = loop
        self._pending: dict[str, asyncio.Future] = {}  # node_id(=job_id) -> future
        self._cancelled = False
        self._cancel_remaining_called = False
        self._executor = DagExecutor(
            dag,
            self._submit,
            on_node_terminal=on_node_terminal,
            walk=walk,
            resource_lock_manager=resource_lock_manager,
            journal=journal,
            debug_controller=debug_controller,
        )

    async def run(self) -> dict[str, NodeState]:
        """走完整张 DAG，返回每节点终态。失败/取消后清理设备侧残余任务。"""
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        try:
            result = await self._executor.run()
        finally:
            # 未决 future 兜底解析，避免异常路径下的悬挂
            self._resolve_all_pending(NodeState.CANCELLED)
        # 任一节点非 SUCCESS -> fail-fast，清理仍在设备侧运行/排队的本 task 任务
        if self._on_cancel_remaining is not None and any(
            st in {NodeState.FAILED, NodeState.CANCELLED}
            for st in result.values()
        ):
            self._cancel_remaining_once()
        return result

    async def _submit(self, node: DagNode) -> NodeState | NodeExecutionResult:
        """DagExecutor 注入点：登记 future -> 触发入队/起跑 -> 等终态。"""
        loop = self._loop or asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        # 先登记再触发副作用：即便终态瞬间回流（跨线程 call_soon_threadsafe），
        # 也只会在本协程让出后执行 _resolve，pending 已就位，无竞态。
        self._pending[node.node_id] = fut
        if self._cancelled:
            self._pending.pop(node.node_id, None)
            return NodeState.CANCELLED
        try:
            self._on_start_node(node)
        except Exception as exc:  # noqa: BLE001 —— 起跑异常后物理状态不确定
            logger.exception("TaskDagRunner on_start_node 失败，节点 %s 置 FAILED", node.node_id)
            self._pending.pop(node.node_id, None)
            return NodeExecutionResult(
                state=NodeState.FAILED,
                terminal_info={
                    "error": str(exc) or exc.__class__.__name__,
                    "physical_state": "unknown",
                    "reconcile_required": True,
                },
            )
        return await fut

    def notify_terminal(
        self,
        job_id: str,
        status: str | NodeState,
        *,
        return_info: dict[str, Any] | None = None,
    ) -> None:
        """由 publish_job_status 终态时**跨线程**回调，解析对应节点 future。"""
        state = status if isinstance(status, NodeState) else _status_to_state(status)
        terminal_info = dict(return_info or {})
        result: NodeState | NodeExecutionResult = state
        if state == NodeState.SUCCESS:
            raw_outputs = terminal_info.get("return_value", {})
            if isinstance(raw_outputs, dict):
                outputs = raw_outputs
            else:
                outputs = {"result": raw_outputs}
            result = NodeExecutionResult(
                state=state,
                envelope=ResultEnvelope(outputs=outputs),
                terminal_info=terminal_info,
            )
        elif terminal_info:
            result = NodeExecutionResult(
                state=state,
                terminal_info=terminal_info,
            )
        loop = self._loop
        if loop is None:
            # run() 尚未起跑：直接同线程解析
            self._resolve(job_id, result)
            return
        loop.call_soon_threadsafe(self._resolve, job_id, result)

    def cancel(self) -> None:
        """外部取消（cancel_task）：停止调度后继，未决节点解析为 CANCELLED。

        设备侧仍在运行的任务由 ws 层 cancel_jobs_by_task_id 取消（此处不重复）。
        """
        self._cancelled = True
        self._executor.cancel()
        loop = self._loop
        if loop is None:
            self._resolve_all_pending(
                NodeExecutionResult(
                    state=NodeState.CANCELLED,
                    terminal_info={
                        "error": "cancel requested before device confirmation",
                        "physical_state": "unknown",
                        "reconcile_required": True,
                    },
                )
            )
        else:
            loop.call_soon_threadsafe(
                self._resolve_all_pending,
                NodeExecutionResult(
                    state=NodeState.CANCELLED,
                    terminal_info={
                        "error": "cancel requested before device confirmation",
                        "physical_state": "unknown",
                        "reconcile_required": True,
                    },
                ),
            )

    async def debug_command(
        self,
        command: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = command.strip().lower()
        projection = await self._executor.debug_command(command, payload)
        if normalized in {"terminate", "emergency_stop"}:
            # DagExecutor.cancel() stops new admissions, while this runner owns
            # the callback-backed futures for already-dispatched device work.
            # Resolve them here so the run cannot hang waiting for a callback
            # that cancellation intentionally suppresses.
            if normalized == "emergency_stop":
                # Emergency stop is run-scoped in debugger v1.  Trigger the
                # injected device cleanup immediately; it is idempotently
                # guarded because run() performs the same safety cleanup.
                self._cancel_remaining_once()
            self.cancel()
        return projection

    def debug_projection(self) -> dict[str, Any]:
        return self._executor.debug_projection()

    def _resolve(self, job_id: str, state: NodeState | NodeExecutionResult) -> None:
        fut = self._pending.pop(job_id, None)
        if fut is None or fut.done():
            return
        fut.set_result(state)

    def _resolve_all_pending(
        self,
        state: NodeState | NodeExecutionResult,
    ) -> None:
        for job_id in list(self._pending):
            self._resolve(job_id, state)

    def _cancel_remaining_once(self) -> None:
        if self._on_cancel_remaining is None or self._cancel_remaining_called:
            return
        self._cancel_remaining_called = True
        try:
            self._on_cancel_remaining()
        except Exception:  # noqa: BLE001 —— 清理失败不应改变已定终态
            # Emergency stop calls this eagerly.  Leave the cleanup retryable
            # so run() can make one more attempt during terminal convergence.
            self._cancel_remaining_called = False
            logger.exception("TaskDagRunner on_cancel_remaining 清理失败，忽略")
