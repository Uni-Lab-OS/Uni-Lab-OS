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
from unilabos.device_mesh.package_static_model import (
    load_package_static_model,
    resolve_graph_local_pose,
    resolve_graph_world_pose,
)

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
    mount_link: str = ""


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


def apply_graph_local_mount(node: Mapping[str, Any]) -> dict[str, Any]:
    """把 Graph 相对 parent 的局部位姿写进型号 Provider 入参。

    参数：``node`` 是 Graph Device。返回：只覆盖 ``position``（毫米）和
    ``config.rotation``（弧度）的浅拷贝。安全：不把导轨轴并入机械臂型号。
    """

    xyz_m, rpy_rad = resolve_graph_local_pose(node)
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


def unique_moving_child_link(render_urdf: str) -> str:
    """若渲染 URDF 恰好一根可动轴，返回其 child link，供 Graph 子设备挂载。"""

    root = ET.fromstring(render_urdf)
    children: list[str] = []
    for joint in root.findall("joint"):
        if joint.attrib.get("type") == "fixed":
            continue
        child = joint.find("child")
        name = str((child.attrib if child is not None else {}).get("link") or "")
        if name:
            children.append(name)
    if len(children) != 1:
        return ""
    return children[0]


def parent_moving_mount_link(
    node: Mapping[str, Any],
    graph_nodes: Mapping[str, Mapping[str, Any]],
    registry_devices: Mapping[str, Any],
) -> str:
    """读取 Graph parent 独立关节模型的唯一运动 child link。

    参数：子设备节点、整张 Graph、Device Catalog。返回：可挂载 link 名；
    parent 没有恰好一根可动轴时返回空串。安全：不把该轴并入机械臂规划组。
    """

    parent_id = _graph_parent_member_id(node, graph_nodes)
    if not parent_id:
        return ""
    parent = graph_nodes[parent_id]
    definition = registry_devices.get(str(parent.get("class") or ""))
    if not isinstance(definition, Mapping):
        return ""
    model_config = definition.get("model")
    if not isinstance(model_config, Mapping):
        return ""
    if not str(model_config.get("joint_state_provider") or "").strip():
        return ""
    return unique_moving_child_link(
        load_joint_state_render_model(model_config, parent).render_urdf
    )


def namespace_link_names(root: Any, device_id: str) -> tuple[str, ...]:
    """返回 URDF 中属于该 Graph device_id 命名空间的 link。"""

    prefix = str(device_id).strip() + "_"
    if prefix == "_":
        return ()
    names: list[str] = []
    for link in root.findall("link"):
        name = str(link.attrib.get("name") or "")
        if name.startswith(prefix):
            names.append(name)
    return tuple(names)


