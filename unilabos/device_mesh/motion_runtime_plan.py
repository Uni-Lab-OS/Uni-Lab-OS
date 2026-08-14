"""把物理图（Graph）的执行选择投影为独立的运动与显示启动计划。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_MOVEIT_BACKENDS = frozenset({"moveit", "moveit_sim"})


@dataclass(frozen=True, slots=True)
class MotionRuntimePlan:
    """一次启动中互相正交的运动运行时与可视化决定。"""

    moveit_device_ids: tuple[str, ...]
    visualization_enabled: bool
    enable_rviz: bool

    @property
    def motion_runtime_required(self) -> bool:
        """返回 Graph 是否要求 OS 启动 MoveIt 执行依赖。

        参数：无。返回：存在至少一个 MoveIt Device 时为 ``True``。安全：该值只
        表示必须启动运动基础设施，不代表设备安全许可、控制权或硬件就绪。
        """

        return bool(self.moveit_device_ids)

    @property
    def ros_launch_required(self) -> bool:
        """返回运动或显示是否需要 ROS Launch owner。

        参数：无。返回：运动运行时或可视化任一开启时为 ``True``。安全：调用方
        必须在运动运行时要求 ROS 而环境缺失时关闭失败，不得按普通显示失败跳过。
        """

        return self.motion_runtime_required or self.visualization_enabled


def plan_motion_runtime(
    devices: Mapping[str, Mapping[str, Any]],
    *,
    visual: str,
) -> MotionRuntimePlan:
    """从 Graph 生成不导入 ROS 的启动计划。

    参数：``devices`` 是 ``dict_from_graph`` 的节点映射；``visual`` 是公共 CLI 的
    ``disable/web/rviz`` 值。返回：稳定排序的 MoveIt Device 集合及独立显示开关。
    异常：节点映射或可视化值无效时抛出 ``TypeError``/``ValueError``。安全：显式
    PLC/TCP/SDK 后端优先于遗留类名，防止打开 RViz 时误启第二套运动执行器。
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
    """判断单个 Device 是否选择 MoveIt，显式配置优先于遗留类名。

    参数：``node`` 是 Graph 单节点投影。返回：该节点要求 OS MoveIt 运行时则为
    ``True``。安全：非 Device 与显式非 MoveIt 后端始终返回 ``False``，调用方
    不得用 RViz 开关覆盖这个执行选择。
    """

    if str(node.get("type") or "") != "device":
        return False
    backend = _selected_backend(node.get("config"))
    if backend:
        return backend in _MOVEIT_BACKENDS
    # 该启发式只保留给尚未迁移 HardwareProfile 投影的内置注册表项；领域包必须
    # 在 Graph 中提供显式后端，避免类名同时承担型号和部署策略两种含义。
    return ".moveit." in str(node.get("class") or "").lower()


def _selected_backend(config: object) -> str:
    """读取 Graph 中明确的 HardwareProfile 执行后端投影。"""

    if not isinstance(config, Mapping):
        return ""
    direct = config.get("standard_execution_backend") or config.get(
        "execution_backend"
    )
    if direct is not None:
        return str(direct).strip().lower()
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
