"""local_api — 桥的实现 B（SZLab local_ui）UI 面 HTTP 服务器（FastAPI，:8014）。

unilabos_local_ui（React 19 + React Flow 本地联调工作台）经 vite `/api` 代理连入本服务。
桥在此扮演「本地联调传输适配器」：把 local_ui 的工作流图归一为 Canonical source，
统一交 RuntimeService 编译与下发；把 OS 回流投影为旧 UI 所需的日志形状。

端点集严格对照 unilabos_local_ui/src/main.tsx：
- GET  /api/preset               → PresetPayload（demo 动作 + 默认配置）
- GET  /api/stack-status         → StackStatusPayload（本地桥无仓储，返回 success 空堆栈）
- POST /api/workflow/build-graph → WorkflowJson（校验 + 归一，含环即 400 detail）
- POST /api/run                  → RunStatus（构 TaskDag 交 submit_dag，返回 run_id）
- GET  /api/run/{id}             → RunStatus（逐节点态 + log_events，供 1s 轮询）
- POST /api/run/{id}/cancel      → RunStatus（cancel_task）

RunStatus.status（对照 main.tsx statusText / pollRun 终态判定）：
  pending / running / completed / failed / cancelled（终态三者停止轮询）。
NodeRunStatus（node_statuses 值）：idle / preparing / running / success / failed / cancelled。
node_statuses 以 node.id 为键——F002 node_id == local_ui node.id，故 applyNodeStatuses 命中。

延迟 import fastapi（未装不拖累其余桥面）。协议翻译集中在 LocalApiState（传输无关，
便于 hermetic TestClient 测）；create_app 只做路由接线。

契约见 docs/features/F003-local-workflow-bridge/interface-design.md §三。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from unilabos.app.local_bridge.bind_security import require_loopback_runtime_host
from unilabos.app.local_bridge.schedule_ws import RunHandle, ScheduleSession
from unilabos.scheduler.dag_model import DagValidationError, NodeState
from unilabos.scheduler.resource_lock import ResourceLockManager
from unilabos.workflow.submission import (
    workflow_submission_to_revision,
)
from unilabos.runtime.event_store import SQLiteEventJournal
from unilabos.runtime.profile_loader import LoadedProfile
from unilabos.runtime.service import RuntimeConflictError, RuntimeService

logger = logging.getLogger(__name__)

# NodeState → NodeRunStatus（main.tsx NodeRunStatus 字面量）
_NODE_STATE_TO_RUN_STATUS: dict[NodeState, str] = {
    NodeState.PENDING: "idle",
    NodeState.READY: "preparing",
    NodeState.RUNNING: "running",
    NodeState.SUCCESS: "success",
    NodeState.FAILED: "failed",
    NodeState.CANCELLED: "cancelled",
    NodeState.SKIPPED: "skipped",
}

# job_status.status → 日志可读词（简体中文，供 local_ui LogPanel 展示）
_STATUS_WORD: dict[str, str] = {
    "running": "运行中",
    "success": "成功",
    "failed": "失败",
    "cancelled": "已终止",
}

# 整个 run 的状态（main.tsx pollRun 终态集 ['completed','failed','cancelled']）
RUN_STATUS_PENDING = "pending"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"
RUN_STATUS_CANCEL_REQUESTED = "cancel_requested"
RUN_STATUS_RECONCILING = "reconciling"


def node_statuses_of(run: RunHandle) -> dict[str, str]:
    """把 RunHandle 逐节点 NodeState 映射成 local_ui 的 NodeRunStatus 表（键 = node.id）。"""
    return {
        node_id: (
            state
            if state in {"cancel_requested", "reconciling"}
            else _NODE_STATE_TO_RUN_STATUS.get(state, "idle")
        )
        for node_id, state in run.node_states.items()
    }


def overall_status_of(run: RunHandle) -> str:
    """由逐节点态推整个 run 的状态（对齐 pollRun 终态判定）。

    未全终态：有节点 running 则 'running'，否则 'pending'。
    全终态：任一 failed → 'failed'；否则任一 cancelled → 'cancelled'；否则 'completed'。
    """
    states = list(run.node_states.values())
    if "reconciling" in states:
        return RUN_STATUS_RECONCILING
    if "cancel_requested" in states:
        return RUN_STATUS_CANCEL_REQUESTED
    if not run.finished:
        return RUN_STATUS_RUNNING if NodeState.RUNNING in states else RUN_STATUS_PENDING
    if NodeState.FAILED in states:
        return RUN_STATUS_FAILED
    if NodeState.CANCELLED in states:
        return RUN_STATUS_CANCELLED
    return RUN_STATUS_COMPLETED


@dataclass
class RunRecord:
    """一次运行的桥侧记录：句柄 + 累积的结构化日志（RunHandle 只管节点态）。"""

    run_id: str
    name: str
    handle: RunHandle
    log_events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_status(self) -> dict[str, Any]:
        """产出 local_ui 消费的 RunStatus 报文（逐字段对照 main.tsx RunStatus）。"""
        return {
            "run_id": self.run_id,
            "status": overall_status_of(self.handle),
            "logs": [event["message"] for event in self.log_events],
            "log_events": list(self.log_events),
            "error": self.error,
            "node_statuses": node_statuses_of(self.handle),
        }


def build_demo_preset() -> dict[str, Any]:
    """返回 demo PresetPayload——本地桥无真实注册表时的可拖拽动作集。

    动作与 workflow_ws.build_demo_graph 的设备/动作对齐（pump_1/pump_liquid、stirrer_1/stir），
    使两套 UI 演示同一批设备动作。default_config 镜像 local_ui DEFAULT_CONFIG。
    """
    return {
        "id": "local_bridge_demo",
        "title": "Uni-Lab 本地桥联调工具",
        "default_workflow_name": "local_bridge_workflow",
        "default_config": {
            "graph": "__generated__",
            "url": "",
            "csv": "",
            "timeout": 300,
            "write_allowed_timeout": 5,
            "no_subscription": True,
            "show_csv": False,
        },
        "actions": [
            {
                "method": "pump_liquid",
                "label": "加液",
                "description": "泵按体积加液",
                "device_id": "pump_1",
                "needs_position": False,
                "params": [
                    {
                        "name": "volume",
                        "label": "体积(mL)",
                        "description": "加液体积",
                        "type": "number",
                        "min": 0,
                        "default": 5.0,
                    }
                ],
                "opc_variables": [],
            },
            {
                "method": "stir",
                "label": "搅拌",
                "description": "磁力搅拌指定秒数",
                "device_id": "stirrer_1",
                "needs_position": False,
                "params": [
                    {
                        "name": "seconds",
                        "label": "时长(秒)",
                        "description": "搅拌时长",
                        "type": "integer",
                        "min": 0,
                        "default": 10,
                    }
                ],
                "opc_variables": [],
            },
        ],
    }


class LocalApiState:
    """实现 B 的协议翻译核（传输无关，便于 hermetic 测）。

    注入已就绪 ScheduleSession；在其上注册唯一 job_status 回调，把回流按 task_id(==run_id)
    路由到对应 RunRecord 并累积 log_events。对外暴露 build_graph / start_run / get_run /
    cancel_run 供 HTTP 路由调用。
    """

    def __init__(
        self,
        schedule_session: ScheduleSession,
        *,
        journal: SQLiteEventJournal | None = None,
        action_catalog: Mapping[str, Mapping[str, Any]] | None = None,
        profiles: Mapping[str, LoadedProfile] | None = None,
        resource_lock_manager: ResourceLockManager | None = None,
        runtime_service: Any | None = None,
    ) -> None:
        self._schedule = schedule_session
        self._journal = journal
        self._profiles = dict(profiles or {})
        self._action_catalog: dict[str, Mapping[str, Any]] = {}
        for profile in self._profiles.values():
            self._action_catalog.update(profile.action_catalog)
        self._action_catalog.update(action_catalog or {})
        self._runs: dict[str, RunRecord] = {}
        self._active_workflow: dict[str, Any] | None = None
        self._seq = 0
        self._schedule.on_job_status(self._on_os_job_status)
        self._runtime_service = runtime_service or RuntimeService(
            schedule_session,
            journal=journal,
            action_catalog=self._action_catalog,
            profiles=self._profiles,
            resource_lock_manager=resource_lock_manager,
        )
        if runtime_service is None:
            self._runtime_service.set_workflow_revision(
                workflow_submission_to_revision(_demo_workflow())
            )

    @property
    def runtime_service(self) -> Any:
        """Expose the shared service to another transport adapter."""

        return self._runtime_service

    def build_graph(self, request: dict[str, Any]) -> dict[str, Any]:
        """校验 + 归一 local_ui 工作流请求，返回 WorkflowJson。含环/缺字段抛 DagValidationError。

        request 为 createWorkflowRequest 输出：{name, nodes, edges}。以 name 为临时 task_id
        交 workflow_to_task_dag 走 F002 解析校验（拒环 = I5），校验通过则回显归一后的图。
        """
        name = str(request.get("name") or "workflow")
        nodes = request.get("nodes") or []
        edges = request.get("edges") or []
        # 这里只做 source→Canonical 归一；唯一 Canonical→TaskDag lowering
        # 留在 RuntimeService.start_run，避免 build 与 run 双编译。
        revision = workflow_submission_to_revision(request)
        workflow = {
            "name": name,
            "nodes": nodes,
            "edges": edges,
        }
        self._active_workflow = workflow
        set_revision = getattr(
            self._runtime_service,
            "set_workflow_revision",
            None,
        )
        if callable(set_revision):
            set_revision(revision)
        return workflow

    async def start_run(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /api/run：构 TaskDag 交 schedule.submit_dag，建 RunRecord，返回 RunStatus。

        body = {workflow: WorkflowJson, ...config}；run_id 即 TaskDag.task_id（回流按此命中）。
        """
        workflow = body.get("workflow") or {}
        name = str(workflow.get("name") or "workflow")
        nodes = workflow.get("nodes") or []
        revision = workflow_submission_to_revision(workflow)
        accepted = await self._runtime_service.start_run(
            {
                "source": {
                    "format": "canonical_workflow_v2",
                    "payload": revision.model_dump(mode="json"),
                },
                "parameters": dict(body.get("parameters") or {}),
            }
        )
        run_id = str(accepted["id"])
        handle = self._schedule.get_run(run_id)
        if handle is None:
            return {
                "run_id": run_id,
                "status": str(accepted.get("status") or "pending"),
            }
        record = RunRecord(run_id=run_id, name=name, handle=handle)
        record.log_events.append(
            self._make_event("workflow", None, f"已下发 workflow「{name}」")
        )
        self._runs[run_id] = record
        logger.info(
            "[local_api] 已启动 run %s（workflow=%s，%d 节点）",
            run_id,
            name,
            len(nodes),
        )
        return record.to_status()

    def runtime_workflow(
        self,
        workflow_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the current workflow as the shared RuntimeClient projection."""

        workflow = self._runtime_service.get_workflow()
        if workflow_id is None:
            return workflow
        definition = workflow.get("definition")
        active_id = (
            str(definition.get("id"))
            if isinstance(definition, Mapping) and definition.get("id")
            else ""
        )
        return workflow if workflow_id == active_id else None

    def runtime_actions(self) -> dict[str, Any]:
        """Project the generic OS action catalog for authoring clients."""

        actions: list[dict[str, Any]] = []
        for action_ref in sorted(self._action_catalog):
            definition = self._action_catalog[action_ref]
            label = (
                definition.get("label")
                or definition.get("title")
                or definition.get("display_name")
                or action_ref
            )
            actions.append(
                {
                    "action_ref": action_ref,
                    "input_schema": dict(definition.get("inputs") or {}),
                    "label": str(label),
                    "output_schema": dict(definition.get("outputs") or {}),
                }
            )
        return {
            "schema_version": "runtime/v1",
            "actions": actions,
        }

    async def start_runtime_run(self, body: dict[str, Any]) -> dict[str, Any]:
        """Delegate generic source submission to the OS RuntimeService."""

        return await self._runtime_service.start_run(body)

    def get_runtime_run(self, run_id: str) -> dict[str, Any] | None:
        return self._runtime_service.get_run(run_id)

    def runtime_events(self, run_id: str) -> list[dict[str, Any]] | None:
        return self._runtime_service.get_events(run_id)

    def runtime_timeline(self, run_id: str) -> dict[str, Any] | None:
        return self._runtime_service.get_timeline(run_id)

    async def cancel_runtime_run(self, run_id: str) -> dict[str, Any] | None:
        return await self._runtime_service.cancel_run(run_id)

    async def reconcile_runtime_run(
        self, run_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._runtime_service.reconcile_run(run_id, body)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """GET /api/run/{id}：返回当前 RunStatus；无此 run 则 None（路由转 404）。"""
        runtime_run = self._runtime_service.get_run(run_id)
        if runtime_run is None:
            return None
        record = self._runs.get(run_id)
        if record is None:
            return {
                "run_id": run_id,
                "status": str(runtime_run.get("status") or "pending"),
                "node_statuses": {},
                "log_events": [],
                "error": None,
            }
        projection = record.to_status()
        projection["status"] = str(runtime_run.get("status") or projection["status"])
        return projection

    async def cancel_run(self, run_id: str) -> dict[str, Any] | None:
        """POST /api/run/{id}/cancel：下发 cancel_task 并回最新 RunStatus；无此 run 则 None。"""
        runtime_run = await self._runtime_service.cancel_run(run_id)
        if runtime_run is None:
            return None
        record = self._runs.get(run_id)
        if record is None:
            return {
                "run_id": run_id,
                "status": str(runtime_run.get("status") or "cancel_requested"),
                "node_statuses": {},
                "log_events": [],
                "error": None,
            }
        record.log_events.append(
            self._make_event("workflow", None, "已请求终止 workflow")
        )
        projection = record.to_status()
        projection["status"] = str(runtime_run.get("status") or projection["status"])
        return projection

    def _on_os_job_status(self, data: dict[str, Any]) -> None:
        """OS job_status 回流：按 task_id(==run_id) 路由到 RunRecord，累积一条 log_event。

        RunHandle 的节点态已由 ScheduleSession._on_job_status 在回调前更新，此处只追加日志。
        """
        task_id = data.get("task_id", "")
        record = self._runs.get(task_id)
        if record is None:
            return
        node_id = data.get("job_id", "")
        status = data.get("status", "")
        action_name = data.get("action_name", "") or node_id
        word = _STATUS_WORD.get(status, status)
        level = "error" if status == "failed" else "info"
        detail = data.get("return_info") or data.get("feedback_data") or None
        record.log_events.append(
            self._make_event(
                "node",
                node_id or None,
                f"[{node_id}] {action_name} {word}",
                level,
                detail,
                event_type=f"node_{status}",
            )
        )
        if status == "failed" and record.error is None:
            record.error = f"节点 {node_id} 执行失败"

    def _make_event(
        self,
        scope: str,
        node_id: str | None,
        message: str,
        level: str = "info",
        detail: Any = None,
        event_type: str = "log",
    ) -> dict[str, Any]:
        """构一条 LogEvent（对照 opcChanges.ts LogEvent：sequence/message/level/scope/node_id/detail）。"""
        self._seq += 1
        return {
            "sequence": self._seq,
            "message": message,
            "level": level,
            "scope": scope,
            "node_id": node_id,
            "detail": detail,
            "type": event_type,
        }


def _demo_workflow() -> dict[str, Any]:
    return {
        "name": "Quick Debug Demo",
        "nodes": [
            {
                "id": "measure-1",
                "data": {
                    "method": "measure",
                    "label": "Measure",
                    "device_id": "balance-1",
                    "params": {},
                },
            },
            {
                "id": "dose-2",
                "data": {
                    "method": "dose",
                    "label": "Dose",
                    "device_id": "pump-1",
                    "params": {},
                },
            },
        ],
        "edges": [{"id": "edge-1", "source": "measure-1", "target": "dose-2"}],
    }


def build_stack_status() -> dict[str, Any]:
    """返回 StackStatusPayload——本地桥无真实仓储，success=True 空堆栈（UI 表现为「等待数据」）。"""
    return {"success": True, "schema": "local_bridge", "stacks": {}}


def create_app(get_state: Callable[[], LocalApiState | None]) -> Any:
    """建 FastAPI app。get_state() 返回已就绪 LocalApiState（OS 未连入时返回 None）。

    延迟 import fastapi（未装不拖累其余桥面）。路由只做请求解码 + 调 LocalApiState + 错误转码。
    """
    from fastapi import Body, FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware

    app = FastAPI(title="Uni-Lab 本地桥（实现 B）", version="1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=(
            r"^http://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{1,5})?$"
        ),
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    def _require_state() -> Any:
        state = get_state()
        if state is None:
            raise HTTPException(status_code=503, detail="OS 未连入，调度会话尚未就绪")
        return state

    def _authoring_payload(
        payload: Any,
        *,
        fields: set[str],
    ) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != fields:
            raise HTTPException(
                status_code=422,
                detail="INVALID_AUTHORING_ENVELOPE",
            )
        return payload

    @app.get("/api/preset")
    async def api_preset() -> Any:
        return build_demo_preset()

    @app.get("/api/stack-status")
    async def api_stack_status() -> Any:
        return build_stack_status()

    @app.post("/api/v1/authoring/compile")
    async def api_authoring_compile(payload: Any = Body(...)) -> Any:
        from unilabos.workflow.canonical_ir import compile_authoring_revision

        state = _require_state()
        request = _authoring_payload(
            payload,
            fields={"base_revision_id", "python_source", "source_uri"},
        )
        try:
            return compile_authoring_revision(
                request,
                action_catalog=state._action_catalog,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/authoring/generate-python")
    async def api_authoring_generate(payload: Any = Body(...)) -> Any:
        from unilabos.workflow.to_python_script import generate_python_revision

        state = _require_state()
        request = _authoring_payload(
            payload,
            fields={"base_revision_id", "canonical_ir", "source_uri"},
        )
        try:
            return generate_python_revision(
                request,
                action_catalog=state._action_catalog,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/authoring/validate")
    async def api_authoring_validate(payload: Any = Body(...)) -> Any:
        from unilabos.workflow.canonical_ir import validate_authoring_revision

        state = _require_state()
        request = _authoring_payload(
            payload,
            fields={"base_revision_id", "candidate"},
        )
        try:
            return validate_authoring_revision(
                request,
                action_catalog=state._action_catalog,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/workflow/build-graph")
    async def api_build_graph(payload: dict[str, Any] = Body(...)) -> Any:
        state = _require_state()
        try:
            return state.build_graph(payload)
        except DagValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/run")
    async def api_run(payload: dict[str, Any] = Body(...)) -> Any:
        state = _require_state()
        try:
            return await state.start_run(payload)
        except DagValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/run/{run_id}")
    async def api_run_status(run_id: str) -> Any:
        state = _require_state()
        status = state.get_run(run_id)
        if status is None:
            raise HTTPException(status_code=404, detail=f"未知 run: {run_id}")
        return status

    @app.post("/api/run/{run_id}/cancel")
    async def api_run_cancel(run_id: str) -> Any:
        state = _require_state()
        status = await state.cancel_run(run_id)
        if status is None:
            raise HTTPException(status_code=404, detail=f"未知 run: {run_id}")
        return status

    @app.get("/api/runtime/local/workflow")
    async def api_runtime_workflow(
        workflow_id: str | None = None,
    ) -> Any:
        workflow = _require_state().runtime_workflow(workflow_id)
        if workflow is None:
            raise HTTPException(
                status_code=404,
                detail=f"WORKFLOW_NOT_FOUND: {workflow_id}",
            )
        return workflow

    @app.get("/api/runtime/local/actions")
    async def api_runtime_actions() -> Any:
        return _require_state().runtime_actions()

    @app.post("/api/runtime/local/runs")
    async def api_runtime_start_run(payload: dict[str, Any] = Body(default={})) -> Any:
        state = _require_state()
        try:
            return await state.start_runtime_run(payload)
        except DagValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/runtime/local/runs/{run_id}")
    async def api_runtime_run(run_id: str) -> Any:
        status = _require_state().get_runtime_run(run_id)
        if status is None:
            raise HTTPException(status_code=404, detail=f"未知 run: {run_id}")
        return status

    @app.get("/api/runtime/local/runs/{run_id}/events")
    async def api_runtime_events(run_id: str) -> Any:
        events = _require_state().runtime_events(run_id)
        if events is None:
            raise HTTPException(status_code=404, detail=f"未知 run: {run_id}")
        return events

    @app.get("/api/runtime/local/runs/{run_id}/timeline")
    async def api_runtime_timeline(run_id: str) -> Any:
        timeline = _require_state().runtime_timeline(run_id)
        if timeline is None:
            raise HTTPException(status_code=404, detail=f"未知 run: {run_id}")
        return timeline

    @app.post("/api/runtime/local/runs/{run_id}/cancel")
    async def api_runtime_cancel(run_id: str) -> Any:
        state = _require_state()
        try:
            status = await state.cancel_runtime_run(run_id)
        except RuntimeConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if status is None:
            raise HTTPException(status_code=404, detail=f"未知 run: {run_id}")
        return status

    @app.post("/api/runtime/local/runs/{run_id}/reconcile")
    async def api_runtime_reconcile(
        run_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> Any:
        state = _require_state()
        try:
            return await state.reconcile_runtime_run(run_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"未知 run: {run_id}") from exc
        except RuntimeConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return app


class LocalApiServer:
    """实现 B UI 面 HTTP 服务器薄壳：uvicorn 起 FastAPI（延迟 import，未装不影响其余桥面）。

    传入 get_state 解析已就绪 LocalApiState；server.py 组合入口在 OS 连入后注入。
    """

    def __init__(
        self,
        get_state: Callable[[], LocalApiState | None],
        host: str = "127.0.0.1",
        port: int = 8014,
    ) -> None:
        require_loopback_runtime_host(host)
        self._get_state = get_state
        self.host = host
        self.port = port
        self._server: Any = None

    async def start(self) -> None:
        import uvicorn

        app = create_app(self._get_state)
        config = uvicorn.Config(app, host=self.host, port=self.port, log_level="info")
        self._server = uvicorn.Server(config)
        logger.info(
            "[local_api] 实现 B UI 面 HTTP 已监听 http://%s:%d/api",
            self.host,
            self.port,
        )
        await self._server.serve()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
            self._server = None
            logger.info("[local_api] 实现 B UI 面 HTTP 已停止")
