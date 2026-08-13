"""Per-workspace lifecycle authority exposed through an authenticated loopback API."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import queue
import signal
import subprocess
import threading
import time
import traceback
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from .discovery import WorkspaceHostLock, ensure_local_token
from .launch import (
    LaunchPlan,
    resolve_backend_launch,
    resolve_edge_launch,
    resolve_plc_launch,
)
from .model import (
    COMPONENT_NAMES,
    SCHEMA_VERSION,
    WorkspaceHostError,
    WorkspacePaths,
    atomic_write_json,
    idle_component,
    read_json,
    utc_timestamp,
)

_STOP_TIMEOUT_SECONDS = 10.0
_READINESS_TIMEOUT_SECONDS = 90.0
_MAX_BODY_BYTES = 1024 * 1024


class WorkspaceHost:
    """Own component processes, operations, state revision, logs, and audit."""

    def __init__(
        self,
        paths: WorkspacePaths,
        token: str,
        *,
        readiness_timeout: float = _READINESS_TIMEOUT_SECONDS,
    ) -> None:
        self.paths = paths
        self.token = token
        self.readiness_timeout = readiness_timeout
        self._lock = threading.RLock()
        self._operation_queue: queue.Queue[str | None] = queue.Queue()
        self._operation_worker = threading.Thread(
            target=self._run_operations,
            name="unilab-workspace-host-operations",
            daemon=True,
        )
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._revision = 0
        self._cursor = 0
        self._endpoint = ""
        self._configuration = self._initial_configuration()
        self._components = {name: idle_component(name) for name in COMPONENT_NAMES}
        self._restore_interrupted_components()
        self._closed = threading.Event()
        self._monitor = threading.Thread(
            target=self._monitor_processes,
            name="unilab-workspace-host-monitor",
            daemon=True,
        )
        self._operation_worker.start()

    def publish_endpoint(self, endpoint: str) -> None:
        with self._lock:
            self._endpoint = endpoint
            self._publish_locked("host.ready", {"endpoint": endpoint})
        self._monitor.start()

    def authorized(self, authorization: str | None) -> bool:
        expected = f"Bearer {self.token}"
        return authorization is not None and hmac.compare_digest(
            authorization,
            expected,
        )

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            self._refresh_processes_locked()
            return self._snapshot_locked()

    def submit(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise WorkspaceHostError("invalid_request", "操作请求必须是 JSON object")
        operation_id = _required_text(payload, "operationId")
        command = _required_text(payload, "command")
        parameters = payload.get("parameters") or {}
        if not isinstance(parameters, dict):
            raise WorkspaceHostError("invalid_request", "parameters 必须是 object")
        expected_revision = payload.get("expectedRevision")
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
        ):
            raise WorkspaceHostError("invalid_request", "expectedRevision 必须是整数")
        normalized = {
            "operationId": operation_id,
            "command": command,
            "parameters": parameters,
            "expectedRevision": expected_revision,
        }
        request_hash = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        operation_path = self.paths.operations / f"{operation_id}.json"
        with self._lock:
            if operation_path.exists():
                existing = read_json(operation_path)
                if existing.get("requestHash") != request_hash:
                    raise WorkspaceHostError(
                        "operation_conflict",
                        f"operationId 已被不同请求使用：{operation_id}",
                    )
                return existing
            if expected_revision is not None and expected_revision != self._revision:
                raise WorkspaceHostError(
                    "revision_conflict",
                    "Workspace Host revision 已变化",
                    details={"expected": expected_revision, "actual": self._revision},
                )
            operation: dict[str, object] = {
                "schemaVersion": SCHEMA_VERSION,
                "operationId": operation_id,
                "command": command,
                "parameters": parameters,
                "requestHash": request_hash,
                "phase": "pending",
                "submittedAt": utc_timestamp(),
                "startedAt": None,
                "finishedAt": None,
                "result": None,
                "error": None,
            }
            atomic_write_json(operation_path, operation)
            self._audit_locked("operation.submitted", operation)
        # One FIFO worker is the mutation lane shared by CLI, Workbench and
        # future MCP clients. It preserves submission order and prevents two
        # callers from spawning or stopping the same component concurrently.
        self._operation_queue.put(operation_id)
        return operation

    def operation(self, operation_id: str) -> dict[str, object]:
        if not operation_id or "/" in operation_id or "\\" in operation_id:
            raise WorkspaceHostError("invalid_request", "operationId 无效")
        path = self.paths.operations / f"{operation_id}.json"
        try:
            return read_json(path)
        except WorkspaceHostError as error:
            if error.code == "host_not_found":
                raise WorkspaceHostError(
                    "operation_not_found",
                    f"操作不存在：{operation_id}",
                ) from error
            raise

    def log_tail(self, component: str, max_bytes: int) -> dict[str, object]:
        if component not in COMPONENT_NAMES:
            raise WorkspaceHostError("component_invalid", f"未知组件：{component}")
        if max_bytes < 1 or max_bytes > 4 * 1024 * 1024:
            raise WorkspaceHostError("invalid_request", "maxBytes 超出范围")
        with self._lock:
            value = self._components[component].get("logPath")
        if not isinstance(value, str) or not value:
            return {"component": component, "logPath": None, "content": ""}
        path = Path(value)
        try:
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - max_bytes))
                content = stream.read().decode("utf-8", errors="replace")
        except FileNotFoundError:
            content = ""
        return {"component": component, "logPath": str(path), "content": content}

    def close(self) -> None:
        """Stop only Host bookkeeping; managed components intentionally survive."""

        self._closed.set()
        self._operation_queue.put(None)
        if self._operation_worker.is_alive():
            self._operation_worker.join(timeout=1.0)
        if self._monitor.is_alive():
            self._monitor.join(timeout=1.0)

    def _run_operations(self) -> None:
        while True:
            operation_id = self._operation_queue.get()
            try:
                if operation_id is None:
                    return
                self._execute_operation(operation_id)
            finally:
                self._operation_queue.task_done()

    def _execute_operation(self, operation_id: str) -> None:
        operation_path = self.paths.operations / f"{operation_id}.json"
        with self._lock:
            operation = read_json(operation_path)
            operation["phase"] = "running"
            operation["startedAt"] = utc_timestamp()
            atomic_write_json(operation_path, operation)
            self._audit_locked("operation.started", operation)
        try:
            result = self._dispatch(
                str(operation["command"]),
                dict(operation.get("parameters") or {}),
            )
        except WorkspaceHostError as error:
            phase = "failed"
            result = None
            failure: object = error.as_dict()
        except BaseException as error:  # noqa: BLE001 - operation boundary normalizes.
            phase = "failed"
            result = None
            failure = {
                "code": "internal_error",
                "message": str(error),
                "traceback": traceback.format_exc(limit=20),
            }
        else:
            phase = "succeeded"
            failure = None
        with self._lock:
            operation = read_json(operation_path)
            operation["phase"] = phase
            operation["finishedAt"] = utc_timestamp()
            operation["result"] = result
            operation["error"] = failure
            atomic_write_json(operation_path, operation)
            self._audit_locked(f"operation.{phase}", operation)

    def _dispatch(self, command: str, parameters: dict[str, object]) -> object:
        if command == "backend.start":
            return self._start_backend(parameters)
        if command == "backend.stop":
            return self._stop_component("backend")
        if command == "backend.restart":
            self._stop_component("backend")
            return self._start_backend(parameters)
        if command == "os.start":
            return self._start_edge()
        if command == "os.stop":
            return self._stop_component("edge")
        if command == "os.restart":
            self._stop_component("edge")
            return self._start_edge()
        if command == "local.reset-state":
            self._stop_component("edge")
            self._stop_component("backend")
            return self._start_backend(parameters)
        if command == "plc.start":
            return self._start_plc()
        if command == "plc.stop":
            return self._stop_component("plc")
        if command == "plc.restart":
            self._stop_component("plc")
            return self._start_plc()
        if command == "configuration.update":
            return self._update_configuration(parameters)
        if command == "renderer.attach":
            return self._attach_renderer(parameters)
        if command == "renderer.detach":
            return self._detach_renderer(parameters)
        raise WorkspaceHostError(
            "command_unknown", f"未知 Workspace Host 命令：{command}"
        )

    def _start_backend(self, parameters: dict[str, object]) -> dict[str, object]:
        with self._lock:
            if self._components["backend"]["phase"] == "ready":
                return self._snapshot_locked()
            configuration = dict(self._configuration)
        plan = resolve_backend_launch(
            self.paths,
            graph_path=_optional_text(parameters.get("graphPath"))
            or _optional_text(configuration.get("graphPath")),
            runtime_mode=_optional_text(parameters.get("runtimeMode"))
            or _optional_text(configuration.get("runtimeMode")),
        )
        self._spawn(plan)
        package_mounts = self._wait_backend_ready(plan)
        with self._lock:
            self._components["backend"]["phase"] = "ready"
            self._components["backend"]["diagnostic"] = None
            metadata = self._components["backend"].setdefault("metadata", {})
            assert isinstance(metadata, dict)
            metadata["packageMounts"] = package_mounts
            self._components["backend"]["capabilities"] = [
                "authoring",
                "inventory",
                "workflow-run",
            ]
            self._publish_locked("backend.ready", {"generation": plan.generation})
            return self._snapshot_locked()

    def _start_edge(self) -> dict[str, object]:
        with self._lock:
            if self._components["edge"]["phase"] == "ready":
                return self._snapshot_locked()
            backend = dict(self._components["backend"])
        if backend.get("phase") != "ready":
            self._start_backend({})
            with self._lock:
                backend = dict(self._components["backend"])
        plan = resolve_edge_launch(self.paths, backend)
        self._spawn(plan)
        ready_file = Path(str(plan.metadata["readyFilePath"]))
        deadline = time.monotonic() + self.readiness_timeout
        while time.monotonic() < deadline:
            with self._lock:
                process = self._processes.get("edge")
                if process is None or process.poll() is not None:
                    raise WorkspaceHostError("os_start_failed", "OS 在就绪前退出")
            if ready_file.is_file():
                with self._lock:
                    self._components["edge"]["phase"] = "ready"
                    self._components["edge"]["diagnostic"] = None
                    self._components["edge"]["capabilities"] = ["device-control"]
                    self._publish_locked("os.ready", {"generation": plan.generation})
                    return self._snapshot_locked()
            time.sleep(0.1)
        self._stop_component("edge")
        raise WorkspaceHostError("os_readiness_failed", "等待 OS 就绪超时")

    def _start_plc(self) -> dict[str, object]:
        with self._lock:
            if self._components["plc"]["phase"] == "ready":
                return self._snapshot_locked()
        plan = resolve_plc_launch(self.paths)
        self._spawn(plan)
        deadline = time.monotonic() + self.readiness_timeout
        while time.monotonic() < deadline:
            with self._lock:
                process = self._processes.get("plc")
                if process is None or process.poll() is not None:
                    raise WorkspaceHostError("plc_start_failed", "PLC-Sim 在就绪前退出")
            try:
                with urlopen(str(plan.ready_url), timeout=1.0) as response:
                    response.read()
                if response.status == 200:
                    break
            except (OSError, URLError):
                time.sleep(0.2)
        else:
            self._stop_component("plc")
            raise WorkspaceHostError(
                "plc_readiness_failed", "等待 PLC-Sim API 就绪超时"
            )
        metadata = plan.metadata
        self._post_json(
            f"{metadata['guiUrl']}/api/server/start",
            {
                "csv": metadata["variableTablePath"],
                "host": "127.0.0.1",
                "port": metadata["opcUaPort"],
            },
        )
        self._post_json(
            f"{metadata['guiUrl']}/api/agent/start",
            {
                "profile": metadata["handshakeProfile"],
                "host": "127.0.0.1",
                "port": metadata["opcUaPort"],
                "csv": metadata["variableTablePath"],
            },
        )
        with self._lock:
            component = self._components["plc"]
            component["phase"] = "ready"
            component["diagnostic"] = None
            component["capabilities"] = ["opcua-simulation", "handshake"]
            self._publish_locked("plc.ready", {"generation": plan.generation})
            return self._snapshot_locked()

    def _post_json(self, url: str, payload: dict[str, object]) -> None:
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.readiness_timeout) as response:
                detail = response.read().decode("utf-8", errors="replace")
        except (OSError, URLError) as error:
            raise WorkspaceHostError("plc_configuration_failed", str(error)) from error
        if not 200 <= response.status < 300:
            raise WorkspaceHostError("plc_configuration_failed", detail)

    def _spawn(self, plan: LaunchPlan) -> None:
        with self._lock:
            component = self._components[plan.component]
            component.update(
                {
                    "phase": "starting",
                    "pid": None,
                    "address": plan.address,
                    "generation": plan.generation,
                    "logPath": str(plan.log_path),
                    "diagnostic": None,
                    "capabilities": [],
                    "metadata": dict(plan.metadata),
                }
            )
            self._publish_locked(
                f"{plan.component}.starting",
                {"generation": plan.generation},
            )
        plan.log_path.parent.mkdir(parents=True, exist_ok=True)
        with plan.log_path.open("ab", buffering=0) as log_stream:
            try:
                process = subprocess.Popen(
                    list(plan.command),
                    cwd=plan.cwd,
                    env=plan.environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log_stream,
                    stderr=subprocess.STDOUT,
                    start_new_session=os.name != "nt",
                    creationflags=(
                        subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                    ),
                )
            except BaseException as error:
                with self._lock:
                    component.update(
                        {
                            "phase": "failed",
                            "diagnostic": str(error),
                        }
                    )
                    self._publish_locked(
                        f"{plan.component}.failed", {"error": str(error)}
                    )
                raise WorkspaceHostError(
                    f"{plan.component}_start_failed",
                    f"{plan.component} 启动失败：{error}",
                ) from error
        with self._lock:
            self._processes[plan.component] = process
            component["pid"] = process.pid
            self._publish_locked(
                f"{plan.component}.spawned",
                {"pid": process.pid, "generation": plan.generation},
            )

    def _wait_backend_ready(self, plan: LaunchPlan) -> dict[str, object]:
        if not plan.address:
            raise WorkspaceHostError("backend_start_failed", "Backend 地址缺失")
        probes = (
            ("/api/v1/health", _health_ready),
            ("/api/v1/devices", _successful_envelope),
            ("/api/v1/workflow-node-templates", _successful_envelope),
            (
                "/api/v1/workflow-node-templates?limit=100&node_type=material_source",
                _material_source_catalog_ready,
            ),
            ("/api/v1/resource-templates?limit=1", _nonempty_catalog_ready),
        )
        for path, accepts in probes:
            self._wait_backend_payload(plan, path, accepts)
        payload = self._wait_backend_payload(
            plan,
            "/api/v1/workspace/package-mounts",
            _package_mounts_ready,
        )
        data = payload.get("data")
        assert isinstance(data, dict)
        return data

    def _wait_backend_payload(
        self,
        plan: LaunchPlan,
        path: str,
        accepts: Callable[[dict[str, object]], bool],
    ) -> dict[str, object]:
        deadline = time.monotonic() + self.readiness_timeout
        while time.monotonic() < deadline:
            with self._lock:
                process = self._processes.get(plan.component)
                if process is None or process.poll() is not None:
                    raise WorkspaceHostError(
                        "backend_start_failed",
                        f"Backend 在 {path} 就绪前退出",
                    )
            try:
                with urlopen(f"{plan.address}{path}", timeout=1.0) as response:
                    payload = json.loads(response.read())
                if (
                    response.status == 200
                    and isinstance(payload, dict)
                    and accepts(payload)
                ):
                    return payload
            except (OSError, ValueError, URLError):
                pass
            time.sleep(0.2)
        self._stop_component(plan.component)
        raise WorkspaceHostError(
            "backend_readiness_failed",
            f"等待 Backend 就绪超时：{path}",
        )

    def _stop_component(self, name: str) -> dict[str, object]:
        with self._lock:
            component = self._components[name]
            pid = component.get("pid")
            process = self._processes.pop(name, None)
            if not isinstance(pid, int) or pid < 1:
                component.update(idle_component(name))
                self._publish_locked(f"{name}.idle", {})
                return self._snapshot_locked()
            component["phase"] = "stopping"
            self._publish_locked(f"{name}.stopping", {"pid": pid})
        _terminate_process_tree(pid, process)
        with self._lock:
            previous = dict(component)
            component.update(idle_component(name))
            component["generation"] = previous.get("generation")
            component["logPath"] = previous.get("logPath")
            component["metadata"] = previous.get("metadata", {})
            self._publish_locked(f"{name}.idle", {"pid": pid})
            return self._snapshot_locked()

    def _update_configuration(self, parameters: dict[str, object]) -> dict[str, object]:
        allowed = {
            "graphPath",
            "runtimeMode",
            "plcSimulatorProjectPath",
            "plcVariableTablePath",
            "plcHandshakeProfile",
        }
        unknown = sorted(set(parameters) - allowed)
        if unknown:
            raise WorkspaceHostError(
                "configuration_invalid",
                f"未知配置字段：{', '.join(unknown)}",
            )
        with self._lock:
            self._configuration.update(parameters)
            payload = {"schemaVersion": 1, **self._configuration}
            atomic_write_json(self.paths.environment, payload)
            self._publish_locked("configuration.updated", parameters)
            return self._snapshot_locked()

    def _attach_renderer(self, parameters: dict[str, object]) -> dict[str, object]:
        pid = parameters.get("pid")
        address = parameters.get("address")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
            raise WorkspaceHostError("renderer_invalid", "Renderer PID 无效")
        if not isinstance(address, str) or not address.startswith("http://127.0.0.1:"):
            raise WorkspaceHostError(
                "renderer_invalid", "Renderer 必须使用 loopback 地址"
            )
        if not _pid_exists(pid):
            raise WorkspaceHostError("renderer_invalid", f"Renderer 进程不存在：{pid}")
        with self._lock:
            generation = str(parameters.get("generation") or pid)
            self._components["renderer"].update(
                {
                    "phase": "ready",
                    "pid": pid,
                    "address": address,
                    "generation": generation,
                    "logPath": None,
                    "diagnostic": None,
                    "capabilities": ["workbench-ui", "theia-rpc"],
                    "metadata": {},
                }
            )
            self._publish_locked("renderer.attached", {"pid": pid, "address": address})
            return self._snapshot_locked()

    def _detach_renderer(self, parameters: dict[str, object]) -> dict[str, object]:
        requested_pid = parameters.get("pid")
        with self._lock:
            current_pid = self._components["renderer"].get("pid")
            if requested_pid is not None and requested_pid != current_pid:
                return self._snapshot_locked()
            self._components["renderer"].update(idle_component("renderer"))
            self._publish_locked("renderer.detached", {"pid": current_pid})
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, object]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "revision": self._revision,
            "eventCursor": self._cursor,
            "workspacePath": str(self.paths.workspace),
            "host": {
                "phase": "ready",
                "pid": os.getpid(),
                "endpoint": self._endpoint,
                "tokenPath": str(self.paths.token),
                "platform": sys_platform(),
            },
            "configuration": dict(self._configuration),
            "components": {
                name: dict(component) for name, component in self._components.items()
            },
            "updatedAt": utc_timestamp(),
        }

    def _publish_locked(self, event: str, details: object) -> None:
        self._revision += 1
        self._cursor += 1
        snapshot = self._snapshot_locked()
        atomic_write_json(self.paths.session, snapshot)
        self._audit_locked(event, details)

    def _audit_locked(self, event: str, details: object) -> None:
        record = {
            "schemaVersion": SCHEMA_VERSION,
            "cursor": self._cursor,
            "timestamp": utc_timestamp(),
            "event": event,
            "details": details,
        }
        self.paths.audit.parent.mkdir(parents=True, exist_ok=True)
        with self.paths.audit.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")

    def _monitor_processes(self) -> None:
        while not self._closed.wait(0.25):
            with self._lock:
                self._refresh_processes_locked()

    def _refresh_processes_locked(self) -> None:
        for name, process in tuple(self._processes.items()):
            return_code = process.poll()
            if return_code is None:
                continue
            self._processes.pop(name, None)
            component = self._components[name]
            if component["phase"] == "stopping":
                continue
            component["phase"] = "failed"
            component["diagnostic"] = f"进程意外退出，exit_code={return_code}"
            self._publish_locked(
                f"{name}.exited",
                {"pid": process.pid, "exitCode": return_code},
            )
        for name, component in self._components.items():
            if name in self._processes:
                continue
            pid = component.get("pid")
            if (
                component.get("phase") not in {"ready", "interrupted"}
                or not isinstance(pid, int)
                or pid < 1
                or _pid_exists(pid)
            ):
                continue
            component["phase"] = "failed"
            component["diagnostic"] = "已接管的进程不再存在"
            self._publish_locked(f"{name}.exited", {"pid": pid, "adopted": True})

    def _initial_configuration(self) -> dict[str, object]:
        try:
            payload = json.loads(self.paths.environment.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"graphPath": None, "runtimeMode": "normal"}
        if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
            return {"graphPath": None, "runtimeMode": "normal"}
        return {key: value for key, value in payload.items() if key != "schemaVersion"}

    def _restore_interrupted_components(self) -> None:
        try:
            previous = read_json(self.paths.session)
        except WorkspaceHostError:
            return
        if previous.get("schemaVersion") != SCHEMA_VERSION:
            return
        components = previous.get("components")
        if not isinstance(components, dict):
            return
        for name in COMPONENT_NAMES:
            value = components.get(name)
            if not isinstance(value, dict):
                continue
            pid = value.get("pid")
            if isinstance(pid, int) and pid > 0 and _pid_exists(pid):
                restored = dict(value)
                if self._component_is_ready(name, restored):
                    restored["phase"] = "ready"
                    restored["diagnostic"] = None
                else:
                    restored["phase"] = "interrupted"
                    restored["diagnostic"] = (
                        "Workspace Host 重启后发现遗留进程，但就绪性无法证明；"
                        "可通过同一控制面停止"
                    )
                self._components[name] = restored

    @staticmethod
    def _component_is_ready(name: str, component: dict[str, object]) -> bool:
        if name == "edge":
            metadata = component.get("metadata")
            ready_path = (
                metadata.get("readyFilePath") if isinstance(metadata, dict) else None
            )
            return isinstance(ready_path, str) and Path(ready_path).is_file()
        address = component.get("address")
        if not isinstance(address, str) or not address:
            return name == "renderer"
        path = "/api/v1/health" if name == "backend" else "/api/state"
        try:
            with urlopen(f"{address}{path}", timeout=0.5) as response:
                response.read()
            return response.status == 200
        except (OSError, URLError):
            return False


def _handler_type(host: WorkspaceHost) -> type[BaseHTTPRequestHandler]:
    class WorkspaceHostHandler(BaseHTTPRequestHandler):
        server_version = "UniLabWorkspaceHost/1"

        def do_GET(self) -> None:
            if not self._authorize():
                return
            try:
                if self.path == "/v1/snapshot":
                    self._json(HTTPStatus.OK, host.snapshot())
                    return
                if self.path.startswith("/v1/operations/"):
                    operation_id = self.path.removeprefix("/v1/operations/")
                    self._json(HTTPStatus.OK, host.operation(operation_id))
                    return
                if self.path.startswith("/v1/logs/"):
                    component, _, query = self.path.removeprefix("/v1/logs/").partition(
                        "?"
                    )
                    max_bytes = 64 * 1024
                    if query.startswith("maxBytes="):
                        max_bytes = int(query.removeprefix("maxBytes="))
                    self._json(HTTPStatus.OK, host.log_tail(component, max_bytes))
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})
            except WorkspaceHostError as error:
                self._json(_error_status(error), {"error": error.as_dict()})
            except (TypeError, ValueError):
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": {"code": "invalid_request", "message": "请求参数无效"}},
                )

        def do_POST(self) -> None:
            if not self._authorize():
                return
            if self.path != "/v1/operations":
                self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})
                return
            try:
                operation = host.submit(self._body())
                self._json(HTTPStatus.ACCEPTED, operation)
            except WorkspaceHostError as error:
                self._json(_error_status(error), {"error": error.as_dict()})

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _authorize(self) -> bool:
            if host.authorized(self.headers.get("Authorization")):
                return True
            self._json(
                HTTPStatus.UNAUTHORIZED,
                {"error": {"code": "unauthorized", "message": "token 无效"}},
            )
            return False

        def _body(self) -> object:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise WorkspaceHostError(
                    "invalid_request", "Content-Length 无效"
                ) from error
            if length < 1 or length > _MAX_BODY_BYTES:
                raise WorkspaceHostError("invalid_request", "请求体大小无效")
            try:
                return json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise WorkspaceHostError(
                    "invalid_request", "请求体不是有效 JSON"
                ) from error

        def _json(self, status: HTTPStatus, payload: object) -> None:
            body = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode()
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return WorkspaceHostHandler


def _error_status(error: WorkspaceHostError) -> HTTPStatus:
    if error.code in {"operation_not_found", "host_not_found"}:
        return HTTPStatus.NOT_FOUND
    if error.code in {
        "operation_conflict",
        "revision_conflict",
        "host_already_running",
    }:
        return HTTPStatus.CONFLICT
    return HTTPStatus.BAD_REQUEST


def _required_text(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceHostError("invalid_request", f"{field} 必须是非空字符串")
    if field == "operationId" and any(character in value for character in "/\\"):
        raise WorkspaceHostError("invalid_request", "operationId 无效")
    return value


def _health_ready(payload: dict[str, object]) -> bool:
    return payload.get("status") == "ok"


def _successful_envelope(payload: dict[str, object]) -> bool:
    return payload.get("code") == 0


def _catalog_items(payload: dict[str, object]) -> list[object]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    items = data.get("items")
    return items if isinstance(items, list) else []


def _material_source_catalog_ready(payload: dict[str, object]) -> bool:
    return payload.get("code") == 0 and any(
        isinstance(item, dict)
        and item.get("node_type") == "material_source"
        and isinstance(item.get("uuid"), str)
        for item in _catalog_items(payload)
    )


def _nonempty_catalog_ready(payload: dict[str, object]) -> bool:
    return payload.get("code") == 0 and bool(_catalog_items(payload))


def _package_mounts_ready(payload: dict[str, object]) -> bool:
    data = payload.get("data")
    return (
        payload.get("code") == 0
        and isinstance(data, dict)
        and data.get("schemaVersion") == "workspace-package-mounts/v1"
        and isinstance(data.get("items"), list)
        and bool(data["items"])
    )


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _terminate_process_tree(pid: int, process: subprocess.Popen[bytes] | None) -> None:
    if process is not None and process.poll() is not None:
        return
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot")
        if not system_root:
            raise WorkspaceHostError("stop_failed", "Windows 缺少 SystemRoot")
        completed = subprocess.run(
            [
                str(Path(system_root) / "System32" / "taskkill.exe"),
                "/PID",
                str(pid),
                "/T",
                "/F",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_STOP_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0 and _pid_exists(pid):
            raise WorkspaceHostError("stop_failed", f"无法停止进程树：{pid}")
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process is not None:
            if process.poll() is not None:
                return
        elif not _pid_exists(pid):
            return
        time.sleep(0.05)
    if (process is not None and process.poll() is None) or (
        process is None and _pid_exists(pid)
    ):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    if process is not None:
        try:
            process.wait(timeout=_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            raise WorkspaceHostError("stop_failed", f"进程树未退出：{pid}") from error


def sys_platform() -> str:
    import platform

    return platform.system().lower()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start one Uni-Lab Workspace Host")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1"])
    parser.add_argument("--port", default=0, type=int)
    parser.add_argument(
        "--readiness-timeout", default=_READINESS_TIMEOUT_SECONDS, type=float
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    paths = WorkspacePaths.resolve(args.workspace)
    paths.prepare()
    singleton = WorkspaceHostLock(paths)
    singleton.acquire()
    token = ensure_local_token(paths)
    host = WorkspaceHost(paths, token, readiness_timeout=args.readiness_timeout)
    server = ThreadingHTTPServer((args.host, args.port), _handler_type(host))
    endpoint = f"http://{args.host}:{server.server_address[1]}"
    host.publish_endpoint(endpoint)
    stopping = threading.Event()

    def stop_server(_signum: int, _frame: object) -> None:
        if stopping.is_set():
            return
        stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    for name in ("SIGINT", "SIGTERM"):
        value = getattr(signal, name, None)
        if value is not None:
            signal.signal(value, stop_server)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        host.close()
        singleton.release()


if __name__ == "__main__":
    main()
