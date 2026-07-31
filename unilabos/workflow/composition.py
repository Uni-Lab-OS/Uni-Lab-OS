"""工作区本地 Workflow Authority 的进程级组合根。"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import threading
from collections.abc import Iterable
from pathlib import Path

from unilabos.workflow.service import (
    AuthoringCompiler,
    WorkflowConflict,
    WorkflowError,
    WorkflowService,
)
from unilabos.workflow.source_discovery import (
    EditablePackageManifest,
    load_editable_package_manifest,
)
from unilabos.workflow.source_monitor import WorkflowSourceMonitor
from unilabos.workflow.store import WorkflowStore

_lock = threading.Lock()
_service: WorkflowService | None = None
_database_path: Path | None = None
_monitor: WorkflowSourceMonitor | None = None
_owner_pid: int | None = None
_workspace_lease_fd: int | None = None
_compiler: AuthoringCompiler | None = None
_editable_package_roots: tuple[Path, ...] = ()


def _configured_package_roots(
    roots: Iterable[str | Path],
) -> tuple[Path, ...]:
    return tuple(Path(os.path.abspath(root)) for root in roots)


def _register_manifests(
    service: WorkflowService,
    manifests: tuple[EditablePackageManifest, ...],
) -> None:
    """在写入任一注册前关闭跨 manifest 和既有注册身份。"""

    workflow_ids: set[str] = set()
    physical_sources: set[tuple[Path, str]] = set()
    source_uris: set[tuple[str, str]] = set()
    declarations: list[tuple[EditablePackageManifest, str, str]] = []
    for manifest in manifests:
        for declaration in manifest.workflows:
            physical = (manifest.package_root, declaration.relative_path)
            source_uri = (manifest.package_id, declaration.relative_path)
            if (
                declaration.workflow_uuid in workflow_ids
                or physical in physical_sources
                or source_uri in source_uris
            ):
                raise WorkflowConflict("invalid_input")
            workflow_ids.add(declaration.workflow_uuid)
            physical_sources.add(physical)
            source_uris.add(source_uri)
            declarations.append(
                (
                    manifest,
                    declaration.workflow_uuid,
                    declaration.relative_path,
                )
            )

    existing = {
        item["workflow_uuid"]: item for item in service.list_registered_sources()
    }
    for manifest, workflow_uuid, relative_path in declarations:
        try:
            service.get_workflow(workflow_uuid)
        except WorkflowError as error:
            if error.code == "not_found":
                raise WorkflowError("workflow_not_found") from None
            raise
        current = existing.get(workflow_uuid)
        if current is not None and (
            current["package_id"] != manifest.package_id
            or Path(current["package_root"]) != manifest.package_root
            or current["relative_path"] != relative_path
        ):
            raise WorkflowConflict("invalid_input")

    for manifest, workflow_uuid, relative_path in declarations:
        service.register_editable_source(
            workflow_uuid=workflow_uuid,
            package_id=manifest.package_id,
            package_root=manifest.package_root,
            relative_path=relative_path,
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
    descriptor: int | None,
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
    compiler: AuthoringCompiler | None = None,
    editable_package_roots: Iterable[str | Path] = (),
) -> WorkflowService:
    """装配工作区唯一的 Workflow authority、启动恢复和 Draft 监视。"""

    global _compiler, _database_path, _editable_package_roots, _monitor
    global _owner_pid, _service, _workspace_lease_fd
    resolved_working_dir = Path(working_dir).resolve()
    database_path = resolved_working_dir / "workflow.db"
    configured_roots = _configured_package_roots(editable_package_roots)
    with _lock:
        if _service is not None:
            if _owner_pid != os.getpid():
                raise RuntimeError("当前工作区已由另一个 OS Workflow Authority 占用")
            if database_path != _database_path:
                raise RuntimeError(
                    "Workflow authority cannot switch working_dir at runtime"
                )
            if compiler is not _compiler:
                raise RuntimeError(
                    "Workflow authority cannot switch compiler at runtime"
                )
            if configured_roots != _editable_package_roots:
                raise RuntimeError(
                    "Workflow authority cannot switch editable packages at runtime"
                )
            return _service
        lease_descriptor = _acquire_workspace_lease(resolved_working_dir)
        new_service: WorkflowService | None = None
        new_monitor: WorkflowSourceMonitor | None = None
        try:
            new_service = WorkflowService(
                WorkflowStore(database_path),
                compiler=compiler,
            )
            manifests = tuple(
                load_editable_package_manifest(root) for root in configured_roots
            )
            _register_manifests(new_service, manifests)
            new_monitor = WorkflowSourceMonitor(new_service)
            _service = new_service
            _database_path = database_path
            _compiler = compiler
            _editable_package_roots = configured_roots
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
                finally:
                    _service = None
                    _database_path = None
                    _compiler = None
                    _editable_package_roots = ()
                    _monitor = None
                    _owner_pid = None
                    _workspace_lease_fd = None
                    _release_workspace_lease(lease_descriptor)
            raise

        return new_service


def setup_workflow_service(
    working_dir: str | Path,
    *,
    compiler: AuthoringCompiler | None = None,
    editable_package_roots: Iterable[str | Path] = (),
) -> WorkflowService:
    """兼容旧装配调用；所有入口统一进入完整运行时组合。"""

    return compose_workflow_runtime(
        working_dir,
        compiler=compiler,
        editable_package_roots=editable_package_roots,
    )


def get_workflow_service() -> WorkflowService | None:
    return _service


def reset_workflow_service_for_test() -> None:
    """停止监视器并关闭测试使用的进程级单例。"""

    global _compiler, _database_path, _editable_package_roots, _monitor
    global _owner_pid, _service, _workspace_lease_fd
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
        _compiler = None
        _editable_package_roots = ()
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
