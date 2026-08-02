"""工作区本地 Workflow Authority 的进程级组合根。"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import threading
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, cast

from unilabos.app.scheduler.inventory import (
    InventoryService,
    ResourceTemplateIdentity,
)
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.catalog import (
    CatalogAuthority,
    LocalResourceTemplateIdentityIndex,
    ResourceTemplateIdentityIndex,
    TemplateCatalog,
)
from unilabos.workflow.device_action_task import (
    DeviceActionTaskRuntimeBridge,
    DeviceActionTaskService,
    HostNodeDeviceActionLiveCatalog,
)
from unilabos.workflow.material_resolver import MaterialResourceSlotResolver
from unilabos.workflow.runtime import (
    WorkflowJobDispatcher,
    WorkflowRuntimeCoordinator,
    WorkflowRuntimeWorker,
)
from unilabos.workflow.service import AuthoringCompiler, WorkflowService
from unilabos.workflow.source_discovery import register_editable_package_sources
from unilabos.workflow.source_monitor import WorkflowSourceMonitor
from unilabos.workflow.store import WorkflowStore

if TYPE_CHECKING:
    from unilabos.package_manager import PackageCatalog

_lock = threading.Lock()
_service: WorkflowService | None = None
_startup_store: WorkflowStore | None = None
_inventory_service: InventoryService | None = None
_database_path: Path | None = None
_monitor: WorkflowSourceMonitor | None = None
_runtime_worker: WorkflowRuntimeWorker | None = None
_owner_pid: int | None = None
_workspace_lease_fd: int | None = None
_compiler: AuthoringCompiler | None = None
_authority: CatalogAuthority | None = None
_editable_package_roots: tuple[Path, ...] = ()
_workflow_job_dispatcher: WorkflowJobDispatcher | None = None
_device_identity_resolver: Callable[[str], str | None] | None = None
_workflow_catalog_configuration: tuple[tuple[str, str], ...] = ()
_ready = False
_device_action_runtime: DeviceActionTaskRuntimeBridge | None = None
_device_action_tasks: DeviceActionTaskService | None = None


def _configured_package_roots(
    roots: Iterable[str | Path],
) -> tuple[Path, ...]:
    return tuple(Path(os.path.abspath(root)) for root in roots)


def _configured_workflow_catalogs(
    catalogs: Iterable[PackageCatalog],
) -> tuple[tuple[PackageCatalog, ...], tuple[tuple[str, str], ...]]:
    configured = tuple(catalogs)
    signature = tuple(
        (catalog.import_package, catalog.catalog_digest) for catalog in configured
    )
    return configured, signature


def _registry_resource_template_identities(
    registry_snapshot: Mapping[str, object],
    resource_registry_snapshot: Mapping[str, object] | None,
) -> tuple[str, ...]:
    """冻结 production Registry 中可被模板投影引用的 source identities。"""

    identities: set[str] = set()
    for registry_key, raw_device in registry_snapshot.items():
        if not isinstance(registry_key, str) or not isinstance(raw_device, Mapping):
            continue
        owner = raw_device.get("source_fqid") or registry_key
        if isinstance(owner, str) and owner:
            identities.add(owner)

    for raw_resource in (resource_registry_snapshot or {}).values():
        if not isinstance(raw_resource, Mapping):
            continue
        class_info = raw_resource.get("class")
        module = class_info.get("module") if isinstance(class_info, Mapping) else None
        if isinstance(module, str) and module:
            identities.add(module)
    return tuple(sorted(identities))


def _bidirectional_identity_index(
    value: ResourceTemplateIdentityIndex | Callable[[str], str] | None,
) -> ResourceTemplateIdentityIndex | None:
    """只接受同时提供正向、反向查询的 identity capability。"""

    if value is None:
        return None
    if callable(getattr(value, "resolve_symbol", None)) and callable(
        getattr(value, "identify_uuid", None)
    ):
        return cast(ResourceTemplateIdentityIndex, value)
    return None


def _registry_has_material_source_owner(
    registry_snapshot: Mapping[str, object],
) -> bool:
    """HostNode 是 production MaterialSource framework template 的 owner。"""

    return isinstance(registry_snapshot.get("host_node"), Mapping)


def _ensure_package_workflow_drafts(
    service: WorkflowService,
    catalogs: Iterable[PackageCatalog],
) -> None:
    """Materialize explicit PackageCatalog Workflow identities before source bind."""

    from unilabos.workflow.service import WorkflowError

    for catalog in catalogs:
        for definition in catalog.definitions.workflows:
            workflow_uuid = definition.details.get("workflow_uuid")
            if not isinstance(workflow_uuid, str):
                raise TypeError("PackageCatalog Workflow 缺少 workflow_uuid")
            try:
                service.get_workflow(workflow_uuid)
            except WorkflowError as error:
                if error.code != "not_found":
                    raise
                service.create_workflow(
                    workflow_uuid=workflow_uuid,
                    name=definition.displayname or definition.id,
                    tags=["package", catalog.import_package],
                    description=definition.description or None,
                    meta_data={
                        "package_fqid": definition.fqid,
                        "package_catalog_digest": catalog.catalog_digest,
                    },
                )


def _inventory_resource_templates(
    assignments: Mapping[str, str],
) -> dict[str, ResourceTemplateIdentity]:
    """Project Registry identities into Inventory's immutable lookup snapshot."""

    return {
        resource_template_uuid: ResourceTemplateIdentity(
            uuid=resource_template_uuid,
            material_class=source_identity,
        )
        for source_identity, resource_template_uuid in assignments.items()
    }


