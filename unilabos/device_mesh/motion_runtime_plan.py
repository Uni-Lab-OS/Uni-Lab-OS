"""Pure graph-to-motion-runtime selection.

Execution and presentation are deliberately orthogonal: a MoveIt profile starts
with ``--visual disable``, while choosing RViz does not turn a PLC robot into a
MoveIt execution backend.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_MOVEIT_SIM_BACKEND = "moveit_sim"


@dataclass(frozen=True)
class MotionRuntimePlan:
    moveit_device_ids: tuple[str, ...]
    unsupported_physical_device_ids: tuple[str, ...]
    enable_rviz_view: bool
    enable_web_view: bool

    @property
    def moveit_enabled(self) -> bool:
        return bool(self.moveit_device_ids)

    @property
    def planning_scene_required(self) -> bool:
        return self.moveit_enabled


def plan_motion_runtime(
    devices: Mapping[str, Mapping[str, Any]],
    *,
    visual: str,
) -> MotionRuntimePlan:
    moveit_ids = tuple(
        sorted(
            str(node.get("id") or key)
            for key, node in devices.items()
            if _selected_backend(_node_config(node)) == _MOVEIT_SIM_BACKEND
        )
    )
    physical_ids = tuple(
        sorted(
            str(node.get("id") or key)
            for key, node in devices.items()
            if _selected_backend(_node_config(node)) == "moveit"
        )
    )
    return MotionRuntimePlan(
        moveit_device_ids=moveit_ids,
        unsupported_physical_device_ids=physical_ids,
        enable_rviz_view=visual == "rviz",
        enable_web_view=visual == "web",
    )


def node_requests_moveit(node: Mapping[str, Any]) -> bool:
    """Whether the explicit package runtime owns this node."""

    return _selected_backend(_node_config(node)) == _MOVEIT_SIM_BACKEND


def legacy_node_requests_moveit(node: Mapping[str, Any]) -> bool:
    """Compatibility heuristic retained only inside the legacy visual Adapter."""

    if _selected_backend(_node_config(node)):
        return False
    return ".moveit." in str(node.get("class") or "")


def _node_config(node: Mapping[str, Any]) -> Mapping[str, Any]:
    config = node.get("config")
    return config if isinstance(config, Mapping) else {}


def _selected_backend(config: Mapping[str, Any]) -> str:
    direct = config.get("standard_execution_backend") or config.get(
        "execution_backend"
    )
    if direct:
        return str(direct).strip().lower()
    robot_execution = config.get("robot_execution")
    if isinstance(robot_execution, Mapping):
        value = robot_execution.get("backend") or robot_execution.get(
            "hardware_profile"
        )
        if value:
            normalized = str(value).strip().lower()
            if "moveit-sim" in normalized:
                return "moveit_sim"
            if "moveit" in normalized:
                return "moveit"
            return normalized
    return ""


__all__ = [
    "MotionRuntimePlan",
    "legacy_node_requests_moveit",
    "node_requests_moveit",
    "plan_motion_runtime",
]
