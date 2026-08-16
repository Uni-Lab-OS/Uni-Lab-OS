"""验证本地调试与生产 Backend 控制面具有互斥的运行所有权。"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from unilabos.app.control_plane import (
    ControlPlaneMode,
    ControlPlaneRuntimeContext,
    should_mount_embedded_scheduler_routes,
    should_mount_workspace_authoring_routes,
    start_control_plane_runtime,
    validate_control_plane_arguments,
)
from unilabos.app.main import parse_args, resolve_resource_graph_source_id
from unilabos.app.runtime_topology import (
    RuntimeProcessRole,
    publish_edge_runtime_ready_signal,
    resolve_runtime_process_plan,
)
from unilabos.config.config import BasicConfig


def test_control_plane_defaults_to_local_debug() -> None:
    arguments = vars(parse_args().parse_args([]))

    assert arguments["control_plane"] == "local"
    assert validate_control_plane_arguments(arguments) is ControlPlaneMode.LOCAL


def test_resource_graph_source_id_preserves_workspace_original_name() -> None:
    arguments = vars(
        parse_args().parse_args(
            ["--resource_graph_source_id", "local-debug.json"]
        )
    )

    assert (
        resolve_resource_graph_source_id(
            arguments, "/runtime/generation/selected-graph.json"
        )
        == "local-debug.json"
    )
    assert (
        resolve_resource_graph_source_id(
            {}, "/runtime/generation/selected-graph.json"
        )
        == "/runtime/generation/selected-graph.json"
    )


def test_workbench_split_runtime_roles_are_orthogonal_to_local_authority() -> None:
    workspace_backend = vars(
        parse_args().parse_args(
            ["--process_role", "workspace_backend", "--app_bridges", "fastapi"]
        )
    )
    edge_runtime = vars(
        parse_args().parse_args(
            [
                "--process_role",
                "edge_runtime",
                "--app_bridges",
                "edge_control",
            ]
        )
    )

    backend_plan = resolve_runtime_process_plan(workspace_backend)
    edge_plan = resolve_runtime_process_plan(edge_runtime)

    assert backend_plan.role is RuntimeProcessRole.WORKSPACE_BACKEND
    assert backend_plan.control_plane is ControlPlaneMode.LOCAL
    assert backend_plan.starts_web_server
    assert not backend_plan.initializes_host_devices
    assert edge_plan.role is RuntimeProcessRole.EDGE_RUNTIME
    assert edge_plan.control_plane is ControlPlaneMode.LOCAL
    assert not edge_plan.starts_web_server
    assert edge_plan.initializes_host_devices


def test_split_runtime_accepts_backend_workspace_authoring_without_edge() -> None:
    local_edge_with_slave = vars(
        parse_args().parse_args(
            [
                "--process_role",
                "edge_runtime",
                "--is_slave",
                "--app_bridges",
                "edge_control",
            ]
        )
    )
    backend_workspace = vars(
        parse_args().parse_args(
            [
                "--process_role",
                "workspace_backend",
                "--control_plane",
                "backend",
                "--app_bridges",
                "fastapi",
            ]
        )
    )

    with pytest.raises(ValueError, match="不能使用 --is_slave"):
        resolve_runtime_process_plan(local_edge_with_slave)
    plan = resolve_runtime_process_plan(backend_workspace)
    assert plan.role is RuntimeProcessRole.WORKSPACE_BACKEND
    assert plan.control_plane is ControlPlaneMode.BACKEND
    assert plan.starts_web_server
    assert not plan.initializes_host_devices


def test_edge_runtime_ready_signal_is_atomic(tmp_path) -> None:
    ready_path = tmp_path / "edge" / "ready.json"

    publish_edge_runtime_ready_signal(str(ready_path))

    assert ready_path.read_text(encoding="utf-8").startswith(
        '{"schemaVersion": 1, "pid": '
    )
    assert list(ready_path.parent.glob("*.tmp")) == []


def test_backend_control_plane_requires_only_production_bridge() -> None:
    arguments = vars(
        parse_args().parse_args(
            [
                "--control_plane",
                "backend",
                "--app_bridges",
                "edge_control",
                "fastapi",
            ]
        )
    )

    assert validate_control_plane_arguments(arguments) is ControlPlaneMode.BACKEND

    arguments["app_bridges"] = ["fastapi"]
    with pytest.raises(ValueError, match="edge_control"):
        validate_control_plane_arguments(arguments)

    workspace_arguments = vars(
        parse_args().parse_args(
            [
                "--process_role",
                "workspace_backend",
                "--control_plane",
                "backend",
                "--app_bridges",
                "fastapi",
            ]
        )
    )
    assert (
        validate_control_plane_arguments(workspace_arguments)
        is ControlPlaneMode.BACKEND
    )

    arguments["app_bridges"] = ["edge_control", "websocket", "fastapi"]
    with pytest.raises(ValueError, match="websocket"):
        validate_control_plane_arguments(arguments)


def test_local_control_plane_rejects_production_bridge() -> None:
    arguments = vars(
        parse_args().parse_args(
            ["--control_plane", "local", "--app_bridges", "edge_control"]
        )
    )

    with pytest.raises(ValueError, match="仅允许 edge_runtime"):
        validate_control_plane_arguments(arguments)


def test_backend_control_plane_rejects_slave_and_local_database_preservation() -> None:
    arguments = vars(
        parse_args().parse_args(
            [
                "--control_plane",
                "backend",
                "--app_bridges",
                "edge_control",
                "--is_slave",
            ]
        )
    )
    with pytest.raises(ValueError, match="is_slave"):
        validate_control_plane_arguments(arguments)

    arguments["is_slave"] = False
    arguments["preserve_runtime_databases"] = True
    with pytest.raises(ValueError, match="preserve_runtime_databases"):
        validate_control_plane_arguments(arguments)


class _ProductionClient:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def test_backend_runtime_does_not_start_scheduler_or_local_databases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unilabos.app.edge_control import runtime as backend_runtime

    client = _ProductionClient()
    monkeypatch.setattr(backend_runtime, "create_edge_control_client", lambda: client)
    scheduler_modules_before = {
        name for name in sys.modules if name.startswith("unilabos.app.scheduler")
    }
    context = ControlPlaneRuntimeContext(
        arguments={
            "control_plane": "backend",
            "app_bridges": ["edge_control", "fastapi"],
            "is_slave": False,
            "preserve_runtime_databases": False,
        },
        working_dir=str(tmp_path),
        resource_tree_set=object(),
        registry=object(),
        graph_source_id="graph.json",
        material_shapes=(),
        material_model_catalog=None,
    )

    handle = start_control_plane_runtime(context)

    assert client.started
    assert handle.bridges == (client,)
    assert handle.communication_clients == (client,)
    assert {
        name for name in sys.modules if name.startswith("unilabos.app.scheduler")
    } == scheduler_modules_before
    assert not (tmp_path / "inventory.db").exists()
    assert not (tmp_path / "device_state.db").exists()
    assert not (tmp_path / "workflow_history.db").exists()


@pytest.mark.parametrize(
    ("control_plane", "expected"),
    [("local", True), ("backend", False)],
)
def test_fastapi_mounts_embedded_routes_only_for_local_control_plane(
    control_plane: str,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(BasicConfig, "control_plane", control_plane)

    assert should_mount_embedded_scheduler_routes() is expected


@pytest.mark.parametrize(
    ("process_role", "expected"),
    [("combined", True), ("workspace_backend", True), ("edge_runtime", False)],
)
def test_workspace_authoring_routes_follow_process_role_not_authority(
    process_role: str,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(BasicConfig, "process_role", process_role)
    monkeypatch.setattr(BasicConfig, "control_plane", "backend")

    assert should_mount_workspace_authoring_routes() is expected


def test_backend_fastapi_does_not_import_or_mount_embedded_scheduler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(BasicConfig, "control_plane", "backend")
    monkeypatch.setattr(BasicConfig, "working_dir", str(tmp_path))
    monkeypatch.setattr(BasicConfig, "workspace_package_mount_projection", None)
    scheduler_modules_before = {
        name for name in sys.modules if name.startswith("unilabos.app.scheduler")
    }

    server = importlib.reload(importlib.import_module("unilabos.app.web.server"))
    application = server.setup_server()

    assert {
        name for name in sys.modules if name.startswith("unilabos.app.scheduler")
    } == scheduler_modules_before
    assert all(
        "edge-scheduler" not in (getattr(route, "tags", None) or [])
        for route in application.routes
    )
    assert not any(
        getattr(route, "path", "").startswith("/api/v1/inventory")
        for route in application.routes
    )
    health_response = TestClient(application).get("/api/v1/health")
    assert health_response.status_code == 200
    assert health_response.json() == {"status": "ok", "scheduler": "disabled"}
    assert {
        name for name in sys.modules if name.startswith("unilabos.app.scheduler")
    } == scheduler_modules_before


def test_backend_ros_runtime_does_not_start_hostlink_microbackend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unilabos.ros import main_slave_run

    calls: list[object] = []
    fake_host_network = types.ModuleType("unilabos.app.scheduler.host_network")
    fake_host_network.setup_host_network_service = lambda *args: calls.append(args)
    monkeypatch.setitem(
        sys.modules,
        "unilabos.app.scheduler.host_network",
        fake_host_network,
    )
    monkeypatch.setattr(BasicConfig, "control_plane", "backend")

    main_slave_run._setup_host_network_before_ros()
    main_slave_run._attach_hostlink_runtime(object())

    assert calls == []