def _retain_runtime(
    service: WorkflowService,
    inventory_service: InventoryService,
    monitor: WorkflowSourceMonitor | None,
    runtime_worker: WorkflowRuntimeWorker | None,
    *,
    database_path: Path,
    compiler: AuthoringCompiler | None,
    authority: CatalogAuthority | None,
    editable_package_roots: tuple[Path, ...],
    workflow_job_dispatcher: WorkflowJobDispatcher | None,
    device_identity_resolver: Callable[[str], str | None] | None,
    workflow_catalog_configuration: tuple[tuple[str, str], ...],
    owner_pid: int,
    lease_descriptor: int,
    ready: bool,
    device_action_runtime: DeviceActionTaskRuntimeBridge | None = None,
    device_action_tasks: DeviceActionTaskService | None = None,
) -> None:
    """发布 ready Authority，或保留失败 cleanup 的独占 ownership。"""

    global _authority, _compiler, _database_path, _editable_package_roots
    global _device_identity_resolver, _workflow_catalog_configuration
    global _workflow_job_dispatcher
    global _inventory_service
    global _monitor, _ready, _runtime_worker, _startup_store
    global _owner_pid, _service, _workspace_lease_fd
    global _device_action_runtime, _device_action_tasks
    _service = service
    _startup_store = None
    _inventory_service = inventory_service
    _database_path = database_path
    _compiler = compiler
    _authority = authority
    _editable_package_roots = editable_package_roots
    _workflow_job_dispatcher = workflow_job_dispatcher
    _device_identity_resolver = device_identity_resolver
    _workflow_catalog_configuration = workflow_catalog_configuration
    _monitor = monitor
    _runtime_worker = runtime_worker
    _owner_pid = owner_pid
    _workspace_lease_fd = lease_descriptor
    _ready = ready
    _device_action_runtime = device_action_runtime
    _device_action_tasks = device_action_tasks


def _retain_startup_store(
    store: WorkflowStore,
    inventory_service: InventoryService | None,
    *,
    database_path: Path,
    owner_pid: int,
    lease_descriptor: int,
) -> None:
    """保留 Service 构造前未确认关闭的 Store 与工作区租约。"""

    global _database_path, _inventory_service, _owner_pid, _ready, _startup_store
    global _workspace_lease_fd
    _startup_store = store
    _inventory_service = inventory_service
    _database_path = database_path
    _owner_pid = owner_pid
    _workspace_lease_fd = lease_descriptor
    _ready = False


