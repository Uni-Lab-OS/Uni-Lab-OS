"""当前 registry 初始化合同的集成测试。

社区设备使用完整 registry key；registry 只强制 JSON 形式的初始化参数，
富对象由驱动自身根据这些参数构造。
"""

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from unilabos.registry.init_enforce import validate_init_param_enforce

PACKAGE_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "registry"
    / "fixtures"
    / "external_variant_pkg"
)

ROS_SCENARIO_SCRIPT = """
import json
import sys
from contextlib import contextmanager

import rclpy

from unilabos.registry.registry import lab_registry
from unilabos.resources.resource_tracker import ResourceDictInstance
from unilabos.ros.initialize_device import initialize_device_from_dict
from unilabos.utils.exception import DeviceClassInvalid

DRIVER_MODULE = "tests.registry.fixtures.initializer_drivers:JsonConfiguredDevice"


def entry(*, channels, deck_name, port):
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


def device_config(registry_key, *, name):
    return ResourceDictInstance.get_resource_instance_from_dict({
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
    })


@contextmanager
def registered(entries):
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


def observe_driver(node):
    driver = node.driver_instance
    return {
        "backend_host": driver.backend.host,
        "backend_port": driver.backend.port,
        "deck_name": driver.deck.name,
        "channels": driver.channels,
        "name": driver.name,
    }


scenario = sys.argv[1]
if scenario == "exact":
    registry_key = "community.vendor.model_384"
    node = None
    rclpy.init()
    try:
        with registered({
            registry_key: entry(
                channels=384,
                deck_name="runtime-deck-384",
                port=4321,
            )
        }):
            node = initialize_device_from_dict(
                "lh_runtime",
                device_config(registry_key, name="lh_runtime"),
            )
            payload = observe_driver(node)
    finally:
        if node is not None:
            node.ros_node_instance.destroy_node()
        rclpy.shutdown()
elif scenario == "alias":
    registry_key = "community.vendor.model_a"
    with registered({
        registry_key: entry(
            channels=8,
            deck_name="model-a-deck",
            port=4008,
        )
    }):
        try:
            initialize_device_from_dict(
                "lh_alias",
                device_config("vendor.model_a", name="lh_alias"),
            )
        except DeviceClassInvalid as exc:
            payload = {
                "error_type": type(exc).__name__,
                "mentions_missing_key": "vendor.model_a not found" in str(exc),
            }
        else:
            raise AssertionError("stripped alias unexpectedly initialized")
elif scenario == "variants":
    entries = {
        "community.vendor.model_a": entry(
            channels=8,
            deck_name="model-a-deck",
            port=4008,
        ),
        "community.vendor.model_b": entry(
            channels=96,
            deck_name="model-b-deck",
            port=4096,
        ),
    }
    nodes = []
    rclpy.init()
    try:
        with registered(entries):
            for registry_key, name in (
                ("community.vendor.model_a", "lh_a"),
                ("community.vendor.model_b", "lh_b"),
            ):
                nodes.append(
                    initialize_device_from_dict(
                        name,
                        device_config(registry_key, name=name),
                    )
                )
            payload = {
                registry_key: observe_driver(node)
                for registry_key, node in zip(entries, nodes)
            }
    finally:
        for node in nodes:
            node.ros_node_instance.destroy_node()
        rclpy.shutdown()
else:
    raise AssertionError(f"unknown scenario: {scenario}")

print(json.dumps(payload))
"""


