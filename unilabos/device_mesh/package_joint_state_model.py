"""把静态设备外壳与独立关节状态 Provider 编译为运动学模型。"""

from __future__ import annotations

import copy
import importlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unilabos.device_mesh.graph_pose import resolve_graph_world_pose
from unilabos.device_mesh.package_static_model import load_package_static_model

_PROVIDER_REF = re.compile(
    r"^(?P<module>[A-Za-z_][A-Za-z0-9_.]*):(?P<symbol>[A-Za-z_][A-Za-z0-9_]*)$"
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_DEVICE_ID = re.compile(r"^[A-Za-z0-9_]+$")


@dataclass(frozen=True, slots=True)
class PackageJointStateModelBundle:
    """已验证的单设备关节拓扑和前端渲染模型。"""

    render_urdf: str
    source_digest: str
    qualified_joint_names: tuple[str, ...]
    topology_digest: str
    mesh_paths: tuple[Path, ...]
    mount_link: str


def has_joint_state_provider(model_config: object) -> bool:
    """判断模型是否显式声明独立关节 Provider；不按模型类型猜测。"""

    return isinstance(model_config, Mapping) and bool(
        str(model_config.get("joint_state_provider") or "").strip()
    )


def load_package_joint_state_model(
    model_config: Mapping[str, Any],
    node: Mapping[str, Any],
) -> PackageJointStateModelBundle:
    """调用并验证领域包（DomainPackage）的独立关节模型 Provider。

    参数：``model_config`` 必须同时锁定 Provider 和源摘要，``node`` 提供实例
    身份与本地位姿。返回：冻结关节拓扑。异常：摘要、命名、XML 或安装 link
    漂移时关闭启动。安全：该接口不加载控制器，也不授予动作（Action）执行权。
    """

    if not isinstance(model_config, Mapping) or not has_joint_state_provider(
        model_config
    ):
        raise TypeError("模型配置必须声明 joint_state_provider")
    if not isinstance(node, Mapping):
        raise TypeError("关节模型物理图节点必须是 Mapping")
    device_id = str(node.get("id") or "").strip()
    if _DEVICE_ID.fullmatch(device_id) is None:
        raise ValueError("关节模型设备 id 只能包含字母、数字与下划线")
    matched = _PROVIDER_REF.fullmatch(
        str(model_config.get("joint_state_provider") or "")
    )
    if matched is None:
        raise ValueError("joint_state_provider 必须是 module:symbol")
    expected_digest = str(
        model_config.get("joint_state_source_digest") or ""
    ).lower()
    if _DIGEST.fullmatch(expected_digest) is None:
        raise ValueError("joint_state_source_digest 必须是 SHA-256")
    provider = getattr(
        importlib.import_module(matched.group("module")),
        matched.group("symbol"),
        None,
    )
    if not callable(provider):
        raise TypeError("joint_state_provider 不可调用")

    config = node.get("config")
    node_config = config if isinstance(config, Mapping) else {}
    raw_bundle = provider(
        device_id=device_id,
        position=_mapping_or_empty(node.get("position")),
        rotation=_mapping_or_empty(node_config.get("rotation")),
    )
    source_digest = str(getattr(raw_bundle, "source_digest", "")).lower()
    if source_digest != expected_digest:
        raise ValueError("joint_state_provider 源资产摘要漂移")
    render_urdf = str(getattr(raw_bundle, "render_urdf", ""))
    root = _robot_xml(render_urdf)
    qualified_joint_names = tuple(
        str(value)
        for value in getattr(raw_bundle, "qualified_joint_names", ())
    )
    if not qualified_joint_names or len(set(qualified_joint_names)) != len(
        qualified_joint_names
    ):
        raise ValueError("joint_state_provider 必须声明不重复的关节名")
    prefix = f"{device_id}_"
    if any(not name.startswith(prefix) for name in qualified_joint_names):
        raise ValueError("joint_state_provider 关节名必须由 device_id 完全限定")
    movable = {
        str(joint.attrib.get("name") or "")
        for joint in root.findall("joint")
        if joint.attrib.get("type") != "fixed"
    }
    if movable != set(qualified_joint_names):
        raise ValueError("joint_state_provider URDF 与关节归属不一致")
    topology_digest = str(getattr(raw_bundle, "topology_digest", "")).lower()
    if _DIGEST.fullmatch(topology_digest) is None:
        raise ValueError("joint_state_provider topology_digest 必须是 SHA-256")
    mesh_paths = tuple(
        Path(value).resolve() for value in getattr(raw_bundle, "mesh_paths", ())
    )
    if len(set(mesh_paths)) != len(mesh_paths):
        raise ValueError("joint_state_provider mesh_paths 必须唯一")
    missing = tuple(path.name for path in mesh_paths if not path.is_file())
    if missing:
        raise ValueError("joint_state_provider mesh 资产缺失: " + ", ".join(missing))
    mount_link = str(getattr(raw_bundle, "mount_link", "")).strip()
    links = {str(link.attrib.get("name") or "") for link in root.findall("link")}
    if not mount_link or mount_link not in links:
        raise ValueError("joint_state_provider mount_link 必须引用本模型 link")
    return PackageJointStateModelBundle(
        render_urdf=render_urdf,
        source_digest=source_digest,
        qualified_joint_names=qualified_joint_names,
        topology_digest=topology_digest,
        mesh_paths=mesh_paths,
        mount_link=mount_link,
    )


def compose_static_joint_state_render_model(
    model_config: Mapping[str, Any],
    node: Mapping[str, Any],
) -> PackageJointStateModelBundle:
    """把静态外壳的视觉/碰撞几何并入关节模型根 link。

    参数：同一注册表模型配置和实例节点。返回：一棵供前端连续运动的 URDF，
    同时保留静态外壳资产。异常：任一 Provider 失败即关闭启动。安全：只合成
    渲染/碰撞数据，不把静态外壳变成执行关节。
    """

    static = load_package_static_model(model_config, node)
    joint = load_package_joint_state_model(model_config, node)
    root = _robot_xml(joint.render_urdf)
    static_root = _robot_xml(static.visual_urdf)
    kinematic_root_link = _unique_root_link(root)
    destination = next(
        link
        for link in root.findall("link")
        if link.attrib.get("name") == kinematic_root_link
    )
    source = next(
        link
        for link in static_root.findall("link")
        if link.attrib.get("name") == static.root_link
    )
    for child in source:
        destination.append(copy.deepcopy(child))
    mesh_paths = tuple(dict.fromkeys((*static.mesh_paths, *joint.mesh_paths)))
    if len({path.name for path in mesh_paths}) != len(mesh_paths):
        raise ValueError("关节渲染模型 mesh 文件名必须唯一")
    owned_by_uri = {path.as_uri(): path for path in mesh_paths}
    for mesh in root.findall(".//mesh"):
        filename = str(mesh.attrib.get("filename") or "")
        owned = owned_by_uri.get(filename)
        if owned is not None:
            mesh.set("filename", f"{node['id']}/meshes/{owned.name}")
    expected_uris = {f"{node['id']}/meshes/{path.name}" for path in mesh_paths}
    actual_uris = {
        str(mesh.attrib.get("filename") or "") for mesh in root.findall(".//mesh")
    }
    if actual_uris != expected_uris:
        raise ValueError("关节渲染模型 mesh URL 与受管资产不一致")
    return PackageJointStateModelBundle(
        render_urdf=ET.tostring(root, encoding="unicode"),
        source_digest=joint.source_digest,
        qualified_joint_names=joint.qualified_joint_names,
        topology_digest=joint.topology_digest,
        mesh_paths=mesh_paths,
        mount_link=joint.mount_link,
    )


def instantiate_joint_state_model(
    model_config: Mapping[str, Any],
    node: Mapping[str, Any],
    graph_nodes: Mapping[str, Mapping[str, Any]],
) -> str:
    """把独立关节 URDF 按物理图世界位姿固定挂到 ``world``。

    参数：模型配置、当前节点和完整物理图。返回：可并入 ROS
    ``robot_description`` 的 XML 片段。异常：父链或关节模型漂移时关闭启动。
    安全：仅增加 fixed 世界安装，不生成 ros2_control 或控制器。
    """

    bundle = load_package_joint_state_model(model_config, node)
    root = _robot_xml(bundle.render_urdf)
    device_id = str(node.get("id") or "").strip()
    xyz_m, rpy_rad = resolve_graph_world_pose(node, graph_nodes)
    mount = ET.Element(
        "joint",
        {"name": f"{device_id}_kinematic_world_joint", "type": "fixed"},
    )
    ET.SubElement(
        mount,
        "origin",
        {"xyz": _format_vector(xyz_m), "rpy": _format_vector(rpy_rad)},
    )
    ET.SubElement(mount, "parent", {"link": "world"})
    ET.SubElement(mount, "child", {"link": _unique_root_link(root)})
    root.insert(0, mount)
    return ET.tostring(root, encoding="unicode")


def _robot_xml(value: str) -> ET.Element:
    """解析并要求根节点是 ``robot``；返回可安全合成的 XML 树。"""

    try:
        root = ET.fromstring(value)
    except ET.ParseError as error:
        raise ValueError("joint_state_provider URDF 不是合法 XML") from error
    if root.tag != "robot":
        raise ValueError("joint_state_provider URDF 根节点必须是 robot")
    if root.find(".//ros2_control") is not None:
        raise ValueError("joint_state_provider 不得声明 ros2_control")
    return root


def _unique_root_link(root: ET.Element) -> str:
    """返回 URDF 唯一根 link；多根或无根均拒绝。"""

    links = {str(link.attrib.get("name") or "") for link in root.findall("link")}
    children = {
        str(child.attrib.get("link") or "")
        for joint in root.findall("joint")
        for child in [joint.find("child")]
        if child is not None
    }
    roots = links - children
    if len(roots) != 1:
        raise ValueError("joint_state_provider URDF 必须只有一个根 link")
    return next(iter(roots))


def _mapping_or_empty(value: object) -> Mapping[str, Any]:
    """把可选对象规范为只读 Mapping；非对象按空配置处理。"""

    return value if isinstance(value, Mapping) else {}


def _format_vector(value: tuple[float, float, float]) -> str:
    """把三维向量格式化为稳定 URDF 文本。"""

    return " ".join("0" if part == 0 else format(part, ".12g") for part in value)


__all__ = [
    "PackageJointStateModelBundle",
    "compose_static_joint_state_render_model",
    "has_joint_state_provider",
    "instantiate_joint_state_model",
    "load_package_joint_state_model",
]
