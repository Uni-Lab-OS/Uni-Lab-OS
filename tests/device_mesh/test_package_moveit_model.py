"""软件包自包含 MoveIt 模型 Provider 的 OS 接入合同。"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from unilabos.device_mesh.package_moveit_model import (
    load_package_moveit_model,
    merge_package_moveit_parameters,
)

_DIGEST = "a" * 64


def _provider_module(*, rviz_required: bool = False) -> ModuleType:
    """创建记录 OS 调用参数的内存模型 Provider 模块。"""

    module = ModuleType("test_moveit_provider")

    def build_moveit_model(**kwargs):
        """返回满足 OS 标准 Bundle 形状的模型。"""

        module.last_kwargs = kwargs
        return SimpleNamespace(
            urdf="<robot name='test'><link name='robot_device_link'/></robot>",
            srdf="<robot name='test'><group name='robot_arm'/></robot>",
            ros2_controllers={"controller_manager": {"ros__parameters": {}}},
            moveit_controllers={
                "moveit_controller_manager": "manager",
                "moveit_simple_controller_manager": {"controller_names": []},
            },
            kinematics={"robot_arm": {}},
            joint_limits={"joint_limits": {}},
            source_digest=_DIGEST,
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
    assert bundle.rviz_required is False


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
