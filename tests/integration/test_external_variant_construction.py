"""当前 registry 初始化合同的集成测试。

社区设备使用完整 registry key；registry 只强制 JSON 形式的初始化参数，
富对象由驱动自身根据这些参数构造。
"""

from contextlib import contextmanager

import pytest

from unilabos.registry.init_enforce import validate_init_param_enforce
from unilabos.registry.registry import lab_registry
from unilabos.resources.resource_tracker import ResourceDictInstance
from unilabos.ros.initialize_device import initialize_device_from_dict
from unilabos.utils.exception import DeviceClassInvalid

DRIVER_MODULE = (
    "tests.registry.fixtures.initializer_drivers:JsonConfiguredDevice"
)


def _entry(*, channels, deck_name, port):
    return {
        "class": {
            "module": DRIVER_MODULE,
            "type": "python",
            "status_types": {},
            "action_value_mappings": {},
        },
        "init_param_schema": {
            "config": {
                "type": "object",
                "properties": {
                    "backend_type": {"type": "string"},
                    "backend_params": {"type": "object"},
                    "deck_name": {"type": "string"},
                    "channels": {"type": "integer"},
                },
            }
        },
        "init_param_enforce": {
            "backend_type": "mock",
            "backend_params": {"port": port},
            "deck_name": deck_name,
            "channels": channels,
        },
    }


def _device_config(registry_key, *, name):
    return ResourceDictInstance.get_resource_instance_from_dict(
        {
            "name": name,
            "type": "device",
            "class": registry_key,
            "config": {
                "backend_type": "runtime-value-must-not-win",
                "backend_params": {
                    "host": "10.0.0.2",
                    "port": 1234,
                },
                "deck_name": "runtime-deck",
                "channels": 1,
                "name": name,
            },
        }
    )


@contextmanager
def _registered(entries):
    missing = object()
    previous = {
        key: lab_registry.device_type_registry.get(key, missing)
        for key in entries
    }
    lab_registry.device_type_registry.update(entries)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is missing:
                lab_registry.device_type_registry.pop(key, None)
            else:
                lab_registry.device_type_registry[key] = value


@pytest.fixture
def rclpy_runtime():
    import rclpy

    started_here = not rclpy.ok()
    if started_here:
        rclpy.init()
    try:
        yield
    finally:
        if started_here and rclpy.ok():
            rclpy.shutdown()


def _destroy_device(node):
    if node is not None:
        node.ros_node_instance.destroy_node()


def test_init_param_enforce_rejects_removed_class_init_factory_dsl():
    with pytest.raises(ValueError, match="不支持 class.init"):
        validate_init_param_enforce(
            "community.vendor.model_a",
            {"config": {"type": "object"}},
            {
                "backend": {
                    "factory": "vendor.drivers:Backend",
                    "kwargs": {"host": "${config.host}"},
                }
            },
        )


def test_exact_community_key_builds_driver_from_merged_json(rclpy_runtime):
    registry_key = "community.vendor.model_384"
    node = None
    with _registered(
        {registry_key: _entry(channels=384, deck_name="runtime-deck-384", port=4321)}
    ):
        try:
            node = initialize_device_from_dict(
                "lh_runtime",
                _device_config(registry_key, name="lh_runtime"),
            )

            assert node is not None
            driver = node.driver_instance
            assert driver.backend.host == "10.0.0.2"
            assert driver.backend.port == 4321
            assert driver.deck.name == "runtime-deck-384"
            assert driver.channels == 384
            assert driver.name == "lh_runtime"
        finally:
            _destroy_device(node)


def test_initialize_requires_exact_registry_key_without_alias_fallback():
    registry_key = "community.vendor.model_a"
    with _registered(
        {registry_key: _entry(channels=8, deck_name="model-a-deck", port=4008)}
    ):
        with pytest.raises(DeviceClassInvalid, match="vendor.model_a not found"):
            initialize_device_from_dict(
                "lh_alias",
                _device_config("vendor.model_a", name="lh_alias"),
            )


def test_shared_driver_supports_two_json_enforced_variants(rclpy_runtime):
    entries = {
        "community.vendor.model_a": _entry(
            channels=8,
            deck_name="model-a-deck",
            port=4008,
        ),
        "community.vendor.model_b": _entry(
            channels=96,
            deck_name="model-b-deck",
            port=4096,
        ),
    }
    nodes = []
    with _registered(entries):
        try:
            for registry_key, name in (
                ("community.vendor.model_a", "lh_a"),
                ("community.vendor.model_b", "lh_b"),
            ):
                node = initialize_device_from_dict(
                    name,
                    _device_config(registry_key, name=name),
                )
                assert node is not None
                nodes.append(node)

            first, second = (node.driver_instance for node in nodes)
            assert first.channels == 8
            assert first.backend.port == 4008
            assert first.deck.name == "model-a-deck"
            assert second.channels == 96
            assert second.backend.port == 4096
            assert second.deck.name == "model-b-deck"
        finally:
            for node in nodes:
                _destroy_device(node)
