"""静态外壳叠加独立关节模型的 OS 合同。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from xml.etree import ElementTree as ET

from unilabos.device_mesh.package_moveit_model import (
    collect_package_joint_state_owners,
    get_package_render_model,
    instantiate_joint_state_model,
)
from unilabos.device_mesh.kinematic_runtime import compile_kinematic_runtime

_STATIC_DIGEST = "a" * 64
_JOINT_DIGEST = "b" * 64
_TOPOLOGY_DIGEST = "c" * 64


def _install_providers(mesh_path: Path) -> None:
    """安装确定性的静态外壳和单轴关节 Provider；只影响本测试进程。"""

    static_module = ModuleType("test_rail_static_provider")
    joint_module = ModuleType("test_rail_joint_provider")

    def build_static(*, member_id: str) -> SimpleNamespace:
        """返回带 visual/collision 的只读外壳。"""

        geometry = f"<geometry><mesh filename='{mesh_path.as_uri()}'/></geometry>"
        return SimpleNamespace(
            visual_urdf=(
                f"<robot name='{member_id}_shell'><link name='{member_id}_shell'>"
                f"<visual>{geometry}</visual><collision>{geometry}</collision>"
                "</link></robot>"
            ),
            root_link=f"{member_id}_shell",
            source_digest=_STATIC_DIGEST,
            mesh_paths=(mesh_path,),
        )

    def build_joint_state(
        *,
        device_id: str,
        position: object,
        rotation: object,
    ) -> SimpleNamespace:
        """返回一根棱柱轴及其子设备安装 link。"""

        del position, rotation
        return SimpleNamespace(
            render_urdf=(
                f"<robot name='{device_id}_axis'>"
                f"<link name='{device_id}_base'/>"
                f"<link name='{device_id}_carriage'/>"
                f"<joint name='{device_id}_axis_joint' type='prismatic'>"
                f"<parent link='{device_id}_base'/><child link='{device_id}_carriage'/>"
                "<axis xyz='1 0 0'/><limit lower='0' upper='1' effort='0' velocity='1'/>"
                "</joint></robot>"
            ),
            source_digest=_JOINT_DIGEST,
            qualified_joint_names=(f"{device_id}_axis_joint",),
            topology_digest=_TOPOLOGY_DIGEST,
            mesh_paths=(),
            mount_link=f"{device_id}_carriage",
        )

    static_module.build_static = build_static
    joint_module.build_joint_state = build_joint_state
    sys.modules[static_module.__name__] = static_module
    sys.modules[joint_module.__name__] = joint_module


def _model_config() -> dict[str, str]:
    """返回同时锁定两份 Provider 摘要的注册表模型配置。"""

    return {
        "type": "package_static",
        "provider": "test_rail_static_provider:build_static",
        "source_digest": _STATIC_DIGEST,
        "joint_state_provider": "test_rail_joint_provider:build_joint_state",
        "joint_state_source_digest": _JOINT_DIGEST,
    }


def test_static_shell_and_joint_provider_compile_one_render_model(
    tmp_path: Path,
) -> None:
    """前端模型必须同时含外壳、可动轴和明确的滑座安装 link。"""

    mesh = tmp_path / "rail.stl"
    mesh.write_bytes(b"0" * 84)
    _install_providers(mesh)
    graph = {
        "rail": {
            "id": "rail",
            "type": "device",
            "class": "test_rail",
            "position": {"x": 1000, "y": 0, "z": 0},
            "config": {},
        }
    }

    owners = collect_package_joint_state_owners(
        graph,
        {"test_rail": {"model": _model_config()}},
    )
    model = get_package_render_model("rail")

    assert owners[0].qualified_joint_names == ("rail_axis_joint",)
    assert model is not None
    assert model.mount_link == "rail_carriage"
    assert "rail_axis_joint" in model.render_urdf
    assert "rail/meshes/rail.stl" in model.render_urdf
    assert model.mesh_paths == (mesh.resolve(),)


def test_joint_model_is_world_mounted_without_granting_motion(
    tmp_path: Path,
) -> None:
    """ROS 可视化模型只增加世界安装和关节，不生成控制器或动作入口。"""

    mesh = tmp_path / "rail.stl"
    mesh.write_bytes(b"0" * 84)
    _install_providers(mesh)
    graph = {
        "rail": {
            "id": "rail",
            "position": {"x": 1000, "y": 0, "z": 0},
            "config": {},
        }
    }

    root = ET.fromstring(
        instantiate_joint_state_model(_model_config(), graph["rail"], graph)
    )
    joints = {
        joint.attrib["name"]: joint.attrib["type"]
        for joint in root.findall("joint")
    }

    assert joints == {
        "rail_kinematic_world_joint": "fixed",
        "rail_axis_joint": "prismatic",
    }
    assert root.find("joint/origin").attrib["xyz"] == "1 0 0"
    assert root.find(".//ros2_control") is None


def test_kinematic_runtime_projects_parent_mount_link_to_child(
    tmp_path: Path,
) -> None:
    """导轨子设备必须拿到滑座 link，前端才能随关节连续运动。"""

    mesh = tmp_path / "rail.stl"
    mesh.write_bytes(b"0" * 84)
    _install_providers(mesh)
    graph = {
        "rail": {
            "id": "rail",
            "type": "device",
            "class": "test_rail",
            "config": {},
        },
        "robot": {
            "id": "robot",
            "type": "device",
            "class": "test_robot",
            "parent": "rail",
            "config": {},
        },
    }
    rail = SimpleNamespace(id="rail", config={}, parent=None)
    robot = SimpleNamespace(id="robot", config={}, parent=rail)
    tree = SimpleNamespace(
        all_nodes=[
            SimpleNamespace(res_content=rail),
            SimpleNamespace(res_content=robot),
        ]
    )

    compile_kinematic_runtime(
        graph,
        {
            "test_rail": {"model": _model_config()},
            "test_robot": {},
        },
        tree,
    )

    assert robot.config["rendering"]["parent_link"] == "rail_carriage"
    assert graph["robot"]["_kinematic_parent_link"] == "rail_carriage"
