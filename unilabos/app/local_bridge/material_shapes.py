"""2.5D 外形声明登记表。

外形是设备包自己的资产：包在 ``unilabos.model_bundles`` entry point 的描述里
多给一个 ``shape_manifest`` 键，桥启动时把清单读进来，前端通过
``/api/v1/material-shapes`` 一次性取回按物料 category 索引的图元列表。这样
前端只需要一个通用图元解释器，不必认识任何具体设备。

清单格式见 ``Uni-Lab-SZLab/schemas/shape-manifest-v1.schema.json``。这里的校验
只拦下会让前端画错的结构（未知图元类型 / 未知样式 token），坏掉的条目整条丢弃
并退回实心包围盒，而不是画出半个设备。
"""

from __future__ import annotations

import importlib.metadata as metadata
import importlib.resources as resources
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml


logger = logging.getLogger(__name__)

CORE_SHAPE_BUNDLE_ID = "unilabos-core"
CORE_SHAPE_MANIFEST = Path(__file__).resolve().parent / "shapes" / "core_shapes.yaml"

PART_TYPES = frozenset(
    {
        "box",
        "slab",
        "cylinder",
        "lathe",
        "disc",
        "rect",
        "edge",
        "grid",
        "sites",
    }
)

STYLE_TOKENS = frozenset(
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

SITE_GENERATORS = frozenset(
    {"open-rack", "stack-shelves", "site-holes", "site-markers"}
)

_SHADOW_MODES = frozenset({"box", "round", "none"})
_SORT_MODES = frozenset({"center", "rear-edge"})
_UNITS = frozenset({"mm", "ratio"})


def normalize_category(value: str) -> str:
    """图里的 category 写法不统一（下划线/连字符/大小写），统一成一种。"""

    return str(value).strip().replace("_", "-").casefold()


@dataclass(frozen=True)
class LocalMaterialShape:
    """一条已通过启动校验的外形声明。"""

    id: str
    bundle: str
    categories: tuple[str, ...]
    category_tokens: tuple[str, ...]
    parts: tuple[Mapping[str, Any], ...]
    display_name: str | None = None
    priority: int = 0
    envelope: tuple[float, float, float] | None = None
    units: str = "mm"
    shadow: str = "box"
    sort: str = "center"
    note: str | None = None

    def public_snapshot(self) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "id": self.id,
            "bundle": self.bundle,
            "categories": list(self.categories),
            "categoryTokens": list(self.category_tokens),
            "priority": self.priority,
            "units": self.units,
            "shadow": self.shadow,
            "sort": self.sort,
            "parts": [dict(part) for part in self.parts],
        }
        if self.display_name:
            snapshot["displayName"] = self.display_name
        if self.envelope is not None:
            snapshot["envelope"] = list(self.envelope)
        if self.note:
            snapshot["note"] = self.note
        return snapshot


@dataclass
class _BundleLoad:
    shapes: list[LocalMaterialShape] = field(default_factory=list)
    rejected: int = 0


def _validate_part(part: Any, *, depth: int = 0) -> str | None:
    """返回错误原因，None 表示通过。"""

    if not isinstance(part, Mapping):
        return "part 不是 mapping"
    part_type = str(part.get("type") or "").strip()
    if part_type not in PART_TYPES:
        return f"未知图元类型 {part_type!r}"
    style = part.get("style")
    if style is not None and str(style) not in STYLE_TOKENS:
        return f"未知样式 token {style!r}"
    units = part.get("units")
    if units is not None and str(units) not in _UNITS:
        return f"未知单位 {units!r}"
    if part_type == "sites":
        generator = str(part.get("generator") or "").strip()
        if generator not in SITE_GENERATORS:
            return f"未知位点生成器 {generator!r}"
    if part_type == "grid":
        if depth >= 1:
            return "grid 不能再嵌套 grid"
        return _validate_part(part.get("part"), depth=depth + 1)
    return None


def _read_shape(
    raw: Any,
    *,
    bundle_id: str,
) -> LocalMaterialShape | None:
    if not isinstance(raw, Mapping):
        return None
    shape_id = str(raw.get("id") or "").strip()
    parts = raw.get("parts")
    if not shape_id or not isinstance(parts, Sequence) or isinstance(parts, str):
        logger.warning(
            "[material-shapes] bundle=%s 丢弃缺 id/parts 的外形条目", bundle_id
        )
        return None

    for part in parts:
        reason = _validate_part(part)
        if reason is not None:
            logger.warning(
                "[material-shapes] bundle=%s 外形 %s 被丢弃：%s",
                bundle_id,
                shape_id,
                reason,
            )
            return None

    categories: list[str] = []
    tokens: list[str] = []
    applies = raw.get("applies_to")
    if isinstance(applies, Sequence) and not isinstance(applies, str):
        for rule in applies:
            if not isinstance(rule, Mapping):
                continue
            exact = rule.get("category")
            contains = rule.get("category_contains")
            if exact:
                categories.append(normalize_category(str(exact)))
            elif contains:
                tokens.append(normalize_category(str(contains)))
    if not categories and not tokens:
        logger.warning(
            "[material-shapes] bundle=%s 外形 %s 没有 applies_to，丢弃",
            bundle_id,
            shape_id,
        )
        return None

    envelope: tuple[float, float, float] | None = None
    raw_envelope = raw.get("envelope")
    if isinstance(raw_envelope, Sequence) and len(raw_envelope) == 3:
        try:
            envelope = (
                float(raw_envelope[0]),
                float(raw_envelope[1]),
                float(raw_envelope[2]),
            )
        except (TypeError, ValueError):
            envelope = None

    units = str(raw.get("units") or "mm")
    shadow = str(raw.get("shadow") or "box")
    sort = str(raw.get("sort") or "center")
    try:
        priority = int(raw.get("priority") or 0)
    except (TypeError, ValueError):
        priority = 0

    return LocalMaterialShape(
        id=shape_id,
        bundle=bundle_id,
        categories=tuple(categories),
        category_tokens=tuple(tokens),
        parts=tuple(dict(part) for part in parts),
        display_name=(
            str(raw["display_name"]) if raw.get("display_name") else None
        ),
        priority=priority,
        envelope=envelope,
        units=units if units in _UNITS else "mm",
        shadow=shadow if shadow in _SHADOW_MODES else "box",
        sort=sort if sort in _SORT_MODES else "center",
        note=str(raw["note"]) if raw.get("note") else None,
    )


