"""JSON 通用设备动作的物料占位符解析回归。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Annotated

import pytest

from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.resources.resource_tracker import JSON_UNILABOS_PARAM, PARAM_SAMPLE_UUIDS
from unilabos.ros.nodes import base_device_node
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode


class _Logger:
    def debug(self, *_args, **_kwargs) -> None:
        return

    def error(self, *_args, **_kwargs) -> None:
        return

    def warning(self, *_args, **_kwargs) -> None:
        return


class _Driver:
    def pick(self, resource: ResourceSlot):
        return resource


class _AnnotatedDriver:
    def pick(self, beaker: Annotated[ResourceSlot, "beaker_500ml"]):
        return beaker


class _AsyncDriver:
    def __init__(self) -> None:
        self.received: tuple[object, object] | None = None

    async def transfer(
        self,
        resource: ResourceSlot,
        mount_resource: ResourceSlot,
    ) -> tuple[object, object]:
        self.received = (resource, mount_resource)
        return self.received


def test_json_command_resolves_uuid_only_resource_slot_before_driver_call() -> None:
    """规范 UUID-only 物料引用必须在调用驱动前解析为完整资源实例。"""

    node = object.__new__(BaseROS2DeviceNode)
    node.driver_instance = _Driver()
    node.resource_tracker = None
    node._resolve_driver_method_name = lambda name: name
    node.lab_logger = lambda: _Logger()
    resolved_resource = object()
    observed_uuids: list[str] = []

    def convert(*uuids: str):
        observed_uuids.extend(uuids)
        return [resolved_resource]

    node._convert_resources_sync = convert
    command = {
        "function_name": "pick",
        "function_args": {
            "resource": {"uuid": "50000000-0000-4000-8000-000000000001"}
        },
        JSON_UNILABOS_PARAM: {PARAM_SAMPLE_UUIDS: {}},
    }

    result = node._execute_driver_command(json.dumps(command))

    assert observed_uuids == ["50000000-0000-4000-8000-000000000001"]
    assert result is resolved_resource


def test_json_command_resolves_annotated_resource_slot_before_driver_call() -> None:
    """带模板约束的物料占位符（ResourceSlot）也必须解析为完整资源实例。"""

    node = object.__new__(BaseROS2DeviceNode)
    node.driver_instance = _AnnotatedDriver()
    node.resource_tracker = None
    node._resolve_driver_method_name = lambda name: name
    node.lab_logger = lambda: _Logger()
    resolved_resource = SimpleNamespace(category="beaker")
    observed_uuids: list[str] = []

    def convert(*uuids: str):
        observed_uuids.extend(uuids)
        return [resolved_resource]

    node._convert_resources_sync = convert
    command = {
        "function_name": "pick",
        "function_args": {
            "beaker": {"uuid": "50000000-0000-4000-8000-000000000008"}
        },
        JSON_UNILABOS_PARAM: {PARAM_SAMPLE_UUIDS: {}},
    }

    result = node._execute_driver_command(json.dumps(command))

    assert observed_uuids == ["50000000-0000-4000-8000-000000000008"]
    assert result is resolved_resource


def test_json_command_maps_inventory_uuid_to_runtime_resource_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """库存稳定 UUID 命中设备运行时别名时仍须返回本地资源实例。"""

    inventory_uuid = "50000000-0000-4000-8000-000000000002"
    runtime_uuid = "50000000-0000-4000-8000-000000000003"
    inventory_resource = SimpleNamespace(
        unilabos_uuid=inventory_uuid,
        children=[],
    )
    runtime_resource = SimpleNamespace(
        unilabos_uuid=runtime_uuid,
        children=[],
    )
    tree_set = SimpleNamespace(
        trees=[
            SimpleNamespace(
                root_node=SimpleNamespace(res_content=inventory_resource)
            )
        ],
        to_plr_resources=lambda: [inventory_resource],
    )
    monkeypatch.setattr(
        base_device_node.ResourceTreeSet,
        "from_raw_dict_list",
        lambda _raw_data: tree_set,
    )

    class _Future:
        def done(self) -> bool:
            return True

        def result(self) -> SimpleNamespace:
            return SimpleNamespace(response=json.dumps([{"uuid": inventory_uuid}]))

    class _Client:
        def call_async(self, _request: object) -> _Future:
            return _Future()

    class _Tracker:
        def figure_resource(self, resource: object, try_mode: bool) -> list[object]:
            assert try_mode is True
            if resource is inventory_resource:
                return [runtime_resource]
            return []

        def loop_find_with_uuid(self, resource: object, target_uuid: str) -> object | None:
            if getattr(resource, "unilabos_uuid", None) == target_uuid:
                return resource
            return None

    node = object.__new__(BaseROS2DeviceNode)
    node._resource_clients = {"c2s_update_resource_tree": _Client()}
    node.resource_tracker = _Tracker()
    node.lab_logger = lambda: _Logger()

    converted = node._convert_resources_sync(inventory_uuid)

    assert converted == [runtime_resource]


def test_json_command_preserves_device_root_skipped_by_plr_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """设备型库位父资源被 PLR 投影跳过时仍须保留稳定引用字段。"""

    device_uuid = "50000000-0000-4000-8000-000000000006"
    raw_device = {
        "uuid": device_uuid,
        "id": device_uuid,
        "name": "S09 移液站",
        "type": "device",
        "class": "community.szlab_poly_studio.szlab_mixer_pipetting_station",
    }
    device_content = SimpleNamespace(
        uuid=device_uuid,
        id=device_uuid,
        type="device",
        model_dump=lambda **_kwargs: dict(raw_device),
    )
    tree_set = SimpleNamespace(
        trees=[SimpleNamespace(root_node=SimpleNamespace(res_content=device_content))],
        to_plr_resources=lambda: [],
    )
    monkeypatch.setattr(
        base_device_node.ResourceTreeSet,
        "from_raw_dict_list",
        lambda _raw_data: tree_set,
    )

    class _Future:
        def done(self) -> bool:
            return True

        def result(self) -> SimpleNamespace:
            return SimpleNamespace(response=json.dumps([raw_device]))

    class _Client:
        def call_async(self, _request: object) -> _Future:
            return _Future()

    node = object.__new__(BaseROS2DeviceNode)
    node._resource_clients = {"c2s_update_resource_tree": _Client()}
    node.resource_tracker = SimpleNamespace()
    node.lab_logger = lambda: _Logger()

    assert node._convert_resources_sync(device_uuid) == [raw_device]


def test_async_resource_conversion_preserves_device_root_skipped_by_plr_projection() -> None:
    """异步 Host 动作也必须保留设备型库位父资源的原始映射。"""

    device_uuid = "50000000-0000-4000-8000-000000000007"
    raw_device = {
        "uuid": device_uuid,
        "id": device_uuid,
        "name": "S08 开关盖",
        "type": "device",
        "class": "community.szlab_poly_studio.szlab_s08_cap_station",
    }
    device_content = SimpleNamespace(
        uuid=device_uuid,
        id=device_uuid,
        type="device",
        model_dump=lambda **_kwargs: dict(raw_device),
    )
    tree_set = SimpleNamespace(
        trees=[SimpleNamespace(root_node=SimpleNamespace(res_content=device_content))],
        to_plr_resources=lambda: [],
    )
    node = object.__new__(BaseROS2DeviceNode)

    async def get_resource(*_args, **_kwargs):
        return tree_set

    node.get_resource = get_resource
    node.resource_tracker = SimpleNamespace()
    node.lab_logger = lambda: _Logger()

    converted = asyncio.run(node._convert_resource_async({"uuid": device_uuid}))

    assert converted == raw_device


def test_transfer_accepts_uuid_mapping_as_target_resource() -> None:
    """转运记账必须接受设备型父资源的稳定 UUID 原始映射。"""

    source = SimpleNamespace(unilabos_uuid="50000000-0000-4000-8000-000000000008")
    target = {"uuid": "50000000-0000-4000-8000-000000000009", "type": "device"}

    class _UnavailableClient:
        def wait_for_service(self, timeout_sec: float) -> bool:
            assert timeout_sec == 5.0
            return False

    node = object.__new__(BaseROS2DeviceNode)
    node.device_id = "host_node"
    node.create_client = lambda *_args: _UnavailableClient()
    node.lab_logger = lambda: _Logger()

    with pytest.raises(ValueError, match="Service .* not available"):
        asyncio.run(
            node.transfer_resource_to_another(
                [source],
                "szlab_mixer_pipetting_station",
                [target],
                ["BEAKER1"],
            )
        )


def test_async_json_command_resolves_uuid_and_runtime_uuid_resource_slots() -> None:
    """异步设备动作必须解析两种规范资源身份并真正等待驱动协程。"""

    inventory_uuid = "50000000-0000-4000-8000-000000000004"
    mount_uuid = "50000000-0000-4000-8000-000000000005"
    resolved_resource = object()
    resolved_mount = object()
    resolved_by_uuid = {
        inventory_uuid: resolved_resource,
        mount_uuid: resolved_mount,
    }
    observed_uuids: list[str] = []
    driver = _AsyncDriver()
    node = object.__new__(BaseROS2DeviceNode)
    node.driver_instance = driver
    node.resource_tracker = None
    node._resolve_driver_method_name = lambda name: name
    node.lab_logger = lambda: _Logger()

    async def convert(resource_data: dict[str, object]) -> object:
        identity = str(
            resource_data.get("uuid") or resource_data.get("unilabos_uuid") or ""
        )
        observed_uuids.append(identity)
        return resolved_by_uuid[identity]

    node._convert_resource_async = convert
    command = {
        "function_name": "transfer",
        "function_args": {
            "resource": {
                "unilabos_uuid": inventory_uuid,
                "name": "已解析物料",
            },
            "mount_resource": {"uuid": mount_uuid},
        },
        JSON_UNILABOS_PARAM: {PARAM_SAMPLE_UUIDS: {}},
    }

    result = asyncio.run(node._execute_driver_command_async(json.dumps(command)))

    assert observed_uuids == [inventory_uuid, mount_uuid]
    assert driver.received == (resolved_resource, resolved_mount)
    assert result == (resolved_resource, resolved_mount)
