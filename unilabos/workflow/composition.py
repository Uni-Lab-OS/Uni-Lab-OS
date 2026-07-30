"""Process composition for the workspace-local Workflow authority."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from unilabos.workflow.service import AuthoringCompiler, WorkflowService
from unilabos.workflow.source_monitor import WorkflowSourceMonitor
from unilabos.workflow.store import WorkflowStore

_lock = threading.Lock()
_service: Optional[WorkflowService] = None
_database_path: Optional[Path] = None
_monitor: Optional[WorkflowSourceMonitor] = None


def compose_workflow_runtime(
    working_dir: str | Path,
    *,
    compiler: Optional[AuthoringCompiler] = None,
) -> WorkflowService:
    """装配工作区唯一的 Workflow authority、启动恢复和 Draft 监视。"""

    global _database_path, _monitor, _service
    database_path = Path(working_dir).resolve() / "workflow.db"
    with _lock:
        if _service is not None:
            if database_path != _database_path:
                raise RuntimeError(
                    "Workflow authority cannot switch working_dir at runtime"
                )
            return _service
        _service = WorkflowService(
            WorkflowStore(database_path),
            compiler=compiler,
        )
        _database_path = database_path
        for registration in _service.store.list_source_registrations():
            try:
                _service.reconcile_registered_source(
                    registration["workflow_uuid"]
                )
            except (OSError, RuntimeError):
                # 一个损坏或暂不可编译的 Draft 不阻塞进程启动。
                continue
        _monitor = WorkflowSourceMonitor(_service)
        _monitor.start()
        return _service


def setup_workflow_service(
    working_dir: str | Path,
    *,
    compiler: Optional[AuthoringCompiler] = None,
) -> WorkflowService:
    """兼容旧装配调用；所有入口统一进入完整运行时组合。"""

    return compose_workflow_runtime(working_dir, compiler=compiler)


def get_workflow_service() -> Optional[WorkflowService]:
    return _service


def reset_workflow_service_for_test() -> None:
    """停止监视器并关闭测试使用的进程级单例。"""

    global _database_path, _monitor, _service
    with _lock:
        if _monitor is not None:
            _monitor.stop()
        if _service is not None:
            _service.store.close()
        _monitor = None
        _service = None
        _database_path = None


__all__ = [
    "compose_workflow_runtime",
    "get_workflow_service",
    "reset_workflow_service_for_test",
    "setup_workflow_service",
]
