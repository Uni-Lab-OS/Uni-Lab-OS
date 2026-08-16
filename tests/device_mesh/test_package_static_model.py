"""领域包（DomainPackage）静态碰撞模型的 OS 合同。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from unilabos.device_mesh.package_static_model import (
    instantiate_package_static_model,
    load_package_static_model,
)

_DIGEST = "a" * 64


def _provider(mesh_path: Path, *, collision: bool = True) -> ModuleType:
    module = ModuleType("test_static_provider")

    def build_static_model(*, member_id: str):
        root_link = f"{member_id}_base_link"
        mesh = f"<geometry><mesh filename='{mesh_path.as_uri()}'/></geometry>"
        collision_xml = f"<collision>{mesh}</collision>" if collision else ""
        return SimpleNamespace(
            visual_urdf=(
                f"<robot name='{member_id}'><link name='{root_link}'>"
                f"<visual>{mesh}</visual>{collision_xml}</link></robot>"
            ),
            root_link=root_link,
            source_digest=_DIGEST,
            mesh_paths=(mesh_path,),
        )

    module.build_static_model = build_static_model
    sys.modules[module.__name__] = module
    return module


def _config() -> dict[str, str]:
    return {
        "type": "package_static",
        "provider": "test_static_provider:build_static_model",
        "source_digest": _DIGEST,
    }


def test_static_model_mounts_graph_pose_and_keeps_collision(tmp_path: Path) -> None:
    """静态碰撞树必须按父子世界位姿固定挂到 ``world``。"""

    mesh = tmp_path / "deck.stl"
    mesh.write_bytes(b"0" * 84)
    _provider(mesh)
    graph = {
        "deck": {"id": "deck", "position": {"x": 350, "y": 0, "z": 0}},
        "rack": {
            "id": "rack",
            "parent": "deck",
            "position": {
                "position": {"x": 1000, "y": 0, "z": 850},
                "rotation": {"x": 0, "y": 0, "z": 90},
            },
        },
    }
    xml = instantiate_package_static_model(_config(), graph["rack"], graph)
    root = ET.fromstring(xml)

    assert root.find("joint/parent").attrib["link"] == "world"
    assert root.find("joint/origin").attrib["xyz"] == "1.35 0 0.85"
    assert root.find("joint/origin").attrib["rpy"] == "0 0 1.57079632679"
    assert root.find(".//collision") is not None


def test_static_model_rejects_missing_collision(tmp_path: Path) -> None:
    """没有碰撞几何的视觉模型不得冒充 MoveIt 环境。"""

    mesh = tmp_path / "deck.stl"
    mesh.write_bytes(b"0" * 84)
    _provider(mesh, collision=False)

    with pytest.raises(ValueError, match="collision geometry"):
        load_package_static_model(_config(), {"id": "deck"})