def _run_ros_scenario(name):
    result = subprocess.run(
        [sys.executable, "-c", ROS_SCENARIO_SCRIPT, name],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _live_ros_asyncio_threads():
    return [
        thread.name
        for thread in threading.enumerate()
        if thread.is_alive() and thread.name == "ROS2DeviceNode"
    ]


@pytest.fixture(autouse=True)
def assert_no_ros_asyncio_thread_leak():
    yield

    assert _live_ros_asyncio_threads() == []


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


def test_exact_community_key_builds_driver_from_merged_json():
    assert _run_ros_scenario("exact") == {
        "backend_host": "10.0.0.2",
        "backend_port": 4321,
        "deck_name": "runtime-deck-384",
        "channels": 384,
        "name": "lh_runtime",
    }


def test_initialize_requires_exact_registry_key_without_alias_fallback():
    assert _run_ros_scenario("alias") == {
        "error_type": "DeviceClassInvalid",
        "mentions_missing_key": True,
    }


def test_shared_driver_supports_two_json_enforced_variants():
    assert _run_ros_scenario("variants") == {
        "community.vendor.model_a": {
            "backend_host": "10.0.0.2",
            "backend_port": 4008,
            "deck_name": "model-a-deck",
            "channels": 8,
            "name": "lh_a",
        },
        "community.vendor.model_b": {
            "backend_host": "10.0.0.2",
            "backend_port": 4096,
            "deck_name": "model-b-deck",
            "channels": 96,
            "name": "lh_b",
        },
    }


def test_ros_construction_scenarios_leave_no_asyncio_thread_in_pytest():
    assert _live_ros_asyncio_threads() == []


def test_fixture_discovery_load_and_initialization_form_one_complete_chain():
    script = """
import json
import sys
from pathlib import Path

import rclpy

from unilabos.package_manager.legacy import discover_registry_paths_from_project
from unilabos.registry.registry import build_registry
from unilabos.resources.resource_tracker import ResourceDictInstance
from unilabos.ros.initialize_device import initialize_device_from_dict

package_dir = Path(sys.argv[1]).resolve()
registry_roots = discover_registry_paths_from_project(package_dir)
registry = build_registry(
    devices_dirs=[str(package_dir)],
    external_only=True,
)

rclpy.init()
nodes = []
observed = {}
try:
    for registry_key, name in (
        ("vendor.lh.model_a", "lh_a"),
        ("vendor.lh.model_b", "lh_b"),
    ):
        device_config = ResourceDictInstance.get_resource_instance_from_dict({
            "name": name,
            "type": "device",
            "class": registry_key,
            "config": {
                "backend_type": "runtime-must-not-win",
                "backend_params": {"host": "10.0.0.2", "port": 1},
                "deck_name": "runtime-deck",
                "channels": 1,
                "name": name,
            },
        })
        node = initialize_device_from_dict(name, device_config)
        nodes.append(node)
        driver = node.driver_instance
        entry = registry.device_type_registry[registry_key]
        observed[registry_key] = {
            "class_init_present": "init" in entry["class"],
            "enforce": entry["init_param_enforce"],
            "backend_host": driver.backend.host,
            "backend_port": driver.backend.port,
            "deck_name": driver.deck.name,
            "channels": driver.channels,
            "name": driver.name,
        }
finally:
    for node in nodes:
        node.ros_node_instance.destroy_node()
    rclpy.shutdown()

print(json.dumps({
    "registry_roots": [str(path) for path in registry_roots],
    "observed": observed,
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(PACKAGE_FIXTURE)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["registry_roots"] == [
        str(PACKAGE_FIXTURE / "unilabos_registry")
    ]
    assert payload["observed"] == {
        "vendor.lh.model_a": {
            "class_init_present": False,
            "enforce": {
                "backend_type": "mock",
                "backend_params": {"port": 4008},
                "deck_name": "model-a-deck",
                "channels": 8,
            },
            "backend_host": "10.0.0.2",
            "backend_port": 4008,
            "deck_name": "model-a-deck",
            "channels": 8,
            "name": "lh_a",
        },
        "vendor.lh.model_b": {
            "class_init_present": False,
            "enforce": {
                "backend_type": "mock",
                "backend_params": {"port": 4096},
                "deck_name": "model-b-deck",
                "channels": 96,
            },
            "backend_host": "10.0.0.2",
            "backend_port": 4096,
            "deck_name": "model-b-deck",
            "channels": 96,
            "name": "lh_b",
        },
    }
