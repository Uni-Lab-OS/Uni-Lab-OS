"""把领域包（DomainPackage）模型 Provider 编译为 OS 运动模型双视图。"""

from __future__ import annotations

import copy
import importlib
import re
import threading
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from unilabos.device_mesh.graph_pose import (
    resolve_graph_parent_id,
    resolve_graph_world_pose,
)
from unilabos.device_mesh.joint_state_projector import JointStateOwner
from unilabos.device_mesh.package_joint_state_model import (
    compose_static_joint_state_render_model,
    has_joint_state_provider,
    instantiate_joint_state_model,
)

_PROVIDER_REF = re.compile(
    r"^(?P<module>[A-Za-z_][A-Za-z0-9_.]*):(?P<symbol>[A-Za-z_][A-Za-z0-9_]*)$"
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_DEVICE_ID = re.compile(r"^[A-Za-z0-9_]+$")


@dataclass(frozen=True, slots=True)
class PackageMoveItModelBundle:
    """已验证且与型号 Provider 对象隔离的运动模型快照。"""

    execution_urdf: str
    render_urdf: str
    srdf: str
    ros2_controllers: dict[str, Any]
    moveit_controllers: dict[str, Any]
    kinematics: dict[str, Any]
    joint_limits: dict[str, Any]
    source_digest: str
    mesh_paths: tuple[Path, ...]
    qualified_joint_names: tuple[str, ...]
    topology_digest: str

    @property
    def urdf(self) -> str:
        """保留启动组合器消费的执行 URDF 别名。"""

        return self.execution_urdf


@dataclass(frozen=True, slots=True)
class PackageRenderModel:
    """给前端场景运行时（SceneRuntime）的实例化渲染模型。"""

    device_id: str
    render_urdf: str
    topology_digest: str
    qualified_joint_names: tuple[str, ...]
    mesh_paths: tuple[Path, ...]
    mount_link: str | None = None


@dataclass(frozen=True, slots=True)
class PackageMoveItClientSpec:
    """从同一模型快照派生的精确 MoveGroup 客户端身份。"""

    joint_names: tuple[str, ...]
    base_link_name: str
    end_effector_name: str
    group_name: str


_catalog_lock = threading.RLock()
_render_models: dict[str, PackageRenderModel] = {}


def get_ros_model_type(model_config: object) -> str | None:
    """只返回 ROS 运动/可视化层拥有的显式模型类型。"""

    if not isinstance(model_config, Mapping):
        return None
    model_type = model_config.get("type")
    return model_type if isinstance(model_type, str) else None


def apply_graph_world_mount(
    node: Mapping[str, Any],
    graph_nodes: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """把物理图父子位姿合成后的世界安装写入 Provider 输入副本。"""

    xyz_m, rpy_rad = resolve_graph_world_pose(node, graph_nodes)
    mounted = dict(node)
    config = node.get("config")
    mounted["config"] = dict(config) if isinstance(config, Mapping) else {}
    mounted["position"] = {
        "x": xyz_m[0] * 1000.0,
        "y": xyz_m[1] * 1000.0,
        "z": xyz_m[2] * 1000.0,
    }
    mounted["config"]["rotation"] = {
        "x": rpy_rad[0],
        "y": rpy_rad[1],
        "z": rpy_rad[2],
    }
    return mounted


def load_package_moveit_model(
    model_config: Mapping[str, Any],
    node: Mapping[str, Any],
) -> PackageMoveItModelBundle:
    """调用领域包型号 Provider 并验证 exact 执行/渲染快照。

    Provider 只接收设备实例身份和世界安装位姿；物料（Material）、库位
    （Site）、硬件许可和执行权均不会穿过该接口。执行与渲染 URDF 必须拥有
    完全相同的可动关节集合和拓扑摘要。
    """

    if not isinstance(model_config, Mapping) or model_config.get("type") != "package_moveit":
        raise TypeError("模型配置必须是 package_moveit Mapping")
    if not isinstance(node, Mapping):
        raise TypeError("物理图节点必须是 Mapping")
    device_id = str(node.get("id") or "").strip()
    if _DEVICE_ID.fullmatch(device_id) is None:
        raise ValueError("package_moveit 设备 id 只能包含字母、数字与下划线")
    matched = _PROVIDER_REF.fullmatch(str(model_config.get("provider") or ""))
    if matched is None:
        raise ValueError("package_moveit provider 必须是 module:symbol")
    expected_digest = str(model_config.get("source_digest") or "").lower()
    if _DIGEST.fullmatch(expected_digest) is None:
        raise ValueError("package_moveit source_digest 必须是 SHA-256")
    provider = getattr(
        importlib.import_module(matched.group("module")),
        matched.group("symbol"),
        None,
    )
    if not callable(provider):
        raise TypeError("package_moveit provider 不可调用")

    config = node.get("config")
    node_config = config if isinstance(config, Mapping) else {}
    rotation = node_config.get("rotation")
    raw_bundle = provider(
        device_id=device_id,
        position=_mapping_or_empty(node.get("position")),
        rotation=_mapping_or_empty(rotation),
    )
    source_digest = str(getattr(raw_bundle, "source_digest", "")).lower()
    if source_digest != expected_digest:
        raise ValueError("package_moveit Provider 源资产摘要漂移")
    if bool(getattr(raw_bundle, "rviz_required", False)):
        raise ValueError("package_moveit Provider 不得要求 RViz")

    execution_urdf = str(getattr(raw_bundle, "execution_urdf", ""))
    render_urdf = str(getattr(raw_bundle, "render_urdf", ""))
    srdf = str(getattr(raw_bundle, "srdf", ""))
    _validate_robot_xml(execution_urdf, label="execution URDF")
    _validate_robot_xml(render_urdf, label="render URDF")
    _validate_robot_xml(srdf, label="SRDF")
    qualified_joint_names = tuple(
        str(value) for value in getattr(raw_bundle, "qualified_joint_names", ())
    )
    if not qualified_joint_names or len(set(qualified_joint_names)) != len(
        qualified_joint_names
    ):
        raise ValueError("package_moveit 必须声明不重复的 qualified_joint_names")
    topology_digest = str(getattr(raw_bundle, "topology_digest", "")).lower()
    if _DIGEST.fullmatch(topology_digest) is None:
        raise ValueError("package_moveit topology_digest 必须是 SHA-256")
    _validate_joint_ownership(
        execution_urdf,
        render_urdf,
        qualified_joint_names=qualified_joint_names,
    )
    mesh_paths = tuple(
        Path(value).resolve() for value in getattr(raw_bundle, "mesh_paths", ())
    )
    if not mesh_paths or len({path.name for path in mesh_paths}) != len(mesh_paths):
        raise ValueError("package_moveit 必须声明文件名不重复的 mesh_paths")
    missing = tuple(path.name for path in mesh_paths if not path.is_file())
    if missing:
        raise ValueError("package_moveit mesh 资产缺失: " + ", ".join(missing))
    _validate_render_mesh_uris(
        render_urdf,
        device_id=device_id,
        mesh_paths=mesh_paths,
    )
    return PackageMoveItModelBundle(
        execution_urdf=execution_urdf,
        render_urdf=render_urdf,
        srdf=srdf,
        ros2_controllers=_copy_mapping(raw_bundle, "ros2_controllers"),
        moveit_controllers=_copy_mapping(raw_bundle, "moveit_controllers"),
        kinematics=_copy_mapping(raw_bundle, "kinematics"),
        joint_limits=_copy_mapping(raw_bundle, "joint_limits"),
        source_digest=source_digest,
        mesh_paths=mesh_paths,
        qualified_joint_names=qualified_joint_names,
        topology_digest=topology_digest,
    )


def load_graph_package_moveit_model(
    model_config: Mapping[str, Any],
    node: Mapping[str, Any],
    graph_nodes: Mapping[str, Mapping[str, Any]],
) -> PackageMoveItModelBundle:
    """按物理图动态父 link 装配领域包 MoveIt 模型。

    参数：模型配置、当前设备节点及完整物理图。返回：无动态父级时沿用世界安装
    Bundle；父设备提供 ``mount_link`` 时，使用子设备局部位姿并把其唯一 world
    固定关节改挂到该 link。异常：父模型或安装关节歧义时关闭启动。安全：只改
    fixed 安装关系，不把父关节并入子机械臂规划组或动作（Action）接口。
    """

    parent_id = resolve_graph_parent_id(node, graph_nodes)
    parent_model = get_package_render_model(parent_id) if parent_id else None
    mount_link = getattr(parent_model, "mount_link", None)
    if not isinstance(mount_link, str) or not mount_link.strip():
        mount_link = str(node.get("_kinematic_parent_link") or "").strip()
    if parent_id and not mount_link:
        raise ValueError("package_moveit 物理图父设备缺少已编译 mount_link")
    if not parent_id:
        return load_package_moveit_model(
            model_config,
            apply_graph_world_mount(node, graph_nodes),
        )

    local_node = dict(node)
    local_node["parent"] = None
    local_node["parent_uuid"] = None
    device_id = str(node.get("id") or "").strip()
    local_mounted = apply_graph_world_mount(
        local_node,
        {device_id: local_node},
    )
    bundle = load_package_moveit_model(model_config, local_mounted)
    root = ET.fromstring(bundle.execution_urdf)
    candidates = []
    for joint in root.findall("joint"):
        parent = joint.find("parent")
        if (
            joint.attrib.get("type") == "fixed"
            and parent is not None
            and parent.attrib.get("link") == "world"
        ):
            candidates.append(parent)
    if len(candidates) != 1:
        raise ValueError("package_moveit 动态父级要求唯一 world 固定安装关节")
    candidates[0].set("link", mount_link)
    return replace(
        bundle,
        execution_urdf=ET.tostring(root, encoding="unicode"),
    )


def package_moveit_client_spec(
    bundle: PackageMoveItModelBundle,
) -> PackageMoveItClientSpec:
    """从 SRDF 唯一 chain 和关节归属生成客户端参数。"""

    root = ET.fromstring(bundle.srdf)
    candidates: list[tuple[str, str, str]] = []
    for group in root.findall("group"):
        chains = group.findall("chain")
        if len(chains) != 1:
            continue
        chain = chains[0]
        candidate = (
            str(group.attrib.get("name", "")).strip(),
            str(chain.attrib.get("base_link", "")).strip(),
            str(chain.attrib.get("tip_link", "")).strip(),
        )
        if all(candidate):
            candidates.append(candidate)
    if len(candidates) != 1:
        raise ValueError("package_moveit SRDF 必须且只能声明一个完整 chain group")
    group_name, base_link, tip_link = candidates[0]
    return PackageMoveItClientSpec(
        joint_names=bundle.qualified_joint_names,
        base_link_name=base_link,
        end_effector_name=tip_link,
        group_name=group_name,
    )


def create_package_moveit_client(
    ros_node: Any,
    bundle: PackageMoveItModelBundle,
    *,
    client_factory: Any = None,
) -> Any:
    """在既有设备 ROS 节点上创建同源 MoveIt 客户端，不拥有启动生命周期。"""

    if client_factory is None:
        from unilabos.devices.ros_dev.moveit2 import MoveIt2

        client_factory = MoveIt2
    spec = package_moveit_client_spec(bundle)
    return client_factory(
        node=ros_node,
        joint_names=list(spec.joint_names),
        base_link_name=spec.base_link_name,
        end_effector_name=spec.end_effector_name,
        group_name=spec.group_name,
        callback_group=getattr(ros_node, "callback_group", None),
        use_move_group_action=True,
        ignore_new_calls_while_executing=True,
    )


def collect_package_joint_state_owners(
    devices: Mapping[str, Any],
    registry_devices: Mapping[str, Any],
) -> tuple[JointStateOwner, ...]:
    """从本次物理图和注册表 exact 型号包编译关节归属与渲染目录。"""

    owners: list[JointStateOwner] = []
    render_models: dict[str, PackageRenderModel] = {}
    for node_id in sorted(devices):
        node = devices[node_id]
        if not isinstance(node, Mapping) or node.get("type") != "device":
            continue
        definition = registry_devices.get(str(node.get("class") or ""))
        if not isinstance(definition, Mapping):
            continue
        model_config = definition.get("model")
        model_type = get_ros_model_type(model_config)
        if model_type not in {"package_moveit", "package_static"}:
            continue
        if model_type == "package_moveit":
            mounted = apply_graph_world_mount(node, devices)
            bundle = load_package_moveit_model(model_config, mounted)
            render_urdf = bundle.render_urdf
            mesh_paths = bundle.mesh_paths
            mount_link = None
        elif has_joint_state_provider(model_config):
            bundle = compose_static_joint_state_render_model(model_config, node)
            render_urdf = bundle.render_urdf
            mesh_paths = bundle.mesh_paths
            mount_link = bundle.mount_link
        else:
            continue
        config = node.get("config")
        node_config = config if isinstance(config, Mapping) else {}
        telemetry = node_config.get("joint_state_telemetry")
        telemetry_config = telemetry if isinstance(telemetry, Mapping) else {}
        device_id = str(node.get("id") or "")
        owners.append(
            JointStateOwner(
                device_id=device_id,
                topology_digest=bundle.topology_digest,
                qualified_joint_names=bundle.qualified_joint_names,
                stale_after_s=float(telemetry_config.get("stale_after_s", 1.0)),
            )
        )
        render_models[device_id] = PackageRenderModel(
            device_id=device_id,
            render_urdf=render_urdf,
            topology_digest=bundle.topology_digest,
            qualified_joint_names=bundle.qualified_joint_names,
            mesh_paths=mesh_paths,
            mount_link=mount_link,
        )
    with _catalog_lock:
        global _render_models
        _render_models = render_models
    return tuple(owners)


def get_package_render_model(device_id: str) -> PackageRenderModel | None:
    """按设备 id 返回本进程启动代际的冻结渲染模型。"""

    with _catalog_lock:
        return _render_models.get(str(device_id))


def get_package_render_mesh(device_id: str, asset_name: str) -> Path | None:
    """只返回型号快照显式拥有的 mesh，拒绝路径与跨实例猜测。"""

    model = get_package_render_model(device_id)
    if model is None or Path(asset_name).name != asset_name:
        return None
    return next((path for path in model.mesh_paths if path.name == asset_name), None)


def merge_package_moveit_parameters(
    bundle: PackageMoveItModelBundle,
    *,
    ros2_controllers: dict[str, Any],
    moveit_controllers: dict[str, Any],
    kinematics: dict[str, Any],
    joint_limits: dict[str, Any],
) -> None:
    """把已验证 Bundle 合入进程唯一 MoveIt 启动参数，名称冲突即拒绝。"""

    controller_manager = bundle.ros2_controllers.get("controller_manager")
    manager_parameters = (
        controller_manager.get("ros__parameters")
        if isinstance(controller_manager, Mapping)
        else None
    )
    if not isinstance(manager_parameters, Mapping):
        raise TypeError("package_moveit 缺少 controller_manager.ros__parameters")
    local_manager = ros2_controllers["controller_manager"]["ros__parameters"]
    for controller_name, declaration in manager_parameters.items():
        if controller_name in local_manager:
            raise ValueError(f"MoveIt controller 名称冲突: {controller_name}")
        parameters = bundle.ros2_controllers.get(controller_name)
        if not isinstance(parameters, Mapping):
            raise TypeError(f"MoveIt controller 参数缺失: {controller_name}")
        local_manager[controller_name] = copy.deepcopy(declaration)
        ros2_controllers[controller_name] = copy.deepcopy(dict(parameters))

    package_moveit = bundle.moveit_controllers.get(
        "moveit_simple_controller_manager"
    )
    if not isinstance(package_moveit, Mapping):
        raise TypeError("package_moveit 缺少 MoveIt controller manager")
    names = package_moveit.get("controller_names")
    if not isinstance(names, list):
        raise TypeError("package_moveit controller_names 必须是列表")
    local_moveit = moveit_controllers["moveit_simple_controller_manager"]
    for controller_name in names:
        if controller_name in local_moveit["controller_names"]:
            raise ValueError(f"MoveIt controller 名称冲突: {controller_name}")
        declaration = package_moveit.get(controller_name)
        if not isinstance(declaration, Mapping):
            raise TypeError(f"MoveIt controller 声明缺失: {controller_name}")
        local_moveit["controller_names"].append(controller_name)
        local_moveit[controller_name] = copy.deepcopy(dict(declaration))

    for group_name, parameters in bundle.kinematics.items():
        if group_name in kinematics:
            raise ValueError(f"MoveIt planning group 名称冲突: {group_name}")
        kinematics[group_name] = copy.deepcopy(parameters)
    package_limits = bundle.joint_limits.get("joint_limits")
    if not isinstance(package_limits, Mapping):
        raise TypeError("package_moveit 缺少 joint_limits")
    local_limits = joint_limits["joint_limits"]
    overlap = set(local_limits).intersection(package_limits)
    if overlap:
        raise ValueError("MoveIt joint limit 名称冲突: " + ", ".join(sorted(overlap)))
    local_limits.update(copy.deepcopy(dict(package_limits)))


def _mapping_or_empty(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _copy_mapping(bundle: object, name: str) -> dict[str, Any]:
    value = getattr(bundle, name, None)
    if not isinstance(value, Mapping):
        raise TypeError(f"package_moveit Bundle 缺少 {name} Mapping")
    return copy.deepcopy(dict(value))


def _validate_robot_xml(value: str, *, label: str) -> None:
    try:
        root = ET.fromstring(value)
    except ET.ParseError as error:
        raise ValueError(f"package_moveit {label} 不是合法 XML") from error
    if root.tag != "robot":
        raise ValueError(f"package_moveit {label} 根节点必须是 robot")


def _validate_joint_ownership(
    execution_urdf: str,
    render_urdf: str,
    *,
    qualified_joint_names: tuple[str, ...],
) -> None:
    expected = set(qualified_joint_names)
    for label, value in (("execution URDF", execution_urdf), ("render URDF", render_urdf)):
        root = ET.fromstring(value)
        movable = {
            str(joint.attrib.get("name") or "")
            for joint in root.findall("joint")
            if joint.attrib.get("type") != "fixed"
        }
        if movable != expected:
            raise ValueError(f"package_moveit {label} 与 qualified_joint_names 不一致")


def _validate_render_mesh_uris(
    render_urdf: str,
    *,
    device_id: str,
    mesh_paths: tuple[Path, ...],
) -> None:
    expected = {f"{device_id}/meshes/{path.name}" for path in mesh_paths}
    root = ET.fromstring(render_urdf)
    actual = {str(mesh.attrib.get("filename") or "") for mesh in root.findall(".//mesh")}
    if actual != expected:
        raise ValueError("package_moveit render URDF mesh URL 与受管资产不一致")


__all__ = [
    "PackageMoveItClientSpec",
    "PackageMoveItModelBundle",
    "PackageRenderModel",
    "apply_graph_world_mount",
    "collect_package_joint_state_owners",
    "create_package_moveit_client",
    "get_package_render_mesh",
    "get_package_render_model",
    "instantiate_joint_state_model",
    "get_ros_model_type",
    "load_package_moveit_model",
    "load_graph_package_moveit_model",
    "merge_package_moveit_parameters",
    "package_moveit_client_spec",
]
