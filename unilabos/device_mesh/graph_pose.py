"""解析物理图（Graph）右手 Z-up 父子安装位姿。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

PoseMatrix = tuple[tuple[float, ...], ...]


def resolve_graph_world_pose(
    node: Mapping[str, Any],
    graph_nodes: Mapping[str, Mapping[str, Any]],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """把相对毫米/角度位姿合成为世界米/弧度位姿。

    参数：目标物理图成员与全图成员。返回：世界 ``xyz``（米）及 URDF 固定轴
    ``rpy``（弧度）。异常：缺失父节点、重复身份、环或非有限坐标时关闭失败。
    """

    members, by_uuid = _normalized_graph_members(graph_nodes)
    node_id = str(node.get("id") or "").strip()
    if node_id not in members:
        raise ValueError("物理图不包含目标成员: " + node_id)
    cache: dict[str, PoseMatrix] = {}
    visiting: set[str] = set()

    def world_matrix(member_id: str) -> PoseMatrix:
        cached = cache.get(member_id)
        if cached is not None:
            return cached
        if member_id in visiting:
            raise ValueError("物理图父子关系存在环")
        visiting.add(member_id)
        member = members[member_id]
        local = _local_pose_matrix(member)
        parent_id = _graph_parent_id(member, members, by_uuid)
        result = (
            local
            if not parent_id
            else _multiply_matrix(world_matrix(parent_id), local)
        )
        visiting.remove(member_id)
        cache[member_id] = result
        return result

    matrix = world_matrix(node_id)
    return (
        (float(matrix[0][3]), float(matrix[1][3]), float(matrix[2][3])),
        _matrix_rpy(matrix),
    )


def resolve_graph_parent_id(
    node: Mapping[str, Any],
    graph_nodes: Mapping[str, Mapping[str, Any]],
) -> str:
    """解析物理图节点的直接父 id。

    参数：目标节点和完整物理图。返回：根节点为空串，否则返回规范设备 id。
    异常：节点、父 id/UUID 缺失或歧义时关闭失败。安全：``parent`` 与运行态
    ``parent_uuid`` 共用同一解析规则，不按边或 ``children`` 猜测父级。
    """

    members, by_uuid = _normalized_graph_members(graph_nodes)
    node_id = str(node.get("id") or "").strip()
    if node_id not in members:
        raise ValueError("物理图不包含目标成员: " + node_id)
    return _graph_parent_id(members[node_id], members, by_uuid)


def _normalized_graph_members(
    graph_nodes: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], dict[str, str]]:
    """校验物理图身份并建立 id/UUID 只读索引。"""

    if any(not isinstance(candidate, Mapping) for candidate in graph_nodes.values()):
        raise TypeError("物理图节点必须全部是 Mapping")
    members: dict[str, Mapping[str, Any]] = {}
    for candidate in graph_nodes.values():
        candidate_id = str(candidate.get("id") or "").strip()
        if not candidate_id or candidate_id in members:
            raise ValueError("物理图成员 id 必须存在且唯一")
        members[candidate_id] = candidate
    by_uuid = {
        str(candidate.get("uuid") or "").strip(): candidate_id
        for candidate_id, candidate in members.items()
        if str(candidate.get("uuid") or "").strip()
    }
    if len(by_uuid) != sum(
        bool(str(candidate.get("uuid") or "").strip())
        for candidate in members.values()
    ):
        raise ValueError("物理图成员 uuid 必须唯一")
    return members, by_uuid


def _graph_parent_id(
    member: Mapping[str, Any],
    members: Mapping[str, Mapping[str, Any]],
    members_by_uuid: Mapping[str, str],
) -> str:
    """从工作区 id 或运行时 ``parent_uuid`` 解析唯一父节点。"""

    for raw in (member.get("parent"), member.get("parent_uuid")):
        token = str(raw or "").strip()
        if not token:
            continue
        if token in members:
            return token
        mapped = members_by_uuid.get(token)
        if mapped:
            return mapped
        raise ValueError("物理图父节点不存在: " + token)
    return ""


def _local_pose_matrix(node: Mapping[str, Any]) -> PoseMatrix:
    """读取单个相对位姿，不在 ``pose`` 与遗留 ``position`` 间猜测。"""

    raw_pose = node.get("pose")
    raw_position = node.get("position")
    if isinstance(raw_pose, Mapping) and isinstance(raw_position, Mapping):
        raise ValueError("物理图成员不能同时声明 pose 与 position")
    container = raw_pose if isinstance(raw_pose, Mapping) else raw_position
    if not isinstance(container, Mapping):
        container = {}
    nested_position = container.get("position")
    position = nested_position if isinstance(nested_position, Mapping) else container
    raw_rotation = container.get("rotation")
    rotation = raw_rotation if isinstance(raw_rotation, Mapping) else {}
    xyz_mm = tuple(_finite(position.get(axis), f"position.{axis}") for axis in "xyz")
    rpy_deg = tuple(_finite(rotation.get(axis), f"rotation.{axis}") for axis in "xyz")
    roll, pitch, yaw = (math.radians(value) for value in rpy_deg)
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    x_m, y_m, z_m = (value / 1000.0 for value in xyz_mm)
    return (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr, x_m),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr, y_m),
        (-sp, cp * sr, cp * cr, z_m),
        (0.0, 0.0, 0.0, 1.0),
    )


def _multiply_matrix(left: PoseMatrix, right: PoseMatrix) -> PoseMatrix:
    return tuple(
        tuple(
            sum(left[row][index] * right[index][column] for index in range(4))
            for column in range(4)
        )
        for row in range(4)
    )


def _matrix_rpy(matrix: PoseMatrix) -> tuple[float, float, float]:
    pitch = math.asin(max(-1.0, min(1.0, -matrix[2][0])))
    return (
        math.atan2(matrix[2][1], matrix[2][2]),
        pitch,
        math.atan2(matrix[1][0], matrix[0][0]),
    )


def _finite(value: object, label: str) -> float:
    if value is None:
        return 0.0
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("物理图 " + label + " 必须是数值") from error
    if not math.isfinite(result):
        raise ValueError("物理图 " + label + " 必须是有限值")
    return result


__all__ = ["resolve_graph_parent_id", "resolve_graph_world_pose"]
