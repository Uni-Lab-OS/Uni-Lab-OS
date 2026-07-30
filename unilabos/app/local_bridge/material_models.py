"""OS 本地物料模型登记表与安全资源解析。

模型文件随 Uni-Lab-OS Python 包分发，也可由已安装设备包通过
``unilabos.model_bundles`` entry point 提供。桥启动并加载物料图时一次性
校验入口文件与模型目录，随后 Material API 只返回稳定的同源 URL；浏览器
不会直接接触宿主机路径。
"""

from __future__ import annotations

import importlib.metadata as metadata
import importlib.resources as resources
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


logger = logging.getLogger(__name__)

MODEL_ASSET_URL_PREFIX = "/api/v1/material-models/assets"


@dataclass(frozen=True)
class LocalMaterialModel:
    """一个已登记并通过启动校验的本地模型入口。"""

    key: str
    relative_path: str
    format: str
    match_tokens: tuple[str, ...]
    color: str | None = None
    model_position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    model_rotation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    instance_relative_path: str | None = None
    instance_format: str = "stl"
    instance_color: str | None = None
    instance_site_kinds: tuple[str, ...] = ()
    instance_visible_states: tuple[str, ...] = ()
    instance_position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    instance_rotation: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def public_snapshot(self) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "key": self.key,
            "path": f"{MODEL_ASSET_URL_PREFIX}/{self.relative_path}",
            "format": self.format,
            "color": self.color,
            "position": list(self.model_position),
            "rotation": list(self.model_rotation),
        }
        if self.instance_relative_path:
            snapshot["instances"] = {
                "path": (
                    f"{MODEL_ASSET_URL_PREFIX}/"
                    f"{self.instance_relative_path}"
                ),
                "format": self.instance_format,
                "color": self.instance_color,
                "siteKinds": list(self.instance_site_kinds),
                "visibleStates": list(self.instance_visible_states),
                "position": list(self.instance_position),
                "rotation": list(self.instance_rotation),
            }
        return snapshot


_MODEL_DEFINITIONS = (
    LocalMaterialModel(
        key="opentrons-liquid-handler",
        relative_path="devices/opentrons_liquid_handler/macro_device.xacro",
        format="xacro",
        match_tokens=("liquid_handler",),
    ),
    LocalMaterialModel(
        key="arm-slider",
        relative_path="devices/arm_slider/macro_device.xacro",
        format="xacro",
        match_tokens=("robotic_arm", "arm_slider"),
    ),
    LocalMaterialModel(
        key="thermo-orbitor-rs2-hotel",
        relative_path="devices/thermo_orbitor_rs2_hotel/macro_device.xacro",
        format="xacro",
        match_tokens=("thermo_orbitor_rs2_hotel", "hotel"),
    ),
    # 这两个 STL 的建模坐标是 Y-up，直接加载可保持孔板平放。模型原点
    # 位于右后角，因此在 Pascal 模型组内旋转并平移到 PLR 左前角原点。
    LocalMaterialModel(
        key="tiprack-96-high",
        relative_path="resources/tiprack_96_high/meshes/tiprack_96_high.stl",
        format="stl",
        match_tokens=("tip_rack", "tiprack"),
        color="#22c55e",
        model_position=(0.128, 0.0, 0.0),
        model_rotation=(0.0, -1.5707963267948966, 0.0),
        instance_relative_path="resources/tip/meshes/tip.stl",
        instance_color="#22c55e",
        instance_site_kinds=("tip-spot",),
        instance_visible_states=("tip-present",),
        instance_rotation=(-1.5707963267948966, 0.0, 0.0),
    ),
    LocalMaterialModel(
        key="plate-96-high",
        relative_path="resources/plate_96_high/meshes/plate_96_high.stl",
        format="stl",
        match_tokens=("wellplate", "plate"),
        color="#22c55e",
        model_position=(0.128, 0.0, 0.0),
        model_rotation=(0.0, -1.5707963267948966, 0.0),
    ),
)


def _prefer_web_representation(
    representations: Mapping[str, Any],
) -> tuple[str, str] | None:
    """Pick web representation first, then kinematics/collision."""

    for name in ("web", "kinematics", "collision"):
        raw = representations.get(name)
        if not isinstance(raw, Mapping):
            continue
        entry = str(raw.get("entry") or "").strip()
        fmt = str(raw.get("format") or "").strip().casefold()
        if entry and fmt:
            return entry, fmt
    return None


