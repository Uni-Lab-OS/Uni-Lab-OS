"""workflow_ws — 桥的实现 A（云端 panel）UI 面 WS 服务器（路径 /ws/workflow/{uuid}）。

uni-lab-cloud 的 WorkflowDAGPanel/WorkflowStepsPanel 经 useWorkflowWebSocket 连入本路径。
桥在此扮演「工作流传输适配器」：解析 WorkflowWSActionType，把旧工作流图归一为
Canonical WorkflowRevision 后交唯一 RuntimeService；把 OS 回流的 job_status 翻译成
panel 的 workflow_update 报文推回。单一事实源——执行和编译均在 OS RuntimeService，
桥只做旧 UI 协议翻译。

上行（panel→桥，见 src/services/workflowService.ts）：
- {"action": "fetch_graph",  "msg_uuid": ...}
- {"action": "run_workflow", "msg_uuid": ...}
- {"action": "stop_workflow","msg_uuid": ..., "data": <task_id>}

下行（桥→panel）——外层 {code, data:{action, data, ...}}，panel 按 data.data.action 分发
（见 WorkflowDAGPanel.onMessageCallback）：
- fetch_graph:     {code:0, data:{action:"fetch_graph",  data:{nodes, edges}}}
- run_workflow:    {code:0, data:{action:"run_workflow", data:<task_id>}}
- workflow_update: {code:0, data:{action:"workflow_update", code:0,
      data:{node_uuid, job_status, task_status, header, msg}}}
- stop_workflow:   {code:0, data:{action:"stop_workflow"}}

task_status（WorkflowStatusEnum）：Running='running' / Finished='end'。
setNodeExecutedExecutor 每收到一条 workflow_update 就按 node_uuid 更新单节点态
（node.status = job_status）；task_status=='end' 时 panel 清空 taskId（视整任务收尾）——
故桥仅当整张 DAG 全终态（RunHandle.finished）时置 'end'，否则 'running'。

为便于 hermetic 测试，协议翻译集中在 WorkflowSession（传输无关——注入 send 协程、
注入已就绪 ScheduleSession，喂 handle_incoming）；translate_job_status_to_update
为纯函数，供逐字段断言下行形状（AC-3）。

契约见 docs/features/F003-local-workflow-bridge/interface-design.md §二。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from unilabos.app.local_bridge.schedule_ws import ScheduleSession
from unilabos.runtime.service import RuntimeService
from unilabos.workflow.submission import workflow_submission_to_revision

logger = logging.getLogger(__name__)

# UI 面 WS 路径前缀 —— useWorkflowWebSocket 连入 /ws/workflow/{uuid}
WORKFLOW_WS_PATH_PREFIX = "/ws/workflow/"

# WorkflowWSActionType（src/types/workflow.ts）—— 仅桥关心的四个动作
FETCH_GRAPH = "fetch_graph"
RUN_WORKFLOW = "run_workflow"
STOP_WORKFLOW = "stop_workflow"
WORKFLOW_UPDATE = "workflow_update"

# WorkflowStatusEnum（src/types/workflow.ts）：Running='running' / Finished='end'
TASK_STATUS_RUNNING = "running"
TASK_STATUS_END = "end"

_DEMO_ACTION_CATALOG: dict[str, dict[str, Any]] = {
    "pump_1.pump_liquid": {
        "inputs": {"volume": {"type": "number"}},
        "outputs": {},
    },
    "stirrer_1.stir": {
        "inputs": {"seconds": {"type": "integer"}},
        "outputs": {},
    },
}


def build_demo_graph() -> dict[str, Any]:
    """返回一张 demo 工作流图，同时服务 panel 渲染与 workflow_to_dag 翻译。

    每节点同时携带：
    - uuid/id/node_id（三者相等）——保证 job_id==node_id==panel node.id，回流可命中节点；
    - device_id/action/action_type/action_args——供 workflow_to_dag 翻译成 F002；
    - pose.position——供 handleNodesToWorkflowReactFlow 渲染坐标。
    边用 source_node_uuid/target_node_uuid（同服务翻译与渲染）。
    """
    nodes = [
        {
            "uuid": "n1",
            "id": "n1",
            "node_id": "n1",
            "name": "加液",
            "device_id": "pump_1",
            "action": "pump_liquid",
            "action_type": "",
            "action_args": {"volume": 5.0},
            "pose": {"position": {"x": 0, "y": 0}},
        },
        {
            "uuid": "n2",
            "id": "n2",
            "node_id": "n2",
            "name": "搅拌",
            "device_id": "stirrer_1",
            "action": "stir",
            "action_type": "",
            "action_args": {"seconds": 10},
            "pose": {"position": {"x": 240, "y": 0}},
        },
    ]
    edges = [
        {
            "uuid": "e1",
            "source_node_uuid": "n1",
            "target_node_uuid": "n2",
            "source_handle_uuid": "",
            "target_handle_uuid": "",
        },
    ]
    return {"nodes": nodes, "edges": edges}


def _current_legacy_graph(runtime_service: Any) -> dict[str, Any]:
    """Project the Runtime-owned current revision for the legacy panel.

    The panel graph is render-only.  Execution always resubmits the lossless
    Canonical payload stored beside it; this adapter must never reconstruct an
    executable workflow from the visual projection.
    """

    workflow = runtime_service.get_workflow()
    revision = workflow.get("revision") if isinstance(workflow, dict) else None
    if not isinstance(revision, dict):
        raise RuntimeError("Runtime workflow projection is missing revision")
    canonical = revision.get("canonical")
    if not isinstance(canonical, dict):
        raise RuntimeError("Runtime workflow projection is missing canonical")

    projected_nodes = revision.get("nodes")
    projected_nodes = projected_nodes if isinstance(projected_nodes, list) else []
    projected_by_id = {
        str(item.get("id") or ""): item
        for item in projected_nodes
        if isinstance(item, dict) and item.get("id")
    }
    nodes: list[dict[str, Any]] = []
    for raw in canonical.get("invocations", []):
        if not isinstance(raw, dict):
            continue
        node_id = str(raw.get("node_id") or "")
        action_ref = str(raw.get("action_ref") or "")
        device_id, separator, action = action_ref.rpartition(".")
        if not node_id or not separator:
            continue
        projected = projected_by_id.get(node_id, {})
        literal_args = {
            str(name): binding.get("value")
            for name, binding in (raw.get("input_bindings") or {}).items()
            if isinstance(binding, dict) and binding.get("kind") == "literal"
        }
        nodes.append(
            {
                "uuid": node_id,
                "id": node_id,
                "node_id": node_id,
                "name": str(
                    projected.get("label")
                    or raw.get("name")
                    or action
                    or node_id
                ),
                "device_id": device_id,
                "action": action,
                "action_type": str(raw.get("node_type") or ""),
                "action_args": literal_args,
                "input_bindings": dict(raw.get("input_bindings") or {}),
                "pose": {"position": {"x": 0, "y": 0}},
            }
        )

    projected_edges = revision.get("edges")
    projected_edges = projected_edges if isinstance(projected_edges, list) else []
    edges = [
        {
            "uuid": str(item.get("id") or f"legacy-edge-{index + 1}"),
            "source_node_uuid": str(item.get("source") or ""),
            "target_node_uuid": str(item.get("target") or ""),
            "source_handle_uuid": "",
            "target_handle_uuid": "",
        }
        for index, item in enumerate(projected_edges)
        if isinstance(item, dict) and item.get("source") and item.get("target")
    ]
    return {"nodes": nodes, "edges": edges}


def _stringify(value: Any) -> str:
    """把 return_info/feedback 归一成可读字符串（供 panel console 展示）。"""
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def translate_job_status_to_update(data: dict[str, Any], *, finished: bool) -> dict[str, Any]:
    """把一条 OS job_status（F002 JobData）翻译成 panel 的 workflow_update 下行报文。

    node_uuid = job_id（F002 node_id==job_id），job_status 直接驱动 panel 单节点态；
    task_status 仅在整张 DAG 全终态（finished）时为 'end'（触发 panel 清空 taskId），
    否则 'running'。外层与内层均带 code:0——panel 错误分支按 data.code 判定，
    setNodeExecutedExecutor 又取 data.data.code。
    """
    job_id = data.get("job_id", "")
    status = data.get("status", "")
    action_name = data.get("action_name", "")
    return_info = data.get("return_info", "")
    return {
        "code": 0,
        "data": {
            "action": WORKFLOW_UPDATE,
            "code": 0,
            "data": {
                "node_uuid": job_id,
                "job_status": status,
                "task_status": TASK_STATUS_END if finished else TASK_STATUS_RUNNING,
                "header": action_name,
                "msg": _stringify(return_info),
            },
        },
    }


class WorkflowSession:
    """单个云端 panel 连接的工作流会话（传输无关，便于 hermetic 测试）。

    - handle_incoming(message)：喂入 panel 上行报文（fetch_graph/run_workflow/stop_workflow）
    - 内部经注入的 RuntimeService 编译、下发及取消，并注册 job_status 回调翻译回流

    每会话在 ScheduleSession 上注册唯一回调，按 self._task_id 动态过滤（避免多次运行累积回调）。
    """

    def __init__(
        self,
        send: Callable[[dict[str, Any]], Awaitable[None]],
        schedule_session: ScheduleSession,
        *,
        uuid: str = "",
        runtime_service: Any | None = None,
    ) -> None:
        self._send = send
        self._schedule = schedule_session
        self.uuid = uuid
        self._task_id = ""
        if runtime_service is None:
            runtime_service = RuntimeService(
                schedule_session,
                action_catalog=_DEMO_ACTION_CATALOG,
            )
            runtime_service.set_workflow_revision(
                workflow_submission_to_revision(
                    {"name": self.uuid or "workflow", **build_demo_graph()}
                )
            )
        self._runtime_service = runtime_service
        self._schedule.on_job_status(self._on_os_job_status)

    def close(self) -> None:
        """会话断开时注销 job_status 回调，避免回调在长寿命 OS 会话上累积（连接 finally 调用）。"""
        self._schedule.off_job_status(self._on_os_job_status)

    async def handle_incoming(self, message: dict[str, Any]) -> None:
        """喂入一条 panel 上行报文，按 action 分发。非对象或未知 action 忽略。"""
        if not isinstance(message, dict):
            logger.debug("[workflow_ws] 丢弃非对象报文: %r", message)
            return
        action = message.get("action", "")
        if action == FETCH_GRAPH:
            await self._on_fetch_graph()
        elif action == RUN_WORKFLOW:
            await self._on_run_workflow()
        elif action == STOP_WORKFLOW:
            await self._on_stop_workflow(message.get("data", ""))
        else:
            logger.debug("[workflow_ws] 忽略上行 action=%s", action)

    async def _on_fetch_graph(self) -> None:
        """Return the current Runtime-owned graph as a legacy render view."""
        graph = _current_legacy_graph(self._runtime_service)
        await self._send({"code": 0, "data": {"action": FETCH_GRAPH, "data": graph}})
        logger.info("[workflow_ws] 已回 fetch_graph（uuid=%s）", self.uuid or "-")

    async def _on_run_workflow(self) -> dict[str, str]:
        """Run the exact current Canonical revision through RuntimeService."""
        workflow = self._runtime_service.get_workflow()
        revision = workflow.get("revision") if isinstance(workflow, dict) else None
        canonical = revision.get("canonical") if isinstance(revision, dict) else None
        if not isinstance(canonical, dict):
            raise RuntimeError("Runtime workflow projection is missing canonical")
        accepted = await self._runtime_service.start_run(
            {
                "source": {
                    "format": "canonical_workflow_v2",
                    "payload": canonical,
                }
            }
        )
        self._task_id = str(accepted["id"])
        await self._send(
            {"code": 0, "data": {"action": RUN_WORKFLOW, "data": self._task_id}}
        )
        logger.info("[workflow_ws] 已下发 run_workflow task_id=%s", self._task_id)
        return accepted

    async def _on_stop_workflow(self, task_id_data: Any) -> None:
        """stop_workflow→RuntimeService.cancel_run，并回 stop_workflow 确认。"""
        task_id = task_id_data if isinstance(task_id_data, str) and task_id_data else self._task_id
        if task_id:
            await self._runtime_service.cancel_run(task_id)
        await self._send({"code": 0, "data": {"action": STOP_WORKFLOW}})
        logger.info("[workflow_ws] 已下发 stop_workflow task_id=%s", task_id or "-")

    async def _on_os_job_status(self, data: dict[str, Any]) -> None:
        """OS job_status 回流回调：翻译成 workflow_update 推 panel（仅本会话 task_id）。"""
        if not self._task_id or data.get("task_id", "") != self._task_id:
            return
        run = self._schedule.get_run(self._task_id)
        finished = run.finished if run is not None else False
        await self._send(translate_job_status_to_update(data, finished=finished))


class WorkflowWSServer:
    """实现 A UI 面 WS 服务器：在 /ws/workflow/{uuid} accept 云端 panel 连接。

    每条连接经注入的 get_schedule_session 拿到已就绪 ScheduleSession 建 WorkflowSession；
    真实传输接线：逐条 json.loads 喂 handle_incoming，send 经 json.dumps(ensure_ascii=False)。
    延迟 import websockets（未装不拖累其余桥面）。
    """

    def __init__(
        self,
        get_schedule_session: Callable[[], ScheduleSession | None],
        host: str = "127.0.0.1",
        port: int = 8891,
        *,
        get_runtime_service: Callable[[], Any | None] | None = None,
    ) -> None:
        self._get_schedule = get_schedule_session
        self._get_runtime_service = get_runtime_service
        self.host = host
        self.port = port
        self._server: Any = None

    async def start(self) -> None:
        import websockets

        async def handler(websocket: Any) -> None:
            path = getattr(websocket, "path", "") or getattr(
                getattr(websocket, "request", None), "path", ""
            )
            if path and WORKFLOW_WS_PATH_PREFIX not in str(path):
                logger.warning("[workflow_ws] 拒绝非法路径连接: %s", path)
                await websocket.close(code=1008, reason="unexpected path")
                return
            await self._serve_connection(websocket, str(path))

        self._server = await websockets.serve(handler, self.host, self.port)
        logger.info(
            "[workflow_ws] 实现 A UI 面 WS 已监听 ws://%s:%d%s{uuid}",
            self.host,
            self.port,
            WORKFLOW_WS_PATH_PREFIX,
        )

    async def _serve_connection(self, websocket: Any, path: str) -> None:
        schedule = self._get_schedule()
        if schedule is None:
            logger.warning("[workflow_ws] OS 未连入，拒绝 panel 连接")
            await websocket.close(code=1011, reason="schedule session not ready")
            return
        uuid = _extract_uuid(path)

        async def send(msg: dict[str, Any]) -> None:
            await websocket.send(json.dumps(msg, ensure_ascii=False))

        runtime_service = (
            self._get_runtime_service()
            if self._get_runtime_service is not None
            else None
        )
        session = WorkflowSession(
            send,
            schedule,
            uuid=uuid,
            runtime_service=runtime_service,
        )
        logger.info("[workflow_ws] panel 已连入 (uuid=%s)", uuid or "-")
        try:
            async for raw in websocket:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    logger.error("[workflow_ws] 收到非法 JSON: %s", raw)
                    continue
                await session.handle_incoming(message)
        finally:
            session.close()
            logger.info("[workflow_ws] panel 连接断开 (uuid=%s)", uuid or "-")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("[workflow_ws] 实现 A UI 面 WS 已停止")


def _extract_uuid(path: str) -> str:
    """从 /ws/workflow/{uuid}[?query] 取 uuid。"""
    if WORKFLOW_WS_PATH_PREFIX not in path:
        return ""
    tail = path.split(WORKFLOW_WS_PATH_PREFIX, 1)[1]
    return tail.split("?", 1)[0].strip("/")
