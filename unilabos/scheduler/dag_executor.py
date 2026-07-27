"""OS 本地 DAG 执行器 — 走图核心。

分两层，职责严格解耦：

1. ``DagWalk``：**纯同步状态机**，只管依赖偏序（I1/I2/I5/I6）。无 I/O、无锁、
   无时钟、无 asyncio。调度是数学 —— 这一层是 Hypothesis 不变量测试的靶子。
2. ``DagExecutor``：**异步驱动**，把纯状态机接到「注入的节点调度器」
   ``submit(node) -> awaitable(NodeState)`` 上。启用 Layer-A 时，执行器先通过统一的
   ``ResourceLockManager`` admission，再派发节点；拿不到 live lease 的节点保持 READY。
   ``DeviceActionManager`` 只保留兼容派发与通信安全，不再承担业务资源锁。

见 docs/features/F002-os-local-dag-executor/interface-design.md §二。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Optional

from unilabos.scheduler.dag_model import (
    EdgeState,
    NodeState,
    TaskDag,
    TERMINAL_STATES,
)
from unilabos.scheduler.result_store import (
    NodeExecutionResult,
    ResultEnvelope,
    materialize_node_inputs,
    validate_result_outputs,
)
from unilabos.scheduler.ready_policy import DeterministicReadyPolicy
from unilabos.scheduler.debug_controller import DebugController
from unilabos.scheduler.python_fallback import (
    python_fallback_lease_request,
    validate_python_fallback_dag,
)
from unilabos.scheduler.resource_lock import (
    LeaseRequest,
    ResolvedResourceClaim,
    ResourceLease,
    ResourceLockManager,
)
from unilabos.runtime.event_store import SQLiteEventJournal
from unilabos.workflow.bindings import BindingPreflightError

logger = logging.getLogger(__name__)

# 注入的节点调度器：给一个节点，返回一个 awaitable，解析为该节点终态。
SubmitFn = Callable[["object"], Awaitable[NodeState | NodeExecutionResult]]
# 节点终态回调（用于游标持久化 / 日志），(node_id, 终态)。
OnTerminalFn = Callable[[str, NodeState], None]

# 非终态集合：可被 fail-fast 取消的状态
_ACTIVE_STATES = frozenset(
    {NodeState.PENDING, NodeState.READY, NodeState.RUNNING}
)


class DagWalk:
    """纯同步走图状态机：依赖偏序的唯一真相。

    - ready()：indeg==0 且仍 PENDING 的节点集（供驱动层提交）。
    - mark_running(id)：PENDING/READY -> RUNNING（每节点恰好一次，I1）。
    - on_success(id)：-> SUCCESS，并对每条 out-edge 递减后继 indeg（I2）。
    - on_failed(id)：-> FAILED 且 fail-fast：其余非终态 -> CANCELLED。
    - resume：以 completed 初始化，已完成节点视作 SUCCESS 并预满足后继依赖（I4）。
    """

    def __init__(
        self,
        dag: TaskDag,
        *,
        completed: Iterable[str] = (),
        start_node_id: str | None = None,
    ) -> None:
        self.dag = dag
        self.indeg: dict[str, int] = dag.build_indegree()
        self.adj: dict[str, list[str]] = dag.adjacency()
        self.states: dict[str, NodeState] = {
            nid: NodeState.PENDING for nid in dag.nodes
        }
        self.failed = False
        self.dispatched_nodes: set[str] = set()
        self.edge_states: list[EdgeState] = [EdgeState.PENDING for _ in dag.edges]
        self.incoming: dict[str, list[int]] = {nid: [] for nid in dag.nodes}
        self.outgoing: dict[str, list[int]] = {nid: [] for nid in dag.nodes}
        for index, edge in enumerate(dag.edges):
            self.incoming[edge.target_node_uuid].append(index)
            self.outgoing[edge.source_node_uuid].append(index)
        self.start_node_id = start_node_id or None
        self.initial_skipped: tuple[str, ...] = ()
        if self.start_node_id is not None:
            if self.start_node_id not in dag.nodes:
                raise ValueError(f"UNKNOWN_START_NODE: {self.start_node_id}")
            reachable: set[str] = set()
            pending = [self.start_node_id]
            while pending:
                current = pending.pop()
                if current in reachable:
                    continue
                reachable.add(current)
                pending.extend(self.adj[current])
            skipped = set(dag.nodes) - reachable
            self.initial_skipped = tuple(
                node_id for node_id in dag.nodes if node_id in skipped
            )
            for node_id in self.initial_skipped:
                self.states[node_id] = NodeState.SKIPPED
            # The selected start is a new execution boundary. Incoming edges
            # from outside the reachable subgraph must not suppress it.
            self.indeg = {node_id: 0 for node_id in dag.nodes}
            for edge_index, edge in enumerate(dag.edges):
                if (
                    edge.source_node_uuid in reachable
                    and edge.target_node_uuid in reachable
                ):
                    self.indeg[edge.target_node_uuid] += 1
                else:
                    self.edge_states[edge_index] = EdgeState.SKIPPED
        # resume：把已完成节点直接置 SUCCESS 并释放其对后继的约束
        for nid in completed:
            if nid in self.states and self.states[nid] == NodeState.PENDING:
                self._apply_success(nid)

    def ready(self) -> list[str]:
        """当前可提交的节点：仍 PENDING 且入度归零。顺序稳定（按插入序）。"""
        if self.failed:
            return []
        return [
            nid
            for nid in self.dag.nodes
            if self.states[nid] in {NodeState.PENDING, NodeState.READY}
            and self.indeg[nid] == 0
        ]

    def mark_running(self, node_id: str) -> None:
        st = self.states[node_id]
        if st not in {NodeState.PENDING, NodeState.READY}:
            raise RuntimeError(f"节点 {node_id} 状态为 {st}，不可重复提交（违反 I1）")
        self.states[node_id] = NodeState.RUNNING
        self.dispatched_nodes.add(node_id)

    def on_success(self, node_id: str) -> None:
        if self.states[node_id] != NodeState.RUNNING:
            raise RuntimeError(f"节点 {node_id} 未在运行，不能置成功")
        self._apply_success(node_id)

    def _apply_success(self, node_id: str) -> None:
        self.states[node_id] = NodeState.SUCCESS
        for edge_index in self.outgoing[node_id]:
            self._resolve_edge(edge_index, EdgeState.TAKEN)

    def on_branch(self, node_id: str, *, selected: str) -> None:
        """Complete a branch and resolve exactly its selected outgoing path."""

        if self.states[node_id] != NodeState.RUNNING:
            raise RuntimeError(f"节点 {node_id} 未在运行，不能选择分支")
        self.states[node_id] = NodeState.SUCCESS
        matched = False
        for edge_index in self.outgoing[node_id]:
            edge = self.dag.edges[edge_index]
            if edge.branch == selected:
                matched = True
                self._resolve_edge(edge_index, EdgeState.TAKEN)
            else:
                self._resolve_edge(edge_index, EdgeState.SKIPPED)
        if not matched:
            raise ValueError(f"branch {node_id!r} has no outgoing selection {selected!r}")

    def skip_outgoing(self, node_id: str) -> None:
        """Skip a node and recursively collapse successors with no active input."""

        if self.states[node_id] not in {NodeState.PENDING, NodeState.READY}:
            raise RuntimeError(f"节点 {node_id} 状态为 {self.states[node_id]}，不能跳过")
        self.states[node_id] = NodeState.SKIPPED
        for edge_index in self.outgoing[node_id]:
            self._resolve_edge(edge_index, EdgeState.SKIPPED)

    def _resolve_edge(self, edge_index: int, state: EdgeState) -> None:
        if self.edge_states[edge_index] != EdgeState.PENDING:
            return
        self.edge_states[edge_index] = state
        target = self.dag.edges[edge_index].target_node_uuid
        self.indeg[target] -= 1
        if self.indeg[target] != 0 or self.states[target] != NodeState.PENDING:
            return
        incoming_states = [self.edge_states[index] for index in self.incoming[target]]
        activating_states = [
            self.edge_states[index]
            for index in self.incoming[target]
            if self.dag.edges[index].activates
        ]
        should_skip = (
            bool(activating_states)
            and all(item == EdgeState.SKIPPED for item in activating_states)
        ) or (
            not activating_states
            and bool(incoming_states)
            and all(item == EdgeState.SKIPPED for item in incoming_states)
        )
        if should_skip:
            self.states[target] = NodeState.SKIPPED
            for child_edge_index in self.outgoing[target]:
                self._resolve_edge(child_edge_index, EdgeState.SKIPPED)

    def on_failed(self, node_id: str) -> None:
        self.states[node_id] = NodeState.FAILED
        self.failed = True
        # fail-fast：对齐 backend gctx 兄弟组取消语义，其余非终态一律 CANCELLED
        for nid, st in self.states.items():
            if nid != node_id and st in _ACTIVE_STATES:
                self.states[nid] = NodeState.CANCELLED

    def cancel_remaining(self) -> None:
        """外部取消（cancel_task）：把所有仍非终态的节点一律标记为 CANCELLED。

        与 on_failed 不同：不置 failed（非失败终止），仅停止后续并把未决节点收敛到
        终态，避免 run() 返回含 PENDING/RUNNING 的非终态快照。
        """
        for nid, st in self.states.items():
            if st in _ACTIVE_STATES:
                self.states[nid] = NodeState.CANCELLED

    def is_done(self) -> bool:
        return all(st in TERMINAL_STATES for st in self.states.values())

    def running_nodes(self) -> list[str]:
        return [nid for nid, st in self.states.items() if st == NodeState.RUNNING]

    def snapshot(self) -> dict[str, NodeState]:
        return dict(self.states)


class DagExecutor:
    """异步驱动：把 ready 集提交给注入的调度器，并发走完整张图。

    submit：async callable(DagNode) -> NodeState（SUCCESS/FAILED）。生产实现 =
        复用 ws_client _handle_job_start 的 send_goal 路径（经 DeviceActionManager）；
        测试实现 = fake 调度器（可控时钟 + 可编程结果）。
    on_node_terminal：每个节点到终态时回调，用于 T03 游标持久化 / 日志。
    walk：可注入既有 DagWalk（用于 resume），缺省新建。
    """

    def __init__(
        self,
        dag: TaskDag,
        submit: SubmitFn,
        *,
        on_node_terminal: Optional[OnTerminalFn] = None,
        walk: Optional[DagWalk] = None,
        resource_lock_manager: Optional[ResourceLockManager] = None,
        journal: Optional[SQLiteEventJournal] = None,
        runtime_parameters: Optional[dict[str, Any]] = None,
        debug_controller: Optional[DebugController] = None,
    ) -> None:
        self.dag = dag
        self._submit = submit
        self._on_terminal = on_node_terminal
        self.walk = (
            walk
            if walk is not None
            else DagWalk(
                dag,
                start_node_id=str(dag.debug.get("start_node_id") or "") or None,
            )
        )
        self._lock_manager = resource_lock_manager
        self._ready_policy = (
            DeterministicReadyPolicy(lock_manager=resource_lock_manager)
            if resource_lock_manager is not None
            else None
        )
        if resource_lock_manager is not None:
            validate_python_fallback_dag(
                dag,
                lock_manager=resource_lock_manager,
            )
        self._journal = journal
        self._runtime_parameters = dict(
            dag.runtime_parameters
            if runtime_parameters is None
            else runtime_parameters
        )
        self._cancelled = False
        self.results: dict[str, ResultEnvelope] = {}
        self.errors: dict[str, BindingPreflightError] = {}
        self._leases: dict[str, ResourceLease] = {}
        self._terminal_payloads: dict[str, dict[str, Any]] = {}
        self._ready_sequence = 0
        self._debug_controller = debug_controller

    def cancel(self) -> None:
        """外部取消（对应 cancel_task）：停止调度后继，已在跑节点由上层取消。"""
        self._cancelled = True
        if self._debug_controller is not None:
            self._debug_controller.cancel_wait()
        if self._lock_manager is not None:
            self._lock_manager.notify_waiters()

    async def debug_command(
        self,
        command: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._debug_controller is None:
            raise ValueError("DEBUG_NOT_ENABLED")
        projection = await self._debug_controller.command(command, payload)
        if command.strip().lower() in {"terminate", "emergency_stop"}:
            self.cancel()
        return projection

    def debug_projection(self) -> dict[str, Any]:
        if self._debug_controller is None:
            return {"enabled": False, "status": "disabled"}
        return self._debug_controller.projection()

    async def run(self) -> dict[str, NodeState]:
        """走完整张图，返回每节点终态快照。无环则有限步终止（I6）。"""
        inflight: dict[str, asyncio.Task] = {}
        try:
            for node_id in self.walk.initial_skipped:
                await self._finalize_node(node_id, NodeState.SKIPPED)
                self._notify_terminal(node_id, NodeState.SKIPPED)
            while not self.walk.is_done() and not self._cancelled:
                # 1) Layer-A admission 后提交本轮可运行节点。未获 lease 的节点保持 READY。
                ready_ids = self.walk.ready()
                if self._debug_controller is not None:
                    ready_ids = await self._debug_controller.select_ready(
                        ready_ids,
                        has_inflight=bool(inflight),
                    )
                admitted = await self._admit_ready(ready_ids)
                if self._debug_controller is not None:
                    self._debug_controller.on_admitted(admitted)
                for nid in admitted:
                    self.walk.mark_running(nid)
                    inflight[nid] = asyncio.ensure_future(self._run_node(nid))

                if not inflight:
                    if self._lock_manager is not None and self.walk.ready():
                        # 资源被其他 run/unknown lease 占用；等待释放或外部取消，禁止忙等。
                        await self._lock_manager.wait_for_change()
                        continue
                    # 无 ready 且无在跑：无环时意味着已完成；否则解析期已拒环。
                    break

                # 2) 等任一节点完成
                done, _ = await asyncio.wait(
                    inflight.values(), return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    nid, status = task.result()
                    inflight.pop(nid, None)
                    if status == NodeState.SUCCESS:
                        node = self.dag.nodes[nid]
                        if node.node_type == "branch":
                            selection = self.results.get(nid, ResultEnvelope()).outputs.get(
                                "branch"
                            )
                            before = self.walk.snapshot()
                            try:
                                self.walk.on_branch(nid, selected=str(selection))
                            except (TypeError, ValueError) as exc:
                                status = NodeState.FAILED
                                self._terminal_payloads[nid] = {
                                    "error": str(exc),
                                    "physical_state": "confirmed",
                                }
                                self.walk.on_failed(nid)
                            else:
                                for skipped_id, skipped_state in self.walk.states.items():
                                    if (
                                        skipped_state == NodeState.SKIPPED
                                        and before[skipped_id] != NodeState.SKIPPED
                                    ):
                                        await self._finalize_node(
                                            skipped_id, NodeState.SKIPPED
                                        )
                                        self._notify_terminal(
                                            skipped_id, NodeState.SKIPPED
                                        )
                        else:
                            self.walk.on_success(nid)
                    elif status == NodeState.CANCELLED:
                        # 外部取消回流：不触发 fail-fast，直接落 CANCELLED 终态
                        self.walk.states[nid] = NodeState.CANCELLED
                    else:
                        self.walk.on_failed(nid)
                    await self._finalize_node(nid, status)
                    if self._debug_controller is not None:
                        self._debug_controller.on_terminal(nid)
                    if status == NodeState.SUCCESS:
                        await self._release_resource_holds(nid)
                    self._notify_terminal(nid, status)
                    # fail-fast：某节点**失败**即取消其余在跑并停止调度（取消不算失败）
                    if status == NodeState.FAILED:
                        await self._cancel_inflight(inflight)
                        await self._run_failure_cleanups(nid)
                        snapshot = self.walk.snapshot()
                        self._record_run_terminal(snapshot)
                        if self._debug_controller is not None:
                            self._debug_controller.on_run_terminal("failed")
                        return snapshot
        finally:
            if inflight:
                await self._cancel_inflight(inflight)
        # 外部取消：把剩余未决节点收敛为 CANCELLED，返回全终态快照
        if self._cancelled:
            self.walk.cancel_remaining()
        snapshot = self.walk.snapshot()
        self._record_run_terminal(snapshot)
        if self._debug_controller is not None:
            terminal = (
                "cancelled"
                if NodeState.CANCELLED in snapshot.values()
                else "failed"
                if NodeState.FAILED in snapshot.values()
                else "completed"
            )
            self._debug_controller.on_run_terminal(terminal)
        return snapshot

    async def _run_failure_cleanups(self, failed_node_id: str) -> None:
        """Run the failed scope's declared cleanup path before Run failure.

        ``DagWalk.on_failed`` intentionally cancels every ordinary successor.
        Cleanup nodes are compiler-authorized exceptions: they are selected by
        an explicit protected-node list, executed in canonical order, and
        journaled through the same node path. Their failure never replaces the
        primary failed node.
        """

        cleanup_nodes = sorted(
            (
                node
                for node in self.dag.nodes.values()
                if node.node_type == "cleanup"
                and failed_node_id in node.cleanup_for
                and self.walk.states[node.node_id] != NodeState.SUCCESS
            ),
            key=lambda node: node.canonical_index,
        )
        for node in cleanup_nodes:
            if self._lock_manager is not None:
                node.lease_request = self._lease_request(
                    node,
                    run_id=self.dag.task_id,
                )
                if node.lease_request.claims:
                    admitted = await self._ready_policy.admit([node])
                    if not admitted:
                        self.walk.states[node.node_id] = NodeState.FAILED
                        self._terminal_payloads[node.node_id] = {
                            "error": "safety cleanup resource lease was not acquired",
                            "primary_failure": failed_node_id,
                            "physical_state": "unknown",
                        }
                        await self._finalize_node(node.node_id, NodeState.FAILED)
                        self._notify_terminal(node.node_id, NodeState.FAILED)
                        continue
                    self._leases[node.node_id] = admitted[0].lease
            self.walk.states[node.node_id] = NodeState.RUNNING
            self.walk.dispatched_nodes.add(node.node_id)
            cleanup_id, cleanup_status = await self._run_node(node.node_id)
            self.walk.states[cleanup_id] = cleanup_status
            await self._finalize_node(cleanup_id, cleanup_status)
            if cleanup_status == NodeState.SUCCESS:
                await self._release_resource_holds(cleanup_id)
            self._notify_terminal(cleanup_id, cleanup_status)

    def _record_run_terminal(self, snapshot: dict[str, NodeState]) -> None:
        """Append the logical run terminal exactly once from the executor.

        Runtime/transport projections may mirror status, but they must never
        manufacture a second terminal event.  An unknown physical fence can
        remain open after the logical run is failed/cancelled and is reconciled
        independently.
        """

        if self._journal is None or not snapshot:
            return
        states = tuple(snapshot.values())
        if not all(state in TERMINAL_STATES for state in states):
            return
        if NodeState.FAILED in states:
            terminal = "failed"
        elif NodeState.CANCELLED in states:
            terminal = "cancelled"
        else:
            terminal = "completed"
        self._journal.record_run_terminal(
            run_id=self.dag.task_id,
            terminal=terminal,
        )

    async def _release_resource_holds(self, release_node_id: str) -> None:
        """Release only compiler-authorized claims after the handoff succeeds."""

        if self._lock_manager is None:
            return
        release_node = self.dag.nodes[release_node_id]
        for directive in release_node.resource_releases:
            acquire_node_id = str(directive.get("acquire_node_id") or "")
            resource_ref = str(directive.get("resource_ref") or "")
            scope = str(directive.get("scope") or "")
            lease = self._leases.get(acquire_node_id)
            if lease is None:
                continue
            current = self._lock_manager.get_lease(lease.lease_id)
            if current.state != "active":
                continue
            released = await self._lock_manager.release(
                lease.lease_id,
                scope=scope,
                resource_id=resource_ref,
            )
            if released and self._journal is not None:
                self._journal.record_lock_released(
                    run_id=self.dag.task_id,
                    node_id=acquire_node_id,
                    lease_id=lease.lease_id,
                    released_scope=scope,
                    released_resource_id=resource_ref,
                )

    async def _admit_ready(self, ready_ids: list[str]) -> list[str]:
        if self._ready_policy is None:
            return ready_ids

        ready_nodes = []
        internal_node_ids: list[str] = []
        for node_id in ready_ids:
            node = self.dag.nodes[node_id]
            if self.walk.states[node_id] == NodeState.PENDING:
                self._ready_sequence += 1
                node.ready_since_seq = self._ready_sequence
                node.admission_state = "ready"
                self.walk.states[node_id] = NodeState.READY
            if (
                node.device_id == "os_control"
                and node.node_type in {"branch", "join", "group"}
                and node.action == node.node_type
            ):
                # Structured control nodes are evaluated by the OS kernel. They
                # neither address a device nor compete for a physical lease.
                node.admission_state = "admitted"
                internal_node_ids.append(node_id)
                continue
            if not self._resource_dependencies_active(node):
                node.admission_state = "waiting_for_hold"
                continue
            node.lease_request = self._lease_request(
                node,
                run_id=self.dag.task_id,
            )
            if not node.lease_request.claims:
                node.admission_state = "admitted"
                internal_node_ids.append(node_id)
                continue
            if self._journal is not None and node_id not in self._leases:
                existing_events = {
                    event.type
                    for event in self._journal.list_events(self.dag.task_id)
                    if event.node_id == node_id
                }
                if "lock_requested" not in existing_events:
                    self._journal.record_lock_requested(
                        run_id=self.dag.task_id,
                        node_id=node_id,
                        holder_id=node.lease_request.holder_id,
                        claims=self._serialize_claims(node.lease_request.claims),
                    )
            ready_nodes.append(node)

        admitted = await self._ready_policy.admit(ready_nodes)
        for item in admitted:
            self._leases[item.node.node_id] = item.lease
            if self._journal is not None:
                self._journal.record_lock_acquired(
                    run_id=self.dag.task_id,
                    node_id=item.node.node_id,
                    lease_id=item.lease.lease_id,
                    holder_id=item.lease.holder_id,
                    claims=self._serialize_claims(item.lease.claims),
                )
        admitted_ids = {
            *internal_node_ids,
            *(item.node.node_id for item in admitted),
        }
        return [node_id for node_id in ready_ids if node_id in admitted_ids]

    def _resource_dependencies_active(self, node: Any) -> bool:
        """Fail closed unless every compiler-declared retained hold is live."""

        if not node.resource_dependencies:
            return True
        if self._lock_manager is None:
            return False
        for directive in node.resource_dependencies:
            acquire_node_id = str(directive.get("acquire_node_id") or "")
            resource_ref = str(directive.get("resource_ref") or "")
            scope = str(directive.get("scope") or "")
            lease = self._leases.get(acquire_node_id)
            if lease is None:
                return False
            current = self._lock_manager.get_lease(lease.lease_id)
            if current.state != "active" or not any(
                claim.resource_id == resource_ref and claim.scope == scope
                for claim in current.claims
            ):
                return False
        return True

    @staticmethod
    def _lease_request(
        node: Any,
        *,
        run_id: str | None = None,
    ) -> LeaseRequest:
        return python_fallback_lease_request(
            node,
            run_id=run_id,
        )

    @staticmethod
    def _serialize_claims(
        claims: Iterable[ResolvedResourceClaim],
    ) -> list[dict[str, Any]]:
        return [
            {
                "resource_id": claim.resource_id,
                "resource_kind": claim.resource_kind,
                "quantity": claim.quantity,
                "mode": claim.mode,
                "scope": claim.scope,
            }
            for claim in claims
        ]

    async def _run_node(self, node_id: str) -> tuple[str, NodeState]:
        node = self.dag.nodes[node_id]
        if self._journal is not None:
            self._journal.record_node_started(
                run_id=self.dag.task_id,
                node_id=node_id,
                attempt=1,
            )
        if node.input_bindings:
            try:
                node.action_args = materialize_node_inputs(
                    input_bindings=node.input_bindings,
                    input_schema=node.input_schema,
                    results=self.results,
                    runtime_parameters=self._runtime_parameters,
                )
            except BindingPreflightError as exc:
                self.errors[node_id] = exc
                self._terminal_payloads[node_id] = {
                    "error": str(exc),
                    "physical_state": "not_started",
                }
                return node_id, NodeState.FAILED
        if (
            node.node_type == "branch"
            and node.device_id == "os_control"
            and node.action == "branch"
        ):
            selected = "true" if node.action_args.get("condition") else "false"
            envelope = ResultEnvelope(outputs={"branch": selected})
            self.results[node_id] = envelope
            self._terminal_payloads[node_id] = dict(envelope.outputs)
            return node_id, NodeState.SUCCESS
        if (
            node.node_type == "join"
            and node.device_id == "os_control"
            and node.action == "join"
        ):
            self.results[node_id] = ResultEnvelope(outputs={})
            self._terminal_payloads[node_id] = {}
            return node_id, NodeState.SUCCESS
        if (
            node.node_type == "group"
            and node.device_id == "os_control"
            and node.action == "group"
        ):
            self.results[node_id] = ResultEnvelope(outputs={})
            self._terminal_payloads[node_id] = {}
            return node_id, NodeState.SUCCESS
        try:
            execution_result = await self._submit(node)
        except asyncio.CancelledError:
            await self._mark_lease_unknown(node_id, "dispatch cancelled; physical state unknown")
            raise
        except Exception as exc:  # noqa: BLE001 - a dispatch exception is physically ambiguous
            reason = str(exc) or exc.__class__.__name__
            self._terminal_payloads[node_id] = {
                "error": reason,
                "physical_state": "unknown",
            }
            await self._mark_lease_unknown(node_id, reason)
            return node_id, NodeState.FAILED
        if isinstance(execution_result, NodeExecutionResult):
            terminal_info = dict(execution_result.terminal_info)
            if execution_result.state == NodeState.SUCCESS:
                try:
                    validate_result_outputs(
                        outputs=execution_result.envelope.outputs,
                        output_schema=node.output_schema,
                    )
                except BindingPreflightError as exc:
                    self.errors[node_id] = exc
                    self._terminal_payloads[node_id] = {
                        "error": str(exc),
                        "physical_state": "confirmed",
                        "reconcile_required": False,
                    }
                    return node_id, NodeState.FAILED
                self._terminal_payloads[node_id] = dict(
                    execution_result.envelope.outputs
                )
            else:
                self._terminal_payloads[node_id] = terminal_info
            if execution_result.state == NodeState.SUCCESS:
                self.results[node_id] = execution_result.envelope
            elif self._is_physically_unknown(terminal_info):
                reason = str(
                    terminal_info.get("error")
                    or "device terminal did not confirm physical safety"
                )
                await self._mark_lease_unknown(node_id, reason)
            return node_id, execution_result.state
        if execution_result in {NodeState.FAILED, NodeState.CANCELLED}:
            self._terminal_payloads[node_id] = {
                "physical_state": "unknown",
                "reconcile_required": True,
            }
            await self._mark_lease_unknown(
                node_id,
                "device terminal did not include physical certainty",
            )
        else:
            self._terminal_payloads[node_id] = {}
        return node_id, execution_result

    @staticmethod
    def _is_physically_unknown(terminal_info: dict[str, Any]) -> bool:
        physical_state = str(terminal_info.get("physical_state") or "").lower()
        return bool(terminal_info.get("reconcile_required")) or physical_state in {
            "unknown",
            "ambiguous",
            "in_motion",
        }

    async def _mark_lease_unknown(self, node_id: str, reason: str) -> None:
        if self._lock_manager is None:
            return
        lease = self._leases.get(node_id)
        if lease is None or self._lock_manager.get_lease(lease.lease_id).state != "active":
            return
        unknown = await self._lock_manager.mark_unknown(lease.lease_id, reason)
        if self._journal is not None:
            self._journal.record_lock_unknown(
                run_id=self.dag.task_id,
                node_id=node_id,
                lease_id=unknown.lease_id,
                holder_id=unknown.holder_id,
                claims=self._serialize_claims(unknown.claims),
                reason=reason,
            )

    async def _finalize_node(self, node_id: str, status: NodeState) -> None:
        node = self.dag.nodes[node_id]
        if self._journal is not None:
            terminal = {
                NodeState.SUCCESS: "succeeded",
                NodeState.FAILED: "failed",
                NodeState.CANCELLED: "cancelled",
                NodeState.SKIPPED: "skipped",
            }.get(status, status.value)
            completed = [
                current_id
                for current_id, current_state in self.walk.states.items()
                if current_state == NodeState.SUCCESS
            ]
            self._journal.commit_node_terminal(
                run_id=self.dag.task_id,
                node_id=node_id,
                terminal=terminal,
                result=self._terminal_payloads.get(node_id, {}),
                effects=list(node.effects) if status == NodeState.SUCCESS else [],
                cursor={"completed": completed},
                outbox=[],
            )

        if self._lock_manager is None:
            return
        lease = self._leases.get(node_id)
        if lease is None:
            return
        current = self._lock_manager.get_lease(lease.lease_id)
        if current.state == "active":
            # A confirmed terminal closes only action-scoped claims. Retained
            # until_handoff/workflow_block claims require their explicit
            # successful release node, including acquire==release workflows.
            release_scope = "action"
            released = await self._lock_manager.release(
                lease.lease_id,
                scope=release_scope,
            )
            if released and self._journal is not None:
                self._journal.record_lock_released(
                    run_id=self.dag.task_id,
                    node_id=node_id,
                    lease_id=lease.lease_id,
                    released_scope=release_scope,
                )

    def _notify_terminal(self, node_id: str, status: NodeState) -> None:
        """触发终态回调；回调失败（如断网时上行 publish 抛错）绝不打断走图（AC-3）。"""
        if self._on_terminal is None:
            return
        try:
            self._on_terminal(node_id, status)
        except Exception:  # noqa: BLE001 —— 上行/持久化失败不应停摆本地走图
            logger.exception("DagExecutor on_node_terminal 回调失败，忽略并继续走图")

    @staticmethod
    async def _cancel_inflight(inflight: dict[str, asyncio.Task]) -> None:
        for task in inflight.values():
            task.cancel()
        # 等待取消完成，吞掉 CancelledError
        await asyncio.gather(*inflight.values(), return_exceptions=True)
        inflight.clear()
