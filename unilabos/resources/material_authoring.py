"""从 Registry 模板构造 OS ResourceTreeSet 节点。

创建命令只生成一棵新的、未放置或世界坐标放置的资源树。模板目录仍由 Registry
提供，运行时权威仍是调用方持有的 ResourceTreeSet；本模块不保存第二份状态。
"""

from __future__ import annotations

import math
import re
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from typing import Any


class MaterialAuthoringError(ValueError):
    """创建命令或模板不满足本地 Material Graph 契约。"""


def build_material_nodes(
    template: Mapping[str, Any],
    command: Mapping[str, Any],
    *,
    existing_names: Sequence[str],
) -> tuple[list[dict[str, Any]], str]:
    """把已验证的模板详情实例化为扁平 ResourceTreeSet 节点。"""

    template_uuid = _required_text(template.get("uuid"), "template.uuid")
    template_key = _required_text(template.get("key"), "template.key")
    kind = template.get("kind")
    if kind not in {"device", "resource"}:
        raise MaterialAuthoringError("template.kind must be device or resource")
    if template.get("status") != "ready":
        raise MaterialAuthoringError("template is not ready")
    creation = _mapping(template.get("creation"))
    if creation.get("available") is not True:
        raise MaterialAuthoringError(
            str(creation.get("reason") or "template creation is unavailable")
        )

    name = unicodedata.normalize(
        "NFKC",
        _required_text(command.get("name"), "name"),
    ).strip()
    if any(
        unicodedata.normalize("NFKC", current).strip().casefold()
        == name.casefold()
        for current in existing_names
    ):
        raise MaterialAuthoringError("当前物料图中已存在同名物料")

    initial_contents = command.get("initial_contents", [])
    if initial_contents not in (None, []):
        raise MaterialAuthoringError(
            "local Material Graph does not support initial contents"
        )
    placement = _mapping(command.get("placement"))
    placement_kind = placement.get("kind", "unplaced")
    if placement_kind not in {"unplaced", "world"}:
        raise MaterialAuthoringError(
            "local Material create only supports unplaced or world placement"
        )

    instance_uuid = str(uuid.uuid4())
    node_id = _instance_node_id(name, instance_uuid)
    dimensions = _dimensions(template)
    pose = _pose(placement, dimensions)
    user_config = command.get("config")
    if user_config is not None and not isinstance(user_config, Mapping):
        raise MaterialAuthoringError("config must be an object")
    root_config: dict[str, Any] = {
        "resource_template_uuid": template_uuid,
        "template_key": template_key,
        "category": "device" if kind == "device" else "resource",
        "size_x": dimensions[0],
        "size_y": dimensions[1],
        "size_z": dimensions[2],
        "material_placement": {"kind": placement_kind},
        **dict(user_config or {}),
    }
    root = {
        "id": node_id,
        "uuid": instance_uuid,
        "name": name,
        "parent": None,
        "parent_uuid": None,
        "type": "device" if kind == "device" else _resource_type(template),
        "class": template_key,
        "pose": pose,
        "position": dict(pose["position"]),
        "config": root_config,
        "data": {},
        "extra": {},
    }
    children = _container_nodes(template, node_id, instance_uuid)
    return [root, *children], node_id