def parent_mount_collision_exclusions(
    *,
    child_links: tuple[str, ...],
    parent_links: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    """机械臂螺栓在父级运动件上时，这对 link 永不互撞。"""

    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for child in child_links:
        child_name = str(child).strip()
        if not child_name:
            continue
        for parent in parent_links:
            parent_name = str(parent).strip()
            if not parent_name or parent_name == child_name:
                continue
            pair = (child_name, parent_name)
            if pair in seen:
                continue
            seen.add(pair)
            pairs.append(pair)
    return tuple(pairs)


def retarget_world_mount_parent(robot: Any, parent_link: str) -> None:
    """把执行 URDF 里唯一的 world 安装关节改挂到父级运动 link。"""

    target = str(parent_link).strip()
    if not target:
        raise ValueError("改挂 world 安装关节缺少父级运动 link")
    matches: list[Any] = []
    for joint in robot.findall("joint"):
        parent = joint.find("parent")
        if parent is None:
            continue
        link = str(parent.attrib.get("link") or parent.get("link") or "")
        if link == "world":
            matches.append(parent)
    if len(matches) != 1:
        raise ValueError("MoveIt 执行 URDF 必须有唯一 world 安装关节才能改挂")
    matches[0].set("link", target)


def overlay_package_static_visual(
    model_config: Mapping[str, Any],
    node: Mapping[str, Any],
    render_model: PackageRenderModel,
) -> PackageRenderModel:
    """把 package_static 外壳视觉挂到独立关节渲染 URDF 的根 link 上。

    FE 3D 只加载 ``/kinematic-models`` 这份 URDF；静态外壳仍留在
    ``robot_description`` 给 MoveIt 做碰撞。安全：不声明可动关节。
    """

    if get_ros_model_type(model_config) != "package_static":
        return render_model
    static = load_package_static_model(model_config, node)
    kinematic = ET.fromstring(render_model.render_urdf)
    base_name = _urdf_root_link(kinematic)
    base = next(
        (
            link
            for link in kinematic.findall("link")
            if str(link.attrib.get("name") or "") == base_name
        ),
        None,
    )
    if base is None:
        raise ValueError("关节渲染 URDF 缺少根 link")
    static_root = ET.fromstring(static.visual_urdf)
    static_link = next(
        (
            link
            for link in static_root.findall("link")
            if str(link.attrib.get("name") or "") == static.root_link
        ),
        None,
    )
    if static_link is None:
        raise ValueError("package_static 缺少 root_link")
    mesh_by_uri = {path.as_uri(): path for path in static.mesh_paths}
    for visual in static_link.findall("visual"):
        copied = ET.fromstring(ET.tostring(visual, encoding="unicode"))
        for mesh in copied.findall(".//mesh"):
            filename = str(mesh.attrib.get("filename") or "")
            path = mesh_by_uri.get(filename)
            if path is None:
                raise ValueError("package_static visual mesh 不在受管资产中")
            mesh.set("filename", f"{render_model.device_id}/meshes/{path.name}")
        base.append(copied)
    owned = {path.resolve() for path in render_model.mesh_paths}
    extra = tuple(path for path in static.mesh_paths if path.resolve() not in owned)
    return PackageRenderModel(
        device_id=render_model.device_id,
        render_urdf=ET.tostring(kinematic, encoding="unicode"),
        topology_digest=render_model.topology_digest,
        qualified_joint_names=render_model.qualified_joint_names,
        mesh_paths=render_model.mesh_paths + extra,
        mount_link=render_model.mount_link,
    )


def _graph_parent_member_id(
    node: Mapping[str, Any],
    graph_nodes: Mapping[str, Mapping[str, Any]],
) -> str:
    """按 Graph id 或 runtime uuid 解析 parent 成员。"""

    members_by_uuid = {
        str(member.get("uuid") or "").strip(): member_id
        for member_id, member in graph_nodes.items()
        if str(member.get("uuid") or "").strip()
    }
    for raw in (node.get("parent"), node.get("parent_uuid")):
        token = str(raw or "").strip()
        if not token:
            continue
        if token in graph_nodes:
            return token
        mapped = members_by_uuid.get(token)
        if mapped:
            return mapped
    return ""


graph_parent_member_id = _graph_parent_member_id


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
    ``/joint_states``，就使用同一投影出口。``package_moveit`` 与 Catalog
    ``joint_state_provider`` 都按 Graph ``device_id`` 完全限定关节名。
    无 exact 型号模型的设备不具备此能力，不做单设备或前缀回退。
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
        bundle: Any = None
        if get_ros_model_type(model_config) == "package_moveit":
            bundle = load_package_moveit_model(model_config, node)
        elif isinstance(model_config, Mapping) and str(
            model_config.get("joint_state_provider") or ""
        ).strip():
            bundle = load_joint_state_render_model(model_config, node)
        if bundle is None:
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
        render_model = PackageRenderModel(
            device_id=device_id,
            render_urdf=bundle.render_urdf,
            topology_digest=bundle.topology_digest,
            qualified_joint_names=bundle.qualified_joint_names,
            mesh_paths=tuple(getattr(bundle, "mesh_paths", ())),
            mount_link=unique_moving_child_link(str(bundle.render_urdf)),
        )
        if (
            isinstance(model_config, Mapping)
            and get_ros_model_type(model_config) == "package_static"
            and str(model_config.get("provider") or "").strip()
        ):
            render_model = overlay_package_static_visual(
                model_config, node, render_model
            )
        render_models[device_id] = render_model
    global _render_models
    _render_models = render_models
    return tuple(owners)


def load_joint_state_render_model(
    model_config: Mapping[str, Any],
    node: Mapping[str, Any],
) -> PackageRenderModel:
    """加载非 MoveIt 设备的 exact 关节渲染模型，命名规则与机械臂相同。

    参数：Catalog ``joint_state_provider`` 与 Graph 节点。返回：实例化渲染
    URDF 与完全限定关节名。异常：Provider 引用、关节名或拓扑摘要无效时拒绝。
    安全：不启动执行器，也不把导轨轴并入机械臂型号。
    """

    if not isinstance(model_config, Mapping) or not isinstance(node, Mapping):
        raise TypeError("关节渲染模型配置必须是 Mapping")
    provider_ref = str(model_config.get("joint_state_provider") or "")
    matched = _PROVIDER_REF.fullmatch(provider_ref)
    if matched is None:
        raise ValueError("joint_state_provider 必须是 module:symbol")
    provider = getattr(
        importlib.import_module(matched.group("module")),
        matched.group("symbol"),
        None,
    )
    if not callable(provider):
        raise TypeError("joint_state_provider 不可调用")
    device_id = str(node.get("id") or "")
    config = node.get("config")
    node_config = config if isinstance(config, Mapping) else {}
    bundle = provider(
        device_id=device_id,
        position=_mapping_or_empty(node.get("position")),
        rotation=_mapping_or_empty(node_config.get("rotation")),
    )
    qualified_joint_names = tuple(
        str(value) for value in getattr(bundle, "qualified_joint_names", ())
    )
    if not qualified_joint_names or len(set(qualified_joint_names)) != len(
        qualified_joint_names
    ):
        raise ValueError("joint_state_provider 必须声明不重复的 qualified_joint_names")
    expected_prefix = f"{device_id}_"
    if any(not name.startswith(expected_prefix) for name in qualified_joint_names):
        raise ValueError("关节名必须以 device_id 完全限定")
    topology_digest = str(getattr(bundle, "topology_digest", "")).lower()
    if _DIGEST.fullmatch(topology_digest) is None:
        raise ValueError("joint_state_provider topology_digest 必须是 SHA-256")
    render_urdf = str(getattr(bundle, "render_urdf", ""))
    _validate_robot_xml(render_urdf, label="render URDF")
    mesh_paths = tuple(
        Path(value).resolve() for value in getattr(bundle, "mesh_paths", ())
    )
    return PackageRenderModel(
        device_id=device_id,
        render_urdf=render_urdf,
        topology_digest=topology_digest,
        qualified_joint_names=qualified_joint_names,
        mesh_paths=mesh_paths,
        mount_link=unique_moving_child_link(render_urdf),
    )


def instantiate_joint_state_model(
    model_config: Mapping[str, Any],
    node: Mapping[str, Any],
    graph_nodes: Mapping[str, Mapping[str, Any]],
) -> str:
    """把非 MoveIt 关节模型按 Graph 位姿挂到 world，供 robot_state_publisher/RViz。

    参数：Catalog ``joint_state_provider``、Graph 节点和整张图。返回：带
    世界安装的 URDF 片段。异常：根 link 不唯一或位姿无效时拒绝。
    安全：不写入 MoveIt 规划组或 controller，也不把导轨轴并入机械臂型号。
    """

    bundle = load_joint_state_render_model(model_config, node)
    root = ET.fromstring(bundle.render_urdf)
    member_id = str(node.get("id") or "").strip()
    xyz_m, rpy_rad = resolve_graph_world_pose(node, graph_nodes)
    mount = ET.Element(
        "joint",
        {"name": f"{member_id}_kinematic_world_joint", "type": "fixed"},
    )
    ET.SubElement(
        mount,
        "origin",
        {"xyz": _format_urdf_vector(xyz_m), "rpy": _format_urdf_vector(rpy_rad)},
    )
    ET.SubElement(mount, "parent", {"link": "world"})
    ET.SubElement(mount, "child", {"link": _urdf_root_link(root)})
    root.insert(0, mount)
    return ET.tostring(root, encoding="unicode")


def _urdf_root_link(root: ET.Element) -> str:
    """返回未被任何 joint 当作 child 的唯一根 link。"""

    links = [str(link.attrib.get("name") or "") for link in root.findall(".//link")]
    children: set[str] = set()
    for joint in root.findall(".//joint"):
        child = joint.find("child")
        if child is None:
            continue
        name = str(child.attrib.get("link") or "")
        if name:
            children.add(name)
    roots = [name for name in links if name and name not in children]
    if len(roots) != 1:
        raise ValueError("joint_state_provider URDF 必须有唯一根 link")
    return roots[0]


def _format_urdf_vector(value: tuple[float, float, float]) -> str:
    """格式化 URDF 向量，并去掉负零。"""

    return " ".join("0" if part == 0 else format(part, ".12g") for part in value)


def get_package_render_model(device_id: str) -> PackageRenderModel | None:
    """按 Graph Device id 返回本启动代际的冻结渲染模型。"""

    return _render_models.get(str(device_id))


def overlay_material_graph_kinematics(graph: Mapping[str, Any]) -> dict[str, Any]:
    """把本启动代际的关节渲染合同叠到库存物料图上。

    参数：``graph`` 是库存权威的物料图。返回：不共享引用的新图；按
    ``meta_data.source_node_id`` 对齐 Graph device_id 后写入
    ``config.rendering.kinematics``，包括父设备唯一运动 child 的
    ``mount_link``。库存指纹不随渲染合同刷新，FE live-parent 必须读到
    当前滑座 link，不能把机械臂展平到导轨根坐标。
    """

    overlaid = copy.deepcopy(dict(graph))
    nodes = overlaid.get("nodes")
    if not isinstance(nodes, list):
        return overlaid
    for node in nodes:
        if not isinstance(node, dict):
            continue
        material = node.get("material")
        if not isinstance(material, dict):
            continue
        device_id = _material_source_device_id(material)
        model = get_package_render_model(device_id) if device_id else None
        if model is None:
            continue
        config = dict(material.get("config") or {})
        rendering = (
            dict(config["rendering"])
            if isinstance(config.get("rendering"), Mapping)
            else {}
        )
        kinematics = (
            dict(rendering["kinematics"])
            if isinstance(rendering.get("kinematics"), Mapping)
            else {}
        )
        kinematics["device_id"] = model.device_id
        kinematics["topology_digest"] = model.topology_digest
        kinematics["qualified_joint_names"] = list(model.qualified_joint_names)
        if model.mount_link:
            kinematics["mount_link"] = model.mount_link
        rendering["kinematics"] = kinematics
        config["rendering"] = rendering
        material["config"] = config
    return overlaid


def _material_source_device_id(material: Mapping[str, Any]) -> str:
    """从库存物料元数据或已有运动学快照读取 Graph device_id。"""

    meta = material.get("meta_data")
    if isinstance(meta, Mapping):
        source_id = str(meta.get("source_node_id") or "").strip()
        if source_id:
            return source_id
    config = material.get("config")
    if not isinstance(config, Mapping):
        return ""
    rendering = config.get("rendering")
    if not isinstance(rendering, Mapping):
        return ""
    kinematics = rendering.get("kinematics")
    if not isinstance(kinematics, Mapping):
        return ""
    return str(kinematics.get("device_id") or "").strip()


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
    "apply_graph_local_mount",
    "apply_graph_world_mount",
    "collect_package_joint_state_owners",
    "get_package_render_mesh",
    "get_package_render_model",
    "overlay_material_graph_kinematics",
    "get_ros_model_type",
    "instantiate_joint_state_model",
    "load_joint_state_render_model",
    "load_package_moveit_model",
    "merge_package_moveit_parameters",
    "overlay_package_static_visual",
    "namespace_link_names",
    "parent_mount_collision_exclusions",
    "parent_moving_mount_link",
    "graph_parent_member_id",
    "retarget_world_mount_parent",
    "unique_moving_child_link",
]
