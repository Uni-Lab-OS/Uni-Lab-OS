"""面向统一前端的 Registry 模板目录投影。

Registry 仍是设备/资源类型的唯一事实源。本模块只把已经加载的 Registry
投影为稳定、可缓存的公共模板契约；不会扫描 YAML、加载当前 ``-g`` 图，也不会
暴露动作 schema、Python 文件路径或 Registry 内部对象。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Mapping

from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.utils.cls_creator import import_class

_IDENTITY_PREFIX = "unilabos:resource-template:v1"
_DEFAULT_NAMESPACE = "unilabos"
_VISIBILITIES = {"public", "internal", "hidden"}
_COMPONENT_KEY = re.compile(r"^([A-Za-z]+)([1-9][0-9]*)$")


class TemplateCatalogError(RuntimeError):
    """模板目录无法满足请求。"""


class TemplateNotFound(TemplateCatalogError):
    """模板 UUID 不存在或不公开。"""


class TemplateCatalogNotReady(TemplateCatalogError):
    """Registry 尚未完成初始化。"""


class TemplateAssetError(TemplateCatalogError):
    """模板资源引用不存在或不安全。"""


def stable_template_uuid(
    source_namespace: str,
    kind: str,
    key: str,
) -> str:
    """根据公开类型身份生成与文件位置、加载顺序无关的 UUID5。"""

    identity = f"{_IDENTITY_PREFIX}:{source_namespace}:{kind}:{key}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))


class ResourceTemplateCatalog:
    """把一个已经初始化的 Registry 投影为只读模板目录。"""

    def __init__(self, registry: Any) -> None:
        self._registry = registry
        self._lock = threading.RLock()
        self._catalog: dict[str, Any] | None = None
        self._entries_by_uuid: dict[str, tuple[str, str, Mapping[str, Any]]] = {}
        self._details: dict[tuple[str, str], dict[str, Any]] = {}
        self._diagnostics: list[dict[str, str]] = []

    @property
    def diagnostics(self) -> list[dict[str, str]]:
        self._ensure_catalog()
        with self._lock:
            return copy.deepcopy(self._diagnostics)

    def list_templates(self) -> dict[str, Any]:
        self._ensure_catalog()
        with self._lock:
            return copy.deepcopy(self._catalog)

    def get_template(self, template_uuid: str) -> dict[str, Any]:
        self._ensure_catalog()
        with self._lock:
            source = self._entries_by_uuid.get(template_uuid)
            if source is None:
                raise TemplateNotFound(template_uuid)
            kind, key, entry = source
            summary = next(
                item
                for item in self._catalog["items"]
                if item["uuid"] == template_uuid
            )
            cache_key = (template_uuid, summary["content_hash"])
            cached = self._details.get(cache_key)
            if cached is not None:
                return copy.deepcopy(cached)

        detail = self._build_detail(kind, key, entry, summary)
        with self._lock:
            self._details[cache_key] = detail
            return copy.deepcopy(detail)

    def resolve_asset(self, template_uuid: str, asset_key: str) -> Path:
        """解析显式声明的资源；仅允许 YAML 所在目录内的相对路径。"""

        self._ensure_catalog()
        with self._lock:
            source = self._entries_by_uuid.get(template_uuid)
            if source is None:
                raise TemplateNotFound(template_uuid)
            _, _, entry = source

        catalog_meta = _mapping(entry.get("catalog"))
        assets = _mapping(catalog_meta.get("assets"))
        raw_path = assets.get(asset_key)
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise TemplateAssetError(f"未声明模板资源: {asset_key}")
        candidate = Path(raw_path)
        if candidate.is_absolute():
            raise TemplateAssetError("模板资源必须使用相对路径")

        file_path = entry.get("file_path")
        if not isinstance(file_path, (str, Path)):
            raise TemplateAssetError("模板缺少安全资源根目录")
        root = Path(file_path).resolve().parent
        resolved = (root / candidate).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise TemplateAssetError("模板资源越出声明目录") from exc
        if not resolved.is_file():
            raise TemplateAssetError(f"模板资源不存在: {asset_key}")
        return resolved

    def _ensure_catalog(self) -> None:
        with self._lock:
            if self._catalog is not None:
                return
            if getattr(self._registry, "_setup_called", True) is False:
                raise TemplateCatalogNotReady("Registry 尚未完成初始化")

            summaries: list[dict[str, Any]] = []
            identities: set[tuple[str, str, str]] = set()
            uuids: set[str] = set()
            diagnostics: list[dict[str, str]] = []
            entries_by_uuid: dict[
                str, tuple[str, str, Mapping[str, Any]]
            ] = {}

            sources = (
                ("device", self._registry.device_type_registry),
                ("resource", self._registry.resource_type_registry),
            )
            for kind, registry_entries in sources:
                for key, raw_entry in registry_entries.items():
                    if not isinstance(key, str) or not key.strip():
                        diagnostics.append(
                            _diagnostic(kind, str(key), "INVALID_KEY")
                        )
                        continue
                    if not isinstance(raw_entry, Mapping):
                        diagnostics.append(
                            _diagnostic(kind, key, "INVALID_ENTRY")
                        )
                        continue

                    entry = copy.deepcopy(raw_entry)
                    catalog_meta = _mapping(entry.get("catalog"))
                    visibility = catalog_meta.get(
                        "visibility",
                        "public" if kind == "resource" else "internal",
                    )
                    if visibility not in _VISIBILITIES:
                        diagnostics.append(
                            _diagnostic(kind, key, "INVALID_VISIBILITY")
                        )
                        continue
                    if visibility != "public":
                        continue

                    namespace = catalog_meta.get(
                        "source_namespace",
                        _DEFAULT_NAMESPACE,
                    )
                    if not isinstance(namespace, str) or not namespace.strip():
                        diagnostics.append(
                            _diagnostic(kind, key, "INVALID_NAMESPACE")
                        )
                        continue
                    namespace = namespace.strip()
                    identity = (namespace, kind, key)
                    template_uuid = stable_template_uuid(*identity)
                    if identity in identities or template_uuid in uuids:
                        diagnostics.append(
                            _diagnostic(kind, key, "DUPLICATE_IDENTITY")
                        )
                        continue

                    summary = self._build_summary(
                        kind,
                        key,
                        namespace,
                        template_uuid,
                        entry,
                    )
                    identities.add(identity)
                    uuids.add(template_uuid)
                    summaries.append(summary)
                    entries_by_uuid[template_uuid] = (kind, key, entry)

            summaries.sort(
                key=lambda item: (
                    item["kind"],
                    tuple(item["category_path"]),
                    item["display_name"].casefold(),
                    item["key"].casefold(),
                )
            )
            revision = _sha256(
                [
                    {
                        "uuid": item["uuid"],
                        "content_hash": item["content_hash"],
                        "status": item["status"],
                    }
                    for item in summaries
                ]
            )
            self._catalog = {
                "revision": revision,
                "stale": False,
                "items": summaries,
            }
            self._entries_by_uuid = entries_by_uuid
            self._diagnostics = diagnostics

    def _build_summary(
        self,
        kind: str,
        key: str,
        namespace: str,
        template_uuid: str,
        entry: Mapping[str, Any],
    ) -> dict[str, Any]:
        catalog_meta = _mapping(entry.get("catalog"))
        class_info = _mapping(entry.get("class"))
        module = class_info.get("module")
        has_implementation = isinstance(module, str) and bool(module.strip())
        has_declared_detail = bool(entry.get("config_info"))
        status = "ready" if has_implementation or has_declared_detail else "unresolved"
        status_reason = (
            None
            if status == "ready"
            else "Registry 条目没有可解析的实现或声明式几何"
        )

        display_name = _first_string(
            catalog_meta.get("display_name"),
            entry.get("displayname"),
            entry.get("name"),
            key,
        )
        category_path = _string_list(
            catalog_meta.get("category", entry.get("category"))
        )
        tags = _string_list(catalog_meta.get("tags", entry.get("tags")))
        icon = _safe_icon(
            catalog_meta.get("icon", entry.get("icon")),
            fallback=kind,
        )
        content_hash = _sha256(
            {
                "identity": {
                    "namespace": namespace,
                    "kind": kind,
                    "key": key,
                },
                "display_name": display_name,
                "description": _optional_string(entry.get("description")),
                "category_path": category_path,
                "tags": tags,
                "icon": icon,
                "version": _json_safe(entry.get("version")),
                "catalog": _json_safe(catalog_meta),
                "configuration": _json_safe(entry.get("init_param_schema")),
                "class": {
                    "module": module if isinstance(module, str) else None,
                    "type": _json_safe(class_info.get("type")),
                    "source_hash": self._module_source_hash(module),
                },
            }
        )
        creation_mode = (
            "dynamic-device" if kind == "device" else "resource-tree"
        )
        result: dict[str, Any] = {
            "uuid": template_uuid,
            "key": key,
            "source_namespace": namespace,
            "kind": kind,
            "display_name": display_name,
            "description": _optional_string(entry.get("description")),
            "category_path": category_path,
            "tags": tags,
            "icon": icon,
            "status": status,
            "content_hash": content_hash,
            "creation": {
                "mode": creation_mode,
                "available": status == "ready",
                "reason": (
                    None
                    if status == "ready"
                    else "模板尚未解析完成，无法创建"
                ),
            },
        }
        if status_reason is not None:
            result["status_reason"] = status_reason
        return result

    def _build_detail(
        self,
        kind: str,
        key: str,
        entry: Mapping[str, Any],
        summary: Mapping[str, Any],
    ) -> dict[str, Any]:
        detail = copy.deepcopy(dict(summary))
        catalog_meta = _mapping(entry.get("catalog"))
        detail["compatibility"] = _json_object(
            catalog_meta.get("compatibility")
        )
        detail["configuration"] = {
            "schema": _json_object(entry.get("init_param_schema")),
            "ui_schema": _json_object(catalog_meta.get("ui_schema")),
        }
        detail["assets"] = {
            asset_key: (
                f"/api/v1/resource-templates/{summary['uuid']}"
                f"/assets/{asset_key}"
            )
            for asset_key in _mapping(catalog_meta.get("assets"))
        }
        detail["geometry"] = None
        detail["container_layout"] = None

        if kind != "resource":
            return detail

        try:
            config_info = self._resource_config_info(key, entry)
            geometry, layout = _normalize_resource_geometry(config_info)
            detail["geometry"] = geometry
            detail["container_layout"] = layout
            if geometry is None and detail["status"] == "ready":
                detail["status"] = "unresolved"
                detail["status_reason"] = "资源实现没有产生可用几何"
        except Exception as exc:
            detail["status"] = "unresolved"
            detail["status_reason"] = f"资源详情解析失败: {exc}"
        return detail

    def _resource_config_info(
        self,
        resource_id: str,
        entry: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        declared = entry.get("config_info")
        if isinstance(declared, list) and declared:
            return _flatten_config_info(declared)

        class_info = _mapping(entry.get("class"))
        module = class_info.get("module")
        if class_info.get("type") != "pylabrobot" or not isinstance(module, str):
            return []

        resource_factory = import_class(module)
        if not callable(resource_factory):
            raise TemplateCatalogError(f"{resource_id} 不是可调用资源工厂")
        resource = resource_factory(resource_factory.__name__)
        tree_set = ResourceTreeSet.from_plr_resources(
            [resource],
            known_newly_created=True,
            old_size=True,
        )
        dumped = tree_set.dump(old_position=True)
        if not dumped:
            return []
        return _flatten_config_info(dumped[0])

    def _module_source_hash(self, module: Any) -> str | None:
        if not isinstance(module, str) or not module:
            return None
        resolver = getattr(self._registry, "_module_source_hash", None)
        if not callable(resolver):
            return None
        return resolver(module)


def _normalize_resource_geometry(
    config_info: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not config_info:
        return None, None
    root = next(
        (
            node
            for node in config_info
            if node.get("parent_uuid") in {None, ""}
        ),
        config_info[0],
    )
    root_config = _mapping(root.get("config"))
    root_pose = _mapping(root.get("pose"))
    root_size = _mapping(root_pose.get("size"))
    size_x = _number(root_config.get("size_x"), root_size.get("width"))
    size_y = _number(root_config.get("size_y"), root_size.get("height"))
    size_z = _number(root_config.get("size_z"), root_size.get("depth"))
    if size_x is None or size_y is None or size_z is None:
        return None, None

    position = _mapping(root_pose.get("position"))
    origin = {
        "x": _number(position.get("x"), 0.0),
        "y": _number(position.get("y"), 0.0),
        "z": _number(position.get("z"), 0.0),
    }
    geometry = {
        "dimensions_mm": {"x": size_x, "y": size_y, "z": size_z},
        "origin_mm": origin,
        "footprint": {
            "points_mm": [
                {"x": 0.0, "y": 0.0},
                {"x": size_x, "y": 0.0},
                {"x": size_x, "y": size_y},
                {"x": 0.0, "y": size_y},
            ]
        },
        "stack_height_mm": size_z,
    }

    ordering = _mapping(root_config.get("ordering"))
    nodes_by_id = {
        str(node.get("id")): node
        for node in config_info
        if node is not root and node.get("id") is not None
    }
    components: list[tuple[str, Mapping[str, Any]]] = []
    for component_key, node_id in ordering.items():
        if not isinstance(component_key, str) or not isinstance(node_id, str):
            continue
        node = nodes_by_id.get(node_id)
        if node is not None and _is_container_node(node):
            components.append((component_key, node))

    if not components:
        for node in config_info:
            if node is root or not _is_container_node(node):
                continue
            node_id = str(node.get("id", ""))
            component_key = node_id.rsplit("_", 1)[-1]
            components.append((component_key, node))
    if not components:
        return geometry, None

    grid = _grid_layout(components)
    if grid is not None:
        return geometry, grid
    return geometry, {
        "type": "explicit",
        "containers": [
            _explicit_component(component_key, node)
            for component_key, node in components
        ],
    }


def _grid_layout(
    components: list[tuple[str, Mapping[str, Any]]],
) -> dict[str, Any] | None:
    parsed: list[tuple[str, int, str, Mapping[str, Any]]] = []
    for component_key, node in components:
        match = _COMPONENT_KEY.fullmatch(component_key)
        if match is None:
            return None
        parsed.append((match.group(1).upper(), int(match.group(2)), component_key, node))

    rows = sorted({row for row, _, _, _ in parsed}, key=_row_number)
    columns = sorted({column for _, column, _, _ in parsed})
    if len(parsed) != len(rows) * len(columns):
        return None
    by_coordinate = {
        (row, column): (component_key, node)
        for row, column, component_key, node in parsed
    }
    try:
        first_key, first = by_coordinate[(rows[0], columns[0])]
    except KeyError:
        return None
    first_geometry = _component_geometry(first)
    if first_geometry is None:
        return None
    for _, node in by_coordinate.values():
        if _component_geometry(node) != first_geometry:
            return None

    first_position = _component_position(first)
    if first_position is None:
        return None
    pitch_x = 0.0
    pitch_y = 0.0
    if len(columns) > 1:
        neighbor = by_coordinate.get((rows[0], columns[1]))
        if neighbor is None:
            return None
        neighbor_position = _component_position(neighbor[1])
        if neighbor_position is None:
            return None
        pitch_x = neighbor_position["x"] - first_position["x"]
    if len(rows) > 1:
        neighbor = by_coordinate.get((rows[1], columns[0]))
        if neighbor is None:
            return None
        neighbor_position = _component_position(neighbor[1])
        if neighbor_position is None:
            return None
        pitch_y = neighbor_position["y"] - first_position["y"]

    return {
        "type": "grid",
        "container_kind": _component_kind(first),
        "rows": rows,
        "columns": len(columns),
        "column_labels": columns,
        "naming": "row-column",
        "geometry": {
            **first_geometry,
            "pitch_mm": {"x": pitch_x, "y": pitch_y},
            "offset_mm": first_position,
            "first_key": first_key,
        },
    }


def _explicit_component(
    component_key: str,
    node: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "key": component_key,
        "kind": _component_kind(node),
        "position_mm": _component_position(node)
        or {"x": 0.0, "y": 0.0, "z": 0.0},
        "geometry": _component_geometry(node) or {},
    }


def _component_geometry(node: Mapping[str, Any]) -> dict[str, Any] | None:
    config = _mapping(node.get("config"))
    pose = _mapping(node.get("pose"))
    size = _mapping(pose.get("size"))
    size_x = _number(config.get("size_x"), size.get("width"))
    size_y = _number(config.get("size_y"), size.get("height"))
    size_z = _number(config.get("size_z"), size.get("depth"))
    if size_x is None or size_y is None or size_z is None:
        return None
    cross_section = pose.get("cross_section_type")
    shape = "circle" if cross_section == "circle" else "rectangle"
    result: dict[str, Any] = {
        "dimensions_mm": {"x": size_x, "y": size_y, "z": size_z},
        "depth_mm": size_z,
        "shape": shape,
    }
    max_volume = _number(config.get("max_volume"))
    if max_volume is not None:
        result["max_volume_ul"] = max_volume
    return result


def _component_position(node: Mapping[str, Any]) -> dict[str, float] | None:
    pose = _mapping(node.get("pose"))
    position = _mapping(pose.get("position"))
    x = _number(position.get("x"))
    y = _number(position.get("y"))
    z = _number(position.get("z"))
    if x is None or y is None or z is None:
        return None
    return {"x": x, "y": y, "z": z}


def _component_kind(node: Mapping[str, Any]) -> str:
    raw = str(node.get("type") or _mapping(node.get("config")).get("category") or "container")
    normalized = raw.strip().casefold().replace("_", "-")
    if normalized in {"tipspot", "tip-spot"}:
        return "tip-spot"
    if normalized == "well":
        return "well"
    return "container"


def _is_container_node(node: Mapping[str, Any]) -> bool:
    return _component_kind(node) in {"well", "tip-spot", "container"}


def _flatten_config_info(value: Any) -> list[dict[str, Any]]:
    while isinstance(value, list) and len(value) == 1 and isinstance(value[0], list):
        value = value[0]
    if not isinstance(value, list):
        return []
    return [copy.deepcopy(item) for item in value if isinstance(item, Mapping)]


def _row_number(row: str) -> int:
    value = 0
    for character in row:
        value = value * 26 + (ord(character) - ord("A") + 1)
    return value


def _diagnostic(kind: str, key: str, code: str) -> dict[str, str]:
    return {"kind": kind, "key": key, "code": code}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json_object(value: Any) -> dict[str, Any]:
    return dict(_json_safe(_mapping(value)))


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in {"file_path", "registry_type", "config_info"}
        }
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return value.name
    name = getattr(value, "__name__", None)
    return name if isinstance(name, str) else str(value)


def _sha256(value: Any) -> str:
    payload = json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _first_string(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _optional_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        return []
    return [
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip()
    ]


def _safe_icon(value: Any, *, fallback: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return fallback
    icon = value.strip()
    if Path(icon).is_absolute() or ".." in Path(icon).parts:
        return fallback
    return icon


def _number(*values: Any) -> float | None:
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
    return None
