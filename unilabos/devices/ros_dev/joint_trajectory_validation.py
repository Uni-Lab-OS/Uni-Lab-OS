"""在执行前把回转轴展开到最短角，并拒绝整圈跳变。"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

DEFAULT_REVOLUTE_JUMP_THRESHOLD_RAD = math.pi / 2
DEFAULT_CONTROLLER_JUMP_THRESHOLD_RAD = math.pi


def unwrap_revolute_to_shortest(current: float, target: float) -> float:
    """把目标角展开到相对当前值的最短有向角。"""

    delta = (float(target) - float(current) + math.pi) % (2.0 * math.pi) - math.pi
    return float(current) + delta


def first_step_exceeds_jump(
    current_positions: Sequence[float],
    first_positions: Sequence[float],
    *,
    threshold_rad: float = DEFAULT_CONTROLLER_JUMP_THRESHOLD_RAD,
) -> bool:
    """第一点相对当前若任一轴超过阈值，则仿真控制器应拒绝。"""

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

    参数：``points`` 是规划器给出的各点关节位置；``current_positions`` 是
    执行前观测。返回展开后的轨迹点。异常：缺名、长度不一致或 ``|Δq|``
    超过阈值时抛 ``ValueError``。
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
                    raise ValueError(
                        f"关节 {name} 跳变超过阈值，拒绝执行"
                    )
            else:
                value = target
            next_point.append(value)
        sanitized.append(tuple(next_point))
        previous = list(next_point)
    return tuple(sanitized)


__all__ = [
    "DEFAULT_CONTROLLER_JUMP_THRESHOLD_RAD",
    "DEFAULT_REVOLUTE_JUMP_THRESHOLD_RAD",
    "first_step_exceeds_jump",
    "unwrap_and_validate_trajectory_points",
    "unwrap_revolute_to_shortest",
]
