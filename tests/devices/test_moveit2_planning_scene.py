"""MoveIt PlanningScene 外部关节读回合同。"""

from __future__ import annotations

from types import SimpleNamespace

from unilabos.devices.ros_dev.moveit2 import MoveIt2


def test_cartesian_fraction_below_threshold_is_retained_for_diagnostics() -> None:
    """零覆盖率路径必须被拒绝，并保留规划错误码与覆盖率供上层解释。"""

    response = SimpleNamespace(
        error_code=SimpleNamespace(val=1),
        fraction=0.0,
        solution=SimpleNamespace(joint_trajectory=object()),
    )
    future = SimpleNamespace(done=lambda: True, result=lambda: response)
    client = MoveIt2.__new__(MoveIt2)
    client._MoveIt2__last_planning_error_code = None
    client._MoveIt2__last_cartesian_fraction = None
    client._node = SimpleNamespace(
        get_logger=lambda: SimpleNamespace(warn=lambda _message: None)
    )

    trajectory = client.get_trajectory(
        future,
        cartesian=True,
        cartesian_fraction_threshold=1.0,
    )

    assert trajectory is None
    assert client.get_last_planning_error_code().val == 1
    assert client.get_last_cartesian_fraction() == 0.0


def test_planning_scene_async_seam_stores_service_response() -> None:
    """场景安装器必须能异步请求并显式接纳 GetPlanningScene 响应。"""

    scene = SimpleNamespace(name="current-scene")
    future = SimpleNamespace(
        done=lambda: True,
        result=lambda: SimpleNamespace(scene=scene),
    )

    class Service:
        srv_name = "/get_planning_scene"

        @staticmethod
        def service_is_ready() -> bool:
            return True

        @staticmethod
        def call_async(request: object) -> object:
            assert request is not None
            return future

    client = MoveIt2.__new__(MoveIt2)
    client._get_planning_scene_service = Service()
    client._MoveIt2__planning_scene = None

    pending = client.request_planning_scene_update()

    assert pending is future
    assert client.process_planning_scene_update(pending) is True
    assert client.planning_scene is scene


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
