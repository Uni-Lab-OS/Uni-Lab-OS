"""工作区本地 Workflow Authority 的进程级组合根。"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import threading
from pathlib import Path
from typing import Optional

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.catalog import CatalogAuthority, TemplateCatalog
from unilabos.workflow.service import AuthoringCompiler, WorkflowService
from unilabos.workflow.source_monitor import WorkflowSourceMonitor
from unilabos.workflow.store import WorkflowStore

_lock = threading.Lock()
_service: Optional[WorkflowService] = None
_database_path: Optional[Path] = None
_monitor: Optional[WorkflowSourceMonitor] = None
_owner_pid: Optional[int] = None
_workspace_lease_fd: Optional[int] = None
_LOCAL_CATALOG_AUTHORITY = CatalogAuthority(
    authority_id="os-local",
    kind="local",
)


def _acquire_workspace_lease(working_dir: Path) -> int:
    """在打开数据库前取得工作区唯一 OS Authority 的进程锁。"""

    working_dir.mkdir(parents=True, exist_ok=True)
    lease_path = working_dir / ".workflow-authority.lock"
    descriptor = os.open(
        lease_path,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError("Workflow Authority 租约路径不是普通文件")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            raise RuntimeError(
                "当前工作区已由另一个 OS Workflow Authority 占用"
            ) from None
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _release_workspace_lease(
    descriptor: Optional[int],
    *,
    unlock: bool = True,
) -> None:
    if descriptor is None:
        return
    try:
        if unlock:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def compose_workflow_runtime(
    working_dir: str | Path,
    *,
    compiler: Optional[AuthoringCompiler] = None,
) -> WorkflowService:
    """装配工作区唯一的 Workflow authority、启动恢复和 Draft 监视。"""

    global _database_path, _monitor, _owner_pid, _service, _workspace_lease_fd
    resolved_working_dir = Path(working_dir).resolve()
    database_path = resolved_working_dir / "workflow.db"
    with _lock:
        if _service is not None:
            if _owner_pid != os.getpid():
                raise RuntimeError("当前工作区已由另一个 OS Workflow Authority 占用")
            if database_path != _database_path:
                raise RuntimeError(
                    "Workflow authority cannot switch working_dir at runtime"
                )
            return _service
        lease_descriptor = _acquire_workspace_lease(resolved_working_dir)
        new_store: Optional[WorkflowStore] = None
        new_service: Optional[WorkflowService] = None
        new_monitor: Optional[WorkflowSourceMonitor] = None
        try:
            new_store = WorkflowStore(database_path)
            effective_compiler = compiler
            if effective_compiler is None:
                effective_compiler = WorkflowAuthoringEngine(
                    catalog=TemplateCatalog(new_store),
                    authority=_LOCAL_CATALOG_AUTHORITY,
                )
            new_service = WorkflowService(
                new_store,
                compiler=effective_compiler,
            )
            new_monitor = WorkflowSourceMonitor(new_service)
            _service = new_service
            _database_path = database_path
            _monitor = new_monitor
            _owner_pid = os.getpid()
            _workspace_lease_fd = lease_descriptor
            new_service.recover_registered_sources()
            new_monitor.start()
        except BaseException:
            try:
                if new_monitor is not None:
                    new_monitor.stop()
            finally:
                try:
                    if new_service is not None:
                        new_service.close()
                    elif new_store is not None:
                        new_store.close()
                finally:
                    _service = None
                    _database_path = None
                    _monitor = None
                    _owner_pid = None
                    _workspace_lease_fd = None
                    _release_workspace_lease(lease_descriptor)
            raise

        return new_service


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

    global _database_path, _monitor, _owner_pid, _service, _workspace_lease_fd
    with _lock:
        lease_owned = _owner_pid == os.getpid()
        if _monitor is not None:
            # 监视线程未退出时必须保留 Service 与租约，允许稍后重试停机。
            _monitor.stop()
        if _service is not None:
            # Store 未确认关闭时同样保留组合根与租约，避免第二 Authority 进入。
            _service.close()
        _monitor = None
        _service = None
        _database_path = None
        _owner_pid = None
        lease_descriptor = _workspace_lease_fd
        _workspace_lease_fd = None
        _release_workspace_lease(
            lease_descriptor,
            unlock=lease_owned,
        )


__all__ = [
    "compose_workflow_runtime",
    "get_workflow_service",
    "reset_workflow_service_for_test",
    "setup_workflow_service",
]
