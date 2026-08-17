"""选择本地调试或正式 Backend 控制面的启动 seam。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from unilabos.config.config import BasicConfig
from unilabos.app.runtime_topology import (
    ControlPlaneMode,
    RuntimeProcessRole,
    resolve_runtime_process_plan,
)


@dataclass(frozen=True)
class ControlPlaneRuntimeContext:
    """两个控制面 adapter 共用的冻结启动输入。"""

    arguments: dict[str, Any]
    working_dir: str
    resource_tree_set: Any
    registry: Any
    graph_source_id: str
    material_shapes: Any
    material_model_catalog: Any


@dataclass(frozen=True)
class ControlPlaneRuntimeHandle:
    """控制面 adapter 向 HostNode 和进程生命周期公开的最小接口。"""

    bridges: tuple[Any, ...]
    communication_clients: tuple[Any, ...]
    shutdown_services: Callable[[], None]


def validate_control_plane_arguments(
    arguments: dict[str, Any],
) -> ControlPlaneMode:
    """验证控制面模式及 bridge 组合，错误配置关闭式失败。"""

    return resolve_runtime_process_plan(arguments).control_plane


def should_mount_embedded_scheduler_routes() -> bool:
    """仅本地调试控制面向 FastAPI 挂载嵌入式微后端路由。"""

    return (
        BasicConfig.control_plane == ControlPlaneMode.LOCAL.value
        and BasicConfig.process_role != RuntimeProcessRole.EDGE_RUNTIME.value
    )


def should_mount_workspace_authoring_routes() -> bool:
    """工作区 Backend 在两种 Authority 下都保留 Authoring Interface。"""

    return BasicConfig.process_role in {
        RuntimeProcessRole.COMBINED.value,
        RuntimeProcessRole.WORKSPACE_BACKEND.value,
    }


def start_control_plane_runtime(
    context: ControlPlaneRuntimeContext,
) -> ControlPlaneRuntimeHandle:
    """在唯一 seam 后按模式惰性加载并启动一个控制面 adapter。"""

    mode = validate_control_plane_arguments(context.arguments)
    if mode is ControlPlaneMode.BACKEND:
        from unilabos.app.edge_control.runtime import start_backend_control_runtime

        return start_backend_control_runtime(context)
    if mode is ControlPlaneMode.LOCAL:
        from unilabos.app.scheduler.runtime import start_embedded_scheduler_runtime

        return start_embedded_scheduler_runtime(context)
    raise ValueError(f"不支持的控制面模式: {mode!r}")


__all__ = [
    "ControlPlaneMode",
    "RuntimeProcessRole",
    "ControlPlaneRuntimeContext",
    "ControlPlaneRuntimeHandle",
    "should_mount_embedded_scheduler_routes",
    "should_mount_workspace_authoring_routes",
    "start_control_plane_runtime",
    "validate_control_plane_arguments",
]
