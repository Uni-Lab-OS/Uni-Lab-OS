"""Managed Runtime Supervisor 公共 HTTP 控制面的进程级跟踪测试。"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="Windows runtime-prefix 可执行文件解析将在后续平台轮次覆盖",
)

_TOKEN = "managed-runtime-integration-token"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class _Response:
    status: int
    body: dict[str, object]


@dataclass
class _RunningSupervisor:
    base_url: str
    process: subprocess.Popen[bytes]
    log_path: Path

    def logs(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _request(
    running: _RunningSupervisor,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
) -> _Response:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        f"{running.base_url}{path}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=1.0) as response:
            raw = response.read()
            return _Response(
                status=int(response.status),
                body=json.loads(raw) if raw else {},
            )
    except HTTPError as error:
        raw = error.read()
        return _Response(
            status=int(error.code),
            body=json.loads(raw) if raw else {},
        )


def _wait_for_status(
    running: _RunningSupervisor,
    expected: str,
    *,
    timeout: float = 10.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last_observation: object = None
    while time.monotonic() < deadline:
        if running.process.poll() is not None:
            pytest.fail(
                "unilab-supervisor exited before reaching the expected status\n"
                f"expected={expected!r} exit={running.process.returncode}\n"
                f"{running.logs()}"
            )
        try:
            response = _request(running, "GET", "/v1/status")
            last_observation = response
            if response.status == 200 and response.body.get("status") == expected:
                return response.body
        except (TimeoutError, URLError) as error:
            last_observation = error
        time.sleep(0.05)
    pytest.fail(
        "unilab-supervisor did not reach the expected status\n"
        f"expected={expected!r} last={last_observation!r}\n{running.logs()}"
    )


def _write_fake_unilab(runtime_prefix: Path, worker_pid_path: Path) -> None:
    executable = runtime_prefix / "bin" / "unilab"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        f"""#!{sys.executable}
import os
import signal
import time
from pathlib import Path

Path({str(worker_pid_path)!r}).write_text(str(os.getpid()), encoding="utf-8")
stopping = False


def stop(*_args):
    global stopping
    stopping = True


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)
while not stopping:
    time.sleep(0.05)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)


def _terminate_pid(pid_path: Path) -> None:
    if not pid_path.exists():
        return
    try:
        pid = int(pid_path.read_text(encoding="utf-8"))
        os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        return


