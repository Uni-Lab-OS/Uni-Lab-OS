"""把软件包模型 Provider 适配为 OS 可组合的 MoveIt Bundle。"""

from __future__ import annotations

import copy
import importlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unilabos.device_mesh.joint_state_projector import JointStateOwner
from unilabos.device_mesh.package_static_model import resolve_graph_world_pose

_PROVIDER_REF = re.compile(
    r"^(?P<module>[A-Za-z_][A-Za-z0-9_.]*):(?P<symbol>[A-Za-z_][A-Za-z0-9_]*)$"
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class PackageMoveItModelBundle:
    """已验证且与 Provider 对象分离的 MoveIt 模型快照。"""

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
    rviz_required: bool

    @property
    def urdf(self) -> str:
        """保留现有 Launch 合并器使用的执行 URDF 别名。"""

        return self.execution_urdf


@dataclass(frozen=True, slots=True)
class PackageRenderModel:
    """供本地 FE 读取的实例化渲染 URDF 和拓扑身份。"""

    device_id: str
    render_urdf: str
    topology_digest: str
    qualified_joint_names: tuple[str, ...]
    mesh_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class PackageMoveItClientSpec:
    """从已验证 Bundle 派生的精确 MoveGroup 客户端身份。"""

    joint_names: tuple[str, ...]
    base_link_name: str
    end_effector_name: str
    group_name: str


_render_models: dict[str, PackageRenderModel] = {}


def get_ros_model_type(model_config: object) -> str | None:
    """返回仅由 ROS 可视化/运动层消费的显式模型类型。

    ``format/entry``、``shape`` 与 ``$ref`` 是工作区模型目录交给 FE 的声明，
    不属于旧式 ROS mesh 或 ``package_moveit`` 模型。它们没有 ``type`` 时返回
    ``None``，避免 ROS 组合器误解析，同时保留显式未知 ``type`` 供调用方报错。
    """

    if not isinstance(model_config, Mapping):
        return None
    model_type = model_config.get("type")
    return model_type if isinstance(model_type, str) else None


def apply_graph_world_mount(
    node: Mapping[str, Any],
    graph_nodes: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """把 Graph 父子位姿合成后的世界安装写进型号 Provider 入参。

    参数：``node`` 是 Graph Device；``graph_nodes`` 是整张 Graph。返回：只
    覆盖 ``position``（毫米）和 ``config.rotation``（弧度）的浅拷贝。型号包
    仍然只收世界安装，不创建导轨轴。
    """

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
    """调用 Catalog 声明的型号 Provider 并验证 exact 模型快照。

    参数：``model_config`` 是 Device Catalog 的 ``package_moveit`` 模型声明；
    ``node`` 是 Graph 实例。返回：深拷贝后的标准 Bundle。异常：Provider 引用、
    XML、参数映射或源摘要不一致时拒绝。安全：只传 Device 身份和安装位姿；不向
    Provider 传业务点位、物料、Site、硬件许可，也禁止 Provider 要求 RViz。
    """

    if not isinstance(model_config, Mapping) or model_config.get("type") != "package_moveit":
        raise TypeError("模型配置必须是 package_moveit Mapping")
    if not isinstance(node, Mapping):
        raise TypeError("Graph 节点必须是 Mapping")
    provider_ref = str(model_config.get("provider") or "")
    matched = _PROVIDER_REF.fullmatch(provider_ref)
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
    bundle = provider(
        device_id=str(node.get("id") or ""),
        position=_mapping_or_empty(node.get("position")),
        rotation=_mapping_or_empty(rotation),
    )
    source_digest = str(getattr(bundle, "source_digest", "")).lower()
    if source_digest != expected_digest:
        raise ValueError("package_moveit Provider 源资产摘要漂移")
    if bool(getattr(bundle, "rviz_required", False)):
        raise ValueError("package_moveit Provider 不得要求 RViz")

    execution_urdf = str(getattr(bundle, "execution_urdf", ""))
    render_urdf = str(getattr(bundle, "render_urdf", ""))
    srdf = str(getattr(bundle, "srdf", ""))
    _validate_robot_xml(execution_urdf, label="execution URDF")
    _validate_robot_xml(render_urdf, label="render URDF")
    _validate_robot_xml(srdf, label="SRDF")
    qualified_joint_names = tuple(
        str(value) for value in getattr(bundle, "qualified_joint_names", ())
    )
    if not qualified_joint_names or len(set(qualified_joint_names)) != len(
        qualified_joint_names
    ):
        raise ValueError("package_moveit 必须声明不重复的 qualified_joint_names")
    topology_digest = str(getattr(bundle, "topology_digest", "")).lower()
    if _DIGEST.fullmatch(topology_digest) is None:
        raise ValueError("package_moveit topology_digest 必须是 SHA-256")
    _validate_joint_ownership(
        execution_urdf,
        render_urdf,
        qualified_joint_names=qualified_joint_names,
    )
    mesh_paths = tuple(Path(value).resolve() for value in getattr(bundle, "mesh_paths", ()))
    if not mesh_paths or len({path.name for path in mesh_paths}) != len(mesh_paths):
        raise ValueError("package_moveit 必须声明名称不重复的 mesh_paths")
    missing_meshes = tuple(path.name for path in mesh_paths if not path.is_file())
    if missing_meshes:
        raise ValueError("package_moveit mesh 资产缺失: " + ", ".join(missing_meshes))
    _validate_render_mesh_uris(
        render_urdf,
        device_id=str(node.get("id") or ""),
        mesh_paths=mesh_paths,
    )
    return PackageMoveItModelBundle(
        execution_urdf=execution_urdf,
        render_urdf=render_urdf,
        srdf=srdf,
        ros2_controllers=_copy_mapping(bundle, "ros2_controllers"),
        moveit_controllers=_copy_mapping(bundle, "moveit_controllers"),
        kinematics=_copy_mapping(bundle, "kinematics"),
        joint_limits=_copy_mapping(bundle, "joint_limits"),
        source_digest=source_digest,
        mesh_paths=mesh_paths,
        qualified_joint_names=qualified_joint_names,
        topology_digest=topology_digest,
        rviz_required=False,
    )


def package_moveit_client_spec(
    bundle: PackageMoveItModelBundle,
) -> PackageMoveItClientSpec:
    """从 SRDF 的唯一 chain 和 Bundle 关节所有权生成客户端参数。

    Device 驱动不得重新拼接 group/base/tip 或关节前缀；否则同型号多实例时会
    把命令发往错误 MoveGroup。当前 Interface 有意只接受单 chain Arm。
    """

    root = ET.fromstring(bundle.srdf)
    candidates: list[tuple[str, str, str]] = []
    for group in root.findall("group"):
        chains = group.findall("chain")
        if len(chains) != 1:
            continue
        chain = chains[0]
        group_name = str(group.attrib.get("name", "")).strip()
        base_link = str(chain.attrib.get("base_link", "")).strip()
        tip_link = str(chain.attrib.get("tip_link", "")).strip()
        if group_name and base_link and tip_link:
            candidates.append((group_name, base_link, tip_link))
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
    """在现有 Device ROS 节点上创建与启动 Bundle 同源的 MoveIt2 客户端。

    该函数只创建 action/service 客户端，不启动 ``move_group``、controller、
    RViz 或 ROS context；这些生命周期仍由 ResourceVisualization 单一拥有。
    ``client_factory`` 仅作为测试注入点。
    """

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
    """从本次 Graph 和已注册 exact 型号包编译关节归属。

    该函数不依赖 MoveIt 是否启动：PLC/SDK 只要真实发布同名
    ``/joint_states``，就使用同一投影出口。无 exact 型号模型的设备
    不具备此能力，不做单设备或前缀回退。
    """

    owners: list[JointStateOwner] = []
    render_models: dict[str, PackageRenderModel] = {}
    for node in devices.values():
        if not isinstance(node, Mapping) or node.get("type") != "device":
            continue
        definition = registry_devices.get(str(node.get("class") or ""))
        if not isinstance(definition, Mapping):
            continue
        model_config = definition.get("model")
        if get_ros_model_type(model_config) != "package_moveit":
            continue
        bundle = load_package_moveit_model(model_config, node)
        config = node.get("config")
        node_config = config if isinstance(config, Mapping) else {}
        telemetry = node_config.get("joint_state_telemetry")
        telemetry_config = telemetry if isinstance(telemetry, Mapping) else {}
        owners.append(
            JointStateOwner(
                device_id=str(node.get("id") or ""),
                topology_digest=bundle.topology_digest,
                qualified_joint_names=bundle.qualified_joint_names,
                stale_after_s=float(telemetry_config.get("stale_after_s", 1.0)),
            )
        )
        device_id = str(node.get("id") or "")
        render_models[device_id] = PackageRenderModel(
            device_id=device_id,
            render_urdf=bundle.render_urdf,
            topology_digest=bundle.topology_digest,
            qualified_joint_names=bundle.qualified_joint_names,
            mesh_paths=bundle.mesh_paths,
        )
    global _render_models
    _render_models = render_models
    return tuple(owners)


def get_package_render_model(device_id: str) -> PackageRenderModel | None:
    """按 Graph Device id 返回本启动代际的冻结渲染模型。"""

    return _render_models.get(str(device_id))


def get_package_render_mesh(device_id: str, asset_name: str) -> Path | None:
    """只返回该实例型号快照显式拥有的 mesh，拒绝路径和跨实例猜测。"""

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
    """把一个已验证 Bundle 合入进程唯一 MoveIt Launch 参数。

    参数：``bundle`` 是已隔离模型；其余映射是 Launch owner 的可变聚合状态。
    返回：无，成功时原位加入完全限定的 controller/group/joint。异常：名称冲突
    或缺失参数时拒绝。安全：禁止后加入的 Device 覆盖已激活执行器配置。
    """

    controller_manager = bundle.ros2_controllers.get("controller_manager")
    if not isinstance(controller_manager, dict):
        raise TypeError("package_moveit 缺少 controller_manager")
    manager_parameters = controller_manager.get("ros__parameters")
    if not isinstance(manager_parameters, dict):
        raise TypeError("package_moveit 缺少 controller_manager.ros__parameters")
    local_manager = ros2_controllers["controller_manager"]["ros__parameters"]
    for controller_name, declaration in manager_parameters.items():
        if controller_name in local_manager:
            raise ValueError(f"MoveIt controller 名称冲突: {controller_name}")
        parameters = bundle.ros2_controllers.get(controller_name)
        if not isinstance(parameters, dict):
            raise TypeError(f"MoveIt controller 参数缺失: {controller_name}")
        local_manager[controller_name] = declaration
        ros2_controllers[controller_name] = parameters

    moveit_manager = bundle.moveit_controllers.get(
        "moveit_simple_controller_manager"
    )
    if not isinstance(moveit_manager, dict):
        raise TypeError("package_moveit 缺少 MoveIt controller manager")
    controller_names = moveit_manager.get("controller_names")
    if not isinstance(controller_names, list):
        raise TypeError("package_moveit controller_names 必须是列表")
    local_moveit_manager = moveit_controllers["moveit_simple_controller_manager"]
    for controller_name in controller_names:
        if controller_name in local_moveit_manager["controller_names"]:
            raise ValueError(f"MoveIt controller 名称冲突: {controller_name}")
        declaration = moveit_manager.get(controller_name)
        if not isinstance(declaration, dict):
            raise TypeError(f"MoveIt controller 声明缺失: {controller_name}")
        local_moveit_manager["controller_names"].append(controller_name)
        local_moveit_manager[controller_name] = declaration

    for group_name, parameters in bundle.kinematics.items():
        if group_name in kinematics:
            raise ValueError(f"MoveIt planning group 名称冲突: {group_name}")
        kinematics[group_name] = parameters
    package_joint_limits = bundle.joint_limits.get("joint_limits")
    if not isinstance(package_joint_limits, dict):
        raise TypeError("package_moveit 缺少 joint_limits")
    local_joint_limits = joint_limits["joint_limits"]
    overlap = set(local_joint_limits).intersection(package_joint_limits)
    if overlap:
        raise ValueError("MoveIt joint limit 名称冲突: " + ", ".join(sorted(overlap)))
    local_joint_limits.update(package_joint_limits)


def _mapping_or_empty(value: object) -> Mapping[str, Any]:
    """保留 Graph 安装位姿 Mapping，其他形状收敛为空映射。"""

    return value if isinstance(value, Mapping) else {}


def _copy_mapping(bundle: object, name: str) -> dict[str, Any]:
    """读取并隔离 Provider 的单个参数映射。"""

    value = getattr(bundle, name, None)
    if not isinstance(value, Mapping):
        raise TypeError(f"package_moveit Bundle 缺少 {name} Mapping")
    return copy.deepcopy(dict(value))


def _validate_robot_xml(value: str, *, label: str) -> None:
    """验证 URDF/SRDF 是非空 robot XML。"""

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
    """确认执行/渲染双视图对同一组限定关节名拥有一致拓扑。"""

    expected = set(qualified_joint_names)
    for label, value in (
        ("execution URDF", execution_urdf),
        ("render URDF", render_urdf),
    ):
        root = ET.fromstring(value)
        movable = {
            joint.attrib.get("name", "")
            for joint in root.findall("joint")
            if joint.attrib.get("type") != "fixed"
        }
        if movable != expected:
            raise ValueError(
                f"package_moveit {label} 与 qualified_joint_names 不一致"
            )


def _validate_render_mesh_uris(
    render_urdf: str,
    *,
    device_id: str,
    mesh_paths: tuple[Path, ...],
) -> None:
    """确认浏览器渲染 URDF 只引用本实例受管的相对 mesh URL。"""

    expected = {f"{device_id}/meshes/{path.name}" for path in mesh_paths}
    root = ET.fromstring(render_urdf)
    actual = {
        mesh.attrib.get("filename", "")
        for mesh in root.findall(".//mesh")
    }
    if actual != expected:
        raise ValueError("package_moveit render URDF mesh URL 与受管资产不一致")


__all__ = [
    "PackageMoveItModelBundle",
    "PackageRenderModel",
    "collect_package_joint_state_owners",
    "get_package_render_mesh",
    "get_package_render_model",
    "apply_graph_world_mount",
    "get_ros_model_type",
    "load_package_moveit_model",
    "merge_package_moveit_parameters",
]
