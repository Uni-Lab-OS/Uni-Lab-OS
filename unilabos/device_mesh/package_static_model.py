"""把领域包（DomainPackage）静态模型编译为 world 下的只读碰撞树。"""

from __future__ import annotations

import importlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unilabos.device_mesh.graph_pose import resolve_graph_world_pose

_PROVIDER_REF = re.compile(
    r"^(?P<module>[A-Za-z_][A-Za-z0-9_.]*):(?P<symbol>[A-Za-z_][A-Za-z0-9_]*)$"
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MEMBER_ID = re.compile(r"^[A-Za-z0-9_]+$")
_FORBIDDEN_TAGS = ("transmission", "ros2_control", "gazebo")


@dataclass(frozen=True, slots=True)
class PackageStaticModelBundle:
    """已验证、带碰撞且没有执行权的静态模型快照。"""

    visual_urdf: str
    root_link: str
    source_digest: str
    mesh_paths: tuple[Path, ...]


def load_package_static_model(
    model_config: Mapping[str, Any],
    node: Mapping[str, Any],
) -> PackageStaticModelBundle:
    """调用领域包 Provider 并验证静态模型的 exact 资产与只读边界。"""

    if not isinstance(model_config, Mapping) or model_config.get("type") != "package_static":
        raise TypeError("静态模型配置必须是 package_static Mapping")
    if not isinstance(node, Mapping):
        raise TypeError("package_static 物理图节点必须是 Mapping")
    member_id = str(node.get("id") or "").strip()
    if _MEMBER_ID.fullmatch(member_id) is None:
        raise ValueError("package_static 成员 id 只能包含字母、数字与下划线")
    matched = _PROVIDER_REF.fullmatch(str(model_config.get("provider") or ""))
    if matched is None:
        raise ValueError("package_static provider 必须是 module:symbol")
    expected_digest = str(model_config.get("source_digest") or "").lower()
    if _DIGEST.fullmatch(expected_digest) is None:
        raise ValueError("package_static source_digest 必须是 SHA-256")
    provider = getattr(
        importlib.import_module(matched.group("module")),
        matched.group("symbol"),
        None,
    )
    if not callable(provider):
        raise TypeError("package_static provider 不可调用")

    raw_bundle = provider(member_id=member_id)
    source_digest = str(getattr(raw_bundle, "source_digest", "")).lower()
    if source_digest != expected_digest:
        raise ValueError("package_static Provider 源资产摘要漂移")
    visual_urdf = str(getattr(raw_bundle, "visual_urdf", ""))
    root_link = str(getattr(raw_bundle, "root_link", ""))
    mesh_paths = tuple(
        Path(value).resolve() for value in getattr(raw_bundle, "mesh_paths", ())
    )
    _validate_visual_urdf(
        visual_urdf,
        member_id=member_id,
        root_link=root_link,
        mesh_paths=mesh_paths,
    )
    return PackageStaticModelBundle(
        visual_urdf=visual_urdf,
        root_link=root_link,
        source_digest=source_digest,
        mesh_paths=mesh_paths,
    )


def instantiate_package_static_model(
    model_config: Mapping[str, Any],
    node: Mapping[str, Any],
    graph_nodes: Mapping[str, Mapping[str, Any]],
) -> str:
    """把冻结物理图世界位姿写成静态根节点到 ``world`` 的 fixed joint。"""

    bundle = load_package_static_model(model_config, node)
    root = ET.fromstring(bundle.visual_urdf)
    member_id = str(node.get("id") or "").strip()
    xyz_m, rpy_rad = resolve_graph_world_pose(node, graph_nodes)
    mount = ET.Element(
        "joint",
        {"name": f"{member_id}_layout_world_joint", "type": "fixed"},
    )
    ET.SubElement(
        mount,
        "origin",
        {"xyz": _format_vector(xyz_m), "rpy": _format_vector(rpy_rad)},
    )
    ET.SubElement(mount, "parent", {"link": "world"})
    ET.SubElement(mount, "child", {"link": bundle.root_link})
    root.insert(0, mount)
    return ET.tostring(root, encoding="unicode")


def _validate_visual_urdf(
    visual_urdf: str,
    *,
    member_id: str,
    root_link: str,
    mesh_paths: tuple[Path, ...],
) -> None:
    """拒绝执行内容，并要求可供规划使用的完整静态碰撞树。"""

    try:
        root = ET.fromstring(visual_urdf)
    except ET.ParseError as error:
        raise ValueError("package_static URDF 不是合法 XML") from error
    if root.tag != "robot":
        raise ValueError("package_static URDF 根节点必须是 robot")
    if any(root.find(f".//{tag}") is not None for tag in _FORBIDDEN_TAGS):
        raise ValueError("package_static 不得声明执行或控制内容")
    if root.find(".//collision") is None:
        raise ValueError("package_static URDF 必须声明 collision geometry")
    links = [str(link.attrib.get("name") or "") for link in root.findall("link")]
    joints = list(root.findall("joint"))
    joint_names = [str(joint.attrib.get("name") or "") for joint in joints]
    prefix = member_id + "_"
    if not links or len(set(links)) != len(links) or root_link not in links:
        raise ValueError("package_static links 与 root_link 必须存在且唯一")
    if any(not name.startswith(prefix) for name in (*links, *joint_names)):
        raise ValueError("package_static link/joint 必须属于成员命名空间")
    if len(set(joint_names)) != len(joint_names):
        raise ValueError("package_static joint 名必须唯一")

    link_set = set(links)
    child_links: set[str] = set()
    for joint in joints:
        if joint.attrib.get("type") != "fixed":
            raise ValueError("package_static 不得声明可动关节")
        parent = joint.find("parent")
        child = joint.find("child")
        parent_link = str((parent.attrib if parent is not None else {}).get("link") or "")
        child_link = str((child.attrib if child is not None else {}).get("link") or "")
        if parent_link not in link_set or child_link not in link_set:
            raise ValueError("package_static Provider 不得挂到局部树以外")
        child_links.add(child_link)
    if link_set - child_links != {root_link}:
        raise ValueError("package_static URDF 必须只有一个声明的根 link")
    if len(set(mesh_paths)) != len(mesh_paths):
        raise ValueError("package_static mesh_paths 必须唯一")
    missing = [path.name for path in mesh_paths if not path.is_file()]
    if missing:
        raise ValueError("package_static mesh 资产缺失: " + ", ".join(missing))
    actual = {str(mesh.attrib.get("filename") or "") for mesh in root.findall(".//mesh")}
    if actual != {path.as_uri() for path in mesh_paths}:
        raise ValueError("package_static URDF mesh URL 与受管资产不一致")


def _format_vector(value: tuple[float, float, float]) -> str:
    return " ".join("0" if part == 0 else format(part, ".12g") for part in value)


__all__ = [
    "PackageStaticModelBundle",
    "instantiate_package_static_model",
    "load_package_static_model",
    "resolve_graph_world_pose",
]
