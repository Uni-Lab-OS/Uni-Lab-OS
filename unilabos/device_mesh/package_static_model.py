"""Load package-owned static world models into the MoveIt robot description."""

from __future__ import annotations

import importlib
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PROVIDER_REF = re.compile(
    r"^(?P<module>[A-Za-z_][A-Za-z0-9_.]*):(?P<symbol>[A-Za-z_][A-Za-z0-9_]*)$"
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MEMBER_ID = re.compile(r"^[A-Za-z0-9_]+$")
_FORBIDDEN_TAGS = ("transmission", "ros2_control", "gazebo")


@dataclass(frozen=True, slots=True)
class PackageStaticModelBundle:
    """A verified static world model with collision, and no execution authority."""

    visual_urdf: str
    root_link: str
    source_digest: str
    mesh_paths: tuple[Path, ...]


def load_package_static_model(
    model_config: Mapping[str, Any],
    node: Mapping[str, Any],
) -> PackageStaticModelBundle:
    """Load and validate one package-owned static world model.

    The provider receives only the stable Graph member id. Installation pose
    remains owned by the Graph/WorkCell composition and is applied by OS.
    Collision geometry is required so MoveIt plans against these links.
    Control and movable-joint declarations are rejected.
    """

    if not isinstance(model_config, Mapping) or model_config.get("type") != "package_static":
        raise TypeError("static model config must be a package_static Mapping")
    if not isinstance(node, Mapping):
        raise TypeError("static model Graph node must be a Mapping")
    member_id = str(node.get("id") or "").strip()
    if _MEMBER_ID.fullmatch(member_id) is None:
        raise ValueError(
            "package_static member id may contain only letters, digits and underscores"
        )

    provider_ref = str(model_config.get("provider") or "")
    matched = _PROVIDER_REF.fullmatch(provider_ref)
    if matched is None:
        raise ValueError("package_static provider must use module:symbol")
    expected_digest = str(model_config.get("source_digest") or "").lower()
    if _DIGEST.fullmatch(expected_digest) is None:
        raise ValueError("package_static source_digest must be SHA-256")
    provider = getattr(
        importlib.import_module(matched.group("module")),
        matched.group("symbol"),
        None,
    )
    if not callable(provider):
        raise TypeError("package_static provider is not callable")

    raw_bundle = provider(member_id=member_id)
    source_digest = str(getattr(raw_bundle, "source_digest", "")).lower()
    if source_digest != expected_digest:
        raise ValueError("package_static provider source digest drifted")
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
    """Create a world-mounted static URDF from the frozen Graph pose."""

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


def resolve_graph_world_pose(
    node: Mapping[str, Any],
    graph_nodes: Mapping[str, Mapping[str, Any]],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Resolve the Issue #183 right-handed Z-up parent/child pose contract."""

    if any(not isinstance(candidate, Mapping) for candidate in graph_nodes.values()):
        raise TypeError("Graph nodes must contain only Mapping values")
    members: dict[str, Mapping[str, Any]] = {}
    for candidate in graph_nodes.values():
        candidate_id = str(candidate.get("id") or "").strip()
        if not candidate_id or candidate_id in members:
            raise ValueError("Graph member ids must be present and unique")
        members[candidate_id] = candidate
    node_id = str(node.get("id") or "").strip()
    if node_id not in members:
        raise ValueError("Graph does not contain static member: " + node_id)
    members_by_uuid = {
        str(candidate.get("uuid") or "").strip(): candidate_id
        for candidate_id, candidate in members.items()
        if str(candidate.get("uuid") or "").strip()
    }

    cache: dict[str, tuple[tuple[float, ...], ...]] = {}
    visiting: set[str] = set()

    def world_matrix(member_id: str) -> tuple[tuple[float, ...], ...]:
        cached = cache.get(member_id)
        if cached is not None:
            return cached
        if member_id in visiting:
            raise ValueError("Graph parent relationship contains a cycle")
        visiting.add(member_id)
        member = members[member_id]
        local = _local_pose_matrix(member)
        parent_id = _graph_parent_id(member, members, members_by_uuid)
        if not parent_id:
            result = local
        else:
            result = _multiply_matrix(world_matrix(parent_id), local)
        visiting.remove(member_id)
        cache[member_id] = result
        return result

    matrix = world_matrix(node_id)
    xyz_m = (float(matrix[0][3]), float(matrix[1][3]), float(matrix[2][3]))
    rpy_rad = _matrix_rpy(matrix)
    return xyz_m, rpy_rad


def _validate_visual_urdf(
    visual_urdf: str,
    *,
    member_id: str,
    root_link: str,
    mesh_paths: tuple[Path, ...],
) -> None:
    """Reject execution content; require collision meshes for MoveIt planning."""

    try:
        root = ET.fromstring(visual_urdf)
    except ET.ParseError as error:
        raise ValueError("package_static URDF is invalid XML") from error
    if root.tag != "robot":
        raise ValueError("package_static URDF root must be robot")
    if any(root.find(f".//{tag}") is not None for tag in _FORBIDDEN_TAGS):
        raise ValueError("package_static cannot declare execution content")
    if root.find(".//collision") is None:
        raise ValueError(
            "package_static URDF must declare collision geometry for MoveIt planning"
        )
    links = [str(link.attrib.get("name") or "") for link in root.findall("link")]
    joints = list(root.findall("joint"))
    joint_names = [str(joint.attrib.get("name") or "") for joint in joints]
    prefix = member_id + "_"
    if (
        not links
        or len(set(links)) != len(links)
        or root_link not in links
    ):
        raise ValueError("package_static links and root_link must be present and unique")
    if any(not name.startswith(prefix) for name in (*links, *joint_names)):
        raise ValueError(
            "package_static link and joint names must stay in the member namespace"
        )
    if len(set(joint_names)) != len(joint_names):
        raise ValueError("package_static joint names must be unique")

    child_links: set[str] = set()
    link_set = set(links)
    for joint in joints:
        if joint.attrib.get("type") != "fixed":
            raise ValueError("package_static cannot declare movable joints")
        parent = joint.find("parent")
        child = joint.find("child")
        parent_link = str((parent.attrib if parent is not None else {}).get("link") or "")
        child_link = str((child.attrib if child is not None else {}).get("link") or "")
        if not parent_link or not child_link:
            raise ValueError("package_static joint is missing parent or child")
        if parent_link not in link_set or child_link not in link_set:
            raise ValueError(
                "package_static provider cannot mount itself outside its local tree"
            )
        child_links.add(child_link)
    roots = set(links) - child_links
    if roots != {root_link}:
        raise ValueError(
            "package_static URDF must contain exactly one declared root link"
        )
    if len(set(mesh_paths)) != len(mesh_paths):
        raise ValueError("package_static mesh paths must be unique")
    missing = [path.name for path in mesh_paths if not path.is_file()]
    if missing:
        raise ValueError(
            "package_static mesh assets are missing: " + ", ".join(missing)
        )
    actual_mesh_uris = {
        str(mesh.attrib.get("filename") or "") for mesh in root.findall(".//mesh")
    }
    expected_mesh_uris = {path.as_uri() for path in mesh_paths}
    if actual_mesh_uris != expected_mesh_uris:
        raise ValueError(
            "package_static URDF mesh URLs do not match owned assets"
        )


def _graph_parent_id(
    member: Mapping[str, Any],
    members: Mapping[str, Mapping[str, Any]],
    members_by_uuid: Mapping[str, str],
) -> str:
    """Resolve Graph parent from workspace id or OS runtime parent_uuid."""

    for raw in (member.get("parent"), member.get("parent_uuid")):
        token = str(raw or "").strip()
        if not token:
            continue
        if token in members:
            return token
        mapped = members_by_uuid.get(token)
        if mapped:
            return mapped
        raise ValueError("Graph member parent is missing: " + token)
    return ""


def _local_pose_matrix(node: Mapping[str, Any]) -> tuple[tuple[float, ...], ...]:
    """Read one relative pose in millimetres/degrees without a second fallback source."""

    raw_pose = node.get("pose")
    raw_position = node.get("position")
    if isinstance(raw_pose, Mapping) and isinstance(raw_position, Mapping):
        raise ValueError("Graph member cannot declare both pose and legacy position")
    container = raw_pose if isinstance(raw_pose, Mapping) else raw_position
    if not isinstance(container, Mapping):
        container = {}
    nested_position = container.get("position")
    position = nested_position if isinstance(nested_position, Mapping) else container
    raw_rotation = container.get("rotation")
    rotation = raw_rotation if isinstance(raw_rotation, Mapping) else {}
    xyz_mm = tuple(
        _finite_number(position.get(axis), f"position.{axis}") for axis in "xyz"
    )
    rpy_deg = tuple(
        _finite_number(rotation.get(axis), f"rotation.{axis}") for axis in "xyz"
    )
    roll, pitch, yaw = (math.radians(value) for value in rpy_deg)
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    x_m, y_m, z_m = (value / 1000.0 for value in xyz_mm)
    return (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr, x_m),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr, y_m),
        (-sp, cp * sr, cp * cr, z_m),
        (0.0, 0.0, 0.0, 1.0),
    )


