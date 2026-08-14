"""软件包自包含 MoveIt 模型 Provider 的 OS 接入合同。"""

from __future__ import annotations

import math
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from unilabos.device_mesh.package_moveit_model import (
    apply_graph_world_mount,
    collect_package_joint_state_owners,
    create_package_moveit_client,
    get_package_render_mesh,
    get_package_render_model,
    get_ros_model_type,
    load_package_moveit_model,
    merge_package_moveit_parameters,
    package_moveit_client_spec,
)

_DIGEST = "a" * 64


@pytest.mark.parametrize(
    ("model_config", "expected"),
    [
        ({"type": "package_moveit"}, "package_moveit"),
        ({"type": "device"}, "device"),
        ({"format": "gltf", "entry": "models/device.glb"}, None),
        ({"shape": {"format": "unilab.shape/v1"}}, None),
        ({"$ref": "real_device"}, None),
        ("legacy-model-name", None),
    ],
)
def test_ros_model_type_ignores_workspace_frontend_models(
    model_config: object,
    expected: str | None,
) -> None:
    """ROS 层只消费显式 ROS 类型，不误解析 FE 的 2.5D 模型声明。"""

    assert get_ros_model_type(model_config) == expected


def _provider_module(*, rviz_required: bool = False) -> ModuleType:
    """创建记录 OS 调用参数的内存模型 Provider 模块。"""

    module = ModuleType("test_moveit_provider")

    def build_moveit_model(**kwargs):
        """返回满足 OS 标准 Bundle 形状的模型。"""

        module.last_kwargs = kwargs
        mesh_path = Path(__file__).resolve()
        device_id = str(kwargs["device_id"])
        return SimpleNamespace(
            execution_urdf=(
                "<robot name='test'><link name='robot_device_link'><visual><geometry>"
                f"<mesh filename='{mesh_path.as_uri()}'/>"
                "</geometry></visual></link>"
                f"<link name='robot_tip'/><joint name='{device_id}_joint_1' type='revolute'>"
                "<parent link='robot_device_link'/><child link='robot_tip'/></joint>"
                "</robot>"
            ),
            render_urdf=(
                "<robot name='test'><link name='robot_device_link'><visual><geometry>"
                f"<mesh filename='{device_id}/meshes/{mesh_path.name}'/>"
                "</geometry></visual></link>"
                f"<link name='robot_tip'/><joint name='{device_id}_joint_1' type='revolute'>"
                "<parent link='robot_device_link'/><child link='robot_tip'/></joint>"
                "</robot>"
            ),
            srdf="<robot name='test'><group name='robot_arm'/></robot>",
            ros2_controllers={"controller_manager": {"ros__parameters": {}}},
            moveit_controllers={
                "moveit_controller_manager": "manager",
                "moveit_simple_controller_manager": {"controller_names": []},
            },
            kinematics={"robot_arm": {}},
            joint_limits={"joint_limits": {}},
            source_digest=_DIGEST,
            mesh_paths=(mesh_path,),
            qualified_joint_names=(f"{device_id}_joint_1",),
            topology_digest="b" * 64,
            rviz_required=rviz_required,
        )

    module.build_moveit_model = build_moveit_model
    return module


def test_package_model_provider_receives_only_instance_pose(monkeypatch) -> None:
    """OS 只向型号 Provider 传 Device 身份和安装位姿，不传业务点位。"""

    provider = _provider_module()
    monkeypatch.setitem(sys.modules, provider.__name__, provider)
    node = {
        "id": "robot",
        "position": {"x": 100, "y": 200, "z": 300},
        "config": {"rotation": {"x": 0, "y": 0, "z": 1.57}},
    }
    bundle = load_package_moveit_model(
        {
            "type": "package_moveit",
            "provider": "test_moveit_provider:build_moveit_model",
            "source_digest": _DIGEST,
        },
        node,
    )

    assert provider.last_kwargs == {
        "device_id": "robot",
        "position": node["position"],
        "rotation": node["config"]["rotation"],
    }
    assert bundle.source_digest == _DIGEST
    assert bundle.execution_urdf == bundle.urdf
    assert bundle.qualified_joint_names == ("robot_joint_1",)
    assert bundle.topology_digest == "b" * 64
    assert bundle.rviz_required is False


