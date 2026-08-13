"""把软件包模型 Provider 适配为 OS 可组合的 MoveIt Bundle。"""

from __future__ import annotations

import copy
import importlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_PROVIDER_REF = re.compile(
    r"^(?P<module>[A-Za-z_][A-Za-z0-9_.]*):(?P<symbol>[A-Za-z_][A-Za-z0-9_]*)$"
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class PackageMoveItModelBundle:
    """已验证且与 Provider 对象分离的 MoveIt 模型快照。"""

    urdf: str
    srdf: str
    ros2_controllers: dict[str, Any]
    moveit_controllers: dict[str, Any]
    kinematics: dict[str, Any]
    joint_limits: dict[str, Any]
    source_digest: str
    rviz_required: bool


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
        raise ValueError("package_moveit provider 不可调用")

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

    urdf = str(getattr(bundle, "urdf", ""))
    srdf = str(getattr(bundle, "srdf", ""))
    _validate_robot_xml(urdf, label="URDF")
    _validate_robot_xml(srdf, label="SRDF")
    return PackageMoveItModelBundle(
        urdf=urdf,
        srdf=srdf,
        ros2_controllers=_copy_mapping(bundle, "ros2_controllers"),
        moveit_controllers=_copy_mapping(bundle, "moveit_controllers"),
        kinematics=_copy_mapping(bundle, "kinematics"),
        joint_limits=_copy_mapping(bundle, "joint_limits"),
        source_digest=source_digest,
        rviz_required=False,
    )


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
        raise ValueError("package_moveit 缺少 controller_manager")
    manager_parameters = controller_manager.get("ros__parameters")
    if not isinstance(manager_parameters, dict):
        raise ValueError("package_moveit 缺少 controller_manager.ros__parameters")
    local_manager = ros2_controllers["controller_manager"]["ros__parameters"]
    for controller_name, declaration in manager_parameters.items():
        if controller_name in local_manager:
            raise ValueError(f"MoveIt controller 名称冲突: {controller_name}")
        parameters = bundle.ros2_controllers.get(controller_name)
        if not isinstance(parameters, dict):
            raise ValueError(f"MoveIt controller 参数缺失: {controller_name}")
        local_manager[controller_name] = declaration
        ros2_controllers[controller_name] = parameters

    moveit_manager = bundle.moveit_controllers.get(
        "moveit_simple_controller_manager"
    )
    if not isinstance(moveit_manager, dict):
        raise ValueError("package_moveit 缺少 MoveIt controller manager")
    controller_names = moveit_manager.get("controller_names")
    if not isinstance(controller_names, list):
        raise ValueError("package_moveit controller_names 必须是列表")
    local_moveit_manager = moveit_controllers["moveit_simple_controller_manager"]
    for controller_name in controller_names:
        if controller_name in local_moveit_manager["controller_names"]:
            raise ValueError(f"MoveIt controller 名称冲突: {controller_name}")
        declaration = moveit_manager.get(controller_name)
        if not isinstance(declaration, dict):
            raise ValueError(f"MoveIt controller 声明缺失: {controller_name}")
        local_moveit_manager["controller_names"].append(controller_name)
        local_moveit_manager[controller_name] = declaration

    for group_name, parameters in bundle.kinematics.items():
        if group_name in kinematics:
            raise ValueError(f"MoveIt planning group 名称冲突: {group_name}")
        kinematics[group_name] = parameters
    package_joint_limits = bundle.joint_limits.get("joint_limits")
    if not isinstance(package_joint_limits, dict):
        raise ValueError("package_moveit 缺少 joint_limits")
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
        raise ValueError(f"package_moveit Bundle 缺少 {name} Mapping")
    return copy.deepcopy(dict(value))


def _validate_robot_xml(value: str, *, label: str) -> None:
    """验证 URDF/SRDF 是非空 robot XML。"""

    try:
        root = ET.fromstring(value)
    except ET.ParseError as error:
        raise ValueError(f"package_moveit {label} 不是合法 XML") from error
    if root.tag != "robot":
        raise ValueError(f"package_moveit {label} 根节点必须是 robot")


__all__ = [
    "PackageMoveItModelBundle",
    "load_package_moveit_model",
    "merge_package_moveit_parameters",
]
