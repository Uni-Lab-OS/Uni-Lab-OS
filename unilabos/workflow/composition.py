"""工作区本地工作流权威（Workflow Authority）的进程级组合根。"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from unilabos.registry.template_projection import RegistryTemplateProjection
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.device_action_run_bridge import (
    DeviceActionRunWorkflowSpecBridge,
)
from unilabos.workflow.service import AuthoringCompiler, WorkflowService
from unilabos.workflow.source_discovery import discover_editable_sources
from unilabos.workflow.source_monitor import WorkflowSourceMonitor
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge

_lock = threading.RLock()
_service: Optional[WorkflowService] = None
_database_path: Optional[Path] = None
_monitor: Optional[WorkflowSourceMonitor] = None
_template_projection: Optional[RegistryTemplateProjection] = None
_compiler: Optional[AuthoringCompiler] = None
_editable_package_roots: tuple[Path, ...] = ()


@dataclass
class _RuntimeCleanupOwner:
    """持有部分启动失败后仍需重试清理的完整运行时资源。"""

    service: WorkflowService
    monitor: WorkflowSourceMonitor
    monitor_stopped: bool = False
    service_closed: bool = False

    def cleanup(self) -> None:
        """按监视器后服务的安全顺序幂等推进清理。

        参数：无。返回：全部资源关闭时无返回值；任一步失败原样抛出，并保留
        已完成步骤标记供下一次重置继续，而不是重复关闭或遗失所有权。
        """

        if not self.monitor_stopped:
            self.monitor.stop()
            self.monitor_stopped = True
        if not self.service_closed:
            self.service.close()
            self.service_closed = True


_failed_runtime: Optional[_RuntimeCleanupOwner] = None


def _configured_package_roots(
    roots: Iterable[str | Path],
) -> tuple[Path, ...]:
    """冻结本次进程组合使用的显式授权包目录。

    参数：``roots`` 是调用者明确配置的可编辑包（Editable Package）选择目录。
    返回：保留输入顺序且不跟随符号链接的绝对路径元组；安全性由发现模块复核。
    """

    if not isinstance(roots, tuple) or any(
        not isinstance(root, (str, Path)) for root in roots
    ):
        raise TypeError("工作流源码授权目录必须是 tuple[str | Path, ...]")
    return tuple(Path(root).absolute() for root in roots)


def compose_workflow_runtime(
    working_dir: str | Path,
    *,
    compiler: Optional[AuthoringCompiler] = None,
    editable_package_roots: Iterable[str | Path] = (),
    material_resolver: Optional[
        Callable[[str], Optional[dict[str, Any]]]
    ] = None,
    scheduler: Optional[Any] = None,
) -> WorkflowService:
    """装配工作区唯一的工作流权威、启动恢复和草稿监视。

    参数：``working_dir`` 决定现有工作流 SQLite 路径；``compiler`` 是可信工作流
    创作编译器；``editable_package_roots`` 是唯一允许发现源码的显式授权目录；
    ``material_resolver`` 按稳定 UUID 读取本地物料权威摘要；``scheduler`` 是仅在
    本地调度模式装配的现有调度器（EdgeScheduler）。
    返回：完成来源注册与启动恢复后发布的进程唯一工作流服务（WorkflowService）。
    异常：运行期间切换数据库、编译器或授权目录集合时失败关闭。
    """

    global _compiler, _database_path, _editable_package_roots, _failed_runtime
    global _monitor, _service
    # 后端形态合同（Backend-shaped Contract）的定义/任务与遗留执行历史共享
    # ``workflow_history.db``，但继续使用相互独立的表。
    database_path = Path(working_dir).resolve() / "workflow_history.db"
    configured_roots = _configured_package_roots(editable_package_roots)
    with _lock:
        if _failed_runtime is not None:
            raise RuntimeError("工作流运行时仍有部分启动资源等待显式重置清理")
        if _service is not None:
            if database_path != _database_path:
                raise RuntimeError(
                    "工作流权威（Workflow Authority）运行期间不能切换 working_dir"
                )
            if compiler is not _compiler:
                raise RuntimeError(
                    "工作流权威（Workflow Authority）运行期间不能切换 compiler"
                )
            if configured_roots != _editable_package_roots:
                raise RuntimeError(
                    "工作流权威（Workflow Authority）运行期间不能切换可编辑包"
                )
            return _service
        # ``workflow_store`` 是本地标准工作流任务（WorkflowTask）/工作流节点作业
        # （WorkflowNodeJob）写模型；执行桥与应用服务必须共享同一实例。
        workflow_store = WorkflowStore(database_path)
        device_action_run_bridge = None
        task_scheduler_bridge = None
        new_service: Optional[WorkflowService] = None
        new_monitor: Optional[WorkflowSourceMonitor] = None
        try:
            if scheduler is not None:
                task_scheduler_bridge = TaskSchedulerBridge(
                    workflow_store,
                    scheduler=scheduler,
                )
            if scheduler is not None and material_resolver is not None:
                device_action_run_bridge = DeviceActionRunWorkflowSpecBridge(
                    workflow_store,
                    scheduler=scheduler,
                    material_resolver=material_resolver,
                )
            new_service = WorkflowService(
                workflow_store,
                compiler=compiler,
                material_resolver=material_resolver,
                device_action_run_bridge=device_action_run_bridge,
                task_scheduler_bridge=task_scheduler_bridge,
            )
            # ``discovery_plan`` 是全量文件预校验结果；服务在单事务中注册后，
            # 才能恢复草稿并建立一致的监视基线。
            discovery_plan = discover_editable_sources(configured_roots)
            new_service.replace_discovered_source_authorizations(discovery_plan)
            new_service.recover_registered_sources()
            new_monitor = WorkflowSourceMonitor(new_service)
        except BaseException:
            if new_service is not None:
                new_service.close()
            else:
                workflow_store.close()
            raise

        # 启动恢复已经完成，此处才一次发布进程内工作流权威及其完整组合身份。
        _service = new_service
        _database_path = database_path
        _compiler = compiler
        _editable_package_roots = configured_roots
        _monitor = new_monitor
        try:
            new_monitor.start()
        except BaseException as start_error:
            # 监视线程未可靠启动时立即撤销公开权威，但完整资源所有权必须保留到
            # 停止和服务关闭都确认成功，不能让清理错误覆盖原始启动错误。
            _service = None
            _database_path = None
            _compiler = None
            _editable_package_roots = ()
            _monitor = None
            cleanup_owner = _RuntimeCleanupOwner(new_service, new_monitor)
            _failed_runtime = cleanup_owner
            try:
                cleanup_owner.cleanup()
            except BaseException as cleanup_error:
                start_error.add_note(f"后续清理失败: {cleanup_error}")
            else:
                _failed_runtime = None
            raise start_error
        return new_service


def setup_workflow_service(
    working_dir: str | Path,
    *,
    compiler: Optional[AuthoringCompiler] = None,
    editable_package_roots: Iterable[str | Path] = (),
) -> WorkflowService:
    """兼容旧装配调用并进入完整工作流运行时组合。

    参数：``working_dir`` 决定数据库路径；``compiler`` 是可信创作编译器；
    ``editable_package_roots`` 是显式授权源码目录。
    返回：完成注册、恢复与发布的工作流服务（WorkflowService）。
    """

    return compose_workflow_runtime(
        working_dir,
        compiler=compiler,
        editable_package_roots=editable_package_roots,
    )


def compose_local_workflow_template_runtime(
    working_dir: str | Path,
    *,
    inventory_store: Any,
    registry: Any,
    scheduler: Optional[Any] = None,
    editable_package_roots: Iterable[str | Path] = (),
) -> tuple[WorkflowService, RegistryTemplateProjection]:
    """装配本地模板权威、F02 创作编译器与工作流服务。

    参数说明：``working_dir`` 决定现有 ``workflow_history.db`` 路径；
    ``inventory_store`` 是 ``inventory.db`` 的只读身份来源；``registry`` 是原始
    Registry 或不可变模板快照；``scheduler`` 是本地模式既有调度器；
    ``editable_package_roots`` 是本次进程唯一授权的工作流源码（Workflow
    Source）目录 tuple。返回共享同一已发布目录代际的服务和投影。
    """

    global _template_projection
    database_path = Path(working_dir).resolve() / "workflow_history.db"
    with _lock:
        if _template_projection is not None:
            # 已发布的模板投影必须复用原编译器和授权目录组合身份。
            service = compose_workflow_runtime(
                working_dir,
                compiler=_compiler,
                editable_package_roots=editable_package_roots,
            )
            return service, _template_projection
        if _service is not None:
            raise RuntimeError(
                "本地工作流服务已在没有模板投影的情况下完成装配"
            )

        def resolve_resource_template_identity(resource_name: str) -> str:
            """按库存活动业务名解析资源模板稳定 UUID。

            参数说明：``resource_name`` 来自规范 Registry 快照；返回现有
            ``inventory.db`` 活动行 UUID，缺失时返回空串供投影关闭式失败。
            """

            # ``resource_row`` 是本地库存权威中的活动资源模板身份映射。
            resource_row = inventory_store.query_one(
                """
                SELECT uuid, name, display_name
                FROM resource_template
                WHERE name = ? AND deleted_at IS NULL
                """,
                (resource_name,),
            )
            return str(resource_row["uuid"]) if resource_row is not None else ""

        def resolve_material_identity(
            material_uuid: str,
        ) -> Optional[dict[str, Any]]:
            """按稳定 UUID 读取活动物料（Material）身份。

            参数：``material_uuid`` 是设备动作最终参数或执行器分配引用的实际物料
            UUID。返回 UUID 与资源模板 UUID 摘要；不存在或已删除时返回 ``None``。
            """

            # ``material_row`` 来自本地库存权威，只提供合同校验所需的稳定身份，
            # 不把可变物料快照复制进工作流写模型。
            material_row = inventory_store.query_one(
                """
                SELECT uuid, resource_template_uuid, meta_data
                FROM material
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (material_uuid,),
            )
            return dict(material_row) if material_row is not None else None

        projection = RegistryTemplateProjection(
            WorkflowStore(database_path),
            authority_id="local",
            resource_template_identity_resolver=resolve_resource_template_identity,
        )
        try:
            snapshot = projection.refresh(registry)
            compiler = WorkflowAuthoringEngine(catalog=snapshot)
            service = compose_workflow_runtime(
                working_dir,
                compiler=compiler,
                editable_package_roots=editable_package_roots,
                material_resolver=resolve_material_identity,
                scheduler=scheduler,
            )
        except BaseException:
            projection.close()
            raise
        _template_projection = projection
        return service, projection


