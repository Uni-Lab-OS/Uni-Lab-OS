"""把物理图（Graph）的执行选择投影为独立运动与显示启动计划。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_MOVEIT_BACKENDS = frozenset({"moveit", "moveit_sim"})


@dataclass(frozen=True, slots=True)
class MotionRuntimePlan:
    """一次启动中相互正交的运动运行时与可视化决定。"""

    moveit_device_ids: tuple[str, ...]
    visualization_enabled: bool
    enable_rviz: bool

    @property
    def motion_runtime_required(self) -> bool:
        """返回物理图是否明确要求 OS 启动 MoveIt 执行依赖。"""

        return bool(self.moveit_device_ids)

    @property
    def ros_launch_required(self) -> bool:
        """返回运动或显示是否要求 ROS Launch 所有者。"""

        return self.motion_runtime_required or self.visualization_enabled


def plan_motion_runtime(
    devices: Mapping[str, Mapping[str, Any]],
    *,
    visual: str,
) -> MotionRuntimePlan:
    """从物理图生成不导入 ROS 的启动计划。

    显式 PLC/TCP/SDK 后端优先于遗留类名；打开 RViz 不能把非 MoveIt 设备变成
    运动执行器，关闭 RViz 也不能关闭物理图已经选择的 MoveIt 执行依赖。
    """

    if not isinstance(devices, Mapping):
        raise TypeError("物理图节点必须是 Mapping")
    normalized_visual = str(visual).strip().lower()
    if normalized_visual not in {"disable", "web", "rviz"}:
        raise ValueError("visual 必须是 disable、web 或 rviz")
    moveit_device_ids = tuple(
        sorted(
            str(node.get("id") or node_id)
            for node_id, node in devices.items()
            if isinstance(node, Mapping) and node_requests_moveit(node)
        )
    )
    return MotionRuntimePlan(
        moveit_device_ids=moveit_device_ids,
        visualization_enabled=normalized_visual != "disable",
        enable_rviz=normalized_visual == "rviz",
    )


def node_requests_moveit(node: Mapping[str, Any]) -> bool:
    """判断单个设备是否选择 MoveIt，显式执行后端优先于遗留类名。"""

    if str(node.get("type") or "") != "device":
        return False
    backend = _selected_backend(node.get("config"))
    if backend:
        return backend in _MOVEIT_BACKENDS
    return ".moveit." in str(node.get("class") or "").lower()


def _selected_backend(config: object) -> str:
    if not isinstance(config, Mapping):
        return ""
    direct = config.get("standard_execution_backend") or config.get(
        "execution_backend"
    )
    if direct is not None:
        return str(direct).strip().lower().replace("-", "_")
    robot_execution = config.get("robot_execution")
    if not isinstance(robot_execution, Mapping):
        return ""
    nested = robot_execution.get("backend") or robot_execution.get(
        "hardware_profile"
    )
    if nested is None:
        return ""
    normalized = str(nested).strip().lower().replace("-", "_")
    if "moveit_sim" in normalized:
        return "moveit_sim"
    if "moveit" in normalized:
        return "moveit"
    return normalized


__all__ = ["MotionRuntimePlan", "node_requests_moveit", "plan_motion_runtime"]
