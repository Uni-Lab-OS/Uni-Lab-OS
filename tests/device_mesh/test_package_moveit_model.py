"""领域包（DomainPackage）运动模型的 OS 接入合同。"""

from __future__ import annotations

import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.web.kinematic_model_api import create_kinematic_model_router
from unilabos.device_mesh.package_moveit_model import (
    apply_graph_world_mount,
    collect_package_joint_state_owners,
    get_package_render_mesh,
    get_package_render_model,
    load_graph_package_moveit_model,
    load_package_moveit_model,
)

_SOURCE_DIGEST = "a" * 64


def _provider_module(*, source_digest: str = _SOURCE_DIGEST) -> ModuleType:
    module = ModuleType("test_moveit_provider")

    def build_moveit_model(**kwargs):
        module.last_kwargs = kwargs
        mesh_path = Path(__file__).resolve()
        device_id = str(kwargs["device_id"])
        execution_urdf = (
            "<robot name='test'><link name='base'><visual><geometry>"
            f"<mesh filename='{mesh_path.as_uri()}'/>"
            "</geometry></visual></link><link name='tip'/>"
            f"<joint name='{device_id}_joint_1' type='revolute'>"
            "<parent link='base'/><child link='tip'/></joint></robot>"
        )
        render_urdf = execution_urdf.replace(
            mesh_path.as_uri(),
            f"{device_id}/meshes/{mesh_path.name}",
        )
        return SimpleNamespace(
            execution_urdf=execution_urdf,
            render_urdf=render_urdf,
            srdf=(
                "<robot name='test'><group name='arm'>"
                "<chain base_link='base' tip_link='tip'/></group></robot>"
            ),
            ros2_controllers={"controller_manager": {"ros__parameters": {}}},
            moveit_controllers={
                "moveit_simple_controller_manager": {"controller_names": []}
            },
            kinematics={"arm": {}},
            joint_limits={"joint_limits": {}},
            source_digest=source_digest,
            mesh_paths=(mesh_path,),
            qualified_joint_names=(f"{device_id}_joint_1",),
            topology_digest="b" * 64,
            rviz_required=False,
        )

    module.build_moveit_model = build_moveit_model
    return module


def _model_config() -> dict[str, str]:
    return {
        "type": "package_moveit",
        "provider": "test_moveit_provider:build_moveit_model",
        "source_digest": _SOURCE_DIGEST,
    }


def test_provider_receives_only_instance_world_pose(monkeypatch) -> None:
    """型号 Provider 只接收设备身份与世界安装位姿。"""

    provider = _provider_module()
    monkeypatch.setitem(sys.modules, provider.__name__, provider)
    node = {
        "id": "robot",
        "position": {"x": 100.0, "y": 200.0, "z": 300.0},
        "config": {"rotation": {"x": 0.0, "y": 0.0, "z": 1.57}},
    }
    bundle = load_package_moveit_model(_model_config(), node)

    assert provider.last_kwargs == {
        "device_id": "robot",
        "position": node["position"],
        "rotation": node["config"]["rotation"],
    }
    assert bundle.execution_urdf == bundle.urdf
    assert bundle.qualified_joint_names == ("robot_joint_1",)


def test_graph_mount_composes_parent_rail_pose() -> None:
    """机械臂相对导轨的毫米/角度位姿必须合成为唯一世界安装。"""

    graph = {
        "rail": {
            "id": "rail",
            "position": {"x": -500.0, "y": 0.0, "z": 850.0},
        },
        "robot": {
            "id": "robot",
            "parent": "rail",
            "position": {
                "position": {"x": 681.301, "y": 0.0, "z": 150.0},
                "rotation": {"x": 0.0, "y": 0.0, "z": 180.0},
            },
            "config": {},
        },
    }

    mounted = apply_graph_world_mount(graph["robot"], graph)

    assert mounted["position"]["x"] == pytest.approx(181.301)
    assert mounted["position"]["z"] == pytest.approx(1000.0)
    assert mounted["config"]["rotation"]["z"] == pytest.approx(math.pi)


def test_graph_mount_does_not_reinterpret_provider_rotation_as_degrees() -> None:
    """Provider 弧度配置不是物理图位姿，缺少图旋转时不得重复换算。"""

    graph = {
        "robot": {
            "id": "robot",
            "position": {"x": 100.0, "y": 0.0, "z": 0.0},
            "config": {"rotation": {"x": 0.0, "y": 0.0, "z": math.pi}},
        },
    }

    mounted = apply_graph_world_mount(graph["robot"], graph)

    assert mounted["config"]["rotation"] == pytest.approx(
        {"x": 0.0, "y": 0.0, "z": 0.0}
    )


