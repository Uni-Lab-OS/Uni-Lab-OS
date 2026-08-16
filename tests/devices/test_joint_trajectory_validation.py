"""验证关节轨迹在派发前按最短角展开并拒绝危险跳变。"""

import math

import pytest


def test_unwrap_revolute_crosses_pi_by_shortest_path():
    """跨越正负 π 边界时必须选择约两度的最短路径。

    参数：无。返回：无。异常：无。安全：避免把边界跨越解释为近一整圈运动。
    """

    from unilabos.devices.ros_dev.joint_trajectory_validation import (
        unwrap_revolute_to_shortest,
    )

    current = math.radians(179.0)
    unwrapped = unwrap_revolute_to_shortest(current, math.radians(-179.0))
    assert math.degrees(unwrapped - current) == pytest.approx(2.0)


def test_validate_trajectory_preserves_prismatic_and_unwraps_revolute():
    """导轨保持线性值，机械臂回转轴按前一点连续展开。

    参数：无。返回：无。异常：无。安全：不同关节类型不得共用角度规则。
    """

    from unilabos.devices.ros_dev.joint_trajectory_validation import (
        unwrap_and_validate_trajectory_points,
    )

    points = unwrap_and_validate_trajectory_points(
        joint_names=("rail_joint", "arm_joint"),
        points=((0.4, math.radians(-179.0)),),
        current_positions={
            "rail_joint": 0.2,
            "arm_joint": math.radians(179.0),
        },
        revolute_joint_names=("arm_joint",),
        jump_threshold_rad=math.radians(5.0),
    )

    assert points[0][0] == pytest.approx(0.4)
    assert math.degrees(points[0][1]) == pytest.approx(181.0)


def test_validate_trajectory_fails_closed_without_observation_or_on_jump():
    """缺少当前观测或首点角跳变过大时必须失败关闭。

    参数：无。返回：无。异常：断言 ``ValueError``。安全：不从零位猜测当前姿态。
    """

    from unilabos.devices.ros_dev.joint_trajectory_validation import (
        unwrap_and_validate_trajectory_points,
    )

    with pytest.raises(ValueError, match="缺少当前观测"):
        unwrap_and_validate_trajectory_points(
            joint_names=("arm_joint",),
            points=((0.1,),),
            current_positions={},
            revolute_joint_names=("arm_joint",),
        )

    with pytest.raises(ValueError, match="跳变超过阈值"):
        unwrap_and_validate_trajectory_points(
            joint_names=("arm_joint",),
            points=((math.radians(80.0),),),
            current_positions={"arm_joint": 0.0},
            revolute_joint_names=("arm_joint",),
            jump_threshold_rad=math.radians(45.0),
        )


def test_controller_first_step_guard_rejects_shape_and_large_jump():
    """仿真控制器必须拒绝首点长度错位和超过 π 的跳变。

    参数：无。返回：无。异常：无。安全：控制器入口保留第二道独立防线。
    """

    from unilabos.devices.ros_dev.joint_trajectory_validation import (
        first_step_exceeds_jump,
    )

    assert first_step_exceeds_jump((0.0,), (0.0, 1.0))
    assert first_step_exceeds_jump((0.0,), (math.pi + 0.01,))
    assert not first_step_exceeds_jump((0.0,), (math.pi,))
