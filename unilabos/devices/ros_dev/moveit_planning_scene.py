"""MoveIt 规划场景（PlanningScene）稳定读回工具。"""

from __future__ import annotations

import math
from collections.abc import Callable


def read_planning_scene_joint_positions(
    *,
    refresh: Callable[[], bool],
    scene: Callable[[], object | None],
) -> dict[str, float]:
    """刷新并返回完整、有限且名称唯一的关节状态映射。

    参数：规划场景刷新操作和当前场景读取器。返回：关节名到 SI 位置的映射；
    刷新失败或消息畸形时返回空。异常：无。安全：不从本地 ``/joint_states``
    或旧缓存猜测 MoveIt 是否已接纳外部导轨状态。
    """

    if not refresh():
        return {}
    state = getattr(getattr(scene(), "robot_state", None), "joint_state", None)
    names = tuple(str(value) for value in getattr(state, "name", ()) or ())
    positions = tuple(getattr(state, "position", ()) or ())
    if not names or len(names) != len(positions) or len(set(names)) != len(names):
        return {}
    try:
        values = tuple(float(value) for value in positions)
    except (TypeError, ValueError):
        return {}
    if not all(math.isfinite(value) for value in values):
        return {}
    return dict(zip(names, values, strict=True))


__all__ = ["read_planning_scene_joint_positions"]
