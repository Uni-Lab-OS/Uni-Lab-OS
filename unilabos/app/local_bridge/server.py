"""server — 本地工作流桥组合入口（单 asyncio event loop 起双面 + 可选离线执行核）。

把桥的两个服务器组合在一个事件循环里：
- schedule_ws.ScheduleWSServer(:8890)——OS 面 WS，真实 OS 的 ws_client 连入。
- local_api.LocalApiServer(:8014)——统一前端使用的 HTTP/WS v1。

两档执行模式（见 interface-design.md §四）：
- 真实下发（默认）：ScheduleWSServer 等真实 OS 连入建 ScheduleSession，统一 UI 面
  经此 session 把整张 DAG 下发 OS、收真实 job_status 与物料快照回流。单一事实源在 OS。
- 离线自足（--offline）：无真实 OS 时，用 offline_os.OfflineOS 在进程内顶替 OS 面——
  同一 ScheduleSession 的 send 接到 OfflineOS.receive，OfflineOS 用 F002 DagExecutor
  走同一张 TaskDag、每设备锁保 I3、逐节点回发 job_status，UI 面因而无 OS 也能完整动。

UI 面经 get_schedule_session / get_local_api_state 解析「当前就绪 session」——真实模式由
OS 连入时的 on_session 回调注入并据此建唯一 LocalApiState；离线模式启动即注入。
build_offline_session 为纯装配（无网络），便于 hermetic 测。

python -m unilabos.app.local_bridge.server [--offline] 独立起桥，不改动既有 unilab 启动路径。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import uuid
from pathlib import Path

from unilabos.app.local_bridge.bind_security import require_loopback_runtime_host
from unilabos.app.local_bridge.local_api import LocalApiServer, LocalApiState
from unilabos.app.local_bridge.material_api import MaterialGraphCatalog
from unilabos.app.local_bridge.material_models import MaterialModelRegistry
from unilabos.app.local_bridge.offline_os import OfflineOS
from unilabos.app.local_bridge.resource_template_api import (
    ResourceTemplateProxy,
)
from unilabos.app.local_bridge.runtime_action_api import (
    RuntimeActionCatalogProxy,
    RuntimeActionCatalogProxyError,
)
from unilabos.app.local_bridge.schedule_ws import ScheduleSession, ScheduleWSServer
from unilabos.runtime.event_store import SQLiteEventJournal
from unilabos.runtime.paths import default_runtime_db_path
from unilabos.runtime.profile_loader import LoadedProfile, load_profiles
from unilabos.runtime.workflow_store import WorkflowDocumentStore
from unilabos.scheduler.dag_model import NodeState
from unilabos.scheduler.resource_lock import ResourceLockManager
from unilabos.workflow.source_library import (
    WorkflowSourceLibrary,
    parse_workflow_library,
)

logger = logging.getLogger(__name__)

# 双面默认端口
DEFAULT_SCHEDULE_PORT = 8890
DEFAULT_API_PORT = 8014


def build_offline_session(
    results: dict[str, NodeState] | None = None,
    *,
    resource_lock_manager: ResourceLockManager | None = None,
    journal: SQLiteEventJournal | None = None,
    node_delay_seconds: float = 0.0,
) -> tuple[ScheduleSession, OfflineOS]:
    """装配离线执行核：ScheduleSession(send→OfflineOS.receive) + OfflineOS.bind(session)。

    返回 (session, offline)——session 的行为与真实 OS 连入时建立的完全一致（下发 task_dag、
    收 job_status），只是对端换成进程内 OfflineOS。纯装配无网络，供离线模式与 hermetic 测复用。
    """
    offline = OfflineOS(
        results=results,
        resource_lock_manager=resource_lock_manager,
        journal=journal,
        node_delay_seconds=node_delay_seconds,
    )
    session = ScheduleSession(offline.receive, session_id="offline")
    offline.bind(session)
    return session, offline


class LocalBridgeServer:
    """组合双面服务器 + 管理「当前就绪 ScheduleSession / LocalApiState」。

    - 真实模式：ScheduleWSServer 于 OS 连入时经 on_session 注入 session，据此建唯一 LocalApiState。
    - 离线模式：构造即经 build_offline_session 装配 session 并建 LocalApiState，OS 面 WS 仍监听
      （允许真实 OS 之后接管，但离线 session 已足以驱动 UI）。
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        schedule_port: int = DEFAULT_SCHEDULE_PORT,
        api_port: int = DEFAULT_API_PORT,
        offline: bool = False,
        journal_path: str | Path | None = None,
        profiles: dict[str, LoadedProfile] | None = None,
        graph_path: str | Path | None = None,
        workflow_libraries: list[tuple[str, str | Path]] | None = None,
        offline_node_delay: float = 0.0,
        execution_http_url: str = "http://127.0.0.1:8002",
        internal_api_token: str | None = None,
        runtime_action_proxy: RuntimeActionCatalogProxy | None = None,
    ) -> None:
        require_loopback_runtime_host(host)
        if graph_path is not None and not offline:
            raise ValueError(
                "--graph belongs to the execution OS; only --offline bridge "
                "may load it directly"
            )
        if offline_node_delay and not offline:
            raise ValueError("--offline-node-delay requires --offline")
        self.host = host
        self.offline = offline
        self._session: ScheduleSession | None = None
        self._local_api_state: LocalApiState | None = None
        self._offline_os: OfflineOS | None = None
        self._journal_path = Path(journal_path) if journal_path is not None else None
        self._profiles = dict(profiles or {})
        self._workflow_source_library = (
            WorkflowSourceLibrary(workflow_libraries)
            if workflow_libraries
            else None
        )
        # 模型是 OS 本地能力，不依赖本次是否选择了 Material Graph。
        # 在桥启动时一次性登记并校验，确保 Electron 随后可直接加载。
        self._material_model_registry = MaterialModelRegistry()
        self._material_catalog = MaterialGraphCatalog(
            graph_path if offline else None,
            model_registry=self._material_model_registry,
        )
        self._runtime_epoch = uuid.uuid4().hex
        self._resource_lock_manager = ResourceLockManager(
            runtime_epoch=self._runtime_epoch
        )
        self._journal = (
            SQLiteEventJournal(
                self._journal_path,
                runtime_epoch=self._runtime_epoch,
            )
            if self._journal_path is not None
            else None
        )
        workflow_root = (
            self._journal_path.parent / "workflows"
            if self._journal_path is not None
            else None
        )
        self._workflow_store = WorkflowDocumentStore(workflow_root)
        self._resource_template_proxy = ResourceTemplateProxy(
            execution_http_url,
            internal_token=internal_api_token,
        )
        self._runtime_action_proxy = (
            runtime_action_proxy
            or RuntimeActionCatalogProxy(
                execution_http_url,
                internal_token=internal_api_token,
            )
        )
        self._runtime_action_sync_task: asyncio.Task[None] | None = None

        if offline:
            self._session, self._offline_os = build_offline_session(
                resource_lock_manager=self._resource_lock_manager,
                journal=self._journal,
                node_delay_seconds=offline_node_delay,
            )
            self._local_api_state = self._build_local_api_state(self._session)
            logger.info("[bridge] 离线模式：进程内 OfflineOS 顶替 OS 面")

        self._schedule_server = ScheduleWSServer(host=host, port=schedule_port)
        self._schedule_server.on_session(self._adopt_session)
        self._api_server = LocalApiServer(
            self._get_local_api_state,
            host=host,
            port=api_port,
            resource_template_proxy=self._resource_template_proxy,
        )

    def _adopt_session(self, session: ScheduleSession) -> None:
        """OS 连入（真实模式）：接管为当前 session 并据此建唯一 LocalApiState。"""
        self._cancel_runtime_action_sync()
        self._session = session
        self._local_api_state = self._build_local_api_state(session)
        if session.session_id != "offline":
            # 不把动作目录同步完全押在单次 host_node_ready 消息上。OS 的 WS
            # 与内部 HTTP 服务并行启动时，HTTP 可能短暂晚于 WS 就绪；连接后立即
            # 尝试并在可恢复错误上退避重试，避免 UI 已连接但目录永久为空。
            self._schedule_runtime_action_sync(
                session,
                self._local_api_state,
            )
        logger.info("[bridge] 已接管 OS 连入的调度会话，UI 面就绪")

    def _build_local_api_state(self, session: ScheduleSession) -> LocalApiState:
        # LocalApiState only projects the OS-owned journal for the shared UI.
        session.on_material_snapshot(
            self._material_catalog.replace_snapshot
        )
        state = LocalApiState(
            session,
            journal=self._journal,
            action_catalog=None if session.session_id == "offline" else {},
            profiles=self._profiles,
            resource_lock_manager=self._resource_lock_manager,
            workflow_store=self._workflow_store,
            material_catalog=self._material_catalog,
            material_model_registry=self._material_model_registry,
            material_refresh=(
                None
                if session.session_id == "offline"
                else session.request_material_snapshot
            ),
            workflow_source_resolver=(
                None
                if self._workflow_source_library is None
                else self._workflow_source_library.resolver
            ),
        )
        if session.session_id != "offline":
            state.mark_runtime_action_catalog_unavailable(
                "等待 Edge Runtime 动作目录"
            )

            async def refresh_runtime_actions(_data: dict[str, object]) -> None:
                # host_node_ready 是一次明确的目录边界：取消连接阶段的旧重试，
                # 立即强制刷新。若此刻 HTTP 仍未就绪，后台继续重试而不阻塞 WS。
                self._cancel_runtime_action_sync()
                retryable = await self._refresh_runtime_action_catalog(
                    session,
                    state,
                )
                if retryable:
                    self._schedule_runtime_action_sync(
                        session,
                        state,
                        initial_delay=0.25,
                    )

            session.on_host_ready(refresh_runtime_actions)
            session.on_runtime_actions_changed(refresh_runtime_actions)
        return state

    async def _refresh_runtime_action_catalog(
        self,
        session: ScheduleSession,
        state: LocalApiState,
        *,
        background: bool = False,
    ) -> bool:
        """刷新一次动作目录；返回失败是否可重试。"""

        if self._session is not session or self._local_api_state is not state:
            return False
        try:
            actions, revision = await self._runtime_action_proxy.fetch(force=True)
        except RuntimeActionCatalogProxyError as exc:
            if self._session is not session or self._local_api_state is not state:
                return False
            state.mark_runtime_action_catalog_unavailable(str(exc))
            log = logger.debug if background else logger.error
            log(
                "[bridge] Runtime 动作目录同步失败 (%s, retryable=%s): %s",
                exc.code,
                exc.retryable,
                exc,
            )
            return exc.retryable
        if self._session is not session or self._local_api_state is not state:
            return False
        current = state.runtime_actions()
        if (
            current["available"] is True
            and current["revision"] == revision
        ):
            return False
        state.replace_runtime_action_catalog(
            actions,
            revision=revision,
        )
        logger.info(
            "[bridge] Runtime 动作目录已同步：%d actions，revision=%s",
            len(actions),
            revision[:12],
        )
        return False

    def _schedule_runtime_action_sync(
        self,
        session: ScheduleSession,
        state: LocalApiState,
        *,
        initial_delay: float = 0.0,
    ) -> None:
        """后台同步动作目录；仅对可恢复错误做有上限的指数退避。"""

        existing = self._runtime_action_sync_task
        if existing is not None and not existing.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 仅白盒同步装配测试会在事件循环外调用 _adopt_session；真实服务器
            # 的 on_session 始终运行在 Schedule WS 的事件循环中。
            return

        async def sync_until_ready() -> None:
            delay = initial_delay
            while self._session is session and self._local_api_state is state:
                if delay > 0:
                    await asyncio.sleep(delay)
                retryable = await self._refresh_runtime_action_catalog(
                    session,
                    state,
                    background=True,
                )
                if not retryable:
                    return
                delay = min(10.0, max(0.25, delay * 2))

        task = loop.create_task(
            sync_until_ready(),
            name=f"runtime-action-sync:{session.session_id}",
        )
        self._runtime_action_sync_task = task

        def clear_completed(completed: asyncio.Task[None]) -> None:
            if self._runtime_action_sync_task is completed:
                self._runtime_action_sync_task = None

        task.add_done_callback(clear_completed)

    def _cancel_runtime_action_sync(self) -> None:
        task = self._runtime_action_sync_task
        self._runtime_action_sync_task = None
        if task is not None and not task.done():
            task.cancel()

    def _get_schedule_session(self) -> ScheduleSession | None:
        return self._session

    def _get_local_api_state(self) -> LocalApiState | None:
        return self._local_api_state

    async def start(self) -> None:
        """并起 schedule 与 unified API 两面并常驻。"""
        await asyncio.gather(
            self._schedule_server.start(),
            self._api_server.start(),
        )

    async def stop(self) -> None:
        self._cancel_runtime_action_sync()
        await asyncio.gather(
            self._schedule_server.stop(),
            self._api_server.stop(),
            return_exceptions=True,
        )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Uni-Lab 本地工作流桥（替代 Go 后端）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--schedule-port", type=int, default=DEFAULT_SCHEDULE_PORT, help="OS 面 WS 端口")
    parser.add_argument("--api-port", type=int, default=DEFAULT_API_PORT, help="统一 HTTP/WS API 端口")
    parser.add_argument(
        "--execution-http-url",
        default="http://127.0.0.1:8002",
        help=(
            "OS Registry 内部 HTTP 地址；显式配置，不根据统一 API 端口推算"
        ),
    )
    parser.add_argument(
        "--internal-api-token",
        default=os.environ.get("UNILABOS_INTERNAL_API_TOKEN"),
        help=(
            "OS 内部 API token；默认读取 UNILABOS_INTERNAL_API_TOKEN"
        ),
    )
    parser.add_argument(
        "--journal-path",
        default=str(default_runtime_db_path()),
        help="Quick Debug SQLite journal 路径",
    )
    parser.add_argument(
        "--profile",
        action="append",
        default=[],
        help="声明式 Profile package.yaml 路径；可重复传入",
    )
    parser.add_argument(
        "--workflow-library",
        action="append",
        default=[],
        metavar="PYTHON_MODULE=SOURCE_ROOT",
        help=(
            "允许 AST 编译器静态解析的工作流函数库；可重复传入，"
            "不会导入或执行其中的 Python"
        ),
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="离线自足模式：进程内 OfflineOS 顶替 OS 面（无真实 OS 亦可驱动 UI）",
    )
    parser.add_argument(
        "--offline-node-delay",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help=(
            "仅 --offline：每个模拟设备节点的非阻塞执行时长；"
            "用于演示和测试可观测的 running/pause_pending 状态"
        ),
    )
    parser.add_argument(
        "-g",
        "--graph",
        help=(
            "仅 --offline 使用的设备图；真实模式由 unilab -g 加载并通过 "
            "schedule 通道发布当前内存物料快照"
        ),
    )
    return parser.parse_args(argv)


async def _amain(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    profiles = load_profiles(args.profile) if args.profile else {}
    workflow_libraries = [
        parse_workflow_library(value)
        for value in args.workflow_library
    ]
    server = LocalBridgeServer(
        host=args.host,
        schedule_port=args.schedule_port,
        api_port=args.api_port,
        offline=args.offline,
        journal_path=args.journal_path,
        profiles=profiles,
        graph_path=args.graph,
        workflow_libraries=workflow_libraries,
        offline_node_delay=args.offline_node_delay,
        execution_http_url=args.execution_http_url,
        internal_api_token=args.internal_api_token,
    )
    logger.info(
        "[bridge] 启动：schedule=ws://%s:%d /api/v1/ws/schedule | api=http://%s:%d/api",
        args.host,
        args.schedule_port,
        args.host,
        args.api_port,
    )
    try:
        await server.start()
    finally:
        await server.stop()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(_amain(argv))
    except KeyboardInterrupt:
        logger.info("[bridge] 收到中断，退出")


if __name__ == "__main__":
    main()
