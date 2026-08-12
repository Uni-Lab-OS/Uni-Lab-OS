"""选择本地调试或正式 Backend 控制面的启动 seam。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from unilabos.config.config import BasicConfig


class ControlPlaneMode(str, Enum):
    """工作流和物料权威所在的位置。"""

    LOCAL = "local"
    BACKEND = "backend"


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

    try:
        mode = ControlPlaneMode(str(arguments.get("control_plane") or "local"))
    except ValueError as error:
        raise ValueError("control_plane 必须是 local 或 backend") from error
    bridges = {str(value) for value in arguments.get("app_bridges") or ()}
    if mode is ControlPlaneMode.LOCAL:
        if "edge_control" in bridges:
            raise ValueError(
                "edge_control 生产 bridge 必须与 --control_plane backend 一起使用"
            )
        return mode

    if arguments.get("is_slave", False):
        raise ValueError("--control_plane backend 不能与 --is_slave 一起使用")
    if arguments.get("preserve_runtime_databases", False):
        raise ValueError(
            "--control_plane backend 不使用 --preserve_runtime_databases；"
            "协议恢复状态由 edge_control.db 独立持久化"
        )
    if "edge_control" not in bridges:
        raise ValueError("--control_plane backend 必须启用 edge_control bridge")
    if "websocket" in bridges:
        raise ValueError(
            "--control_plane backend 不能同时启用遗留 websocket bridge"
        )
    return mode


def should_mount_embedded_scheduler_routes() -> bool:
    """仅本地调试控制面向 FastAPI 挂载嵌入式微后端路由。"""

    return BasicConfig.control_plane == ControlPlaneMode.LOCAL.value


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
    "ControlPlaneRuntimeContext",
    "ControlPlaneRuntimeHandle",
    "should_mount_embedded_scheduler_routes",
    "start_control_plane_runtime",
    "validate_control_plane_arguments",
]