def _clear_runtime() -> None:
    """清除已确认关闭的进程内引用；lease 由调用方显式释放。"""

    global _authority, _compiler, _database_path, _editable_package_roots
    global _device_identity_resolver, _workflow_catalog_configuration
    global _workflow_job_dispatcher
    global _inventory_service
    global _monitor, _ready, _runtime_worker, _startup_store
    global _owner_pid, _service, _workspace_lease_fd
    global _device_action_runtime, _device_action_tasks
    _service = None
    _startup_store = None
    _inventory_service = None
    _database_path = None
    _compiler = None
    _authority = None
    _editable_package_roots = ()
    _workflow_job_dispatcher = None
    _device_identity_resolver = None
    _workflow_catalog_configuration = ()
    _monitor = None
    _runtime_worker = None
    _owner_pid = None
    _workspace_lease_fd = None
    _ready = False
    _device_action_runtime = None
    _device_action_tasks = None


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
    resource_registry_snapshot: Mapping[str, object] | None = None,
    resource_template_identity_resolver: (
        ResourceTemplateIdentityIndex | Callable[[str], str] | None
    ) = None,
    workflow_job_dispatcher: WorkflowJobDispatcher | None = None,
    device_identity_resolver: Callable[[str], str | None] | None = None,
    workflow_package_catalogs: Iterable[PackageCatalog] = (),
    inventory_graph_snapshot: Mapping[str, object] | None = None,
    package_sources: Iterable[object] = (),
    package_catalogs: Iterable[object] = (),
) -> WorkflowService:
    """装配工作区唯一的 Workflow authority、启动恢复和 Draft 监视。"""

    if compiler is not None and authority is not None:
        raise ValueError("compiler 与 authority 只能选择一种生产组合方式")
    if (workflow_job_dispatcher is None) != (device_identity_resolver is None):
        raise ValueError(
            "workflow_job_dispatcher 与 device_identity_resolver 必须同时配置"
        )
    if authority is not None and not isinstance(authority, CatalogAuthority):
        raise TypeError("authority 必须是 CatalogAuthority")
    if authority is not None and authority.kind != "local":
        raise ValueError("persistent Workflow runtime 只支持 local Graph Authority")
    if registry_snapshot is None and resource_template_identity_resolver is not None:
        raise ValueError("ResourceTemplate resolver 缺少 Registry snapshot")
    if registry_snapshot is None and resource_registry_snapshot is not None:
        raise ValueError("Resource Registry snapshot 缺少 Device Registry snapshot")
    if registry_snapshot is not None and authority is None:
        raise ValueError("Registry Catalog 发布需要显式 Graph Authority")
    if registry_snapshot is not None and compiler is not None:
        raise ValueError("Registry Catalog 发布不能使用外部 compiler")
    if (
        registry_snapshot is not None
        and resource_template_identity_resolver is None
        and resource_registry_snapshot is None
    ):
        from unilabos.registry.catalog_consumer import (
            RegistryTemplateProjectionError,
        )

        raise RegistryTemplateProjectionError(
            "template_catalog_mismatch",
            "/resource_registry",
        )
    if (
        registry_snapshot is not None
        and resource_template_identity_resolver is not None
        and _registry_has_material_source_owner(registry_snapshot)
        and _bidirectional_identity_index(resource_template_identity_resolver) is None
    ):
        from unilabos.registry.catalog_consumer import (
            RegistryTemplateProjectionError,
        )

        raise RegistryTemplateProjectionError(
            "template_catalog_mismatch",
            "/resource_template_identity_resolver",
        )
    resolved_working_dir = Path(working_dir).resolve()
    database_path = resolved_working_dir / "workflow.db"
    configured_roots = _configured_package_roots(editable_package_roots)
    configured_workflow_catalogs, configured_catalog_signature = (
        _configured_workflow_catalogs(workflow_package_catalogs)
    )
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
            if workflow_job_dispatcher is not _workflow_job_dispatcher:
                raise RuntimeError(
                    "Workflow runtime configuration cannot switch dispatcher"
                )
            if device_identity_resolver is not _device_identity_resolver:
                raise RuntimeError(
                    "Workflow runtime configuration cannot switch device resolver"
                )
            if configured_catalog_signature != _workflow_catalog_configuration:
                raise RuntimeError(
                    "Workflow runtime configuration cannot switch package catalogs"
                )
            if not _ready:
                raise RuntimeError(
                    "Workflow authority startup cleanup must complete before retry"
                )
            return _service
        lease_descriptor = _acquire_workspace_lease(resolved_working_dir)
        new_service: WorkflowService | None = None
        new_store: WorkflowStore | None = None
        new_inventory_service: InventoryService | None = None
        new_monitor: WorkflowSourceMonitor | None = None
        new_runtime_worker: WorkflowRuntimeWorker | None = None
        new_device_action_runtime: DeviceActionTaskRuntimeBridge | None = None
        new_device_action_tasks: DeviceActionTaskService | None = None
        published = False
        try:
            store = WorkflowStore(database_path)
            new_store = store
            runtime_compiler = compiler
            resolved_identities: dict[str, str] = {}
            if authority is not None:
                catalog = TemplateCatalog(store)
                identity_index: ResourceTemplateIdentityIndex | None = None
                if registry_snapshot is not None:
                    from unilabos.registry.catalog_consumer import (
                        workflow_template_imports_from_registry_snapshot,
                    )

                    identity_index = _bidirectional_identity_index(
                        resource_template_identity_resolver
                    )
                    identity_resolver = resource_template_identity_resolver
                    if identity_resolver is None:
                        identity_index = LocalResourceTemplateIdentityIndex(
                            store,
                            authority,
                            _registry_resource_template_identities(
                                registry_snapshot,
                                resource_registry_snapshot,
                            ),
                        )
                    if identity_index is not None or identity_resolver is not None:
                        base_identity_resolver = identity_resolver

                        def resolve_identity(source_identity: str) -> str:
                            resolved = (
                                identity_index.resolve_symbol(source_identity)
                                if identity_index is not None
                                else base_identity_resolver(source_identity)  # type: ignore[misc]
                            )
                            resolved_identities[source_identity] = resolved
                            return resolved

                        identity_resolver = resolve_identity
                        for source_identity in _registry_resource_template_identities(
                            registry_snapshot,
                            resource_registry_snapshot,
                        ):
                            resolve_identity(source_identity)
                    templates = workflow_template_imports_from_registry_snapshot(
                        registry_snapshot,
                        authority_id=authority.authority_id,
                        resource_template_identity_resolver=identity_resolver,
                    )
                    catalog.replace(
                        authority,
                        templates,
                        resource_template_identities=(
                            resolved_identities if identity_index is not None else None
                        ),
                    )
            configured_package_sources = tuple(package_sources)
            configured_package_catalogs = tuple(package_catalogs)
            material_shapes: tuple[Mapping[str, object], ...] = ()
            package_material_projection = None
            if configured_package_sources or configured_package_catalogs:
                from unilabos.app.scheduler.inventory.material_projection import (
                    build_package_material_projection,
                )

                package_material_projection = build_package_material_projection(
                    configured_package_sources,  # type: ignore[arg-type]
                    configured_package_catalogs,  # type: ignore[arg-type]
                )
                material_shapes = package_material_projection.shapes
            new_inventory_service = InventoryService.open(
                working_dir=resolved_working_dir,
                resource_templates=_inventory_resource_templates(
                    resolved_identities,
                ),
                material_shapes=material_shapes,
            )
            if inventory_graph_snapshot is not None:
                if package_material_projection is None:
                    raise RuntimeError(
                        "ResourceTreeSet bootstrap 缺少 PackageCatalog projection"
                    )
                from unilabos.app.scheduler.inventory.material_projection import (
                    build_resource_graph_import,
                )

                new_inventory_service.bootstrap_resource_graph(
                    build_resource_graph_import(
                        inventory_graph_snapshot,
                        package_material_projection,
                        resolved_identities,
                    )
                )
            material_authority = MaterialResourceSlotResolver(
                new_inventory_service,
            )
            runtime_coordinator = WorkflowRuntimeCoordinator(store)
            runtime_coordinator.recover_startup()
            if authority is not None:
                runtime_compiler = WorkflowAuthoringEngine(
                    catalog=catalog,
                    authority=authority,
                    resource_template_identity_index=identity_index,
                    material_source_authority=new_inventory_service,
                )
            new_service = WorkflowService(
                store,
                compiler=runtime_compiler,
                resource_resolver=material_authority,
                material_source_authority=new_inventory_service,
            )
            _ensure_package_workflow_drafts(
                new_service,
                configured_workflow_catalogs,
            )
            if authority is not None:
                from unilabos.app.scheduler.integration import (
                    get_edge_backend,
                    get_edge_scheduler,
                )

                new_device_action_runtime = DeviceActionTaskRuntimeBridge(
                    store=store,
                    coordinator=runtime_coordinator,
                    scheduler=get_edge_scheduler(),
                    backend=get_edge_backend(),
                )
                new_device_action_runtime.start()
                new_device_action_tasks = DeviceActionTaskService(
                    store=store,
                    template_catalog=catalog,
                    authority=authority,
                    live_catalog=HostNodeDeviceActionLiveCatalog(
                        template_catalog=catalog,
                        authority=authority,
                    ),
                    admission=new_device_action_runtime,
                )
            register_editable_package_sources(
                new_service,
                configured_roots,
            )
            new_service.recover_registered_sources()
            new_monitor = WorkflowSourceMonitor(new_service)
            if workflow_job_dispatcher is None:
                new_runtime_worker = WorkflowRuntimeWorker(runtime_coordinator)
            else:
                new_runtime_worker = WorkflowRuntimeWorker(
                    runtime_coordinator,
                    dispatcher=workflow_job_dispatcher,
                    device_identity_resolver=device_identity_resolver,
                )
            # Reconciliation 已完成，可以读取一致 baseline；monitor 从空签名集启动，
            # 会捕获此发布点与线程启动之间发生的变化。
            _retain_runtime(
                new_service,
                new_inventory_service,
                new_monitor,
                new_runtime_worker,
                database_path=database_path,
                compiler=runtime_compiler,
                authority=authority,
                editable_package_roots=configured_roots,
                workflow_job_dispatcher=workflow_job_dispatcher,
                device_identity_resolver=device_identity_resolver,
                workflow_catalog_configuration=configured_catalog_signature,
                owner_pid=os.getpid(),
                lease_descriptor=lease_descriptor,
                ready=True,
                device_action_runtime=new_device_action_runtime,
                device_action_tasks=new_device_action_tasks,
            )
            published = True
            new_monitor.start()
            new_runtime_worker.start()
        except BaseException as startup_error:
            cleanup_error: BaseException | None = None
            if new_device_action_runtime is not None:
                try:
                    new_device_action_runtime.stop()
                except BaseException as error:  # noqa: BLE001 - 保留租约
                    cleanup_error = error
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
            if cleanup_error is None and new_inventory_service is not None:
                try:
                    new_inventory_service.close()
                except BaseException as error:  # noqa: BLE001 - 保留租约需捕获关闭失败
                    cleanup_error = error
            if cleanup_error is not None:
                if new_service is not None and new_inventory_service is not None:
                    _retain_runtime(
                        new_service,
                        new_inventory_service,
                        new_monitor,
                        new_runtime_worker,
                        database_path=database_path,
                        compiler=runtime_compiler,
                        authority=authority,
                        editable_package_roots=configured_roots,
                        workflow_job_dispatcher=workflow_job_dispatcher,
                        device_identity_resolver=device_identity_resolver,
                        workflow_catalog_configuration=configured_catalog_signature,
                        owner_pid=os.getpid(),
                        lease_descriptor=lease_descriptor,
                        ready=False,
                        device_action_runtime=new_device_action_runtime,
                        device_action_tasks=new_device_action_tasks,
                    )
                elif new_store is not None:
                    _retain_startup_store(
                        new_store,
                        new_inventory_service,
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
    resource_registry_snapshot: Mapping[str, object] | None = None,
    resource_template_identity_resolver: (
        ResourceTemplateIdentityIndex | Callable[[str], str] | None
    ) = None,
    workflow_job_dispatcher: WorkflowJobDispatcher | None = None,
    device_identity_resolver: Callable[[str], str | None] | None = None,
    workflow_package_catalogs: Iterable[PackageCatalog] = (),
) -> WorkflowService:
    """兼容旧装配调用；所有入口统一进入完整运行时组合。"""

    return compose_workflow_runtime(
        working_dir,
        compiler=compiler,
        authority=authority,
        editable_package_roots=editable_package_roots,
        registry_snapshot=registry_snapshot,
        resource_registry_snapshot=resource_registry_snapshot,
        resource_template_identity_resolver=resource_template_identity_resolver,
        workflow_job_dispatcher=workflow_job_dispatcher,
        device_identity_resolver=device_identity_resolver,
        workflow_package_catalogs=workflow_package_catalogs,
    )


