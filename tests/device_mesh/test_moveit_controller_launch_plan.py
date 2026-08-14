"""MoveIt ros2_control 启动顺序的回归合同。"""

from unilabos.device_mesh.resource_visalization import (
    controller_spawn_order,
    should_use_builtin_simulation_controller,
    simulation_controller_specs,
)


def test_joint_state_broadcaster_starts_before_motion_controllers() -> None:
    """macOS/RoboStack 下不得并行切换 broadcaster 与轨迹控制器。"""

    assert controller_spawn_order(
        ["robot_cr5_controller", "rail_controller"]
    ) == (
        "joint_state_broadcaster",
        "robot_cr5_controller",
        "rail_controller",
    )


def test_controller_spawn_order_is_stable_and_deduplicated() -> None:
    """合并多个 Package MoveIt 模型后仍只允许每个控制器激活一次。"""

    assert controller_spawn_order(
        ["robot_controller", "robot_controller", "joint_state_broadcaster"]
    ) == ("joint_state_broadcaster", "robot_controller")


def test_darwin_simulation_uses_builtin_trajectory_controller() -> None:
    """macOS 仿真避开已知会在 switch_controller 崩溃的原生路径。"""

    assert should_use_builtin_simulation_controller(
        platform_name="darwin",
        moveit_device_ids=("robot",),
        simulated_moveit_device_ids=("robot",),
    ) is True
    assert should_use_builtin_simulation_controller(
        platform_name="linux",
        moveit_device_ids=("robot",),
        simulated_moveit_device_ids=("robot",),
    ) is False


def test_mixed_live_and_simulation_never_selects_builtin_controller() -> None:
    """同一 Launch 混入 Live Device 时必须关闭失败到原生硬件路径。"""

    assert should_use_builtin_simulation_controller(
        platform_name="darwin",
        moveit_device_ids=("robot", "robot_live"),
        simulated_moveit_device_ids=("robot",),
    ) is False


def test_simulation_controller_specs_follow_moveit_contract() -> None:
    """仿真 Action 名称和关节列表必须直接来自 MoveIt controller 配置。"""

    specs = simulation_controller_specs(
        {
            "moveit_simple_controller_manager": {
                "controller_names": ["robot_cr5_controller"],
                "robot_cr5_controller": {
                    "type": "FollowJointTrajectory",
                    "action_ns": "follow_joint_trajectory",
                    "joints": ["robot_cr5_joint_1", "robot_cr5_joint_2"],
                },
            }
        }
    )

    assert specs == (
        {
            "name": "robot_cr5_controller",
            "action": "/robot_cr5_controller/follow_joint_trajectory",
            "joints": ["robot_cr5_joint_1", "robot_cr5_joint_2"],
        },
    )