def test_apply_graph_world_mount_composes_rail_parent_pose() -> None:
    """机械臂相对导轨的毫米位姿必须合成回原来的世界安装。"""

    graph = {
        "rail": {
            "id": "rail",
            "parent": None,
            "position": {"x": -500, "y": 0, "z": 850},
        },
        "robot": {
            "id": "robot",
            "parent": "rail",
            "position": {
                "position": {"x": 681.301, "y": 0, "z": 150},
                "rotation": {"x": 0, "y": 0, "z": 180},
            },
            "config": {"rotation": {"x": 0.0, "y": 0.0, "z": 0.0}},
        },
    }

    mounted = apply_graph_world_mount(graph["robot"], graph)

    assert mounted["position"]["x"] == pytest.approx(181.301)
    assert mounted["position"]["y"] == pytest.approx(0.0)
    assert mounted["position"]["z"] == pytest.approx(1000.0)
    assert mounted["config"]["rotation"]["x"] == pytest.approx(0.0)
    assert mounted["config"]["rotation"]["y"] == pytest.approx(0.0)
    assert mounted["config"]["rotation"]["z"] == pytest.approx(math.pi)


def test_apply_graph_world_mount_follows_os_runtime_graph_dump() -> None:
    """canonicalize 之后 parent 变成 parent_uuid，MoveIt 安装仍要落在导轨世界位姿。"""

    import networkx as nx

    from unilabos.resources.graphio import canonicalize_nodes_data, dict_from_graph

    tree = canonicalize_nodes_data(
        [
            {
                "id": "rail",
                "name": "rail",
                "type": "device",
                "class": "community.ptlc_station.rail",
                "parent": None,
                "children": ["robot"],
                "position": {"x": -500, "y": 0, "z": 850},
                "config": {},
            },
            {
                "id": "robot",
                "name": "robot",
                "type": "device",
                "class": "community.ptlc_station.robot",
                "parent": "rail",
                "children": [],
                "position": {
                    "position": {"x": 681.301, "y": 0, "z": 150},
                    "rotation": {"x": 0, "y": 0, "z": 180},
                },
                "config": {"rotation": {"x": 0.0, "y": 0.0, "z": 0.0}},
            },
        ]
    )
    assert [node.res_content.id for node in tree.root_nodes] == ["rail"]
    assert [node.res_content.id for node in tree.device_nodes] == ["rail", "robot"]
    dumped = [node.res_content.model_dump(by_alias=True) for node in tree.all_nodes]
    robot_dump = next(node for node in dumped if node["id"] == "robot")
    assert robot_dump.get("parent") in (None, "")
    assert robot_dump["parent_uuid"]
    runtime = dict_from_graph(
        nx.node_link_graph({"nodes": dumped, "links": []}, edges="links", multigraph=False)
    )
    mounted = apply_graph_world_mount(runtime["robot"], runtime)
    assert mounted["position"]["x"] == pytest.approx(181.301)
    assert mounted["position"]["z"] == pytest.approx(1000.0)
    assert mounted["config"]["rotation"]["z"] == pytest.approx(math.pi)


def test_graph_freezes_two_same_model_instances_without_joint_cross_talk(
    monkeypatch,
) -> None:
    """Graph node.id 必须同时冻结关节归属、渲染模型和 mesh 资产。"""

    provider = _provider_module()
    monkeypatch.setitem(sys.modules, provider.__name__, provider)
    model = {
        "type": "package_moveit",
        "provider": "test_moveit_provider:build_moveit_model",
        "source_digest": _DIGEST,
    }
    owners = collect_package_joint_state_owners(
        {
            "robot_a": {
                "id": "robot_a",
                "type": "device",
                "class": "test_robot",
                "config": {"joint_state_telemetry": {"stale_after_s": 0.8}},
            },
            "robot_b": {
                "id": "robot_b",
                "type": "device",
                "class": "test_robot",
                "config": {},
            },
        },
        {"test_robot": {"model": model}},
    )

    assert [owner.device_id for owner in owners] == ["robot_a", "robot_b"]
    assert owners[0].qualified_joint_names == ("robot_a_joint_1",)
    assert owners[1].qualified_joint_names == ("robot_b_joint_1",)
    assert owners[0].stale_after_s == pytest.approx(0.8)
    assert get_package_render_model("robot_a").mesh_paths == (
        Path(__file__).resolve(),
    )
    assert get_package_render_mesh("robot_a", Path(__file__).name) == Path(
        __file__
    ).resolve()
    assert get_package_render_mesh("robot_a", "../test_package_moveit_model.py") is None
    assert get_package_render_mesh("robot_b", Path(__file__).name) == Path(
        __file__
    ).resolve()


