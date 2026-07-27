"""把 OS 设备图投影为统一的只读 Material API。

本地桥不维护第二份物料数据库，而是缓存 OS 通过 schedule 通道发布的
当前内存物料快照，并将物料粒度节点投影为与 Backend API 相同的
``GET /api/v1/materials`` 数据行。PLR Well 和 TipSpot 子节点只作为
渲染/详情数据，转换为所属物料的 Site，避免在前端生成数百个顶层节点。
"""

from __future__ import annotations

import copy
import json
import logging
import math
import threading
import uuid
import zlib
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from unilabos.app.local_bridge.material_models import MaterialModelRegistry


_INTERNAL_SITE_TYPES = {"tipspot", "tip_spot", "well"}
_SBS_FOOTPRINT_MM = (127.76, 85.48, 0.0)
_HAMILTON_RAIL_WIDTH_MM = 22.5

logger = logging.getLogger(__name__)


class MaterialGraphUnavailable(RuntimeError):
    """OS 本地服务没有配置可读的物料图。"""


class InvalidMaterialQuery(ValueError):
    """Material API 查询参数不符合统一契约。"""


class MaterialGraphCatalog:
    """缓存 OS 当前内存物料快照，并提供只读 Material API 投影。

    ``graph_path`` 仅保留为测试/离线模式的一次性启动输入。构造完成后不会再次
    读取文件；真实 OS 模式通过 ``replace_snapshot`` 接收 ResourceTreeSet 的当前
    快照，因此桥不是物料权威。
    """

    def __init__(
        self,
        graph_path: str | Path | None = None,
        *,
        model_registry: MaterialModelRegistry | None = None,
    ) -> None:
        self.model_registry = model_registry or MaterialModelRegistry()
        self._lock = threading.RLock()
        self.graph_path = Path("os-current")
        self._nodes: list[dict[str, Any]] | None = None
        self._revision = 0
        self._modified_at = ""
        if graph_path is not None:
            source_path = Path(graph_path).expanduser().resolve()
            if not source_path.is_file():
                raise MaterialGraphUnavailable(
                    f"Material graph does not exist: {source_path}"
                )
            source = source_path.read_text(encoding="utf-8")
            payload = json.loads(source)
            self.replace_snapshot(
                {
                    "source_id": source_path.name,
                    "revision": max(
                        zlib.crc32(source.encode("utf-8")),
                        1,
                    ),
                    "modified_at": datetime.fromtimestamp(
                        source_path.stat().st_mtime,
                        tz=timezone.utc,
                    ).isoformat().replace("+00:00", "Z"),
                    "nodes": (
                        payload.get("nodes")
                        if isinstance(payload, Mapping)
                        else None
                    ),
                }
            )
            logger.info(
                "[material-state] 已一次性加载离线物料图=%s，本地模型=%d",
                source_path,
                len(self.model_registry.list_models()),
            )

    @property
    def is_available(self) -> bool:
        with self._lock:
            return self._nodes is not None

    def replace_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        """原子替换 bridge 中的只读投影缓存。

        snapshot 必须来自当前 OS 内存 ResourceTreeSet，不能由 HTTP/UI 构造。
        """

        raw_nodes = snapshot.get("nodes")
        if not isinstance(raw_nodes, list):
            raise MaterialGraphUnavailable(
                "Material snapshot has no nodes array"
            )
        nodes = [
            copy.deepcopy(dict(node))
            for node in raw_nodes
            if isinstance(node, Mapping)
        ]
        source_id = _public_source_id(snapshot.get("source_id"))
        revision = _positive_revision(snapshot.get("revision"), nodes)
        modified_at = str(snapshot.get("modified_at") or "")
        if not modified_at:
            modified_at = datetime.now(tz=timezone.utc).isoformat().replace(
                "+00:00",
                "Z",
            )
        with self._lock:
            self.graph_path = Path(source_id)
            self._nodes = nodes
            self._revision = revision
            self._modified_at = modified_at
        logger.info(
            "[material-state] 已接收 OS 内存快照 source=%s revision=%d nodes=%d",
            source_id,
            revision,
            len(nodes),
        )

    def list_materials(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        name: str | None = None,
        code: str | None = None,
        resource_template_uuid: str | None = None,
    ) -> dict[str, Any]:
        if page < 1:
            raise InvalidMaterialQuery("page must be greater than or equal to 1")
        if page_size < 1 or page_size > 100:
            raise InvalidMaterialQuery("page_size must be between 1 and 100")

        template_filter: str | None = None
        if resource_template_uuid:
            template_filter = _canonical_uuid(
                resource_template_uuid,
                field="resource_template_uuid",
            )

        rows = self._material_rows()
        if name:
            needle = name.strip().casefold()
            rows = [
                row
                for row in rows
                if needle in str(row["name"]).casefold()
            ]
        if code:
            needle = code.strip().casefold()
            rows = [
                row
                for row in rows
                if needle in str(row["code"]).casefold()
            ]
        if template_filter:
            rows = [
                row
                for row in rows
                if row["resource_template_uuid"] == template_filter
            ]

        total = len(rows)
        start = (page - 1) * page_size
        return {
            "items": rows[start : start + page_size],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def get_material(self, material_uuid: str) -> dict[str, Any] | None:
        wanted = _canonical_uuid(material_uuid, field="material_uuid")
        return next(
            (
                row
                for row in self._material_rows()
                if row["uuid"] == wanted
            ),
            None,
        )

    def _material_rows(self) -> list[dict[str, Any]]:
        with self._lock:
            raw_nodes = copy.deepcopy(self._nodes)
            graph_revision = self._revision
            modified_at = self._modified_at
            graph_path = self.graph_path
        if raw_nodes is None:
            raise MaterialGraphUnavailable(
                "OS has not published its current material snapshot"
            )

        nodes = [
            dict(node)
            for node in raw_nodes
            if isinstance(node, Mapping)
        ]
        material_nodes = [
            node
            for node in nodes
            if not _is_internal_site(node) and not _is_deck_slot_node(node)
        ]
        material_node_ids = {
            str(node.get("id") or "") for node in material_nodes
        }
        material_uuid_by_node_id = {
            node_id: _stable_uuid(
                graph_path,
                "material",
                node_id,
            )
            for node_id in material_node_ids
            if node_id
        }
        children_by_parent: dict[str, list[dict[str, Any]]] = {}
        nodes_by_id: dict[str, dict[str, Any]] = {}
        for node in nodes:
            node_id = str(node.get("id") or "")
            if node_id:
                nodes_by_id[node_id] = node
            parent_id = _optional_string(node.get("parent"))
            if parent_id:
                children_by_parent.setdefault(parent_id, []).append(node)

        rows = [
            _project_material_row(
                graph_path=graph_path,
                node=node,
                material_uuid_by_node_id=material_uuid_by_node_id,
                material_node_ids=material_node_ids,
                nodes_by_id=nodes_by_id,
                children_by_parent=children_by_parent,
                child_nodes=children_by_parent.get(
                    str(node.get("id") or ""),
                    [],
                ),
                model_registry=self.model_registry,
                modified_at=modified_at,
                revision=graph_revision,
            )
            for node in material_nodes
            if str(node.get("id") or "")
        ]
        return sorted(
            rows,
            key=lambda row: (row["create_time"], row["uuid"]),
            reverse=True,
        )


def _project_material_row(
    *,
    graph_path: Path,
    node: Mapping[str, Any],
    material_uuid_by_node_id: Mapping[str, str],
    material_node_ids: set[str],
    nodes_by_id: Mapping[str, Mapping[str, Any]],
    children_by_parent: Mapping[str, list[dict[str, Any]]],
    child_nodes: list[dict[str, Any]],
    model_registry: MaterialModelRegistry,
    modified_at: str,
    revision: int,
) -> dict[str, Any]:
    node_id = str(node["id"])
    source_type = str(node.get("type") or "resource")
    source_class = str(node.get("class") or "")
    resource_config = _record(node.get("config"))
    template_key = (
        source_class
        or str(resource_config.get("type") or "")
        or source_type
    )
    template_uuid = _stable_uuid(
        graph_path,
        "resource-template",
        template_key,
    )
    parent_node_id = _optional_string(node.get("parent"))
    pose = _pose(node)

    parent_node = nodes_by_id.get(parent_node_id or "")
    if parent_node_id and _is_deck_slot_node(parent_node or {}):
        deck_node_id = _optional_string(parent_node.get("parent"))
        if deck_node_id and deck_node_id in material_node_ids:
            placement = {
                "kind": "site",
                "parentId": material_uuid_by_node_id[deck_node_id],
                "siteId": _stable_uuid(
                    graph_path,
                    "site",
                    parent_node_id,
                ),
                "offsetPose": pose,
            }
        else:
            placement = {"kind": "world", "pose": pose}
    elif parent_node_id and parent_node_id in material_node_ids:
        placement: dict[str, Any] = {
            "kind": "parent",
            "parentId": material_uuid_by_node_id[parent_node_id],
            "anchor": {"kind": "root"},
            "localPose": pose,
        }
    else:
        placement = {
            "kind": "world",
            "pose": pose,
        }

    sites = [
        _project_site(
            graph_path,
            owner_material_id=material_uuid_by_node_id[node_id],
            node=child,
        )
        for child in child_nodes
        if _is_internal_site(child)
    ]
    sites.extend(
        _project_deck_slot_site(
            graph_path,
            owner_material_id=material_uuid_by_node_id[node_id],
            node=child,
            occupied_nodes=[
                occupied
                for occupied in children_by_parent.get(
                    str(child.get("id") or ""),
                    [],
                )
                if str(occupied.get("id") or "") in material_node_ids
            ],
            material_uuid_by_node_id=material_uuid_by_node_id,
        )
        for child in child_nodes
        if _is_deck_slot_node(child)
    )
    sites.extend(
        _project_embedded_sites(
            graph_path,
            owner_material_id=material_uuid_by_node_id[node_id],
            node=node,
            material_uuid_by_node_id=material_uuid_by_node_id,
        )
    )
    if not sites:
        sites.extend(
            _project_default_deck_sites(
                graph_path,
                owner_material_id=material_uuid_by_node_id[node_id],
                node=node,
            )
        )
    config = {
        "source": {
            "kind": "unilabos-device-graph",
            "graph": graph_path.name,
            "nodeId": node_id,
            "nodeType": source_type,
            "nodeClass": source_class,
        },
        "placement": placement,
        "rendering": _rendering(node, model_registry),
        "sites": sites,
        "resourceConfig": resource_config,
    }
    return {
        "uuid": material_uuid_by_node_id[node_id],
        "create_time": modified_at,
        "update_time": modified_at,
        "description": (
            f"Uni-Lab-OS graph node {node_id} "
            f"({source_class or source_type})"
        ),
        "meta_data": {
            "source": "unilabos",
            "source_graph": graph_path.name,
            "source_node_id": node_id,
        },
        "resource_template_uuid": template_uuid,
        "revision": revision,
        "code": node_id,
        "name": str(node.get("name") or node_id),
        "config": config,
        "data": _record(node.get("data")),
    }


def _project_site(
    graph_path: Path,
    *,
    owner_material_id: str,
    node: Mapping[str, Any],
) -> dict[str, Any]:
    node_id = str(node.get("id") or "")
    config = _record(node.get("config"))
    data = _record(node.get("data"))
    site_kind = _site_kind(node)
    max_volume = _finite_number(config.get("max_volume"))
    liquids = data.get("liquids")
    liquid_volume = sum(
        _finite_number(item[1])
        for item in liquids
        if (
            isinstance(liquids, list)
            and isinstance(item, (list, tuple))
            and len(item) >= 2
        )
    ) if isinstance(liquids, list) else 0.0
    has_tip = data.get("tip") is not None
    visual_state = (
        "tip-present"
        if has_tip
        else "filled"
        if liquid_volume > 0
        else "empty"
    )
    return {
        "id": _stable_uuid(graph_path, "site", node_id),
        "ownerMaterialId": owner_material_id,
        "key": node_id,
        "name": str(node.get("name") or node_id),
        "anchor": {"kind": "root"},
        "poseInAnchor": _pose(node),
        "sizeMm": [
            _positive_number(config.get("size_x"), 1.0),
            _positive_number(config.get("size_y"), 1.0),
            _positive_number(config.get("size_z"), 1.0),
        ],
        "capacity": 1,
        "allowedTemplateIds": [],
        "occupiedMaterialIds": [],
        "kind": site_kind,
        "shape": (
            "circle"
            if site_kind in {"well", "tip-spot"}
            else "rectangle"
        ),
        "visible": True,
        "maxVolumeUl": max_volume if max_volume > 0 else None,
        "visual": {
            "state": visual_state,
            "fillFraction": (
                min(liquid_volume / max_volume, 1.0)
                if max_volume > 0
                else 0.0
            ),
        },
    }


def _project_deck_slot_site(
    graph_path: Path,
    *,
    owner_material_id: str,
    node: Mapping[str, Any],
    occupied_nodes: list[Mapping[str, Any]],
    material_uuid_by_node_id: Mapping[str, str],
) -> dict[str, Any]:
    """把旧设备图中的 T1…T16 容器壳归一成 Material Site。"""

    node_id = str(node.get("id") or "")
    config = _record(node.get("config"))
    embedded = config.get("sites")
    declared = (
        embedded[0]
        if isinstance(embedded, list)
        and embedded
        and isinstance(embedded[0], Mapping)
        else {}
    )
    size = _record(declared.get("size"))
    content_types = declared.get("content_type")
    occupied_material_ids = [
        material_uuid_by_node_id[occupied_id]
        for occupied in occupied_nodes
        if (
            (occupied_id := str(occupied.get("id") or ""))
            in material_uuid_by_node_id
        )
    ]
    return {
        "id": _stable_uuid(graph_path, "site", node_id),
        "ownerMaterialId": owner_material_id,
        "key": node_id,
        "name": str(node.get("name") or node_id),
        "anchor": {"kind": "root"},
        "poseInAnchor": _pose(node),
        "sizeMm": [
            _positive_number(
                size.get("width"),
                _positive_number(config.get("size_x"), 1.0),
            ),
            _positive_number(
                size.get("height"),
                _positive_number(config.get("size_y"), 1.0),
            ),
            max(_finite_number(size.get("depth")), 0.0),
        ],
        "capacity": 1,
        "allowedTemplateIds": (
            [str(value) for value in content_types]
            if isinstance(content_types, list)
            else []
        ),
        "occupiedMaterialIds": occupied_material_ids,
        "kind": "deck-slot",
        "shape": "rectangle",
        "visible": bool(declared.get("visible", True)),
        "maxVolumeUl": None,
        "visual": {
            "state": "occupied" if occupied_material_ids else "empty",
            "fillFraction": 0.0,
        },
    }


def _project_embedded_sites(
    graph_path: Path,
    *,
    owner_material_id: str,
    node: Mapping[str, Any],
    material_uuid_by_node_id: Mapping[str, str],
) -> list[dict[str, Any]]:
    """投影 PRCXI 等设备图在 config.sites 中声明的台面插槽。"""

    config = _record(node.get("config"))
    embedded = config.get("sites")
    if not isinstance(embedded, list):
        nested = _record(config.get("init_param_data")).get("sites")
        embedded = nested if isinstance(nested, list) else []

    sites: list[dict[str, Any]] = []
    node_id = str(node.get("id") or "")
    for index, value in enumerate(embedded):
        if not isinstance(value, Mapping):
            continue
        label = str(value.get("label") or value.get("name") or index + 1)
        position = _record(value.get("position"))
        size = _record(value.get("size"))
        occupied_by = _optional_string(value.get("occupied_by"))
        occupied_material_id = (
            material_uuid_by_node_id.get(occupied_by)
            if occupied_by is not None
            else None
        )
        sites.append(
            {
                "id": _stable_uuid(
                    graph_path,
                    "site",
                    f"{node_id}:embedded:{label}",
                ),
                "ownerMaterialId": owner_material_id,
                "key": label,
                "name": label,
                "anchor": {"kind": "root"},
                "poseInAnchor": {
                    "positionMm": [
                        _finite_number(position.get("x")),
                        _finite_number(position.get("y")),
                        _finite_number(position.get("z")),
                    ],
                    "rotationDegXYZ": [0.0, 0.0, 0.0],
                },
                "sizeMm": [
                    _positive_number(size.get("width"), 1.0),
                    _positive_number(size.get("height"), 1.0),
                    max(_finite_number(size.get("depth")), 0.0),
                ],
                "capacity": 1,
                "allowedTemplateIds": [
                    str(content_type)
                    for content_type in value.get("content_type", [])
                ] if isinstance(value.get("content_type"), list) else [],
                "occupiedMaterialIds": (
                    [occupied_material_id] if occupied_material_id else []
                ),
                "kind": "deck-slot",
                "shape": "rectangle",
                "visible": bool(value.get("visible", True)),
                "maxVolumeUl": None,
                "visual": {
                    "state": (
                        "occupied" if occupied_material_id else "empty"
                    ),
                    "fillFraction": 0.0,
                },
            }
        )
    return sites


def _project_default_deck_sites(
    graph_path: Path,
    *,
    owner_material_id: str,
    node: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """仅在图未声明 sites 时补设备厂商的确定性 deck 坐标。"""

    config = _record(node.get("config"))
    config_type = str(config.get("type") or "").replace("-", "_").casefold()
    node_id = str(node.get("id") or "")
    definitions: list[tuple[str, list[float], list[float]]] = []
    if config_type == "otdeck":
        # 坐标来自随 OS 分发的 opentrons_liquid_handler XACRO 中 11 个
        # socketTypeGenericSbsFootprint 固定关节，并已叠加 main_link 原点。
        for label, x, y in (
            ("1", 63.85, 42.0),
            ("2", 196.35, 42.0),
            ("3", 328.85, 42.0),
            ("4", 63.85, 132.5),
            ("5", 196.35, 132.5),
            ("6", 328.85, 132.5),
            ("7", 63.85, 223.0),
            ("8", 196.35, 223.0),
            ("9", 328.85, 223.0),
            ("10", 63.85, 313.5),
            ("11", 196.35, 313.5),
        ):
            definitions.append(
                (label, [x, y, 70.0], list(_SBS_FOOTPRINT_MM))
            )
    elif "hamiltonstardeck" in config_type:
        rail_count = max(0, int(_finite_number(config.get("num_rails"))))
        for rail in range(1, rail_count + 1):
            definitions.append(
                (
                    f"R{rail}",
                    [
                        100.0 + (rail - 1) * _HAMILTON_RAIL_WIDTH_MM,
                        63.0,
                        100.0,
                    ],
                    [_HAMILTON_RAIL_WIDTH_MM, 478.0, 0.0],
                )
            )

    return [
        {
            "id": _stable_uuid(
                graph_path,
                "site",
                f"{node_id}:default:{label}",
            ),
            "ownerMaterialId": owner_material_id,
            "key": label,
            "name": label,
            "anchor": {"kind": "root"},
            "poseInAnchor": {
                "positionMm": position,
                "rotationDegXYZ": [0.0, 0.0, 0.0],
            },
            "sizeMm": size,
            "capacity": 1,
            "allowedTemplateIds": [],
            "occupiedMaterialIds": [],
            "kind": "deck-slot",
            "shape": "rectangle",
            "visible": True,
            "maxVolumeUl": None,
            "visual": {"state": "empty", "fillFraction": 0.0},
        }
        for label, position, size in definitions
    ]


def _rendering(
    node: Mapping[str, Any],
    model_registry: MaterialModelRegistry,
) -> dict[str, Any]:
    config = _record(node.get("config"))
    source_type = str(node.get("type") or "").casefold()
    source_class = str(node.get("class") or "").casefold()
    config_type = str(config.get("type") or "").casefold()
    identity = " ".join((source_type, source_class, config_type))
    is_prcxi_handler = (
        "liquid_handler.prcxi" in source_class
        or "liquid_handler_prcxi" in source_class
    )

    if is_prcxi_handler:
        kind = "liquid-handler"
        # PRCXI 的可视台面为 542×374 mm，设备根节点只保留 10 mm
        # 包边用于承载 deck 子节点，避免套用 Opentrons 的外壳尺寸。
        dimensions = [562.0, 650.0, 394.0]
    elif "liquid_handler" in identity:
        kind = "liquid-handler"
        dimensions = [624.3, 662.0, 567.2]
    elif "deck" in identity:
        kind = "deck"
        dimensions = [
            _positive_number(config.get("size_x"), 900.0),
            min(_positive_number(config.get("size_z"), 50.0), 80.0),
            _positive_number(config.get("size_y"), 600.0),
        ]
    elif "robotic_arm" in identity or "arm_slider" in identity:
        kind = "robotic-arm"
        dimensions = [420.0, 850.0, 420.0]
    elif "hotel" in identity:
        kind = "hotel"
        dimensions = [200.0, 700.0, 660.0]
    else:
        kind = (
            str(config.get("category") or config.get("type") or source_type)
            .strip()
            .lower()
            or "custom"
        )
        dimensions = [
            _positive_number(config.get("size_x"), 180.0),
            _positive_number(config.get("size_z"), 120.0),
            _positive_number(config.get("size_y"), 180.0),
        ]

    model = model_registry.model_for_identity(identity)
    if is_prcxi_handler or "trash" in identity:
        model = {
            "path": "",
            "format": "none",
            "attachPoints": [],
        }
    model.setdefault("attachPoints", [])
    return {
        "kind": kind,
        "dimensionsMm": dimensions,
        "footprintMm": [
            _positive_number(config.get("size_x"), dimensions[0]),
            _positive_number(config.get("size_y"), dimensions[2]),
        ],
        "scale": [1.0, 1.0, 1.0],
        "model": model,
    }


def _pose(node: Mapping[str, Any]) -> dict[str, list[float]]:
    pose = _record(node.get("pose"))
    position = _record(node.get("position")) or _record(
        pose.get("position")
    )
    config = _record(node.get("config"))
    rotation = _record(config.get("rotation")) or _record(
        pose.get("rotation")
    )
    return {
        "positionMm": [
            _finite_number(position.get("x")),
            _finite_number(position.get("y")),
            _finite_number(position.get("z")),
        ],
        "rotationDegXYZ": [
            math.degrees(_finite_number(rotation.get("x"))),
            math.degrees(_finite_number(rotation.get("y"))),
            math.degrees(_finite_number(rotation.get("z"))),
        ],
    }


def _is_internal_site(node: Mapping[str, Any]) -> bool:
    config = _record(node.get("config"))
    candidates = {
        str(node.get("type") or "").replace("-", "_").casefold(),
        str(config.get("type") or "").replace("-", "_").casefold(),
        str(config.get("category") or "").replace("-", "_").casefold(),
    }
    return bool(candidates & _INTERNAL_SITE_TYPES)


def _is_deck_slot_node(node: Mapping[str, Any]) -> bool:
    """识别旧 PLR 图中承载真实物料的 PRCXI 插槽壳节点。"""

    config_type = (
        str(_record(node.get("config")).get("type") or "")
        .replace("-", "_")
        .casefold()
    )
    return config_type.startswith("prcxi") and config_type.endswith(
        "container"
    )


def _site_kind(node: Mapping[str, Any]) -> str:
    config = _record(node.get("config"))
    candidates = (
        str(config.get("category") or ""),
        str(config.get("type") or ""),
        str(node.get("type") or ""),
    )
    normalized = " ".join(candidates).replace("-", "_").casefold()
    if "tip_spot" in normalized or "tipspot" in normalized:
        return "tip-spot"
    if "well" in normalized:
        return "well"
    return "site"


def _stable_uuid(graph_path: Path, domain: str, value: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"unilabos:{graph_path.name}:{domain}:{value}",
        )
    )


def _public_source_id(value: Any) -> str:
    source_id = Path(str(value or "os-current")).name
    normalized = "".join(
        character
        for character in source_id
        if character.isalnum() or character in {"-", "_", "."}
    )
    return normalized or "os-current"


def _positive_revision(value: Any, nodes: list[dict[str, Any]]) -> int:
    try:
        revision = int(value)
    except (TypeError, ValueError):
        revision = 0
    if revision > 0:
        return revision
    source = json.dumps(
        nodes,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return max(zlib.crc32(source.encode("utf-8")), 1)


def _canonical_uuid(value: str, *, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError) as exc:
        raise InvalidMaterialQuery(f"{field} must be a valid UUID") from exc


def _record(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _finite_number(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback


def _positive_number(value: Any, fallback: float) -> float:
    number = _finite_number(value, fallback)
    return number if number > 0 else fallback
