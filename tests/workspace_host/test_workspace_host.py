"""AIW-02 Workspace Host lifecycle, idempotency, and discovery contracts."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from unilabos.app.edge_control.store import EdgeControlStore
from unilabos.workspace_host.client import WorkspaceHostClient
from unilabos.workspace_host.discovery import WorkspaceHostLock, ensure_local_token
from unilabos.workspace_host.host import (
    WorkspaceHost,
    _handler_type,
    _renderer_process_environment,
)
from unilabos.workspace_host.launch import (
    LaunchPlan,
    resolve_backend_launch,
    resolve_edge_launch,
    resolve_plc_launch,
)
from unilabos.workspace_host.model import WorkspaceHostError, WorkspacePaths


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "deployment" / "graphs").mkdir(parents=True)
    (root / "deployment" / "graphs" / "graph.json").write_text("{}\n")
    (root / "deployment" / "local_config.py").write_text("# fixture\n")
    return root


def test_workspace_lock_fails_closed_with_discovery_details(workspace: Path) -> None:
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    paths.session.write_text(json.dumps({"endpoint": "http://127.0.0.1:1"}))
    first = WorkspaceHostLock(paths)
    first.acquire()
    try:
        with pytest.raises(WorkspaceHostError) as caught:
            WorkspaceHostLock(paths).acquire()
        assert caught.value.code == "host_already_running"
        assert caught.value.details == {"endpoint": "http://127.0.0.1:1"}
    finally:
        first.release()


def test_workspace_token_is_private_and_stable(workspace: Path) -> None:
    paths = WorkspacePaths.resolve(workspace)
    first = ensure_local_token(paths)
    second = ensure_local_token(paths)
    assert first == second
    assert len(first) == 64
    if os.name != "nt":
        assert paths.token.stat().st_mode & 0o777 == 0o600


def test_workspace_client_executes_and_normalizes_one_host_operation(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = WorkspacePaths.resolve(workspace)
    client = WorkspaceHostClient(paths, "http://127.0.0.1:48100", "token")
    calls: list[tuple[str, object]] = []

    def submit(command: str, **options: object) -> dict[str, object]:
        calls.append(("submit", (command, options)))
        return {"operationId": "operation-1", "phase": "pending"}

    def wait(operation_id: str, **options: object) -> dict[str, object]:
        calls.append(("wait", (operation_id, options)))
        return {
            "operationId": operation_id,
            "phase": "succeeded",
            "result": {"components": {"edge": {"phase": "ready"}}},
        }

    monkeypatch.setattr(client, "submit", submit)
    monkeypatch.setattr(client, "wait", wait)

    result = client.execute(
        "os.restart",
        parameters={"runtimeMode": "normal"},
        operation_id="operation-1",
        expected_revision=7,
        timeout=4.0,
    )

    assert result["phase"] == "succeeded"
    assert calls == [
        (
            "submit",
            (
                "os.restart",
                {
                    "parameters": {"runtimeMode": "normal"},
                    "operation_id": "operation-1",
                    "expected_revision": 7,
                },
            ),
        ),
        ("wait", ("operation-1", {"timeout": 4.0})),
    ]


def test_workspace_client_raises_the_host_operation_failure(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = WorkspacePaths.resolve(workspace)
    client = WorkspaceHostClient(paths, "http://127.0.0.1:48100", "token")
    monkeypatch.setattr(
        client,
        "submit",
        lambda *_args, **_kwargs: {"operationId": "operation-failed"},
    )
    monkeypatch.setattr(
        client,
        "wait",
        lambda *_args, **_kwargs: {
            "operationId": "operation-failed",
            "phase": "failed",
            "error": {
                "code": "os_readiness_failed",
                "message": "OS 未就绪",
                "details": {"logPath": "/tmp/os.log"},
            },
        },
    )

    with pytest.raises(WorkspaceHostError) as caught:
        client.execute("os.restart")

    assert caught.value.code == "os_readiness_failed"
    assert caught.value.details == {"logPath": "/tmp/os.log"}


def test_workspace_client_status_is_stable_while_host_is_offline(
    workspace: Path,
) -> None:
    result = WorkspaceHostClient.status(workspace)

    assert result["schemaVersion"] == "unilab-workspace-host/v1"
    assert result["workspacePath"] == str(workspace)
    assert result["host"] == {"phase": "offline", "pid": None, "endpoint": None}
    assert set(result["components"]) == {"backend", "edge", "plc", "renderer"}
    assert result["diagnostic"]["code"] == "host_not_found"


def test_attached_renderer_records_a_reusable_headless_adapter(workspace: Path) -> None:
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)
    snapshot = host._dispatch(
        "renderer.attach",
        {
            "pid": os.getpid(),
            "address": "http://127.0.0.1:3100",
            "generation": "renderer-1",
            "workbenchProjectPath": "/opt/unilab/workbench",
            "nodeExecutable": "/opt/unilab/node",
        },
    )

    renderer = snapshot["components"]["renderer"]
    assert "material-scene-reload" in renderer["capabilities"]
    assert renderer["metadata"]["workbenchProjectPath"] == "/opt/unilab/workbench"
    host._material_renderer_usable = lambda _renderer: True  # type: ignore[method-assign]
    ensured = host._dispatch("renderer.headless.ensure", {})
    assert ensured["adapter"] == "attached"
    host.close()


def test_headless_renderer_inherits_selected_backend_authority() -> None:
    environment = _renderer_process_environment(
        {
            "domainMode": "backend",
            "backendUrl": "http://127.0.0.1:18080",
        },
        base_environment={"PATH": "/fixture/bin"},
    )

    assert environment["UNILAB_RENDERER_MANAGED_HEADLESS"] == "1"
    assert environment["UNILAB_BACKEND_PROXY_TARGET"] == "http://127.0.0.1:18080"
    assert environment["PATH"] == "/fixture/bin"


def test_host_restart_adopts_a_live_workbench_renderer(workspace: Path) -> None:
    renderer_server = ThreadingHTTPServer(("127.0.0.1", 0), _RendererHandler)
    threading.Thread(target=renderer_server.serve_forever, daemon=True).start()
    renderer_port = int(renderer_server.server_address[1])
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    token = ensure_local_token(paths)
    first = WorkspaceHost(paths, token, readiness_timeout=0.1)
    first._dispatch(
        "renderer.attach",
        {
            "pid": os.getpid(),
            "address": f"http://127.0.0.1:{renderer_port}",
            "generation": "renderer-before-host-restart",
        },
    )
    first.close()

    restarted = WorkspaceHost(paths, token, readiness_timeout=0.1)
    try:
        assert restarted.snapshot()["components"]["renderer"]["phase"] == "ready"
    finally:
        restarted.close()
        renderer_server.shutdown()
        renderer_server.server_close()


def test_split_runtime_launches_share_local_edge_protocol_and_stable_state(
    workspace: Path,
) -> None:
    paths = WorkspacePaths.resolve(workspace)
    token = ensure_local_token(paths)
    backend = resolve_backend_launch(
        paths,
        graph_path="deployment/graphs/graph.json",
        backend_port=48_101,
        hostlink_port=48_102,
    )
    backend_component = {
        "address": backend.address,
        "metadata": backend.metadata,
    }
    first_edge = resolve_edge_launch(paths, backend_component)
    second_edge = resolve_edge_launch(paths, backend_component)

    assert backend.environment["UNILABOS_EDGECONTROLCONFIG_API_KEY"] == token
    assert backend.environment["UNILABOS_EDGECONTROLCONFIG_BACKEND_ADDR"] == (
        "http://127.0.0.1:48101"
    )
    assert "--process_role" in backend.command
    assert "workspace_backend" in backend.command
    assert "edge_control" in first_edge.command
    assert "fastapi" not in first_edge.command
    assert "--is_slave" not in first_edge.command
    assert "--hostlink_addr" not in first_edge.command
    assert first_edge.environment["UNILABOS_EDGECONTROLCONFIG_API_KEY"] == token
    assert first_edge.environment["UNILABOS_EDGECONTROLCONFIG_BACKEND_ADDR"] == (
        backend.address
    )
    stable_state = str(paths.runtime / "edge" / "edge_control.db")
    assert first_edge.environment["UNILABOS_EDGECONTROLCONFIG_STATE_DB"] == stable_state
    assert second_edge.environment["UNILABOS_EDGECONTROLCONFIG_STATE_DB"] == stable_state
    assert first_edge.metadata["runtimeDirectory"] != second_edge.metadata[
        "runtimeDirectory"
    ]


def test_backend_launch_keeps_local_domain_store_across_process_generations(
    workspace: Path,
) -> None:
    """Backend crash recovery must not create a fresh Local Domain database."""

    paths = WorkspacePaths.resolve(workspace)
    ensure_local_token(paths)

    first = resolve_backend_launch(
        paths,
        graph_path="deployment/graphs/graph.json",
        backend_port=48_121,
        hostlink_port=48_122,
    )
    second = resolve_backend_launch(
        paths,
        graph_path="deployment/graphs/graph.json",
        backend_port=48_123,
        hostlink_port=48_124,
    )

    expected_state = str(paths.runtime / "backend" / "local-domain")
    assert _argument_value(first.command, "--working_dir") == expected_state
    assert _argument_value(second.command, "--working_dir") == expected_state
    assert "--preserve_runtime_databases" in first.command
    assert "--preserve_runtime_databases" in second.command
    assert first.metadata["stateDirectory"] == expected_state
    assert second.metadata["stateDirectory"] == expected_state
    assert first.metadata["runtimeDirectory"] != second.metadata["runtimeDirectory"]


def test_backend_launch_migrates_the_last_generation_into_stable_local_domain(
    workspace: Path,
) -> None:
    """The first split-runtime upgrade preserves the previous Backend facts."""

    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    ensure_local_token(paths)
    legacy = paths.runtime / "backend" / "legacy-generation"
    legacy.mkdir(parents=True)
    legacy_database = legacy / "workflow_history.db"
    connection = sqlite3.connect(legacy_database)
    connection.execute("CREATE TABLE acceptance (value TEXT NOT NULL)")
    connection.execute("INSERT INTO acceptance(value) VALUES ('preserved')")
    connection.commit()
    connection.close()
    paths.session.write_text(
        json.dumps(
            {
                "schemaVersion": "unilab-workspace-host/v1",
                "components": {
                    "backend": {
                        "metadata": {"runtimeDirectory": str(legacy)},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    plan = resolve_backend_launch(
        paths,
        graph_path="deployment/graphs/graph.json",
        backend_port=48_125,
        hostlink_port=48_126,
    )

    migrated = paths.runtime / "backend" / "local-domain" / "workflow_history.db"
    connection = sqlite3.connect(migrated)
    try:
        assert connection.execute("SELECT value FROM acceptance").fetchone() == (
            "preserved",
        )
    finally:
        connection.close()
    assert plan.metadata["legacyStateMigratedFrom"] == str(legacy)


def test_backend_authority_keeps_authoring_backend_and_routes_edge_remotely(
    workspace: Path,
) -> None:
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    ensure_local_token(paths)
    paths.environment.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "graphPath": "deployment/graphs/graph.json",
                "runtimeMode": "normal",
                "domainMode": "backend",
                "backendUrl": "https://backend.example.test",
            }
        )
    )

    backend = resolve_backend_launch(
        paths,
        backend_port=48_111,
        hostlink_port=48_112,
    )
    edge = resolve_edge_launch(
        paths,
        {"address": backend.address, "metadata": backend.metadata},
    )

    assert _argument_value(backend.command, "--control_plane") == "backend"
    assert "fastapi" in backend.command
    assert "edge_control" not in backend.command
    assert backend.metadata["domainMode"] == "backend"
    assert _argument_value(edge.command, "--control_plane") == "backend"
    assert edge.environment["UNILABOS_EDGECONTROLCONFIG_BACKEND_ADDR"] == (
        "https://backend.example.test"
    )
    assert edge.environment["UNILABOS_EDGECONTROLCONFIG_API_KEY"]
    assert edge.metadata["authorityAddress"] == "https://backend.example.test"
    backend_state = edge.environment["UNILABOS_EDGECONTROLCONFIG_STATE_DB"]
    assert backend_state.startswith(
        str(paths.runtime / "edge" / "edge_control-backend-")
    )
    assert backend_state.endswith(".db")
    assert backend_state != str(paths.runtime / "edge" / "edge_control.db")

    repeated = resolve_edge_launch(
        paths,
        {"address": backend.address, "metadata": backend.metadata},
    )
    assert repeated.environment["UNILABOS_EDGECONTROLCONFIG_STATE_DB"] == backend_state


def test_authority_switch_preflights_before_restart_and_persists_mode(
    workspace: Path,
) -> None:
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)
    calls: list[str] = []
    host._preflight_backend_authority = lambda url: calls.append(f"preflight:{url}")  # type: ignore[method-assign]
    host._bootstrap_backend_authority = lambda url: calls.append(f"bootstrap:{url}")  # type: ignore[method-assign]
    host._stop_component = lambda name: calls.append(f"stop:{name}") or {}  # type: ignore[method-assign]
    host._start_backend = lambda parameters: calls.append("start:backend") or {}  # type: ignore[method-assign]
    host._start_edge = lambda: calls.append("start:edge") or {}  # type: ignore[method-assign]
    host._components["backend"]["phase"] = "ready"
    host._components["edge"]["phase"] = "ready"

    snapshot = host._dispatch(
        "authority.switch",
        {"mode": "backend", "backendUrl": "http://127.0.0.1:8080/"},
    )

    assert calls == [
        "preflight:http://127.0.0.1:8080",
        "bootstrap:http://127.0.0.1:8080",
        "stop:edge",
        "stop:backend",
        "start:backend",
        "start:edge",
    ]
    assert snapshot["configuration"]["domainMode"] == "backend"
    assert snapshot["configuration"]["backendUrl"] == "http://127.0.0.1:8080"
    persisted = json.loads(paths.environment.read_text(encoding="utf-8"))
    assert persisted["domainMode"] == "backend"
    host.close()


def test_plc_launch_preserves_explicit_handshake_workflow(workspace: Path) -> None:
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    plc_project = workspace / "plc-sim"
    (plc_project / "OpcUaSim" / "gui").mkdir(parents=True)
    (plc_project / "OpcUaSim" / "gui" / "backend.py").write_text("# fixture\n")
    variable_table = workspace / "plc.csv"
    variable_table.write_text("变量名,数据类型\n")
    paths.environment.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "plcSimulatorProjectPath": str(plc_project),
                "plcVariableTablePath": str(variable_table),
                "plcHandshakeProfile": "szlab",
                "plcHandshakeWorkflow": "s_z_lab_单样品全流程_物料感知",
            }
        )
    )

    plan = resolve_plc_launch(paths)

    assert plan.metadata["handshakeProfile"] == "szlab"
    assert plan.metadata["handshakeWorkflow"] == "s_z_lab_单样品全流程_物料感知"


def test_operation_is_recoverable_idempotent_and_audited(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready_server = ThreadingHTTPServer(("127.0.0.1", 0), _ReadyHandler)
    threading.Thread(target=ready_server.serve_forever, daemon=True).start()
    ready_port = int(ready_server.server_address[1])
    paths = WorkspacePaths.resolve(workspace)
    token = ensure_local_token(paths)

    def fake_backend(*_args: object, **_kwargs: object) -> LaunchPlan:
        generation = "test-backend-generation"
        return LaunchPlan(
            component="backend",
            command=(sys.executable, "-c", "import time; time.sleep(120)"),
            cwd=workspace,
            environment=dict(os.environ),
            generation=generation,
            log_path=paths.logs / "backend.log",
            address=f"http://127.0.0.1:{ready_port}",
            ready_url=f"http://127.0.0.1:{ready_port}/api/v1/health",
            metadata={"runtimeMode": "normal"},
        )

    monkeypatch.setattr(
        "unilabos.workspace_host.host.resolve_backend_launch",
        fake_backend,
    )
    host = WorkspaceHost(paths, token, readiness_timeout=2.0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_type(host))
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    host.publish_endpoint(endpoint)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = WorkspaceHostClient(paths, endpoint, token)
    try:
        submitted = client.submit("backend.start", operation_id="start-once")
        duplicate = client.submit("backend.start", operation_id="start-once")
        assert duplicate["requestHash"] == submitted["requestHash"]
        completed = client.wait("start-once", timeout=5)
        assert completed["phase"] == "succeeded"
        snapshot = client.snapshot()
        assert snapshot["components"]["backend"]["phase"] == "ready"
        assert snapshot["components"]["backend"]["pid"] > 0

        recovered = client.operation("start-once")
        assert recovered == completed
        with pytest.raises(WorkspaceHostError) as caught:
            client.submit(
                "backend.stop",
                operation_id="start-once",
            )
        assert caught.value.code == "operation_conflict"

        stopped = client.submit("backend.stop", operation_id="stop-once")
        stopped = client.wait(str(stopped["operationId"]), timeout=5)
        assert stopped["phase"] == "succeeded"
        assert client.snapshot()["components"]["backend"]["phase"] == "idle"
        events = paths.audit.read_text(encoding="utf-8")
        assert "operation.submitted" in events
        assert "backend.ready" in events
    finally:
        try:
            operation = client.submit("backend.stop", operation_id="cleanup")
            client.wait(str(operation["operationId"]), timeout=5)
        except WorkspaceHostError:
            pass
        server.shutdown()
        server.server_close()
        host.close()
        ready_server.shutdown()
        ready_server.server_close()


def test_os_restart_and_local_reset_state_are_distinct_commands(
    workspace: Path,
) -> None:
    paths = WorkspacePaths.resolve(workspace)
    token = ensure_local_token(paths)
    host = WorkspaceHost(paths, token, readiness_timeout=0.1)
    calls: list[tuple[str, dict[str, object]]] = []

    def dispatch(command: str, parameters: dict[str, object]) -> object:
        calls.append((command, parameters))
        return {"command": command}

    host._dispatch = dispatch  # type: ignore[method-assign]
    first = host.submit(
        {"operationId": "os", "command": "os.restart", "parameters": {}}
    )
    second = host.submit(
        {
            "operationId": "reset",
            "command": "local.reset-state",
            "parameters": {},
        }
    )
    _wait_operation(host, str(first["operationId"]))
    _wait_operation(host, str(second["operationId"]))
    assert calls == [("os.restart", {}), ("local.reset-state", {})]
    audit = paths.audit.read_text(encoding="utf-8")
    assert '"command":"os.restart"' in audit
    assert '"command":"local.reset-state"' in audit
    host.close()


def test_backend_restart_reconnects_an_existing_edge_to_the_new_dynamic_port(
    workspace: Path,
) -> None:
    paths = WorkspacePaths.resolve(workspace)
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)
    calls: list[str] = []
    host._components["backend"]["phase"] = "ready"
    host._components["edge"]["phase"] = "ready"
    host._stop_component = lambda name: calls.append(f"stop:{name}") or {}  # type: ignore[method-assign]
    host._start_backend = lambda parameters: calls.append("start:backend") or {}  # type: ignore[method-assign]
    host._start_edge = lambda: calls.append("start:edge") or {"ready": True}  # type: ignore[method-assign]

    result = host._dispatch("backend.restart", {})

    assert result == {"ready": True}
    assert calls == ["stop:edge", "stop:backend", "start:backend", "start:edge"]
    host.close()


def test_local_reset_state_clears_edge_work_but_preserves_identity(
    workspace: Path,
) -> None:
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    state_path = paths.runtime / "edge" / "edge_control.db"
    store = EdgeControlStore(str(state_path))
    instance_uuid = store.get_or_create_instance_uuid()
    command_uuid = "50000000-0000-4000-8000-000000000201"
    job_uuid = "50000000-0000-4000-8000-000000000202"
    store.record_command(
        {
            "message_uuid": command_uuid,
            "sequence": 1,
            "type": "job.start",
            "payload": {"job_uuid": job_uuid},
        }
    )
    store.save_job_start(
        {
            "job_uuid": job_uuid,
            "task_uuid": "50000000-0000-4000-8000-000000000203",
            "node_uuid": "50000000-0000-4000-8000-000000000204",
            "job_access_token": "workspace-reset-token",
        },
        command_uuid,
    )
    store.close()
    local_domain = paths.runtime / "backend" / "local-domain"
    local_domain.mkdir(parents=True)
    local_domain_databases = [
        local_domain / "inventory.db",
        local_domain / "device_state.db",
        local_domain / "workflow_history.db",
        local_domain / "edge_authority.db",
    ]
    for database in local_domain_databases:
        database.write_bytes(b"stale")
        database.with_name(f"{database.name}-wal").write_bytes(b"stale-wal")

    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)
    stopped: list[str] = []
    host._stop_component = lambda name: stopped.append(name) or {}  # type: ignore[method-assign]
    host._start_backend = lambda parameters: {"parameters": parameters}  # type: ignore[method-assign]

    result = host._dispatch("local.reset-state", {"runtimeMode": "normal"})

    assert result == {"parameters": {"runtimeMode": "normal"}}
    assert stopped == ["edge", "backend"]
    reopened = EdgeControlStore(str(state_path))
    assert reopened.get_or_create_instance_uuid() == instance_uuid
    assert reopened.get_job(job_uuid) is None
    reopened.close()
    assert all(not database.exists() for database in local_domain_databases)
    assert all(
        not database.with_name(f"{database.name}-wal").exists()
        for database in local_domain_databases
    )
    host.close()


def test_host_restart_adopts_a_ready_backend_without_stopping_it(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready_server = ThreadingHTTPServer(("127.0.0.1", 0), _ReadyHandler)
    threading.Thread(target=ready_server.serve_forever, daemon=True).start()
    ready_port = int(ready_server.server_address[1])
    paths = WorkspacePaths.resolve(workspace)
    token = ensure_local_token(paths)

    def fake_backend(*_args: object, **_kwargs: object) -> LaunchPlan:
        return LaunchPlan(
            component="backend",
            command=(sys.executable, "-c", "import time; time.sleep(120)"),
            cwd=workspace,
            environment=dict(os.environ),
            generation="adopted-generation",
            log_path=paths.logs / "adopted.log",
            address=f"http://127.0.0.1:{ready_port}",
            ready_url=f"http://127.0.0.1:{ready_port}/api/v1/health",
            metadata={"runtimeMode": "normal"},
        )

    monkeypatch.setattr(
        "unilabos.workspace_host.host.resolve_backend_launch",
        fake_backend,
    )
    original = WorkspaceHost(paths, token, readiness_timeout=2.0)
    original.publish_endpoint("http://127.0.0.1:1")
    original._start_backend({})
    pid = int(original.snapshot()["components"]["backend"]["pid"])
    original.close()
    try:
        assert _pid_running(pid)
        recovered = WorkspaceHost(paths, token, readiness_timeout=2.0)
        assert recovered.snapshot()["components"]["backend"]["phase"] == "ready"
        assert recovered.snapshot()["components"]["backend"]["pid"] == pid
        recovered.close()
        original._stop_component("backend")
        assert not _pid_running(pid)
    finally:
        if _pid_running(pid):
            os.kill(pid, 9)
        ready_server.shutdown()
        ready_server.server_close()


def test_backend_crash_is_supervised_into_a_new_process_generation(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ready Local Backend is a supervised service, not a UI child process."""

    ready_server = ThreadingHTTPServer(("127.0.0.1", 0), _ReadyHandler)
    threading.Thread(target=ready_server.serve_forever, daemon=True).start()
    ready_port = int(ready_server.server_address[1])
    paths = WorkspacePaths.resolve(workspace)
    token = ensure_local_token(paths)
    launches = 0

    def fake_backend(*_args: object, **_kwargs: object) -> LaunchPlan:
        nonlocal launches
        launches += 1
        return LaunchPlan(
            component="backend",
            command=(sys.executable, "-c", "import time; time.sleep(120)"),
            cwd=workspace,
            environment=dict(os.environ),
            generation=f"backend-generation-{launches}",
            log_path=paths.logs / f"backend-{launches}.log",
            address=f"http://127.0.0.1:{ready_port}",
            ready_url=f"http://127.0.0.1:{ready_port}/api/v1/health",
            metadata={
                "runtimeMode": "normal",
                "domainMode": "local",
                "stateDirectory": str(paths.runtime / "backend" / "local-domain"),
            },
        )

    monkeypatch.setattr(
        "unilabos.workspace_host.host.resolve_backend_launch",
        fake_backend,
    )
    host = WorkspaceHost(paths, token, readiness_timeout=2.0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_type(host))
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    host.publish_endpoint(endpoint)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = WorkspaceHostClient(paths, endpoint, token)
    recovered_pid: int | None = None
    try:
        started = client.submit("backend.start", operation_id="crash-start")
        client.wait(str(started["operationId"]), timeout=5)
        before = client.snapshot()["components"]["backend"]
        first_pid = int(before["pid"])
        os.kill(first_pid, signal.SIGKILL)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            backend = client.snapshot()["components"]["backend"]
            if backend["phase"] == "ready" and backend["pid"] != first_pid:
                recovered_pid = int(backend["pid"])
                assert backend["generation"] == "backend-generation-2"
                assert backend["metadata"]["stateDirectory"] == str(
                    paths.runtime / "backend" / "local-domain"
                )
                break
            time.sleep(0.05)
        assert recovered_pid is not None
        assert "backend.recovery.succeeded" in paths.audit.read_text(encoding="utf-8")
    finally:
        try:
            stopped = client.submit("backend.stop", operation_id="crash-cleanup")
            client.wait(str(stopped["operationId"]), timeout=5)
        except WorkspaceHostError:
            if recovered_pid and _pid_running(recovered_pid):
                os.kill(recovered_pid, signal.SIGKILL)
        server.shutdown()
        server.server_close()
        host.close()
        ready_server.shutdown()
        ready_server.server_close()


