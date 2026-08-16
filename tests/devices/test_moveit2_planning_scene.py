"""MoveIt PlanningScene 外部关节读回合同。"""

from __future__ import annotations

from types import SimpleNamespace

from unilabos.devices.ros_dev.moveit2 import MoveIt2


def test_planning_scene_joint_positions_returns_exact_name_value_map() -> None:
    """规划场景关节读回必须先刷新并保留导轨完全限定名。"""

    client = MoveIt2.__new__(MoveIt2)
    client._MoveIt2__planning_scene = SimpleNamespace(
        robot_state=SimpleNamespace(
            joint_state=SimpleNamespace(
                name=["rail_rail_joint"],
                position=[0.0],
            )
        )
    )

    def refresh() -> bool:
        client._MoveIt2__planning_scene.robot_state.joint_state = SimpleNamespace(
            name=["rail_rail_joint", "robot_joint_1"],
            position=[0.35, -0.2],
        )
        return True

    client.update_planning_scene = refresh

    assert client.planning_scene_joint_positions() == {
        "rail_rail_joint": 0.35,
        "robot_joint_1": -0.2,
    }


def test_planning_scene_joint_positions_rejects_refresh_failure_and_malformed_state() -> None:
    """规划场景刷新失败或关节数组错位时必须失败关闭。"""

    client = MoveIt2.__new__(MoveIt2)
    client._MoveIt2__planning_scene = SimpleNamespace(
        robot_state=SimpleNamespace(
            joint_state=SimpleNamespace(name=["rail_rail_joint"], position=[0.35])
        )
    )
    client.update_planning_scene = lambda: False
    assert client.planning_scene_joint_positions() == {}

    client.update_planning_scene = lambda: True
    client._MoveIt2__planning_scene.robot_state.joint_state.position = [0.35]
    client._MoveIt2__planning_scene.robot_state.joint_state.name = [
        "rail_rail_joint",
        "robot_joint_1",
    ]
    assert client.planning_scene_joint_positions() == {}
