"""PackageCatalog 与 ResourceTreeSet 到 Backend MaterialGraph 的受控投影。"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from unilabos.package_manager import PackageAssetResolver, PackageCatalog
from unilabos.package_manager.sources import PackageSource
from unilabos.app.scheduler.inventory.domain import MaterialModelAsset

_INTERNAL_SITE_TYPES = frozenset({"tipspot", "tip_spot", "well"})
_PART_TYPES = frozenset(
    {"box", "slab", "cylinder", "lathe", "disc", "rect", "edge", "grid", "sites"}
)
_STYLE_TOKENS = frozenset(
    {
        "plain",
        "frame",
        "plate",
        "board",
        "body",
        "column",
        "module",
        "shell",
        "beam",
        "shaft",
        "probe",
        "deck",
        "gear",
        "motor",
        "foot",
        "glass",
        "cap",
        "hole",
        "bore",
        "port",
        "seat",
        "pad",
        "rim",
        "hairline",
    }
)
_SITE_GENERATORS = frozenset(
    {"open-rack", "stack-shelves", "site-holes", "site-markers"}
)


@dataclass(frozen=True, slots=True)
class MaterialDefinitionProjection:
    """一个 PackageCatalog definition 对 ResourceTreeSet class 的读模型。"""

    graph_class: str
    source_identity: str
    kind: str
    categories: tuple[str, ...]
    envelope_mm: tuple[float, float, float] | None
    model: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class PackageMaterialProjection:
    """PackageCatalog 的物料渲染子集，不复制 Registry 或 TemplateCatalog。"""

    definitions: Mapping[str, MaterialDefinitionProjection]
    shapes: tuple[dict[str, Any], ...]
    model_assets: tuple[MaterialModelAsset, ...]
    fingerprint: str


def build_package_material_projection(
    sources: Sequence[PackageSource],
    catalogs: Sequence[PackageCatalog],
) -> PackageMaterialProjection:
    """只通过已审计 PackageCatalog asset closure 读取 shape manifest。"""

    if len(sources) != len(catalogs):
        raise ValueError("Package source 与 PackageCatalog 数量不一致")
    definitions: dict[str, MaterialDefinitionProjection] = {}
    shapes_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    model_assets_by_path: dict[str, MaterialModelAsset] = {}
    digests: list[str] = []
    for source, catalog in zip(sources, catalogs, strict=True):
        resolver = PackageAssetResolver(source, catalog)
        digests.append(catalog.catalog_digest)
        for asset in catalog.assets:
            public_path = _model_asset_path(catalog.namespace, asset.logical_path)
            projected_asset = MaterialModelAsset(
                public_path=public_path,
                media_type=asset.media_type,
                digest=asset.digest,
                size=asset.size,
                read_bytes=lambda resolver=resolver, logical_path=asset.logical_path: (
                    resolver.open_binary(logical_path).read()
                ),
            )
            existing_asset = model_assets_by_path.get(public_path)
            if existing_asset is not None and (
                existing_asset.digest != projected_asset.digest
                or existing_asset.size != projected_asset.size
            ):
                raise ValueError(f"同一 Package model asset path 指向不同内容: {public_path}")
            model_assets_by_path[public_path] = projected_asset
        records = (*catalog.definitions.devices, *catalog.definitions.resources)
        for record in records:
            shape = _shape_for_definition(
                resolver,
                record.details.get("model"),
                bundle=catalog.namespace,
            )
            if shape is not None:
                shape_identity = (shape["bundle"], shape["id"])
                existing_shape = shapes_by_identity.get(shape_identity)
                if existing_shape is not None and existing_shape != shape:
                    raise ValueError(
                        "同一 Package shape identity 指向不同内容: "
                        f"{shape_identity[0]}/{shape_identity[1]}"
                    )
                shapes_by_identity[shape_identity] = shape
            categories = tuple(_normalize_category(item) for item in record.category)
            kind = _definition_kind(shape, categories, record.kind)
            envelope = _shape_envelope(shape)
            source_identity = (
                record.fqid
                if record.kind == "device"
                else f"{record.module}:{record.symbol}"
            )
            definitions[record.id] = MaterialDefinitionProjection(
                graph_class=record.id,
                source_identity=source_identity,
                kind=kind,
                categories=categories,
                envelope_mm=envelope,
                model=_model_for_definition(
                    resolver,
                    record.details.get("model"),
                    bundle=catalog.namespace,
                ),
            )
    shapes = sorted(
        shapes_by_identity.values(), key=lambda item: (item["bundle"], item["id"])
    )
    canonical = json.dumps(
        {
            "catalogs": sorted(digests),
            "definitions": {
                key: {
                    "source_identity": value.source_identity,
                    "kind": value.kind,
                    "categories": value.categories,
                    "envelope_mm": value.envelope_mm,
                    "model": value.model,
                }
                for key, value in sorted(definitions.items())
            },
            "shapes": shapes,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return PackageMaterialProjection(
        definitions=definitions,
        shapes=tuple(shapes),
        model_assets=tuple(
            model_assets_by_path[key] for key in sorted(model_assets_by_path)
        ),
        fingerprint="sha256:" + hashlib.sha256(canonical).hexdigest(),
    )


def build_resource_graph_import(
    snapshot: Mapping[str, Any],
    package_projection: PackageMaterialProjection,
    resolved_identities: Mapping[str, str],
) -> dict[str, Any]:
    """把 ResourceTreeSet snapshot 规范化成 Inventory 首次导入命令。"""

    source_id = Path(str(snapshot.get("source_id") or "os-current")).name
    raw_nodes = snapshot.get("nodes")
    if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes)):
        raise ValueError("ResourceTreeSet snapshot 缺少 nodes")
    nodes = [_json_object(item) for item in raw_nodes if isinstance(item, Mapping)]
    if len(nodes) != len(raw_nodes):
        raise ValueError("ResourceTreeSet snapshot nodes 必须全是对象")
    material_nodes = [node for node in nodes if not _is_internal_site(node)]
    material_uuid_by_runtime_uuid = {
        str(node["uuid"]): _stable_uuid(source_id, "material", str(node["id"]))
        for node in material_nodes
    }
    materials: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    for node in material_nodes:
        node_id = _required_string(node.get("id"), "node.id")
        runtime_uuid = _required_string(node.get("uuid"), f"nodes[{node_id}].uuid")
        graph_class = _required_string(node.get("class"), f"nodes[{node_id}].class")
        definition = package_projection.definitions.get(graph_class)
        if definition is None:
            raise ValueError(f"ResourceTreeSet class 未进入 PackageCatalog: {graph_class}")
        template_uuid = resolved_identities.get(definition.source_identity)
        if template_uuid is None:
            raise ValueError(
                f"ResourceTemplate identity 未解析: {definition.source_identity}"
            )
        material_uuid = material_uuid_by_runtime_uuid[runtime_uuid]
        parent_runtime_uuid = _optional_string(node.get("parent_uuid"))
        parent_uuid = material_uuid_by_runtime_uuid.get(parent_runtime_uuid or "")
        dimensions = _dimensions(node, definition)
        raw_config = _json_object(node.get("config"))
        config = {
            **raw_config,
            "rendering": {
                "kind": definition.kind,
                "dimensions_mm": list(dimensions),
                "categories": list(definition.categories),
                **({"model": dict(definition.model)} if definition.model else {}),
            },
        }
        materials.append(
            {
                "uuid": material_uuid,
                "resource_template_uuid": template_uuid,
                "parent_uuid": parent_uuid,
                "class": graph_class,
                "barcode": str(node.get("barcode") or ""),
                "name": str(node.get("name") or node_id),
                "description": str(node.get("description") or "") or None,
                "meta_data": {
                    "source": "resource-tree-set",
                    "source_graph": source_id,
                    "source_node_id": node_id,
                    "source_runtime_uuid": runtime_uuid,
                },
                "config": config,
                "data": _json_object(node.get("data")),
                "material_kind": "device" if node.get("type") == "device" else "business",
            }
        )
        position = _position(node)
        positions.append(
            {
                "uuid": _stable_uuid(source_id, "relative-position", node_id),
                "material_uuid": material_uuid,
                "description": None,
                "meta_data": {"source": "resource-tree-set"},
                "position_x": position[0],
                "position_y": position[1],
                "position_z": position[2],
                "depth": dimensions[2],
                "length": dimensions[1],
                "width": dimensions[0],
                **_scale_and_rotation(node),
            }
        )

    sites: list[dict[str, Any]] = []
    for node in nodes:
        if not _is_internal_site(node):
            continue
        node_id = _required_string(node.get("id"), "site node.id")
        parent_runtime_uuid = _optional_string(node.get("parent_uuid"))
        owner_uuid = material_uuid_by_runtime_uuid.get(parent_runtime_uuid or "")
        if owner_uuid is None:
            raise ValueError(f"Site {node_id} 的 owner 不是 Material")
        dimensions = _raw_dimensions(node)
        position = _position(node)
        sites.append(
            {
                "uuid": _stable_uuid(source_id, "site", node_id),
                "material_uuid": owner_uuid,
                "name": str(node.get("name") or node_id),
                "sort_order": len(sites),
                "allowed_resource_template_uuids": [],
                "occupied_material_uuid": None,
                "description": str(node.get("description") or "") or None,
                "meta_data": {
                    "source": "resource-tree-set",
                    "source_node_id": node_id,
                },
                "position_x": position[0],
                "position_y": position[1],
                "position_z": position[2],
                "depth": dimensions[2],
                "length": dimensions[1],
                "width": dimensions[0],
            }
        )

    canonical = json.dumps(
        {
            "source_id": source_id,
            "package_fingerprint": package_projection.fingerprint,
            "materials": materials,
            "relative_positions": positions,
            "sites": sites,
        },
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return {
        "source_id": source_id,
        "fingerprint": "sha256:" + hashlib.sha256(canonical).hexdigest(),
        "materials": materials,
        "relative_positions": positions,
        "sites": sites,
    }


def _shape_for_definition(
    resolver: PackageAssetResolver,
    model: object,
    *,
    bundle: str,
) -> dict[str, Any] | None:
    if not isinstance(model, Mapping):
        return None
    representation = model.get("shape")
    if not isinstance(representation, Mapping):
        return None
    if representation.get("format") != "unilab.shape/v1":
        return None
    entry = representation.get("entry")
    if not isinstance(entry, str) or not entry:
        return None
    with resolver.open_binary(entry) as stream:
        manifest = yaml.safe_load(stream.read().decode("utf-8"))
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 1:
        raise ValueError(f"shape manifest 格式无效: {entry}")
    raw_shape = manifest.get("shape")
    if raw_shape is None:
        values = manifest.get("shapes")
        raw_shape = values[0] if isinstance(values, list) and len(values) == 1 else None
    if not isinstance(raw_shape, Mapping):
        raise ValueError(f"shape manifest 必须包含唯一 shape: {entry}")
    return _public_shape(raw_shape, bundle=bundle)


def _model_for_definition(
    resolver: PackageAssetResolver,
    model: object,
    *,
    bundle: str,
) -> dict[str, Any] | None:
    if not isinstance(model, Mapping):
        return None
    entry = model.get("entry")
    model_format = model.get("format")
    if not isinstance(entry, str) or not entry:
        return None
    if not isinstance(model_format, str) or not model_format:
        return None
    metadata = resolver.public_metadata(entry)
    public_path = _model_asset_path(bundle, entry)
    result: dict[str, Any] = {
        "path": public_path,
        "format": model_format,
        "meshDir": public_path.rsplit("/", 1)[0],
        "version": metadata.digest,
    }
    for key in ("macro", "color", "position", "rotation", "scale"):
        value = model.get(key)
        if value is not None:
            result[key] = value
    return result


def _model_asset_path(bundle: str, logical_path: str) -> str:
    return (
        "/api/v1/material-models/"
        + quote(bundle, safe="")
        + "/"
        + quote(logical_path, safe="/")
    )


def _public_shape(raw: Mapping[str, Any], *, bundle: str) -> dict[str, Any]:
    shape_id = _required_string(raw.get("id"), "shape.id")
    raw_parts = raw.get("parts")
    if not isinstance(raw_parts, list) or not raw_parts:
        raise ValueError(f"shape {shape_id} 缺少 parts")
    for part in raw_parts:
        _validate_part(part)
    categories: list[str] = []
    tokens: list[str] = []
    for rule in raw.get("applies_to", []):
        if not isinstance(rule, Mapping):
            continue
        if rule.get("category"):
            categories.append(_normalize_category(str(rule["category"])))
        if rule.get("category_contains"):
            tokens.append(_normalize_category(str(rule["category_contains"])))
    if not categories and not tokens:
        raise ValueError(f"shape {shape_id} 缺少 applies_to")
    result: dict[str, Any] = {
        "id": shape_id,
        "bundle": bundle,
        "categories": categories,
        "categoryTokens": tokens,
        "priority": int(raw.get("priority") or 0),
        "units": str(raw.get("units") or "mm"),
        "shadow": str(raw.get("shadow") or "box"),
        "sort": str(raw.get("sort") or "center"),
        "parts": [_json_object(part) for part in raw_parts],
    }
    if raw.get("display_name"):
        result["displayName"] = str(raw["display_name"])
    envelope = raw.get("envelope")
    if isinstance(envelope, list) and len(envelope) == 3:
        result["envelope"] = [_finite_number(value, "shape.envelope") for value in envelope]
    return result


def _validate_part(raw: object, *, nested: bool = False) -> None:
    if not isinstance(raw, Mapping):
        raise ValueError("shape part 必须是对象")
    part_type = str(raw.get("type") or "")
    if part_type not in _PART_TYPES:
        raise ValueError(f"shape part type 无效: {part_type}")
    style = str(raw.get("style") or "plain")
    if style not in _STYLE_TOKENS:
        raise ValueError(f"shape style 无效: {style}")
    if part_type == "sites" and raw.get("generator") not in _SITE_GENERATORS:
        raise ValueError(f"shape sites generator 无效: {raw.get('generator')}")
    if part_type == "grid":
        if nested:
            raise ValueError("shape grid 不得嵌套 grid")
        _validate_part(raw.get("part"), nested=True)


def _definition_kind(
    shape: Mapping[str, Any] | None,
    categories: tuple[str, ...],
    definition_kind: str,
) -> str:
    if shape is not None and shape["categories"]:
        return str(shape["categories"][0])
    return categories[-1] if categories else definition_kind


def _shape_envelope(shape: Mapping[str, Any] | None) -> tuple[float, float, float] | None:
    if shape is None or "envelope" not in shape:
        return None
    value = shape["envelope"]
    return (float(value[0]), float(value[1]), float(value[2]))


def _dimensions(
    node: Mapping[str, Any],
    definition: MaterialDefinitionProjection,
) -> tuple[float, float, float]:
    raw = _raw_dimensions(node)
    fallback = definition.envelope_mm or _default_dimensions(definition.kind)
    return tuple(raw[index] if raw[index] > 0 else fallback[index] for index in range(3))  # type: ignore[return-value]


def _raw_dimensions(node: Mapping[str, Any]) -> tuple[float, float, float]:
    pose = _json_object(node.get("pose"))
    size = _json_object(pose.get("size"))
    config = _json_object(node.get("config"))
    return (
        max(_number(size.get("width"), config.get("size_x")), 0.0),
        max(_number(size.get("height"), config.get("size_y")), 0.0),
        max(_number(size.get("depth"), config.get("size_z")), 0.0),
    )


def _default_dimensions(kind: str) -> tuple[float, float, float]:
    normalized = _normalize_category(kind)
    if "deck" in normalized:
        return (2400.0, 1800.0, 60.0)
    if "stack" in normalized or "warehouse" in normalized:
        return (360.0, 300.0, 720.0)
    if "robot" in normalized or "arm" in normalized:
        return (420.0, 420.0, 850.0)
    if "container" in normalized or "bottle" in normalized or "vial" in normalized:
        return (100.0, 100.0, 180.0)
    if "tip-box" in normalized:
        return (130.0, 90.0, 75.0)
    if "workstation" in normalized or "liquid-handler" in normalized:
        return (480.0, 420.0, 520.0)
    return (320.0, 280.0, 320.0)


def _position(node: Mapping[str, Any]) -> tuple[float, float, float]:
    pose = _json_object(node.get("pose"))
    position = _json_object(pose.get("position"))
    return (
        _number(position.get("x")),
        _number(position.get("y")),
        _number(position.get("z")),
    )


def _scale_and_rotation(node: Mapping[str, Any]) -> dict[str, float]:
    pose = _json_object(node.get("pose"))
    scale = _json_object(pose.get("scale"))
    rotation = _json_object(pose.get("rotation"))
    return {
        "scale_x": _positive_number(scale.get("x"), 1.0),
        "scale_y": _positive_number(scale.get("y"), 1.0),
        "scale_z": _positive_number(scale.get("z"), 1.0),
        "rotation_x": _number(rotation.get("x")),
        "rotation_y": _number(rotation.get("y")),
        "rotation_z": _number(rotation.get("z")),
    }


def _is_internal_site(node: Mapping[str, Any]) -> bool:
    config = _json_object(node.get("config"))
    candidates = {
        str(node.get("type") or "").replace("-", "_").casefold(),
        str(config.get("type") or "").replace("-", "_").casefold(),
        str(config.get("category") or "").replace("-", "_").casefold(),
    }
    return bool(candidates & _INTERNAL_SITE_TYPES)


def _stable_uuid(source_id: str, domain: str, value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"unilabos:{source_id}:{domain}:{value}"))


def _normalize_category(value: str) -> str:
    return value.strip().replace("_", "-").casefold()


def _json_object(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("投影字段必须是 JSON object")
    return json.loads(json.dumps(dict(value), allow_nan=False, ensure_ascii=False))


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value.strip()


def _optional_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是有限数值")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} 必须是有限数值") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} 必须是有限数值")
    return result


def _number(primary: object, fallback: object = 0.0) -> float:
    for value in (primary, fallback):
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(result):
            return result
    return 0.0


def _positive_number(value: object, fallback: float) -> float:
    result = _number(value, fallback)
    return result if result > 0 else fallback


__all__ = [
    "MaterialDefinitionProjection",
    "PackageMaterialProjection",
    "build_package_material_projection",
    "build_resource_graph_import",
]