def _container_nodes(
    template: Mapping[str, Any],
    root_id: str,
    root_uuid: str,
) -> list[dict[str, Any]]:
    layout = _mapping(template.get("container_layout"))
    layout_type = layout.get("type")
    definitions: list[tuple[str, str, Mapping[str, Any], list[float]]] = []
    if layout_type == "grid":
        rows = [
            str(row)
            for row in layout.get("rows", [])
            if isinstance(row, (str, int))
        ]
        columns = _positive_int(layout.get("columns"))
        column_labels = layout.get("column_labels")
        labels = (
            [str(value) for value in column_labels]
            if isinstance(column_labels, list)
            and len(column_labels) == columns
            else [str(index) for index in range(1, columns + 1)]
        )
        geometry = _mapping(layout.get("geometry"))
        pitch = _vector(geometry.get("pitch_mm"))
        offset = _vector(geometry.get("offset_mm"))
        kind = str(layout.get("container_kind") or "well")
        for row_index, row in enumerate(rows):
            for column_index, column in enumerate(labels):
                key = f"{row}{column}"
                definitions.append(
                    (
                        key,
                        kind,
                        geometry,
                        [
                            offset[0] + column_index * pitch[0],
                            offset[1] + row_index * pitch[1],
                            offset[2],
                        ],
                    )
                )
    elif layout_type == "explicit":
        containers = layout.get("containers")
        if isinstance(containers, list):
            for index, raw in enumerate(containers):
                item = _mapping(raw)
                key = str(item.get("key") or index + 1)
                definitions.append(
                    (
                        key,
                        str(item.get("kind") or "site"),
                        _mapping(item.get("geometry")),
                        _vector(item.get("position_mm")),
                    )
                )

    result: list[dict[str, Any]] = []
    for key, kind, geometry, position in definitions:
        child_uuid = str(uuid.uuid4())
        child_id = f"{root_id}-{_safe_token(key)}"
        dimensions = _geometry_dimensions(geometry)
        child_pose = {
            "size": {
                "width": dimensions[0],
                "height": dimensions[1],
                "depth": dimensions[2],
            },
            "position": _point(position),
            "position3d": _point(position),
            "rotation": _point([0.0, 0.0, 0.0]),
            "cross_section_type": (
                "circle"
                if geometry.get("shape") == "circle"
                else "rectangle"
            ),
        }
        result.append(
            {
                "id": child_id,
                "uuid": child_uuid,
                "name": key,
                "parent": root_id,
                "parent_uuid": root_uuid,
                "type": kind.replace("-", "_"),
                "class": "",
                "pose": child_pose,
                "position": dict(child_pose["position"]),
                "config": {
                    "type": kind.replace("-", "_"),
                    "category": kind.replace("-", "_"),
                    "size_x": dimensions[0],
                    "size_y": dimensions[1],
                    "size_z": dimensions[2],
                    **(
                        {"max_volume": float(geometry["max_volume_ul"])}
                        if _finite(geometry.get("max_volume_ul")) > 0
                        else {}
                    ),
                },
                "data": {},
                "extra": {},
            }
        )
    return result


def _pose(
    placement: Mapping[str, Any],
    dimensions: tuple[float, float, float],
) -> dict[str, Any]:
    source = (
        _mapping(placement.get("pose"))
        if placement.get("kind") == "world"
        else {}
    )
    position = _vector(source.get("positionMm"))
    rotation_degrees = _vector(source.get("rotationDegXYZ"))
    rotation = [math.radians(value) for value in rotation_degrees]
    return {
        "size": {
            "width": dimensions[0],
            "height": dimensions[1],
            "depth": dimensions[2],
        },
        "position": _point(position),
        "position3d": _point(position),
        "rotation": _point(rotation),
        "cross_section_type": "rectangle",
    }


def _dimensions(
    template: Mapping[str, Any],
) -> tuple[float, float, float]:
    geometry = _mapping(template.get("geometry"))
    dimensions = _geometry_dimensions(geometry)
    if dimensions != (1.0, 1.0, 1.0):
        return dimensions
    if template.get("kind") == "device":
        return (180.0, 180.0, 120.0)
    return dimensions


def _geometry_dimensions(
    geometry: Mapping[str, Any],
) -> tuple[float, float, float]:
    dimensions = _mapping(geometry.get("dimensions_mm"))
    return (
        _positive(dimensions.get("x"), 1.0),
        _positive(dimensions.get("y"), 1.0),
        _positive(dimensions.get("z"), 1.0),
    )


def _resource_type(template: Mapping[str, Any]) -> str:
    layout = _mapping(template.get("container_layout"))
    if layout.get("container_kind") == "well":
        return "plate"
    return "resource"


def _instance_node_id(name: str, instance_uuid: str) -> str:
    token = _safe_token(name)[:48].strip("-_")
    return f"{token or 'material'}-{instance_uuid[:8]}"


def _safe_token(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]+", "-", value).strip("-").casefold()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MaterialAuthoringError(f"{field} is required")
    return value.strip()


def _vector(value: Any) -> list[float]:
    if isinstance(value, Mapping):
        return [
            _finite(value.get("x")),
            _finite(value.get("y")),
            _finite(value.get("z")),
        ]
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return [_finite(item) for item in value]
    return [0.0, 0.0, 0.0]


def _point(value: Sequence[float]) -> dict[str, float]:
    return {"x": value[0], "y": value[1], "z": value[2]}


def _positive_int(value: Any) -> int:
    number = int(_finite(value))
    return max(0, number)


def _finite(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _positive(value: Any, fallback: float) -> float:
    number = _finite(value)
    return number if number > 0 else fallback
