"""工作区本地 Workflow Authority 的进程级组合根。"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import threading
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from unilabos.resources.authority import MaterialModule
from unilabos.resources.authority.sqlite import SQLiteMaterialAdapter
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.catalog import (
    CatalogAuthority,
    LocalResourceTemplateIdentityResolver,
    TemplateCatalog,
)
from unilabos.workflow.material_resolver import MaterialResourceSlotResolver
from unilabos.workflow.runtime import (
    WorkflowRuntimeCoordinator,
    WorkflowRuntimeWorker,
)
from unilabos.workflow.service import AuthoringCompiler, WorkflowService
from unilabos.workflow.source_discovery import register_editable_package_sources
from unilabos.workflow.source_monitor import WorkflowSourceMonitor
from unilabos.workflow.store import WorkflowStore

_lock = threading.Lock()
_service: WorkflowService | None = None
_startup_store: WorkflowStore | None = None
_database_path: Path | None = None
_monitor: WorkflowSourceMonitor | None = None
_runtime_worker: WorkflowRuntimeWorker | None = None
_owner_pid: int | None = None
_workspace_lease_fd: int | None = None
_compiler: AuthoringCompiler | None = None
_authority: CatalogAuthority | None = None
_editable_package_roots: tuple[Path, ...] = ()
_ready = False


def _configured_package_roots(
    roots: Iterable[str | Path],
) -> tuple[Path, ...]:
    return tuple(Path(os.path.abspath(root)) for root in roots)


def _retain_runtime(
    service: WorkflowService,
    monitor: WorkflowSourceMonitor | None,
    runtime_worker: WorkflowRuntimeWorker | None,
    *,
    database_path: Path,
    compiler: AuthoringCompiler | None,
    authority: CatalogAuthority | None,
    editable_package_roots: tuple[Path, ...],
    owner_pid: int,
    lease_descriptor: int,
    ready: bool,
) -> None:
    """发布 ready Authority，或保留失败 cleanup 的独占 ownership。"""

    global _authority, _compiler, _database_path, _editable_package_roots
    global _monitor, _ready, _runtime_worker, _startup_store
    global _owner_pid, _service, _workspace_lease_fd
    _service = service
    _startup_store = None
    _database_path = database_path
    _compiler = compiler
    _authority = authority
    _editable_package_roots = editable_package_roots
    _monitor = monitor
    _runtime_worker = runtime_worker
    _owner_pid = owner_pid
    _workspace_lease_fd = lease_descriptor
    _ready = ready


def _retain_startup_store(
    store: WorkflowStore,
    *,
    database_path: Path,
    owner_pid: int,
    lease_descriptor: int,
) -> None:
    """保留 Service 构造前未确认关闭的 Store 与工作区租约。"""

    global _database_path, _owner_pid, _ready, _startup_store
    global _workspace_lease_fd
    _startup_store = store
    _database_path = database_path
    _owner_pid = owner_pid
    _workspace_lease_fd = lease_descriptor
    _ready = False


def _clear_runtime() -> None:
    """清除已确认关闭的进程内引用；lease 由调用方显式释放。"""

    global _authority, _compiler, _database_path, _editable_package_roots
    global _monitor, _ready, _runtime_worker, _startup_store
    global _owner_pid, _service, _workspace_lease_fd
    _service = None
    _startup_store = None
    _database_path = None
    _compiler = None
    _authority = None
    _editable_package_roots = ()
    _monitor = None
    _runtime_worker = None
    _owner_pid = None
    _workspace_lease_fd = None
    _ready = False


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
    authority: CatalogAuthority | None = None,
    editable_package_roots: Iterable[str | Path] = (),
    registry_snapshot: Mapping[str, object] | None = None,
    resource_template_identity_resolver: Callable[[str], str] | None = None,
) -> WorkflowService:
    """装配工作区唯一的 Workflow authority、启动恢复和 Draft 监视。"""

    if compiler is not None and authority is not None:
        raise ValueError("compiler 与 authority 只能选择一种生产组合方式")
    if authority is not None and not isinstance(authority, CatalogAuthority):
        raise TypeError("authority 必须是 CatalogAuthority")
    if authority is not None and authority.kind != "local":
        raise ValueError("persistent Workflow runtime 只支持 local Graph Authority")
    if registry_snapshot is None and resource_template_identity_resolver is not None:
        raise ValueError("ResourceTemplate resolver 缺少 Registry snapshot")
    if registry_snapshot is not None and authority is None:
        raise ValueError("Registry Catalog 发布需要显式 Graph Authority")
    if registry_snapshot is not None and compiler is not None:
        raise ValueError("Registry Catalog 发布不能使用外部 compiler")
    if (
        registry_snapshot is not None
        and resource_template_identity_resolver is None
        and authority is not None
        and authority.kind != "local"
    ):
        raise ValueError("Backend Registry Catalog 发布需要显式 identity resolver")
    resolved_working_dir = Path(working_dir).resolve()
    database_path = resolved_working_dir / "workflow.db"
    configured_roots = _configured_package_roots(editable_package_roots)
    with _lock:
        if _startup_store is not None:
            if _owner_pid != os.getpid():
                raise RuntimeError("当前工作区已由另一个 OS Workflow Authority 占用")
            raise RuntimeError(
                "Workflow authority startup cleanup must complete before retry"
            )
        if _service is not None:
            if _owner_pid != os.getpid():
                raise RuntimeError("当前工作区已由另一个 OS Workflow Authority 占用")
            if database_path != _database_path:
                raise RuntimeError(
                    "Workflow authority cannot switch working_dir at runtime"
                )
            if authority != _authority:
                raise RuntimeError(
                    "Workflow authority cannot switch graph authority at runtime"
                )
            if authority is None and compiler is not _compiler:
                raise RuntimeError(
                    "Workflow authority cannot switch compiler at runtime"
                )
            if configured_roots != _editable_package_roots:
                raise RuntimeError(
                    "Workflow authority cannot switch editable packages at runtime"
                )
            if not _ready:
                raise RuntimeError(
                    "Workflow authority startup cleanup must complete before retry"
                )
            return _service
        lease_descriptor = _acquire_workspace_lease(resolved_working_dir)
        new_service: WorkflowService | None = None
        new_store: WorkflowStore | None = None
        new_monitor: WorkflowSourceMonitor | None = None
        new_runtime_worker: WorkflowRuntimeWorker | None = None
        published = False
        try:
            store = WorkflowStore(database_path)
            new_store = store
            runtime_coordinator = WorkflowRuntimeCoordinator(store)
            runtime_coordinator.recover_startup()
            runtime_compiler = compiler
            if authority is not None:
                catalog = TemplateCatalog(store)
                if registry_snapshot is not None:
                    from unilabos.registry.catalog_consumer import (
                        workflow_template_imports_from_registry_snapshot,
                    )

                    identity_resolver = resource_template_identity_resolver
                    if identity_resolver is None:
                        identity_resolver = LocalResourceTemplateIdentityResolver(
                            store,
                            authority,
                        )
                    templates = workflow_template_imports_from_registry_snapshot(
                        registry_snapshot,
                        authority_id=authority.authority_id,
                        resource_template_identity_resolver=identity_resolver,
                    )
                    catalog.replace(authority, templates)
                runtime_compiler = WorkflowAuthoringEngine(
                    catalog=catalog,
                    authority=authority,
                )
            material_module = MaterialModule(
                SQLiteMaterialAdapter.from_runtime_authority(store),
                # concrete ResourceSlot 解析只读取持久 Material identity；
                # template discovery 不属于 M1C。
                resource_templates={},
            )
            new_service = WorkflowService(
                store,
                compiler=runtime_compiler,
                resource_resolver=MaterialResourceSlotResolver(material_module),
            )
            register_editable_package_sources(
                new_service,
                configured_roots,
            )
            new_service.recover_registered_sources()
            new_monitor = WorkflowSourceMonitor(new_service)
            new_runtime_worker = WorkflowRuntimeWorker(runtime_coordinator)
            # Reconciliation 已完成，可以读取一致 baseline；monitor 从空签名集启动，
            # 会捕获此发布点与线程启动之间发生的变化。
            _retain_runtime(
                new_service,
                new_monitor,
                new_runtime_worker,
                database_path=database_path,
                compiler=runtime_compiler,
                authority=authority,
                editable_package_roots=configured_roots,
                owner_pid=os.getpid(),
                lease_descriptor=lease_descriptor,
                ready=True,
            )
            published = True
            new_monitor.start()
            new_runtime_worker.start()
        except BaseException as startup_error:
            cleanup_error: BaseException | None = None
            if new_monitor is not None:
                try:
                    new_monitor.stop()
                except BaseException as error:  # noqa: BLE001 - 保留租约需捕获停机失败
                    cleanup_error = error
            if cleanup_error is None and new_runtime_worker is not None:
                try:
                    new_runtime_worker.stop()
                    new_runtime_worker.join(timeout=5)
                    if new_runtime_worker.is_alive():
                        raise RuntimeError("Workflow runtime worker 未能停止")
                except BaseException as error:  # noqa: BLE001 - 保留租约
                    cleanup_error = error
            if cleanup_error is None and new_service is not None:
                try:
                    new_service.close()
                except BaseException as error:  # noqa: BLE001 - 保留租约需捕获关闭失败
                    cleanup_error = error
            elif cleanup_error is None and new_store is not None:
                try:
                    new_store.close()
                except BaseException as error:  # noqa: BLE001 - 保留租约需捕获关闭失败
                    cleanup_error = error
            if cleanup_error is not None:
                if new_service is not None:
                    _retain_runtime(
                        new_service,
                        new_monitor,
                        new_runtime_worker,
                        database_path=database_path,
                        compiler=runtime_compiler,
                        authority=authority,
                        editable_package_roots=configured_roots,
                        owner_pid=os.getpid(),
                        lease_descriptor=lease_descriptor,
                        ready=False,
                    )
                elif new_store is not None:
                    _retain_startup_store(
                        new_store,
                        database_path=database_path,
                        owner_pid=os.getpid(),
                        lease_descriptor=lease_descriptor,
                    )
                raise startup_error from cleanup_error
            if published:
                _clear_runtime()
            _release_workspace_lease(lease_descriptor)
            raise

        return new_service


def setup_workflow_service(
    working_dir: str | Path,
    *,
    compiler: AuthoringCompiler | None = None,
    authority: CatalogAuthority | None = None,
    editable_package_roots: Iterable[str | Path] = (),
    registry_snapshot: Mapping[str, object] | None = None,
    resource_template_identity_resolver: Callable[[str], str] | None = None,
) -> WorkflowService:
    """兼容旧装配调用；所有入口统一进入完整运行时组合。"""

    return compose_workflow_runtime(
        working_dir,
        compiler=compiler,
        authority=authority,
        editable_package_roots=editable_package_roots,
        registry_snapshot=registry_snapshot,
        resource_template_identity_resolver=resource_template_identity_resolver,
    )


def get_workflow_service() -> WorkflowService | None:
    if not _ready or _owner_pid != os.getpid():
        return None
    return _service


def reset_workflow_service_for_test() -> None:
    """停止监视器并关闭测试使用的进程级单例。"""

    global _authority, _compiler, _database_path, _editable_package_roots
    global _monitor, _ready, _runtime_worker, _startup_store
    global _owner_pid, _service, _workspace_lease_fd
    with _lock:
        lease_owned = _owner_pid == os.getpid()
        if _monitor is not None:
            # 监视线程未退出时必须保留 Service 与租约，允许稍后重试停机。
            _monitor.stop()
        if _runtime_worker is not None:
            _runtime_worker.stop()
            _runtime_worker.join(timeout=5)
            if _runtime_worker.is_alive():
                raise RuntimeError("Workflow runtime worker 未能停止")
        if _service is not None:
            # Store 未确认关闭时同样保留组合根与租约，避免第二 Authority 进入。
            _service.close()
        elif _startup_store is not None:
            # Service 构造前的 Store 也必须确认关闭后才能释放租约。
            _startup_store.close()
        _monitor = None
        _runtime_worker = None
        _service = None
        _startup_store = None
        _database_path = None
        _compiler = None
        _authority = None
        _editable_package_roots = ()
        _ready = False
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