def _multiply_matrix(
    left: tuple[tuple[float, ...], ...],
    right: tuple[tuple[float, ...], ...],
) -> tuple[tuple[float, ...], ...]:
    """Multiply two 4x4 pose matrices."""

    return tuple(
        tuple(sum(left[row][k] * right[k][col] for k in range(4)) for col in range(4))
        for row in range(4)
    )


def _matrix_rpy(matrix: tuple[tuple[float, ...], ...]) -> tuple[float, float, float]:
    """Convert an Rz*Ry*Rx rotation matrix to URDF fixed-axis RPY."""

    pitch = math.asin(max(-1.0, min(1.0, -matrix[2][0])))
    roll = math.atan2(matrix[2][1], matrix[2][2])
    yaw = math.atan2(matrix[1][0], matrix[0][0])
    return (roll, pitch, yaw)


def _finite_number(value: object, label: str) -> float:
    """Return one finite physical pose component."""

    if value is None:
        return 0.0
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Graph " + label + " must be numeric") from error
    if not math.isfinite(result):
        raise ValueError("Graph " + label + " must be finite")
    return result


def _format_vector(value: tuple[float, float, float]) -> str:
    """Format a stable URDF vector and normalize negative zero."""

    return " ".join("0" if part == 0 else format(part, ".12g") for part in value)


__all__ = [
    "PackageStaticModelBundle",
    "instantiate_package_static_model",
    "load_package_static_model",
    "resolve_graph_world_pose",
]