def test_kinematic_render_api_serves_frozen_urdf_and_owned_mesh(monkeypatch) -> None:
    """本地 FE 只能读取本次启动冻结的 URDF 与该实例拥有的 mesh。"""

    from unilabos.app.web.api import (
        read_kinematic_mesh,
        read_kinematic_render_model,
    )

    provider = _provider_module()
    monkeypatch.setitem(sys.modules, provider.__name__, provider)
    collect_package_joint_state_owners(
        {
            "robot_a": {
                "id": "robot_a",
                "type": "device",
                "class": "test_robot",
                "config": {},
            }
        },
        {
            "test_robot": {
                "model": {
                    "type": "package_moveit",
                    "provider": "test_moveit_provider:build_moveit_model",
                    "source_digest": _DIGEST,
                }
            }
        },
    )

    urdf_response = read_kinematic_render_model("robot_a")
    mesh_response = read_kinematic_mesh("robot_a", Path(__file__).name)

    assert urdf_response.status_code == 200
    assert b"robot_a_joint_1" in urdf_response.body
    assert urdf_response.headers["x-unilab-device-id"] == "robot_a"
    assert mesh_response.status_code == 200
    assert Path(mesh_response.path) == Path(__file__).resolve()
    assert read_kinematic_render_model("missing").status_code == 404
    assert read_kinematic_mesh("robot_a", "../secret.stl").status_code == 404


def test_package_model_digest_drift_is_rejected(monkeypatch) -> None:
    """Catalog 模型摘要与 Provider 源资产不一致时必须关闭失败。"""

    provider = _provider_module()
    monkeypatch.setitem(sys.modules, provider.__name__, provider)

    with pytest.raises(ValueError, match="摘要漂移"):
        load_package_moveit_model(
            {
                "type": "package_moveit",
                "provider": "test_moveit_provider:build_moveit_model",
                "source_digest": "b" * 64,
            },
            {"id": "robot", "config": {}},
        )


def test_package_model_cannot_require_rviz(monkeypatch) -> None:
    """型号 Provider 不得把 RViz 提升为 MoveIt 执行依赖。"""

    provider = _provider_module(rviz_required=True)
    monkeypatch.setitem(sys.modules, provider.__name__, provider)

    with pytest.raises(ValueError, match="RViz"):
        load_package_moveit_model(
            {
                "type": "package_moveit",
                "provider": "test_moveit_provider:build_moveit_model",
                "source_digest": _DIGEST,
            },
            {"id": "robot", "config": {}},
        )


def test_package_model_parameters_merge_into_one_launch_owner(monkeypatch) -> None:
    """Provider 参数必须并入 OS 单一 controller/move_group 配置。"""

    provider = _provider_module()
    monkeypatch.setitem(sys.modules, provider.__name__, provider)
    bundle = load_package_moveit_model(
        {
            "type": "package_moveit",
            "provider": "test_moveit_provider:build_moveit_model",
            "source_digest": _DIGEST,
        },
        {"id": "robot", "config": {}},
    )
    ros2_controllers = {
        "controller_manager": {"ros__parameters": {"joint_state_broadcaster": {}}}
    }
    moveit_controllers = {
        "moveit_simple_controller_manager": {"controller_names": []}
    }
    kinematics = {}
    joint_limits = {"joint_limits": {}}

    merge_package_moveit_parameters(
        bundle,
        ros2_controllers=ros2_controllers,
        moveit_controllers=moveit_controllers,
        kinematics=kinematics,
        joint_limits=joint_limits,
    )

    assert "joint_state_broadcaster" in ros2_controllers["controller_manager"][
        "ros__parameters"
    ]
    assert "robot_arm" in kinematics


def test_package_model_exposes_exact_moveit_client_spec(monkeypatch) -> None:
    """Device 驱动创建客户端时必须复用 Bundle 的 group/chain/joint 身份。"""

    provider = _provider_module()
    monkeypatch.setitem(sys.modules, provider.__name__, provider)
    bundle = load_package_moveit_model(
        {
            "type": "package_moveit",
            "provider": "test_moveit_provider:build_moveit_model",
            "source_digest": _DIGEST,
        },
        {"id": "robot", "config": {}},
    )
    bundle = replace(
        bundle,
        srdf=(
            "<robot name='test'><group name='robot_arm'>"
            "<chain base_link='robot_base' tip_link='robot_tip'/>"
            "</group></robot>"
        ),
    )

    spec = package_moveit_client_spec(bundle)

    assert spec.group_name == "robot_arm"
    assert spec.base_link_name == "robot_base"
    assert spec.end_effector_name == "robot_tip"
    assert spec.joint_names == ("robot_joint_1",)

    captured = {}

    def client_factory(**kwargs):
        captured.update(kwargs)
        return "client"

    ros_node = SimpleNamespace(callback_group="device-callback-group")
    assert (
        create_package_moveit_client(
            ros_node,
            bundle,
            client_factory=client_factory,
        )
        == "client"
    )
    assert captured == {
        "node": ros_node,
        "joint_names": ["robot_joint_1"],
        "base_link_name": "robot_base",
        "end_effector_name": "robot_tip",
        "group_name": "robot_arm",
        "callback_group": "device-callback-group",
        "use_move_group_action": True,
        "ignore_new_calls_while_executing": True,
    }
