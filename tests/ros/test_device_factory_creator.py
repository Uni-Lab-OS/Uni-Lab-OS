"""设备创建器调用同步工厂的边界合同。"""

from __future__ import annotations

from typing import Any

import pytest

from unilabos.resources.resource_tracker import DeviceNodeResourceTracker
from unilabos.ros.nodes.base_device_node import DeviceInitError
from unilabos.ros.utils.driver_creator import DeviceClassCreator, PyLabRobotCreator


class _Driver:
    def __init__(self, name: str) -> None:
        self.name = name


def test_creator_invokes_factory_exactly_once_and_preserves_contract_class() -> None:
    """每个实例只调用一次工厂，且返回注解类的真实实例。"""

    calls: list[dict[str, Any]] = []

    def factory(**config: Any) -> _Driver:
        calls.append(dict(config))
        return _Driver(**config)

    creator = DeviceClassCreator(
        _Driver,
        children=[],
        resource_tracker=DeviceNodeResourceTracker(),
        constructor=factory,
    )

    instance = creator.create_instance({"name": "cytomat-a"})

    assert isinstance(instance, _Driver)
    assert instance.name == "cytomat-a"
    assert calls == [{"name": "cytomat-a"}]


def test_creator_rejects_factory_returning_the_wrong_runtime_type() -> None:
    """工厂不能以 duck typing 绕过 Catalog 声明的返回类。"""

    creator = DeviceClassCreator(
        _Driver,
        children=[],
        resource_tracker=DeviceNodeResourceTracker(),
        constructor=lambda **_config: object(),
    )

    with pytest.raises(DeviceInitError, match="factory_return_type_mismatch"):
        creator.create_instance({"name": "cytomat-a"})


def test_creator_rejects_factory_instance_missing_catalog_members() -> None:
    """工厂实例必须真实提供 Catalog 声明的动作与状态成员。"""

    creator = DeviceClassCreator(
        _Driver,
        children=[],
        resource_tracker=DeviceNodeResourceTracker(),
        constructor=lambda **config: _Driver(**config),
        required_action_members=("rotate",),
        required_status_members=("temperature",),
    )

    with pytest.raises(DeviceInitError, match="factory_contract_member_missing"):
        creator.create_instance({"name": "cytomat-a"})


def test_creator_ignores_ros_transport_actions_when_validating_factory_contract() -> None:
    """ROS 通用命令端点由节点包装器提供，不是工厂返回类的公开动作。"""

    class DriverWithRotate(_Driver):
        def rotate(self) -> None:
            return None

    creator = DeviceClassCreator(
        DriverWithRotate,
        children=[],
        resource_tracker=DeviceNodeResourceTracker(),
        constructor=lambda **config: DriverWithRotate(**config),
        required_action_members=("rotate", "_execute_driver_command_async"),
    )

    instance = creator.create_instance({"name": "cytomat-a"})

    assert isinstance(instance, DriverWithRotate)


def test_pylabrobot_factory_registers_its_complete_resource_root() -> None:
    """工厂自产的 rack/site 树必须进入设备 ResourceTracker，供物料画布同步。"""

    from pylabrobot.resources import Coordinate, Resource

    class FactoryResource(Resource):
        def __init__(self, name: str) -> None:
            super().__init__(name=name, size_x=100, size_y=100, size_z=100)
            self.assign_child_resource(
                Resource(name=f"{name}-stack", size_x=10, size_y=20, size_z=80),
                location=Coordinate(x=20, y=0, z=0),
            )

    tracker = DeviceNodeResourceTracker()
    calls: list[str] = []

    def factory(name: str) -> FactoryResource:
        calls.append(name)
        return FactoryResource(name)

    creator = PyLabRobotCreator(
        FactoryResource,
        children=[],
        resource_tracker=tracker,
        constructor=factory,
    )

    instance = creator.create_instance({"name": "cytomat-a"})

    assert calls == ["cytomat-a"]
    assert tracker.resources == [instance]
    assert [child.name for child in tracker.resources[0].children] == [
        "cytomat-a-stack"
    ]