def test_renderer_registration_is_host_owned_and_detachable(workspace: Path) -> None:
    paths = WorkspacePaths.resolve(workspace)
    host = WorkspaceHost(paths, ensure_local_token(paths))
    attached = host._dispatch(
        "renderer.attach",
        {"pid": os.getpid(), "address": "http://127.0.0.1:3100"},
    )
    assert attached["components"]["renderer"] == {
        "name": "renderer",
        "phase": "ready",
        "pid": os.getpid(),
        "address": "http://127.0.0.1:3100",
        "generation": str(os.getpid()),
        "logPath": None,
        "diagnostic": None,
        "capabilities": [
            "workbench-ui",
            "theia-rpc",
            "material-scene-inspect",
            "material-scene-capture",
            "material-scene-reload",
        ],
        "metadata": {
            "automationBaseUrl": "http://127.0.0.1:3100/__unilab_renderer/v1",
            "automationContract": "unilab-material-renderer/v1",
        },
    }
    detached = host._dispatch("renderer.detach", {"pid": os.getpid()})
    assert detached["components"]["renderer"]["phase"] == "idle"
    host.close()


def _wait_operation(host: WorkspaceHost, operation_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        operation = host.operation(operation_id)
        if operation["phase"] in {"succeeded", "failed"}:
            return operation
        time.sleep(0.01)
    raise AssertionError(f"operation did not finish: {operation_id}")


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _argument_value(command: tuple[str, ...], name: str) -> str:
    return command[command.index(name) + 1]


class _ReadyHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/api/v1/health":
            payload: object = {"status": "ok"}
        elif self.path.startswith("/api/v1/workflow-node-templates?"):
            payload = {
                "code": 0,
                "data": {
                    "items": [
                        {"uuid": "material-source", "node_type": "material_source"}
                    ]
                },
            }
        elif self.path == "/api/v1/resource-templates?page=1&page_size=1":
            payload = {"code": 0, "data": {"items": [{"uuid": "resource"}]}}
        elif self.path == "/api/v1/workspace/package-mounts":
            payload = {
                "code": 0,
                "data": {
                    "schemaVersion": "workspace-package-mounts/v1",
                    "items": [{"packageId": "fixture"}],
                },
            }
        else:
            payload = {"code": 0, "data": {"items": []}}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _RendererHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/":
            body = b"workbench"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return