def get_workflow_service() -> WorkflowService | None:
    if not _ready or _owner_pid != os.getpid():
        return None
    return _service


def get_device_action_task_service() -> DeviceActionTaskService | None:
    if not _ready or _owner_pid != os.getpid():
        return None
    return _device_action_tasks


def configure_device_action_runtime(
    service: WorkflowService,
    scheduler: object | None,
    backend: object | None,
) -> bool:
    """把后创建的 production Edge stack 绑定到既有 D1A authority。"""

    with _lock:
        if (
            not _ready
            or _owner_pid != os.getpid()
            or service is not _service
            or _device_action_runtime is None
        ):
            return False
        if scheduler is None and backend is None:
            _device_action_runtime.unbind_execution_stack()
            return True
        if scheduler is None or backend is None:
            raise ValueError("scheduler 与 backend 必须同时配置")
        _device_action_runtime.bind_execution_stack(scheduler, backend)
        return True


def get_workflow_inventory_service() -> InventoryService | None:
    """Return the InventoryService owned by the active workspace composition."""

    if not _ready or _owner_pid != os.getpid():
        return None
    return _inventory_service


def configure_workflow_task_reconciler(
    service: WorkflowService,
    reconciler: Callable[[str], object] | None,
    dispatch_guard: Callable[[str], bool] | None = None,
) -> bool:
    """Attach/detach the Scheduler callback on the active runtime worker."""

    with _lock:
        if (
            not _ready
            or _owner_pid != os.getpid()
            or service is not _service
            or _runtime_worker is None
        ):
            return False
        _runtime_worker.set_task_reconciler(reconciler, dispatch_guard)
        return True


