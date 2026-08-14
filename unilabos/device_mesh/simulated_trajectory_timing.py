"""内置仿真控制器的无 ROS 轨迹计时内核。"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence

TrajectoryPoint = tuple[float, Sequence[float]]


def play_trajectory(
    *,
    points: Iterable[TrajectoryPoint],
    initial_positions: Sequence[float],
    update: Callable[[Sequence[float]], None],
    is_cancel_requested: Callable[[], bool],
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval_s: float = 0.02,
) -> bool:
    """按 ``time_from_start`` 绝对截止时间插值，避免逐点轮询误差累积。"""

    started = monotonic()
    previous_positions = tuple(float(value) for value in initial_positions)
    previous_time = 0.0
    for raw_point_time, raw_target in points:
        point_time = max(previous_time, float(raw_point_time))
        target = tuple(float(value) for value in raw_target)
        if len(target) != len(previous_positions):
            raise ValueError("仿真轨迹点的关节数量不一致")
        segment_duration = point_time - previous_time
        while True:
            if is_cancel_requested():
                return False
            elapsed = max(0.0, monotonic() - started)
            ratio = (
                1.0
                if segment_duration <= 0.0
                else min(1.0, max(0.0, (elapsed - previous_time) / segment_duration))
            )
            update(tuple(
                current + (wanted - current) * ratio
                for current, wanted in zip(
                    previous_positions, target, strict=True
                )
            ))
            remaining = point_time - elapsed
            if remaining <= 0.0:
                break
            sleep(min(max(0.001, poll_interval_s), remaining))
        update(target)
        previous_positions = target
        previous_time = point_time
    return True
