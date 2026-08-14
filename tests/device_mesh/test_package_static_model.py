"""软件包静态视觉 URDF 接入 MoveIt/RViz 的 OS 合同。"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from unilabos.device_mesh.package_moveit_model import get_ros_model_type
from unilabos.device_mesh.package_static_model import (
    instantiate_package_static_model,
    load_package_static_model,
    resolve_graph_world_pose,
)

_DIGEST = "a" * 64


def test_ros_model_type_accepts_package_static() -> None:
    """ROS 层把 package_static 当作显式场景模型，而不是 FE 的 format/entry。"""

    assert get_ros_model_type({"type": "package_static"}) == "package_static"
    assert get_ros_model_type({"format": "gltf", "entry": "models/deck.glb"}) is None


def _provider_module(
    mesh_path: Path, *, collision: bool = True, execution: bool = False
) -> ModuleType:
    module = ModuleType("test_static_provider")

    def build_static_model(*, member_id: str):
        root_link = f"{member_id}_base_link"
        mesh = f"<geometry><mesh filename='{mesh_path.as_uri()}'/></geometry>"
        extra = f"<collision>{mesh}</collision>" if collision else ""
        extra += "<transmission name='x'/>" if execution else ""
        return SimpleNamespace(
            visual_urdf=(
                f"<robot name='{member_id}_layout'><link name='{root_link}'>"
                f"<visual>{mesh}</visual>{extra}</link></robot>"
            ),
            root_link=root_link,
            source_digest=_DIGEST,
            mesh_paths=(mesh_path,),
        )

    module.build_static_model = build_static_model
    sys.modules[module.__name__] = module
    return module


def _config() -> dict:
    return {
        "type": "package_static",
        "provider": "test_static_provider:build_static_model",
        "source_digest": _DIGEST,
    }


def test_package_static_mounts_composed_graph_pose(tmp_path: Path) -> None:
    """安装位姿由 Graph 父子合成；URDF 必须带 collision 供 MoveIt 规划。"""

    mesh_path = tmp_path / "deck.stl"
    mesh_path.write_bytes(b"0" * 84)
    _provider_module(mesh_path)
    graph = {
        "ptlc_deck": {
            "id": "ptlc_deck",
            "parent": None,
            "position": {"position": {"x": 350, "y": 0, "z": 0}},
        },
        "group_rack_warehouse": {
            "id": "group_rack_warehouse",
            "parent": "ptlc_deck",
            "position": {
                "position": {"x": 1370, "y": -14, "z": 850},
                "rotation": {"x": 0, "y": 0, "z": 90},
            },
        },
    }
    xml = instantiate_package_static_model(
        _config(),
        graph["group_rack_warehouse"],
        graph,
    )
    root = ET.fromstring(xml)
    joint = root.find("joint")
    assert joint is not None
    assert joint.attrib["name"] == "group_rack_warehouse_layout_world_joint"
    assert joint.attrib["type"] == "fixed"
    assert joint.find("parent").attrib["link"] == "world"
    assert joint.find("child").attrib["link"] == "group_rack_warehouse_base_link"
    origin = joint.find("origin")
    xyz = [float(part) for part in origin.attrib["xyz"].split()]
    rpy = [float(part) for part in origin.attrib["rpy"].split()]
    assert xyz == pytest.approx((1.72, -0.014, 0.85), abs=1e-9)
    assert rpy[0] == pytest.approx(0.0, abs=1e-9)
    assert rpy[1] == pytest.approx(0.0, abs=1e-9)
    assert rpy[2] == pytest.approx(1.5707963267948966, abs=1e-9)
    assert root.find(".//collision") is not None


def test_package_static_requires_collision_geometry(tmp_path: Path) -> None:
    """没有 collision 的静态 URDF 不能进入 MoveIt 规划模型。"""

    mesh_path = tmp_path / "deck.stl"
    mesh_path.write_bytes(b"0" * 84)
    _provider_module(mesh_path, collision=False)
    with pytest.raises(ValueError, match="collision geometry"):
        load_package_static_model(
            _config(),
            {"id": "ptlc_deck"},
        )


def test_package_static_rejects_execution_content(tmp_path: Path) -> None:
    """静态模型不得声明传动或控制器。"""

    mesh_path = tmp_path / "deck.stl"
    mesh_path.write_bytes(b"0" * 84)
    _provider_module(mesh_path, execution=True)
    with pytest.raises(ValueError, match="execution content"):
        load_package_static_model(
            _config(),
            {"id": "ptlc_deck"},
        )


def test_resolve_graph_world_pose_uses_nested_millimetre_position() -> None:
    """Issue #183：毫米/度、右手 Z-up，父节点平移后再叠加子节点。"""

    graph = {
        "ptlc_deck": {
            "id": "ptlc_deck",
            "parent": None,
            "position": {"position": {"x": 350, "y": 0, "z": 0}},
        },
        "source_sample_warehouse": {
            "id": "source_sample_warehouse",
            "parent": "ptlc_deck",
            "position": {"x": -1230, "y": 500, "z": 850},
        },
    }
    xyz_m, rpy_rad = resolve_graph_world_pose(
        graph["source_sample_warehouse"],
        graph,
    )
    assert xyz_m == pytest.approx((-0.88, 0.5, 0.85), abs=1e-9)
    assert rpy_rad == pytest.approx((0.0, 0.0, 0.0), abs=1e-9)


def test_resolve_graph_world_pose_follows_os_runtime_parent_uuid() -> None:
    """OS 运行时 Graph 只有 parent_uuid 和 pose，必须仍合成导轨上的世界安装。"""

    graph = {
        "rail": {
            "id": "rail",
            "uuid": "rail-uuid",
            "parent_uuid": None,
            "pose": {"position": {"x": -500, "y": 0, "z": 850}},
        },
        "robot": {
            "id": "robot",
            "uuid": "robot-uuid",
            "parent_uuid": "rail-uuid",
            "pose": {
                "position": {"x": 681.301, "y": 0, "z": 150},
                "rotation": {"x": 0, "y": 0, "z": 180},
            },
        },
    }
    xyz_m, rpy_rad = resolve_graph_world_pose(graph["robot"], graph)
    assert xyz_m == pytest.approx((0.181301, 0.0, 1.0), abs=1e-9)
    assert rpy_rad[2] == pytest.approx(math.pi, abs=1e-9)
