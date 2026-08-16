"""MoveIt2 工具上下文（ToolContext）规划场景桥接测试。"""

from __future__ import annotations

from types import SimpleNamespace

from moveit_msgs.msg import CollisionObject
from shape_msgs.msg import SolidPrimitive

from unilabos.devices.ros_dev.moveit2 import MoveIt2


class _ApplyService:
    """记录同步 ApplyPlanningScene 请求的最小服务替身。"""

    def __init__(self) -> None:
        self.requests: list[object] = []

    def service_is_ready(self) -> bool:
        return True

    def call(self, request: object) -> SimpleNamespace:
        self.requests.append(request)
        return SimpleNamespace(success=True)


def test_apply_tool_context_installs_and_confirms_attached_collision_body() -> None:
    """当前 pTLC ToolContext 应成为带限定 link 的 MoveIt 附着碰撞体。"""

    client = MoveIt2.__new__(MoveIt2)
    client._MoveIt2__end_effector_name = "robot_cr5_link_6"
    service = _ApplyService()
    client._apply_planning_scene_service = service

    attached = SimpleNamespace(
        link_name="robot_cr5_link_6",
        object=SimpleNamespace(id="ptlc-cr5-controller-tool1"),
    )
    client._MoveIt2__planning_scene = SimpleNamespace(
        robot_state=SimpleNamespace(attached_collision_objects=[attached])
    )
    client.update_planning_scene = lambda: True
    context = SimpleNamespace(
        digest="a" * 64,
        attachment_generation=1,
        planning_scene={
            "attached_body_id": "ptlc-cr5-controller-tool1",
            "parent_link": "cr5_link_6",
            "allowed_touch_links": ["cr5_link_6", "cr5_link_5"],
            "collision_primitives": [
                {
                    "shape": "box",
                    "size_m": [0.04, 0.04, 0.08],
                    "pose": {
                        "xyz_m": [0.0, 0.0, 0.04],
                        "orientation_xyzw": [0.0, 0.0, 0.0, 1.0],
                    },
                }
            ],
        },
    )

    receipt = client.apply_tool_context(context)

    assert receipt == {
        "applied": True,
        "tool_context_digest": "a" * 64,
        "attachment_generation": 1,
    }
    request = service.requests[0]
    assert request.scene.is_diff is True
    assert request.scene.robot_state.is_diff is True
    message = request.scene.robot_state.attached_collision_objects[0]
    assert message.link_name == "robot_cr5_link_6"
    assert message.touch_links == ["robot_cr5_link_6", "robot_cr5_link_5"]
    assert message.object.id == "ptlc-cr5-controller-tool1"
    assert message.object.operation == CollisionObject.ADD
    assert message.object.primitives[0].type == SolidPrimitive.BOX
    assert list(message.object.primitives[0].dimensions) == [0.04, 0.04, 0.08]