def _load_bundle_models() -> tuple[
    dict[str, LocalMaterialModel],
    dict[str, Path],
]:
    """Discover installed ``unilabos.model_bundles`` and register present assets."""

    registrations: dict[str, LocalMaterialModel] = {}
    asset_index: dict[str, Path] = {}
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
                "[material-models] 跳过损坏的 model_bundle %s: %s",
                entry_point.name,
                exc,
            )
            continue
        if not isinstance(descriptor, Mapping):
            logger.warning(
                "[material-models] model_bundle %s 未返回 mapping",
                entry_point.name,
            )
            continue
        package_name = str(descriptor.get("package") or "").strip()
        manifest_name = str(
            descriptor.get("manifest") or "model_manifest.yaml"
        ).strip()
        if not package_name:
            continue
        try:
            package_root = Path(str(resources.files(package_name)))
            manifest_text = (
                resources.files(package_name)
                .joinpath(manifest_name)
                .read_text(encoding="utf-8")
            )
            manifest = yaml.safe_load(manifest_text) or {}
        except Exception as exc:
            logger.warning(
                "[material-models] 无法读取 bundle %s/%s: %s",
                package_name,
                manifest_name,
                exc,
            )
            continue
        if not isinstance(manifest, Mapping):
            continue
        bundle = manifest.get("bundle") if isinstance(manifest.get("bundle"), Mapping) else {}
        bundle_id = str(bundle.get("id") or entry_point.name).strip() or entry_point.name
        models = manifest.get("models")
        if not isinstance(models, list):
            continue
        skipped = 0
        registered = 0
        for model in models:
            if not isinstance(model, Mapping):
                continue
            key = str(model.get("key") or "").strip()
            representations = model.get("representations")
            if not key or not isinstance(representations, Mapping):
                continue
            preferred = _prefer_web_representation(representations)
            if preferred is None:
                skipped += 1
                continue
            entry, fmt = preferred
            absolute = (package_root / entry).resolve()
            try:
                absolute.relative_to(package_root.resolve())
            except ValueError:
                skipped += 1
                continue
            if not absolute.is_file():
                skipped += 1
                continue
            applies = model.get("applies_to")
            tokens: list[str] = []
            if isinstance(applies, list):
                for rule in applies:
                    if not isinstance(rule, Mapping):
                        continue
                    klass = str(rule.get("class") or "").strip()
                    if klass:
                        tokens.append(klass.replace("-", "_").casefold())
            if not tokens:
                tokens.append(key.replace("-", "_").casefold())
            public_rel = f"bundles/{bundle_id}/{entry}".replace("\\", "/")
            asset_index[public_rel] = absolute
            registrations[f"{bundle_id}:{key}"] = LocalMaterialModel(
                key=f"{bundle_id}:{key}",
                relative_path=public_rel,
                format=fmt,
                match_tokens=tuple(tokens),
            )
            registered += 1
        logger.info(
            "[material-models] bundle=%s registered=%d skipped_missing=%d root=%s",
            bundle_id,
            registered,
            skipped,
            package_root,
        )
    return registrations, asset_index


class MaterialModelRegistry:
    """登记 OS 与设备包模型，并把设备身份解析为可公开的模型快照。"""

    def __init__(self, asset_root: str | Path | None = None) -> None:
        package_root = Path(__file__).resolve().parents[2]
        self.asset_root = (
            Path(asset_root).expanduser().resolve()
            if asset_root is not None
            else (package_root / "device_mesh").resolve()
        )
        if not self.asset_root.is_dir():
            raise FileNotFoundError(
                f"Material model asset directory does not exist: {self.asset_root}"
            )

        registrations: dict[str, LocalMaterialModel] = {}
        asset_index: dict[str, Path] = {}
        for definition in _MODEL_DEFINITIONS:
            entry = (self.asset_root / definition.relative_path).resolve()
            try:
                entry.relative_to(self.asset_root)
            except ValueError as exc:
                raise ValueError(
                    "Material model asset path escapes asset root"
                ) from exc
            if not entry.is_file():
                raise FileNotFoundError(
                    f"Material model entry does not exist: {entry}"
                )
            if definition.instance_relative_path:
                instance_entry = (
                    self.asset_root / definition.instance_relative_path
                ).resolve()
                try:
                    instance_entry.relative_to(self.asset_root)
                except ValueError as exc:
                    raise ValueError(
                        "Material model asset path escapes asset root"
                    ) from exc
                if not instance_entry.is_file():
                    raise FileNotFoundError(
                        "Material instance model entry does not exist: "
                        f"{instance_entry}"
                    )
                asset_index[definition.instance_relative_path] = instance_entry
            asset_index[definition.relative_path] = entry
            registrations[definition.key] = definition

        bundle_models, bundle_assets = _load_bundle_models()
        registrations.update(bundle_models)
        asset_index.update(bundle_assets)

        self._registrations = registrations
        self._asset_index = asset_index
        logger.info(
            "[material-models] 已登记 %d 个模型（含设备包 bundle），OS 资源根=%s",
            len(registrations),
            self.asset_root,
        )

    def list_models(self) -> list[dict[str, Any]]:
        """返回前端可消费、不含宿主机绝对路径的模型清单。"""

        return [
            model.public_snapshot()
            for model in self._registrations.values()
        ]

    def model_for_identity(self, identity: str) -> dict[str, Any]:
        """按节点类型/类名解析模型；更具体的登记项优先。"""

        normalized = identity.replace("-", "_").casefold()
        matches = [
            (
                max(
                    (
                        len(token)
                        for token in model.match_tokens
                        if token in normalized
                    ),
                    default=0,
                ),
                model,
            )
            for model in self._registrations.values()
        ]
        if not matches:
            return {
                "path": "",
                "format": "none",
                "attachPoints": [],
            }
        best_score, best_model = max(matches, key=lambda item: item[0])
        if best_score:
            return best_model.public_snapshot()
        return {
            "path": "",
            "format": "none",
            "attachPoints": [],
        }

    def resolve_asset(self, relative_path: str) -> Path:
        """把公开相对路径解析到模型根，并拒绝目录穿越。"""

        normalized = relative_path.replace("\\", "/").lstrip("/")
        indexed = self._asset_index.get(normalized)
        if indexed is not None:
            return indexed

        candidate = (self.asset_root / normalized).resolve()
        try:
            candidate.relative_to(self.asset_root)
        except ValueError as exc:
            raise ValueError("Material model asset path escapes asset root") from exc
        return candidate