def test_moveit_child_mounts_to_parent_kinematic_link(monkeypatch) -> None:
    """机械臂执行 URDF 必须挂到导轨滑座，而不是把父位姿压平到 world。"""

    provider = _provider_module()
    original = provider.build_moveit_model

    def build_moveit_model(**kwargs):
        """给通用测试 Bundle 增加 Provider 正常声明的 world 安装关节。"""

        bundle = original(**kwargs)
        root = ET.fromstring(bundle.execution_urdf)
        mount = ET.Element(
            "joint",
            {"name": "robot_world_mount_joint", "type": "fixed"},
        )
        ET.SubElement(mount, "origin", {"xyz": "0 0 0.15", "rpy": "0 0 0"})
        ET.SubElement(mount, "parent", {"link": "world"})
        ET.SubElement(mount, "child", {"link": "base"})
        root.insert(0, mount)
        bundle.execution_urdf = ET.tostring(root, encoding="unicode")
        return bundle

    provider.build_moveit_model = build_moveit_model
    monkeypatch.setitem(sys.modules, provider.__name__, provider)
    monkeypatch.setattr(
        "unilabos.device_mesh.package_moveit_model.get_package_render_model",
        lambda _device_id: None,
    )
    graph = {
        "rail": {
            "id": "rail",
            "uuid": "rail-uuid",
            "position": {"x": -500, "y": 0, "z": 850},
        },
        "robot": {
            "id": "robot",
            "parent_uuid": "rail-uuid",
            "position": {
                "position": {"x": 0, "y": 0, "z": 150},
                "rotation": {"x": 0, "y": 0, "z": 180},
            },
            "config": {},
            "_kinematic_parent_link": "rail_carriage",
        },
    }

    bundle = load_graph_package_moveit_model(_model_config(), graph["robot"], graph)
    root = ET.fromstring(bundle.execution_urdf)
    mount = root.find("joint")

    assert mount.find("parent").attrib["link"] == "rail_carriage"
    assert provider.last_kwargs["position"] == {"x": 0.0, "y": 0.0, "z": 150.0}


def test_two_instances_freeze_separate_owners_and_render_assets(monkeypatch) -> None:
    """同型号双实例共享模型语义，但关节名、渲染目录和资产不串实例。"""

    provider = _provider_module()
    monkeypatch.setitem(sys.modules, provider.__name__, provider)
    graph = {
        device_id: {
            "id": device_id,
            "type": "device",
            "class": "test_robot",
            "config": {},
        }
        for device_id in ("robot_a", "robot_b")
    }
    owners = collect_package_joint_state_owners(
        graph,
        {"test_robot": {"model": _model_config()}},
    )

    assert [owner.device_id for owner in owners] == ["robot_a", "robot_b"]
    assert owners[0].qualified_joint_names == ("robot_a_joint_1",)
    assert owners[1].qualified_joint_names == ("robot_b_joint_1",)
    assert get_package_render_model("robot_a") is not None
    assert get_package_render_mesh("robot_a", Path(__file__).name) == Path(
        __file__
    ).resolve()
    assert get_package_render_mesh("robot_a", "../secret.stl") is None


def test_render_api_serves_only_frozen_model_and_owned_mesh(monkeypatch) -> None:
    """前端只可读取当前启动代际冻结的 URDF 与其 exact 资产。"""

    provider = _provider_module()
    monkeypatch.setitem(sys.modules, provider.__name__, provider)
    collect_package_joint_state_owners(
        {
            "robot": {
                "id": "robot",
                "type": "device",
                "class": "test_robot",
                "config": {},
            }
        },
        {"test_robot": {"model": _model_config()}},
    )
    app = FastAPI()
    app.include_router(create_kinematic_model_router())
    client = TestClient(app)

    urdf = client.get("/api/v1/kinematic-models/robot.urdf")
    mesh = client.get(
        f"/api/v1/kinematic-models/robot/meshes/{Path(__file__).name}"
    )

    assert urdf.status_code == 200
    assert "robot_joint_1" in urdf.text
    assert urdf.headers["x-unilab-topology-digest"] == "b" * 64
    assert mesh.status_code == 200
    assert client.get("/api/v1/kinematic-models/missing.urdf").status_code == 404
    assert (
        client.get("/api/v1/kinematic-models/robot/meshes/secret.stl").status_code
        == 404
    )


def test_source_digest_drift_closes_startup(monkeypatch) -> None:
    """注册表摘要与领域包源资产不一致时必须关闭启动。"""

    provider = _provider_module(source_digest="c" * 64)
    monkeypatch.setitem(sys.modules, provider.__name__, provider)
    with pytest.raises(ValueError, match="摘要漂移"):
        load_package_moveit_model(
            _model_config(),
            {"id": "robot", "config": {}},
        )