def reset_workflow_service_for_test() -> None:
    """停止监视器并关闭测试使用的进程级单例。"""

    global _authority, _compiler, _database_path, _editable_package_roots
    global _device_identity_resolver, _workflow_catalog_configuration
    global _workflow_job_dispatcher
    global _inventory_service
    global _monitor, _ready, _runtime_worker, _startup_store
    global _owner_pid, _service, _workspace_lease_fd
    global _device_action_runtime, _device_action_tasks
    with _lock:
        lease_owned = _owner_pid == os.getpid()
        if _device_action_runtime is not None:
            _device_action_runtime.stop()
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
        if _inventory_service is not None:
            _inventory_service.close()
        _monitor = None
        _runtime_worker = None
        _service = None
        _startup_store = None
        _inventory_service = None
        _database_path = None
        _compiler = None
        _authority = None
        _editable_package_roots = ()
        _workflow_job_dispatcher = None
        _device_identity_resolver = None
        _workflow_catalog_configuration = ()
        _ready = False
        _device_action_runtime = None
        _device_action_tasks = None
        _owner_pid = None
        lease_descriptor = _workspace_lease_fd
        _workspace_lease_fd = None
        _release_workspace_lease(
            lease_descriptor,
            unlock=lease_owned,
        )


__all__ = [
    "compose_workflow_runtime",
    "configure_device_action_runtime",
    "get_device_action_task_service",
    "configure_workflow_task_reconciler",
    "get_workflow_inventory_service",
    "get_workflow_service",
    "reset_workflow_service_for_test",
    "setup_workflow_service",
]
