"""工作区本地 Workflow Authority 的进程级组合根。"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Optional

from unilabos.registry.template_projection import RegistryTemplateProjection
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.service import AuthoringCompiler, WorkflowService
from unilabos.workflow.source_monitor import WorkflowSourceMonitor
from unilabos.workflow.store import WorkflowStore

_lock = threading.RLock()
_service: Optional[WorkflowService] = None
_database_path: Optional[Path] = None
_monitor: Optional[WorkflowSourceMonitor] = None
_template_projection: Optional[RegistryTemplateProjection] = None


def compose_workflow_runtime(
    working_dir: str | Path,
    *,
    compiler: Optional[AuthoringCompiler] = None,
) -> WorkflowService:
    """装配工作区唯一的 Workflow authority、启动恢复和 Draft 监视。"""

    global _database_path, _monitor, _service
    # Backend-shaped definitions/tasks and legacy execution history share the
    # documented workflow_history SQLite file, but remain separate tables.
    database_path = Path(working_dir).resolve() / "workflow_history.db"
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
        _service.recover_registered_sources()
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


def compose_local_workflow_template_runtime(
    working_dir: str | Path,
    *,
    inventory_store: Any,
    registry: Any,
) -> tuple[WorkflowService, RegistryTemplateProjection]:
    """装配本地模板权威、F02 创作编译器与工作流服务。

    参数说明：``working_dir`` 决定现有 ``workflow_history.db`` 路径；
    ``inventory_store`` 是 ``inventory.db`` 的只读身份来源；``registry`` 是原始
    Registry 或不可变模板快照。返回共享同一已发布目录代际的服务和投影。
    """

    global _template_projection
    database_path = Path(working_dir).resolve() / "workflow_history.db"
    with _lock:
        if _template_projection is not None:
            service = compose_workflow_runtime(working_dir)
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

        projection = RegistryTemplateProjection(
            WorkflowStore(database_path),
            authority_id="local",
            resource_template_identity_resolver=resolve_resource_template_identity,
        )
        try:
            snapshot = projection.refresh(registry)
            compiler = WorkflowAuthoringEngine(catalog=snapshot)
            service = compose_workflow_runtime(working_dir, compiler=compiler)
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
    """停止监视器并关闭测试使用的工作流服务与模板投影单例。"""

    global _database_path, _monitor, _service, _template_projection
    with _lock:
        if _monitor is not None:
            _monitor.stop()
        if _service is not None:
            _service.close()
        if _template_projection is not None:
            _template_projection.close()
        _monitor = None
        _service = None
        _database_path = None
        _template_projection = None


__all__ = [
    "compose_workflow_runtime",
    "compose_local_workflow_template_runtime",
    "get_registry_template_projection",
    "get_workflow_service",
    "reset_workflow_service_for_test",
    "setup_workflow_service",
]