def _load_manifest_text(text: str, *, fallback_bundle_id: str) -> _BundleLoad:
    load = _BundleLoad()
    try:
        manifest = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        logger.warning(
            "[material-shapes] 清单 %s 解析失败: %s", fallback_bundle_id, exc
        )
        return load
    if not isinstance(manifest, Mapping):
        return load
    bundle = (
        manifest.get("bundle")
        if isinstance(manifest.get("bundle"), Mapping)
        else {}
    )
    bundle_id = str(bundle.get("id") or fallback_bundle_id).strip()
    bundle_id = bundle_id or fallback_bundle_id
    shapes = manifest.get("shapes")
    if not isinstance(shapes, Sequence) or isinstance(shapes, str):
        return load
    for raw in shapes:
        shape = _read_shape(raw, bundle_id=bundle_id)
        if shape is None:
            load.rejected += 1
            continue
        load.shapes.append(shape)
    return load


def _load_bundle_shapes() -> list[LocalMaterialShape]:
    """扫描已安装设备包声明的 ``shape_manifest``。

    与 :mod:`material_models` 共用 ``unilabos.model_bundles`` 这一个 entry point
    组：一个设备包只需要声明一次自己是 bundle，模型与外形都从同一个描述里取。
    """

    collected: list[LocalMaterialShape] = []
    entry_points = metadata.entry_points()
    selected = (
        entry_points.select(group="unilabos.model_bundles")
        if hasattr(entry_points, "select")
        else entry_points.get("unilabos.model_bundles", [])
    )
    for entry_point in selected:
        try:
            provider = entry_point.load()
            descriptor = provider() if callable(provider) else provider
        except Exception as exc:  # pragma: no cover - plugin isolation
            logger.warning(
                "[material-shapes] 跳过损坏的 model_bundle %s: %s",
                entry_point.name,
                exc,
            )
            continue
        if not isinstance(descriptor, Mapping):
            continue
        package_name = str(descriptor.get("package") or "").strip()
        manifest_name = str(descriptor.get("shape_manifest") or "").strip()
        if not package_name or not manifest_name:
            continue
        try:
            text = (
                resources.files(package_name)
                .joinpath(manifest_name)
                .read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning(
                "[material-shapes] 无法读取 %s/%s: %s",
                package_name,
                manifest_name,
                exc,
            )
            continue
        load = _load_manifest_text(text, fallback_bundle_id=entry_point.name)
        collected.extend(load.shapes)
        logger.info(
            "[material-shapes] bundle=%s registered=%d rejected=%d",
            load.shapes[0].bundle if load.shapes else entry_point.name,
            len(load.shapes),
            load.rejected,
        )
    return collected


class MaterialShapeRegistry:
    """登记 OS 通用外形与设备包外形，供前端 2.5D 解释器消费。"""

    def __init__(self, *, core_manifest: Path | None = None) -> None:
        manifest_path = core_manifest or CORE_SHAPE_MANIFEST
        shapes: list[LocalMaterialShape] = []
        if manifest_path.is_file():
            shapes.extend(
                _load_manifest_text(
                    manifest_path.read_text(encoding="utf-8"),
                    fallback_bundle_id=CORE_SHAPE_BUNDLE_ID,
                ).shapes
            )
        else:  # pragma: no cover - 打包缺失时不该让桥起不来
            logger.warning(
                "[material-shapes] 缺少 OS 通用外形清单 %s", manifest_path
            )
        shapes.extend(_load_bundle_shapes())

        # id 只在 bundle 内唯一；跨 bundle 命中同一个 category 时，由 applies_to
        # 的精确度（exact 胜 contains）与 priority 决定用哪一个。
        by_id: dict[str, LocalMaterialShape] = {}
        for shape in shapes:
            by_id[f"{shape.bundle}:{shape.id}"] = shape
        self._shapes = tuple(by_id.values())
        logger.info(
            "[material-shapes] 已登记 %d 个外形（含设备包 bundle）",
            len(self._shapes),
        )

    def list_shapes(self) -> list[dict[str, Any]]:
        """返回前端可直接消费的外形清单。"""

        return [shape.public_snapshot() for shape in self._shapes]
