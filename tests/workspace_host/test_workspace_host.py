"""AIW-02 Workspace Host lifecycle, idempotency, and discovery contracts."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from unilabos.workspace_host.client import WorkspaceHostClient
from unilabos.workspace_host.discovery import WorkspaceHostLock, ensure_local_token
from unilabos.workspace_host.host import WorkspaceHost, _handler_type
from unilabos.workspace_host.launch import LaunchPlan
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
        "capabilities": ["workbench-ui", "theia-rpc"],
        "metadata": {},
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
        elif self.path == "/api/v1/resource-templates?limit=1":
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