def get_workflow_service() -> Optional[WorkflowService]:
    """返回进程内已经装配的本地工作流权威。

    返回值：工作流运行时尚未装配时为 ``None``；本函数只读取现有单例，绝不
    隐式创建工作流任务权威（WorkflowTask Authority）。
    """

    return _service


def get_registry_template_projection() -> Optional[RegistryTemplateProjection]:
    """返回本地模式最近装配的设备注册表模板投影。

    返回值：尚未建立本地模板权威时为 ``None``；Backend-controlled 模式不得用
    此函数隐式创建第二写权威。
    """

    return _template_projection


def reset_workflow_service_for_test() -> None:
    """停止并清除测试使用的完整工作流运行时组合。

    参数：无。
    返回：无；监视器、服务、模板投影和组合身份全部恢复为空。
    """

    global _compiler, _database_path, _editable_package_roots, _failed_runtime
    global _monitor, _service, _template_projection
    with _lock:
        if _failed_runtime is not None:
            # 失败运行时是一个整体清理所有者；任一步再次失败都保留原对象和已完成
            # 标记，调用者可在外部条件修复后再次执行重置。
            _failed_runtime.cleanup()
            _failed_runtime = None
        if _monitor is not None:
            _monitor.stop()
        if _service is not None:
            _service.close()
        if _template_projection is not None:
            _template_projection.close()
        _monitor = None
        _service = None
        _database_path = None
        _compiler = None
        _editable_package_roots = ()
        _template_projection = None


__all__ = [
    "compose_workflow_runtime",
    "compose_local_workflow_template_runtime",
    "get_registry_template_projection",
    "get_workflow_service",
    "reset_workflow_service_for_test",
    "setup_workflow_service",
]
