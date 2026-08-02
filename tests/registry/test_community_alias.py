"""Community device classes use their namespaced registry keys directly."""

from types import SimpleNamespace

import pytest

import unilabos.ros.initialize_device as device_initializer
from unilabos.utils.exception import DeviceClassInvalid


def _device_config(class_name, config=None):
    return SimpleNamespace(
        res_content=SimpleNamespace(
            klass=class_name,
            uuid="test-device-uuid",
            config=config or {},
        )
    )


def test_initialize_device_uses_exact_community_registry_key(monkeypatch):
    class_name = "community.test_package.pump"
    registry_entry = {
        "class": {
            "module": "tests.registry.fixtures.initializer_drivers:SharedDevice",
            "type": "python",
            "status_types": {},
            "action_value_mappings": {},
        },
        "init_param_enforce": {
            "channels": 8,
            "transport": {"port": 5000},
        },
    }
    monkeypatch.setitem(device_initializer.lab_registry.device_type_registry, class_name, registry_entry)

    captured = {}

    class _WrappedDevice:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(device_initializer.default_manager, "get_class", lambda _module: object())
    monkeypatch.setattr(device_initializer, "ros2_device_node", lambda _device, **_kwargs: _WrappedDevice)

    result = device_initializer.initialize_device_from_dict(
        "pump-1",
        _device_config(
            class_name,
            {
                "channels": 1,
                "transport": {"host": "127.0.0.1", "port": 9000},
            },
        ),
    )

    assert isinstance(result, _WrappedDevice)
    assert captured["driver_params"] == {
        "channels": 8,
        "transport": {"host": "127.0.0.1", "port": 5000},
    }


def test_initialize_device_does_not_fall_back_to_an_unprefixed_alias(monkeypatch):
    class_name = "community.test_package.alias_only"
    monkeypatch.delitem(device_initializer.lab_registry.device_type_registry, class_name, raising=False)
    monkeypatch.setitem(
        device_initializer.lab_registry.device_type_registry,
        "test_package.alias_only",
        {"class": {"module": "unused:Driver"}},
    )

    with pytest.raises(DeviceClassInvalid, match=class_name):
        device_initializer.initialize_device_from_dict("alias-1", _device_config(class_name))
