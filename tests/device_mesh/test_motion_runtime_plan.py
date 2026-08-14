"""运动运行时（Motion Runtime）与可视化启动计划的公开合同。"""

from unilabos.device_mesh.motion_runtime_plan import plan_motion_runtime


def _robot(backend: str, *, class_name: str = "community.example.robot") -> dict:
    """构造只包含一个机械臂的最小物理图节点。

    参数：``backend`` 是 Graph 显式选择的执行后端，``class_name`` 用于覆盖遗留
    类名。返回：可直接交给公开规划接口的节点映射。安全：测试不会加载 ROS 或
    实例化设备。
    """

    return {
        "robot": {
            "id": "robot",
            "type": "device",
            "class": class_name,
            "config": {"standard_execution_backend": backend},
        }
    }


def test_moveit_motion_runtime_does_not_depend_on_visual_mode() -> None:
    """关闭界面不得关闭显式选择的 MoveIt 运动运行时。"""

    disabled = plan_motion_runtime(_robot("moveit_sim"), visual="disable")
    web = plan_motion_runtime(_robot("moveit_sim"), visual="web")
    rviz = plan_motion_runtime(_robot("moveit_sim"), visual="rviz")

    assert disabled.moveit_device_ids == ("robot",)
    assert web.moveit_device_ids == disabled.moveit_device_ids
    assert rviz.moveit_device_ids == disabled.moveit_device_ids
    assert disabled.motion_runtime_required is True
    assert disabled.visualization_enabled is False
    assert disabled.enable_rviz is False
    assert web.visualization_enabled is True
    assert web.enable_rviz is False
    assert rviz.visualization_enabled is True
    assert rviz.enable_rviz is True


def test_plc_robot_does_not_start_moveit_merely_for_rviz() -> None:
    """PLC 机械臂打开 RViz 时仍不得获得第二套 MoveIt 执行器。"""

    plan = plan_motion_runtime(_robot("plc"), visual="rviz")

    assert plan.motion_runtime_required is False
    assert plan.moveit_device_ids == ()
    assert plan.visualization_enabled is True
    assert plan.enable_rviz is True


def test_explicit_plc_backend_overrides_legacy_moveit_class_name() -> None:
    """Graph 的显式 HardwareProfile 投影优先于遗留类名启发式。"""

    plan = plan_motion_runtime(
        _robot("plc", class_name="robotic_arm.legacy.moveit.virtual"),
        visual="disable",
    )

    assert plan.motion_runtime_required is False
    assert plan.moveit_device_ids == ()


def test_legacy_moveit_class_remains_headless_compatible() -> None:
    """尚未迁移的内置 MoveIt 类在无显式后端时仍可无界面启动。"""

    robot = _robot("", class_name="robotic_arm.legacy.moveit.virtual")
    plan = plan_motion_runtime(robot, visual="disable")

    assert plan.moveit_device_ids == ("robot",)
    assert plan.motion_runtime_required is True
    assert plan.enable_rviz is False


def test_non_device_nodes_never_request_motion_runtime() -> None:
    """物料或库位上的同名配置不得被解释为设备执行后端。"""

    resource = _robot("moveit_sim")
    resource["robot"]["type"] = "resource"

    assert plan_motion_runtime(resource, visual="disable").moveit_device_ids == ()
