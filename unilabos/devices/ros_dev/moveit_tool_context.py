"""把版本化工具上下文（ToolContext）应用到 MoveIt 规划场景。"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

from geometry_msgs.msg import Point, Pose, Quaternion
from moveit_msgs.msg import AttachedCollisionObject, CollisionObject, PlanningScene
from moveit_msgs.srv import ApplyPlanningScene
from shape_msgs.msg import SolidPrimitive

_SHAPES = {
    "box": (SolidPrimitive.BOX, 3),
    "sphere": (SolidPrimitive.SPHERE, 1),
    "cylinder": (SolidPrimitive.CYLINDER, 2),
    "cone": (SolidPrimitive.CONE, 2),
}


def apply_tool_context(
    client: Any,
    tool_context: Any,
    *,
    end_effector_name: str,
    confirmation_timeout_s: float = 5.0,
) -> dict[str, Any]:
    """安装附着碰撞体并以 MoveIt 回读确认摘要对应的附着代次。

    参数：``client`` 是现有 MoveIt2 客户端，``tool_context`` 是机械臂包已校验
    的工具上下文（ToolContext），``end_effector_name`` 是部署后完全限定的末端
    link。返回：只有 ApplyPlanningScene 成功且回读同一附着体后才生成确认。
    安全：不支持的几何、link 漂移、服务失败或回读超时全部失败关闭。
    """

    scene_data = _mapping(tool_context.planning_scene, "ToolContext.planning_scene")
    body_id = _text(scene_data.get("attached_body_id"), "attached_body_id")
    parent_link = _text(scene_data.get("parent_link"), "parent_link")
    prefix = _link_prefix(parent_link, end_effector_name)
    touch_links = [
        _qualified_link(_text(item, "allowed_touch_links"), prefix)
        for item in _sequence(
            scene_data.get("allowed_touch_links", ()),
            "allowed_touch_links",
        )
    ]
    primitives = _sequence(
        scene_data.get("collision_primitives"),
        "collision_primitives",
    )
    if not primitives:
        raise ValueError("ToolContext.collision_primitives 不能为空")

    collision = CollisionObject()
    collision.header.frame_id = end_effector_name
    collision.id = body_id
    collision.operation = CollisionObject.ADD
    for index, raw_primitive in enumerate(primitives):
        primitive, pose = _compile_primitive(raw_primitive, index=index)
        collision.primitives.append(primitive)
        collision.primitive_poses.append(pose)

    attached = AttachedCollisionObject()
    attached.link_name = end_effector_name
    attached.touch_links = touch_links
    attached.object = collision
    scene = PlanningScene()
    scene.is_diff = True
    scene.robot_state.is_diff = True
    scene.robot_state.attached_collision_objects = [attached]

    service = client._apply_planning_scene_service
    if not service.service_is_ready():
        raise RuntimeError("MoveIt ApplyPlanningScene 服务未就绪")
    response = service.call(ApplyPlanningScene.Request(scene=scene))
    if response is None or response.success is not True:
        raise RuntimeError("MoveIt 拒绝应用 ToolContext PlanningScene")

    deadline = time.monotonic() + float(confirmation_timeout_s)
    while time.monotonic() < deadline:
        if client.update_planning_scene() and _attached_body_confirmed(
            client.planning_scene,
            body_id=body_id,
            link_name=end_effector_name,
        ):
            return {
                "applied": True,
                "tool_context_digest": str(tool_context.digest),
                "attachment_generation": int(tool_context.attachment_generation),
            }
        time.sleep(0.05)
    raise RuntimeError("MoveIt PlanningScene 未回读 ToolContext 附着碰撞体")


def _compile_primitive(raw: object, *, index: int) -> tuple[SolidPrimitive, Pose]:
    value = _mapping(raw, f"collision_primitives[{index}]")
    shape = _text(value.get("shape"), f"collision_primitives[{index}].shape")
    try:
        shape_type, dimension_count = _SHAPES[shape]
    except KeyError as error:
        raise ValueError(f"ToolContext 不支持碰撞几何: {shape}") from error
    dimensions = _float_sequence(
        value.get("size_m"),
        dimension_count,
        f"collision_primitives[{index}].size_m",
    )
    if any(item <= 0.0 for item in dimensions):
        raise ValueError("ToolContext 碰撞几何尺寸必须为正数")
    pose_data = _mapping(value.get("pose"), f"collision_primitives[{index}].pose")
    xyz = _float_sequence(
        pose_data.get("xyz_m"),
        3,
        f"collision_primitives[{index}].pose.xyz_m",
    )
    orientation = _float_sequence(
        pose_data.get("orientation_xyzw"),
        4,
        f"collision_primitives[{index}].pose.orientation_xyzw",
    )
    return (
        SolidPrimitive(type=shape_type, dimensions=list(dimensions)),
        Pose(
            position=Point(x=xyz[0], y=xyz[1], z=xyz[2]),
            orientation=Quaternion(
                x=orientation[0],
                y=orientation[1],
                z=orientation[2],
                w=orientation[3],
            ),
        ),
    )


def _attached_body_confirmed(
    scene: object,
    *,
    body_id: str,
    link_name: str,
) -> bool:
    robot_state = getattr(scene, "robot_state", None)
    for item in getattr(robot_state, "attached_collision_objects", ()):
        if (
            str(getattr(getattr(item, "object", None), "id", "")) == body_id
            and str(getattr(item, "link_name", "")) == link_name
        ):
            return True
    return False


def _link_prefix(parent_link: str, end_effector_name: str) -> str:
    if parent_link == end_effector_name:
        return ""
    if end_effector_name.endswith(f"_{parent_link}"):
        return end_effector_name[: -len(parent_link)]
    raise ValueError("ToolContext.parent_link 与 MoveIt 末端 link 不一致")


def _qualified_link(link_name: str, prefix: str) -> str:
    if not prefix or link_name.startswith(prefix):
        return link_name
    return f"{prefix}{link_name}"


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} 必须是对象")
    return value


def _sequence(value: object, name: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError(f"{name} 必须是数组")
    return value


def _float_sequence(value: object, size: int, name: str) -> tuple[float, ...]:
    raw = _sequence(value, name)
    if len(raw) != size:
        raise ValueError(f"{name} 必须包含 {size} 个数")
    try:
        return tuple(float(item) for item in raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} 必须包含 {size} 个数") from error


def _text(value: object, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{name} 不能为空")
    return result


__all__ = ["apply_tool_context"]
