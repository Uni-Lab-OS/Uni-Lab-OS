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
import uuid
from pathlib import Path

from unilabos.app.local_bridge.bind_security import require_loopback_runtime_host
from unilabos.app.local_bridge.local_api import LocalApiServer, LocalApiState
from unilabos.app.local_bridge.material_api import MaterialGraphCatalog
from unilabos.app.local_bridge.material_models import MaterialModelRegistry
from unilabos.app.local_bridge.offline_os import OfflineOS
from unilabos.app.local_bridge.schedule_ws import ScheduleSession, ScheduleWSServer
from unilabos.scheduler.dag_model import NodeState
from unilabos.scheduler.resource_lock import ResourceLockManager
from unilabos.runtime.event_store import SQLiteEventJournal
from unilabos.runtime.paths import default_runtime_db_path
from unilabos.runtime.profile_loader import LoadedProfile, load_profiles
from unilabos.runtime.workflow_store import WorkflowDocumentStore
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
) -> tuple[ScheduleSession, OfflineOS]:
    """装配离线执行核：ScheduleSession(send→OfflineOS.receive) + OfflineOS.bind(session)。

    返回 (session, offline)——session 的行为与真实 OS 连入时建立的完全一致（下发 task_dag、
    收 job_status），只是对端换成进程内 OfflineOS。纯装配无网络，供离线模式与 hermetic 测复用。
    """
    offline = OfflineOS(
        results=results,
        resource_lock_manager=resource_lock_manager,
        journal=journal,
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
    ) -> None:
        require_loopback_runtime_host(host)
        if graph_path is not None and not offline:
            raise ValueError(
                "--graph belongs to the execution OS; only --offline bridge "
                "may load it directly"
            )
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

        if offline:
            self._session, self._offline_os = build_offline_session(
                resource_lock_manager=self._resource_lock_manager,
                journal=self._journal,
            )
            self._local_api_state = self._build_local_api_state(self._session)
            logger.info("[bridge] 离线模式：进程内 OfflineOS 顶替 OS 面")

        self._schedule_server = ScheduleWSServer(host=host, port=schedule_port)
        self._schedule_server.on_session(self._adopt_session)
        self._api_server = LocalApiServer(
            self._get_local_api_state, host=host, port=api_port
        )

    def _adopt_session(self, session: ScheduleSession) -> None:
        """OS 连入（真实模式）：接管为当前 session 并据此建唯一 LocalApiState。"""
        self._session = session
        self._local_api_state = self._build_local_api_state(session)
        logger.info("[bridge] 已接管 OS 连入的调度会话，UI 面就绪")

    def _build_local_api_state(self, session: ScheduleSession) -> LocalApiState:
        # LocalApiState only projects the OS-owned journal for the shared UI.
        session.on_material_snapshot(
            self._material_catalog.replace_snapshot
        )
        return LocalApiState(
            session,
            journal=self._journal,
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
