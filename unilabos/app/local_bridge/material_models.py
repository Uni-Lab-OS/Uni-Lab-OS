"""OS 本地物料模型登记表与安全资源解析。

模型文件随 Uni-Lab-OS Python 包分发。桥启动并加载物料图时一次性校验
入口文件与模型目录，随后 Material API 只返回稳定的同源 URL；浏览器不会
直接接触宿主机路径。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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


class MaterialModelRegistry:
    """登记 OS 包内模型，并把设备身份解析为可公开的模型快照。"""

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
        for definition in _MODEL_DEFINITIONS:
            entry = self.resolve_asset(definition.relative_path)
            if not entry.is_file():
                raise FileNotFoundError(
                    f"Material model entry does not exist: {entry}"
                )
            if definition.instance_relative_path:
                instance_entry = self.resolve_asset(
                    definition.instance_relative_path
                )
                if not instance_entry.is_file():
                    raise FileNotFoundError(
                        "Material instance model entry does not exist: "
                        f"{instance_entry}"
                    )
            registrations[definition.key] = definition
        self._registrations = registrations
        logger.info(
            "[material-models] 已登记 %d 个 OS 本地模型，资源根目录=%s",
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

        candidate = (self.asset_root / relative_path).resolve()
        try:
            candidate.relative_to(self.asset_root)
        except ValueError as exc:
            raise ValueError("Material model asset path escapes asset root") from exc
        return candidate
