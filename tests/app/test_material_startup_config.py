"""UniLabOS startup arguments for the host-owned material service."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from unilabos.app.main import (
    configure_material_startup,
    parse_args,
    should_attach_legacy_http_bridge,
    should_request_remote_startup,
    should_start_edge_scheduler,
    should_start_embedded_material_service,
)
from unilabos.config.config import HTTPConfig


@pytest.fixture(autouse=True)
def _restore_material_config(monkeypatch):
    monkeypatch.setattr(HTTPConfig, "material_source", "microbackend")
    monkeypatch.setattr(HTTPConfig, "material_microbackend_addr", "")


def test_default_starts_embedded_microbackend_with_host_db() -> None:
    args = vars(parse_args().parse_args([]))

    mode = configure_material_startup(args)

    assert HTTPConfig.material_source == "microbackend"
    assert mode == "embedded"
    assert HTTPConfig.material_microbackend_addr == ""
    assert args["edge_inventory_db"] == "~/.unilabos/inventory.db"
    assert args["edge_scheduler"] is True
    assert args["edge_device_state_db"] == "~/.unilabos/device_state.db"
    assert args["edge_workflow_history_db"] == "~/.unilabos/workflow_history.db"
    assert should_start_embedded_material_service(args, is_host_mode=True)
    assert not should_start_embedded_material_service(args, is_host_mode=False)
    assert should_start_edge_scheduler(args, is_host_mode=True)
    assert not should_start_edge_scheduler(args, is_host_mode=False)


def test_scheduler_can_be_explicitly_disabled() -> None:
    args = vars(parse_args().parse_args(["--no_edge_scheduler"]))

    assert args["edge_scheduler"] is False
    assert not should_start_edge_scheduler(args, is_host_mode=True)


def test_production_edge_control_disables_local_scheduler_and_inventory() -> None:
    args = vars(parse_args().parse_args(["--app_bridges", "edge_control", "fastapi"]))

    mode = configure_material_startup(args)

    assert mode == "embedded"
    assert HTTPConfig.material_source == "backend"
    assert not should_start_embedded_material_service(args, is_host_mode=True)
    assert not should_start_edge_scheduler(args, is_host_mode=True)
    assert not should_attach_legacy_http_bridge(args)


def test_local_graph_does_not_request_legacy_remote_startup() -> None:
    assert not should_request_remote_startup(
        startup_json=None,
        graph_file_path="/config/devices.json",
    )
    assert should_request_remote_startup(
        startup_json=None,
        graph_file_path=None,
    )


def test_scheduler_database_paths_are_configurable() -> None:
    args = vars(
        parse_args().parse_args(
            [
                "--device_state_db",
                "/tmp/device-state.db",
                "--workflow_history_db",
                "/tmp/workflow-history.db",
            ]
        )
    )

    assert args["edge_device_state_db"] == "/tmp/device-state.db"
    assert args["edge_workflow_history_db"] == "/tmp/workflow-history.db"


def test_explicit_working_directory_is_the_only_local_runtime_storage_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明显式工作目录统一隔离本地运行时 SQLite，且不删除旧库存权威。

    参数：``tmp_path`` 提供显式运行目录与遗留用户目录；``monkeypatch`` 将
    ``HOME`` 隔离到测试目录。返回：无；断言统一运行时存储路径
    （RuntimeStoragePaths）把库存权威（Inventory Authority）、设备状态投影和
    工作流历史全部放入显式 ``working_dir``。存储权威不变量：既有
    ``~/.unilabos/*.db`` 不得被继承、覆盖或删除，资源图指纹冲突必须通过选择
    正确的运行时权威解决，不能通过清空旧权威绕过。
    """

    legacy_home = tmp_path / "legacy-home"
    legacy_storage = legacy_home / ".unilabos"
    legacy_storage.mkdir(parents=True)
    # ``legacy_payloads`` 是升级前仍须原样保留的三类本地持久事实哨兵。
    legacy_payloads = {
        legacy_storage / "inventory.db": b"existing-inventory-authority",
        legacy_storage / "device_state.db": b"existing-device-projection",
        legacy_storage / "workflow_history.db": b"existing-workflow-authority",
    }
    for legacy_path, payload in legacy_payloads.items():
        legacy_path.write_bytes(payload)
    monkeypatch.setenv("HOME", str(legacy_home))

    runtime_root = tmp_path / "szlab-runtime"
    args = vars(parse_args().parse_args(["--working_dir", str(runtime_root)]))
    try:
        runtime_storage = importlib.import_module("unilabos.app.runtime_storage")
    except ModuleNotFoundError as error:
        if error.name != "unilabos.app.runtime_storage":
            raise
        pytest.fail(
            "显式 --working_dir 尚未接入统一运行时存储路径（RuntimeStoragePaths）；"
            f"当前库存权威仍继承 {args['edge_inventory_db']!r}",
            pytrace=False,
        )

    resolve_paths = getattr(runtime_storage, "resolve_runtime_storage_paths", None)
    assert callable(resolve_paths), "缺少统一运行时存储路径解析入口"
    paths = resolve_paths(args, working_dir=str(runtime_root))

    assert paths.inventory_db == str(runtime_root / "inventory.db")
    assert paths.device_state_db == str(runtime_root / "device_state.db")
    assert paths.workflow_history_db == str(runtime_root / "workflow_history.db")
    assert args["edge_inventory_db"] == paths.inventory_db
    assert args["edge_device_state_db"] == paths.device_state_db
    assert args["edge_workflow_history_db"] == paths.workflow_history_db
    assert Path(paths.inventory_db) not in legacy_payloads
    assert Path(paths.device_state_db) not in legacy_payloads
    assert Path(paths.workflow_history_db) not in legacy_payloads
    assert {
        legacy_path: legacy_path.read_bytes() for legacy_path in legacy_payloads
    } == legacy_payloads


def test_directed_discovery_ports_are_configurable() -> None:
    args = vars(
        parse_args().parse_args(
            [
                "--hostlink_addr",
                "0.0.0.0:7302",
                "--ros_discovery_port",
                "11811",
                "--ros_discovery_server",
                "192.168.1.20:11811",
            ]
        )
    )

    assert args["hostlink_addr"] == "0.0.0.0:7302"
    assert args["ros_discovery_port"] == 11811
    assert args["ros_discovery_server"] == "192.168.1.20:11811"


def test_startup_arguments_switch_to_formal_backend() -> None:
    args = vars(
        parse_args().parse_args(
            ["--material_source", "backend", "--material_db", "/tmp/material.db"]
        )
    )

    mode = configure_material_startup(args)

    assert HTTPConfig.material_source == "backend"
    assert mode == "embedded"
    assert args["edge_inventory_db"] == "/tmp/material.db"


def test_external_mode_defaults_to_standalone_scheduler_port() -> None:
    args = vars(parse_args().parse_args(["--material_service_mode", "external"]))

    mode = configure_material_startup(args)

    assert mode == "external"
    assert HTTPConfig.material_microbackend_addr == "http://127.0.0.1:8092/api/v1"


def test_explicit_microbackend_address_implies_external_mode() -> None:
    args = vars(
        parse_args().parse_args(
            [
                "--material_microbackend_addr",
                "http://10.0.0.2:8092/api/v1",
            ]
        )
    )

    mode = configure_material_startup(args)

    assert mode == "external"
    assert HTTPConfig.material_microbackend_addr == "http://10.0.0.2:8092/api/v1"
