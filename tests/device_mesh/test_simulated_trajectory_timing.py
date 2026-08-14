"""内置 MoveIt 仿真轨迹控制器的时间合同。"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from unilabos.device_mesh.simulated_trajectory_timing import play_trajectory


def test_controller_supports_direct_script_launch(tmp_path: Path) -> None:
    """ROS launch 以文件路径执行控制器时，不能依赖 package 相对导入。"""

    controller = (
        Path(__file__).parents[2]
        / "unilabos"
        / "device_mesh"
        / "simulated_trajectory_controller.py"
    )
    result = subprocess.run(
        [sys.executable, str(controller), "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += float(seconds)


def test_dense_trajectory_uses_absolute_deadlines_without_poll_drift() -> None:
    """短轨迹点不得各自向上取整一个轮询周期并拖过 MoveIt 截止时间。"""

    clock = _FakeClock()
    updates: list[tuple[float, ...]] = []

    completed = play_trajectory(
        points=((0.01, (0.1,)), (0.02, (0.2,)), (0.03, (0.3,))),
        initial_positions=(0.0,),
        update=lambda values: updates.append(tuple(values)),
        is_cancel_requested=lambda: False,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert completed is True
    assert clock.now == pytest.approx(0.03)
    assert updates[-1] == pytest.approx((0.3,))


def test_trajectory_cancellation_stops_before_later_targets() -> None:
    """取消必须停止插值，不能继续写入剩余点位。"""

    clock = _FakeClock()
    updates: list[Sequence[float]] = []

    completed = play_trajectory(
        points=((0.10, (1.0,)),),
        initial_positions=(0.0,),
        update=updates.append,
        is_cancel_requested=lambda: clock.now >= 0.04,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert completed is False
    assert clock.now == pytest.approx(0.04)
    assert updates[-1][0] < 1.0