@contextmanager
def _start_supervisor(
    tmp_path: Path,
    runtime_prefix: Path,
    worker_pid_path: Path,
) -> Iterator[_RunningSupervisor]:
    port = _free_port()
    log_path = tmp_path / "supervisor.log"
    command = [
        sys.executable,
        "-m",
        "unilabos.managed_runtime.supervisor",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--token",
        _TOKEN,
        "--runtime-prefix",
        str(runtime_prefix),
    ]
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(_REPOSITORY_ROOT), existing_pythonpath) if part
    )
    with log_path.open("wb") as log_stream:
        process = subprocess.Popen(
            command,
            cwd=tmp_path,
            env=environment,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        running = _RunningSupervisor(
            base_url=f"http://127.0.0.1:{port}",
            process=process,
            log_path=log_path,
        )
        try:
            yield running
        finally:
            if process.poll() is None:
                try:
                    _request(running, "DELETE", "/v1/simulators/current")
                    _request(running, "DELETE", "/v1/workers/current")
                except (TimeoutError, URLError):
                    pass
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
            _terminate_pid(worker_pid_path)


def test_supervisor_controls_one_managed_unilab_worker_over_http(
    tmp_path: Path,
) -> None:
    runtime_prefix = tmp_path / "runtime-prefix"
    worker_pid_path = tmp_path / "fake-unilab.pid"
    _write_fake_unilab(runtime_prefix, worker_pid_path)

    workspace = tmp_path / "workspace"
    working_dir = tmp_path / "runtime-work"
    workspace.mkdir()
    working_dir.mkdir()
    graph_path = workspace / "graph.json"
    config_path = workspace / "local_config.py"
    graph_path.write_text("{}\n", encoding="utf-8")
    config_path.write_text("# Managed Runtime integration fixture\n", encoding="utf-8")

    with _start_supervisor(
        tmp_path,
        runtime_prefix,
        worker_pid_path,
    ) as supervisor:
        _wait_for_status(supervisor, "idle")

        start = _request(
            supervisor,
            "POST",
            "/v1/workers",
            {
                "workspace_path": str(workspace),
                "graph_path": str(graph_path),
                "config_path": str(config_path),
                "working_dir": str(working_dir),
                "backend": "simple",
            },
        )
        assert 200 <= start.status < 300, start.body
        _wait_for_status(supervisor, "running")

        stop = _request(supervisor, "DELETE", "/v1/workers/current")
        assert 200 <= stop.status < 300, stop.body
        _wait_for_status(supervisor, "idle")


def test_supervisor_restart_marks_previous_worker_interrupted_without_replay(
    tmp_path: Path,
) -> None:
    runtime_prefix = tmp_path / "runtime-prefix"
    worker_pid_path = tmp_path / "fake-unilab.pid"
    _write_fake_unilab(runtime_prefix, worker_pid_path)

    workspace = tmp_path / "workspace"
    working_dir = tmp_path / "runtime-work"
    workspace.mkdir()
    working_dir.mkdir()
    graph_path = workspace / "graph.json"
    config_path = workspace / "local_config.py"
    graph_path.write_text("{}\n", encoding="utf-8")
    config_path.write_text("# Managed Runtime restart fixture\n", encoding="utf-8")

    with _start_supervisor(
        tmp_path,
        runtime_prefix,
        worker_pid_path,
    ) as supervisor:
        _wait_for_status(supervisor, "idle")
        start = _request(
            supervisor,
            "POST",
            "/v1/workers",
            {
                "workspace_path": str(workspace),
                "graph_path": str(graph_path),
                "config_path": str(config_path),
                "working_dir": str(working_dir),
                "backend": "simple",
            },
        )
        assert 200 <= start.status < 300, start.body
        _wait_for_status(supervisor, "running")
        _wait_for_file(worker_pid_path)
        original_worker_pid = worker_pid_path.read_text(encoding="utf-8")
        os.killpg(supervisor.process.pid, signal.SIGKILL)
        supervisor.process.wait(timeout=5)

    with _start_supervisor(
        tmp_path,
        runtime_prefix,
        worker_pid_path,
    ) as restarted:
        status = _wait_for_status(restarted, "interrupted")
        assert status["worker"] is None
        time.sleep(0.2)
        assert worker_pid_path.read_text(encoding="utf-8") == original_worker_pid


def test_supervisor_controls_source_plc_sim_independently_from_worker(
    tmp_path: Path,
) -> None:
    runtime_prefix = tmp_path / "runtime-prefix"
    worker_pid_path = tmp_path / "fake-unilab.pid"
    _write_fake_unilab(runtime_prefix, worker_pid_path)
    python_executable = runtime_prefix / "bin" / "python"
    python_executable.write_text(
        f'#!/bin/sh\nexec {sys.executable!s} "$@"\n',
        encoding="utf-8",
    )
    python_executable.chmod(0o755)

    simulator_root = tmp_path / "PLC-Sim" / "OpcUaSim"
    backend_path = simulator_root / "gui" / "backend.py"
    backend_path.parent.mkdir(parents=True)
    (backend_path.parent / "__init__.py").write_text("", encoding="utf-8")
    simulator_pid_path = tmp_path / "fake-simulator.pid"
    backend_path.write_text(
        f"""import os
import signal
import time
from pathlib import Path

Path({str(simulator_pid_path)!r}).write_text(str(os.getpid()), encoding="utf-8")
stopping = False


def stop(*_args):
    global stopping
    stopping = True


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)
while not stopping:
    time.sleep(0.05)
""",
        encoding="utf-8",
    )

    with _start_supervisor(
        tmp_path,
        runtime_prefix,
        worker_pid_path,
    ) as supervisor:
        _wait_for_status(supervisor, "idle")
        started = _request(
            supervisor,
            "POST",
            "/v1/simulators",
            {"source_path": str(simulator_root.parent)},
        )
        assert 200 <= started.status < 300, started.body
        _wait_for_file(simulator_pid_path)
        status = _request(supervisor, "GET", "/v1/status")
        assert status.body["status"] == "idle"
        assert status.body["simulator"] == {
            "status": "running",
            "pid": int(simulator_pid_path.read_text(encoding="utf-8")),
            "error": None,
        }

        stopped = _request(
            supervisor,
            "DELETE",
            "/v1/simulators/current",
        )
        assert 200 <= stopped.status < 300, stopped.body
        assert stopped.body["simulator"] == {
            "status": "idle",
            "pid": None,
            "error": None,
        }


def _wait_for_file(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.02)
    pytest.fail(f"worker fixture did not create {path}")
