"""在执行前把回转轴展开到最短角，并拒绝整圈跳变。"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

DEFAULT_REVOLUTE_JUMP_THRESHOLD_RAD = math.pi / 2
DEFAULT_CONTROLLER_JUMP_THRESHOLD_RAD = math.pi


def unwrap_revolute_to_shortest(current: float, target: float) -> float:
    """把目标角展开到相对当前值的最短有向角。

    参数：当前角和目标角，单位均为弧度。返回：与目标同角、但相对当前不超过
    π 的连续角。异常：输入不能转为浮点时抛 ``TypeError``/``ValueError``。
    安全：避免正负 π 边界被解释为接近整圈的运动。
    """

    delta = (float(target) - float(current) + math.pi) % (2.0 * math.pi) - math.pi
    return float(current) + delta


def first_step_exceeds_jump(
    current_positions: Sequence[float],
    first_positions: Sequence[float],
    *,
    threshold_rad: float = DEFAULT_CONTROLLER_JUMP_THRESHOLD_RAD,
) -> bool:
    """判断首轨迹点相对当前观测是否超过控制器阈值。

    参数：当前位置、首点位置及弧度阈值。返回：长度不等或任一轴超阈值时为真。
    异常：元素不能转为浮点时抛 ``TypeError``/``ValueError``。安全：作为控制器
    入口的第二道防线，拒绝整圈跳变。
    """

    if len(current_positions) != len(first_positions):
        return True
    limit = float(threshold_rad)
    return any(
        abs(float(target) - float(current)) > limit
        for current, target in zip(current_positions, first_positions, strict=True)
    )


def unwrap_and_validate_trajectory_points(
    *,
    joint_names: Sequence[str],
    points: Sequence[Sequence[float]],
    current_positions: Mapping[str, float],
    revolute_joint_names: Sequence[str],
    jump_threshold_rad: float = DEFAULT_REVOLUTE_JUMP_THRESHOLD_RAD,
    joint_limits: Mapping[str, tuple[float, float]] | None = None,
) -> tuple[tuple[float, ...], ...]:
    """展开回转轴并在相邻点跳变超阈值时失败关闭。

    参数：关节名、轨迹点、当前观测、回转关节集合、跳变阈值和可选关节限位。
    返回：按输入顺序展开后的不可变轨迹点。异常：空轨迹、缺当前观测、长度
    不一致、非有限值或超阈值时抛 ``ValueError``。安全：直线导轨不做角度展开，
    回转轴不从零位猜测当前值。
    """

    names = tuple(str(name) for name in joint_names)
    if not names or not points:
        raise ValueError("关节轨迹不能为空")
    revolute = {str(name) for name in revolute_joint_names}
    threshold = float(jump_threshold_rad)
    if not math.isfinite(threshold) or threshold <= 0:
        threshold = DEFAULT_REVOLUTE_JUMP_THRESHOLD_RAD
    previous = [
        float(current_positions[name]) if name in current_positions else None
        for name in names
    ]
    sanitized: list[tuple[float, ...]] = []
    for raw_point in points:
        if len(raw_point) != len(names):
            raise ValueError("关节轨迹点与关节名长度不一致")
        next_point: list[float] = []
        for index, name in enumerate(names):
            target = float(raw_point[index])
            if not math.isfinite(target):
                raise ValueError(f"关节 {name} 的轨迹值无效")
            current = previous[index]
            if name in revolute:
                if current is None:
                    raise ValueError(f"关节 {name} 缺少当前观测，拒绝执行")
                value = unwrap_revolute_to_shortest(current, target)
                if joint_limits is not None and name in joint_limits:
                    lower, upper = joint_limits[name]
                    value = min(max(value, float(lower)), float(upper))
                if abs(value - current) > threshold:
                    raise ValueError(f"关节 {name} 跳变超过阈值，拒绝执行")
            else:
                value = target
            next_point.append(value)
        sanitized.append(tuple(next_point))
        previous = list(next_point)
    return tuple(sanitized)


def sanitize_joint_trajectory(
    trajectory: Any,
    *,
    current_joint_state: object | None,
    revolute_joint_names: Sequence[str],
    jump_threshold_rad: float,
) -> None:
    """原地规范 ROS 关节轨迹并保持直线轴量纲。

    参数：轨迹消息、当前完整关节观测、回转关节名和跳变阈值。返回：无。
    异常：缺少观测、数组错位、非有限值或危险跳变时抛 ``ValueError``。
    安全：只对显式回转关节做最短角展开，导轨等直线轴保持原值。
    """

    names = tuple(str(name) for name in getattr(trajectory, "joint_names", ()))
    points = tuple(
        tuple(float(value) for value in point.positions)
        for point in getattr(trajectory, "points", ())
    )
    current_names = tuple(
        str(name) for name in getattr(current_joint_state, "name", ())
    )
    current_values = tuple(
        float(value) for value in getattr(current_joint_state, "position", ())
    )
    current_positions = (
        dict(zip(current_names, current_values, strict=True))
        if current_joint_state is not None
        else {}
    )
    threshold = float(jump_threshold_rad)
    if not math.isfinite(threshold) or threshold <= 0:
        threshold = DEFAULT_REVOLUTE_JUMP_THRESHOLD_RAD
    unwrapped = unwrap_and_validate_trajectory_points(
        joint_names=names,
        points=points,
        current_positions=current_positions,
        revolute_joint_names=revolute_joint_names,
        jump_threshold_rad=threshold,
    )
    for point, positions in zip(trajectory.points, unwrapped, strict=True):
        point.positions = list(positions)


__all__ = [
    "DEFAULT_CONTROLLER_JUMP_THRESHOLD_RAD",
    "DEFAULT_REVOLUTE_JUMP_THRESHOLD_RAD",
    "first_step_exceeds_jump",
    "sanitize_joint_trajectory",
    "unwrap_and_validate_trajectory_points",
    "unwrap_revolute_to_shortest",
]
