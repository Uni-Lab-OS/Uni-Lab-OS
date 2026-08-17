"""Resolve the OS process composition independently from domain authority."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class ControlPlaneMode(str, Enum):
    """工作流、物料与任务事实的权威位置。"""

    LOCAL = "local"
    BACKEND = "backend"


class RuntimeProcessRole(str, Enum):
    """一个 OS 进程在 Workbench 运行拓扑中的职责。"""

    COMBINED = "combined"
    WORKSPACE_BACKEND = "workspace_backend"
    EDGE_RUNTIME = "edge_runtime"


@dataclass(frozen=True)
class RuntimeProcessPlan:
    """启动组合根消费的完整、已验证进程计划。"""

    role: RuntimeProcessRole
    control_plane: ControlPlaneMode
    starts_web_server: bool
    initializes_host_devices: bool


def resolve_runtime_process_plan(arguments: dict[str, Any]) -> RuntimeProcessPlan:
    """解析正交的进程角色与 Authority，并关闭式拒绝无效组合。"""

    try:
        control_plane = ControlPlaneMode(
            str(arguments.get("control_plane") or ControlPlaneMode.LOCAL.value)
        )
    except ValueError as error:
        raise ValueError("control_plane 必须是 local 或 backend") from error
    try:
        role = RuntimeProcessRole(
            str(arguments.get("process_role") or RuntimeProcessRole.COMBINED.value)
        )
    except ValueError as error:
        raise ValueError(
            "process_role 必须是 combined、workspace_backend 或 edge_runtime"
        ) from error

    is_slave = bool(arguments.get("is_slave", False))
    bridges = {str(value) for value in arguments.get("app_bridges") or ()}

    if role is RuntimeProcessRole.WORKSPACE_BACKEND:
        if is_slave:
            raise ValueError("workspace_backend 进程不能使用 --is_slave")
    elif role is RuntimeProcessRole.EDGE_RUNTIME:
        if control_plane is ControlPlaneMode.LOCAL and is_slave:
            raise ValueError(
                "local Authority 的 edge_runtime 直接拥有设备，不能使用 --is_slave"
            )
        if control_plane is ControlPlaneMode.BACKEND and is_slave:
            raise ValueError(
                "backend Authority 的 edge_runtime 不能使用 --is_slave"
            )

    if control_plane is ControlPlaneMode.LOCAL:
        if (
            "edge_control" in bridges
            and role is not RuntimeProcessRole.EDGE_RUNTIME
        ):
            raise ValueError(
                "local Authority 仅允许 edge_runtime 使用 edge_control bridge"
            )
        if (
            role is RuntimeProcessRole.EDGE_RUNTIME
            and "edge_control" not in bridges
        ):
            raise ValueError(
                "local Authority 的 edge_runtime 必须启用 edge_control bridge"
            )
    else:
        if is_slave:
            raise ValueError("--control_plane backend 不能与 --is_slave 一起使用")
        if arguments.get("preserve_runtime_databases", False):
            raise ValueError(
                "--control_plane backend 不使用 --preserve_runtime_databases；"
                "协议恢复状态由 edge_control.db 独立持久化"
            )
        if (
            role is not RuntimeProcessRole.WORKSPACE_BACKEND
            and "edge_control" not in bridges
        ):
            raise ValueError("--control_plane backend 必须启用 edge_control bridge")
        if (
            role is RuntimeProcessRole.WORKSPACE_BACKEND
            and "edge_control" in bridges
        ):
            raise ValueError(
                "backend Authority 的 workspace_backend 只保留 Authoring，"
                "不能启用 edge_control bridge"
            )
        if "websocket" in bridges:
            raise ValueError(
                "--control_plane backend 不能同时启用遗留 websocket bridge"
            )

    return RuntimeProcessPlan(
        role=role,
        control_plane=control_plane,
        starts_web_server=role is not RuntimeProcessRole.EDGE_RUNTIME,
        initializes_host_devices=role
        in {RuntimeProcessRole.COMBINED, RuntimeProcessRole.EDGE_RUNTIME},
    )


def publish_edge_runtime_ready_signal(path: str | None = None) -> None:
    """Atomically publish that the Edge device initialization phase completed."""

    target_value = path or os.environ.get("UNILABOS_EDGE_READY_FILE")
    if not target_value:
        return
    target = Path(target_value)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps({"schemaVersion": 1, "pid": os.getpid()}) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


__all__ = [
    "ControlPlaneMode",
    "RuntimeProcessPlan",
    "RuntimeProcessRole",
    "publish_edge_runtime_ready_signal",
    "resolve_runtime_process_plan",
]
