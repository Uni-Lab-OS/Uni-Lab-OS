"""运动运行时（MotionRuntime）与显示开关的正交计划测试。"""

from unilabos.device_mesh.motion_runtime_plan import plan_motion_runtime


def test_moveit_execution_survives_disabled_visualization() -> None:
    plan = plan_motion_runtime(
        {
            "robot": {
                "id": "robot",
                "type": "device",
                "class": "community.robot",
                "config": {"execution_backend": "moveit"},
            }
        },
        visual="disable",
    )
    assert plan.moveit_device_ids == ("robot",)
    assert plan.motion_runtime_required is True
    assert plan.visualization_enabled is False
    assert plan.ros_launch_required is True


def test_rviz_does_not_promote_plc_device_to_moveit() -> None:
    plan = plan_motion_runtime(
        {
            "robot": {
                "id": "robot",
                "type": "device",
                "class": "community.robot.moveit.compat",
                "config": {"execution_backend": "plc"},
            }
        },
        visual="rviz",
    )
    assert plan.moveit_device_ids == ()
    assert plan.visualization_enabled is True
    assert plan.enable_rviz is True
    assert plan.ros_launch_required is True
