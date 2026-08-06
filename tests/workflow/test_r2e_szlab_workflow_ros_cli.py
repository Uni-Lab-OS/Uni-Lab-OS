"""R2E public-contract tracer for a complete SZLab-shaped ROS Workflow.

The system under test is a real ``unilab`` console process.  This test never
imports the production composition, scheduler, HostNode, or Workflow store.
All setup, execution, and observation cross the frozen CLI, HTTP, ROS graph,
and process-log boundaries.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_WORKSPACE = _REPOSITORY_ROOT / "tests" / "fixtures" / "r2e_szlab_workspace"
_DEVICE_UUID = "64000000-0000-4000-8000-000000000001"
_DEVICE_ID = "r2e_szlab_mixer"
_WORKFLOW_UUID = "65000000-0000-4000-8000-000000000001"
_TERMINAL_TASK_STATUSES = {"succeeded", "failed", "canceled", "timeout"}


@dataclass(frozen=True)
class _Response:
    status: int
    body: dict[str, Any]


@dataclass
class _RunningOS:
    base_url: str
    process: subprocess.Popen[bytes]
    log_path: Path
    ros_domain_id: str

    def logs(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def log_tail(self, line_count: int = 120) -> str:
        return "\n".join(self.logs().splitlines()[-line_count:])


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _request(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> _Response:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        f"{base_url}{path}",
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=2.0) as response:
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


def _wait_for_health(running: _RunningOS, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        if running.process.poll() is not None:
            pytest.fail(
                "unilab CLI exited before health became ready\n"
                f"exit={running.process.returncode}\n{running.logs()}"
            )
        try:
            response = _request(running.base_url, "GET", "/api/v1/health")
            if response.status == 200:
                return
        except (TimeoutError, URLError) as error:
            last_error = error
        time.sleep(0.1)
    pytest.fail(
        "unilab CLI health did not become ready\n"
        f"last_error={last_error!r}\n{running.logs()}"
    )


@contextmanager
def _start_cli(
    tmp_path: Path,
    *,
    test_mode: bool = True,
) -> Iterator[_RunningOS]:
    workspace = tmp_path / "r2e-szlab"
    shutil.copytree(_FIXTURE_WORKSPACE, workspace)
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    shutil.copy(
        workspace / "local_config.py",
        runtime_root / "local_config.py",
    )
    port = _free_port()
    ros_domain_id = str(100 + (uuid4().int % 100))
    cli = Path(
        os.environ.get(
            "UNILAB_R2E_CLI",
            str(Path(sys.executable).with_name("unilab")),
        )
    )
    if not cli.is_file():
        pytest.fail(f"unilab console script is unavailable: {cli}")

    command = [
        str(cli),
        "--workspace",
        str(workspace),
        "--graph",
        str(workspace / "graph.json"),
        "--working_dir",
        str(runtime_root),
        "--backend",
        "ros",
        "--app_bridges",
        "fastapi",
        "--visual",
        "disable",
        "--port",
        str(port),
        "--disable_browser",
        "--skip_env_check",
        "--ros_domain_id",
        ros_domain_id,
    ]
    if test_mode:
        command.append("--test_mode")
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    environment["ROS_DOMAIN_ID"] = ros_domain_id
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(_REPOSITORY_ROOT), existing_pythonpath) if part
    )
    log_path = tmp_path / "unilab-os.log"
    with log_path.open("wb") as log_stream:
        process = subprocess.Popen(
            command,
            cwd=_REPOSITORY_ROOT,
            env=environment,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        running = _RunningOS(
            base_url=f"http://127.0.0.1:{port}",
            process=process,
            log_path=log_path,
            ros_domain_id=ros_domain_id,
        )
        try:
            _wait_for_health(running)
            yield running
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)


def _data(response: _Response, *, expected_status: int = 200) -> Any:
    assert response.status == expected_status, response.body
    assert response.body.get("code") == 0, response.body
    return response.body["data"]


def _template_summary(base_url: str, action_name: str) -> dict[str, Any]:
    deadline = time.monotonic() + 8.0
    response: _Response | None = None
    while time.monotonic() < deadline:
        response = _request(base_url, "GET", "/api/v1/workflow-node-templates")
        if response.status == 200:
            catalog = _data(response)
            matches = [item for item in catalog["items"] if item["name"] == action_name]
            assert len(matches) == 1, catalog
            return matches[0]
        time.sleep(0.1)
    assert response is not None
    return _data(response)


def _wait_for_device_catalog(base_url: str) -> dict[str, Any]:
    deadline = time.monotonic() + 8.0
    response: _Response | None = None
    while time.monotonic() < deadline:
        response = _request(base_url, "GET", "/api/v1/devices")
        if response.status == 200:
            return _data(response)
        time.sleep(0.1)
    assert response is not None
    return _data(response)


def _template_detail(base_url: str, template_uuid: str) -> dict[str, Any]:
    return _data(
        _request(
            base_url,
            "GET",
            f"/api/v1/workflow-node-templates/{template_uuid}",
        )
    )


def _handle(detail: dict[str, Any], *, io_type: str, handle_key: str) -> dict[str, Any]:
    matches = [
        item
        for item in detail["handles"]
        if item["io_type"] == io_type and item["handle_key"] == handle_key
    ]
    assert len(matches) == 1, detail
    return matches[0]


def _wait_for_terminal_task(
    base_url: str, task_uuid: str, timeout: float = 20.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = _request(base_url, "GET", f"/api/v1/workflow-tasks/{task_uuid}")
        last = _data(response)
        if last["status"] in _TERMINAL_TASK_STATUSES:
            return last
        time.sleep(0.05)
    pytest.fail(f"WorkflowTask did not become terminal: {last}")


def _ros_actions(running: _RunningOS) -> set[str]:
    environment = os.environ.copy()
    environment["ROS_DOMAIN_ID"] = running.ros_domain_id
    command = shutil.which("ros2", path=environment.get("PATH"))
    if command is None:
        command = str(Path(sys.executable).with_name("ros2"))
    completed = subprocess.run(
        [command, "action", "list"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
        env=environment,
    )
    return {line.strip() for line in completed.stdout.splitlines() if line.strip()}


def _assert_correlated_execution_logs(
    logs: str,
    *,
    task_uuid: str,
    jobs: list[dict[str, Any]],
) -> None:
    lines = logs.splitlines()
    for job in jobs:
        job_uuid = job["uuid"]
        assert any(
            task_uuid in line
            and job_uuid in line
            and _DEVICE_ID in line
            and job["action_name"] in line
            for line in lines
        ), f"missing correlated task/job/action log for {job_uuid}\n{logs}"
        assert any(
            job_uuid in line and "succeeded" in line.lower() for line in lines
        ), f"missing terminal Job log for {job_uuid}\n{logs}"
    assert any(task_uuid in line and "succeeded" in line.lower() for line in lines), (
        f"missing terminal Task log for {task_uuid}\n{logs}"
    )


@pytest.fixture(scope="module")
def running_os(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_RunningOS]:
    with _start_cli(tmp_path_factory.mktemp("r2e-szlab-cli")) as running:
        yield running


def test_unilab_cli_publishes_workspace_template_catalog(
    running_os: _RunningOS,
) -> None:
    assert _template_summary(running_os.base_url, "prepare")["name"] == "prepare"
    assert _template_summary(running_os.base_url, "finish")["name"] == "finish"


def test_unilab_cli_hostnode_resolves_short_graph_class_and_registers_ros_action(
    running_os: _RunningOS,
) -> None:
    devices = _wait_for_device_catalog(running_os.base_url)
    matches = [item for item in devices["items"] if item["id"] == _DEVICE_ID]
    assert len(matches) == 1, running_os.log_tail()
    assert {item["id"] for item in matches[0]["actions"]} >= {
        "prepare",
        "finish",
    }
    assert f"/devices/{_DEVICE_ID}/_execute_driver_command" in _ros_actions(running_os)


def test_unilab_cli_non_test_mode_does_not_enable_unclaimed_ros_dispatcher(
    tmp_path: Path,
) -> None:
    with _start_cli(tmp_path, test_mode=False) as running:
        assert "WorkflowTask ROS 执行后端已启用" not in running.logs()


def test_unilab_cli_ros_executes_complete_szlab_workflow_with_correlated_logs(
    running_os: _RunningOS,
) -> None:
    prepare_summary = _template_summary(running_os.base_url, "prepare")
    finish_summary = _template_summary(running_os.base_url, "finish")
    prepare = _template_detail(running_os.base_url, prepare_summary["uuid"])
    finish = _template_detail(running_os.base_url, finish_summary["uuid"])

    workflow = _data(
        _request(
            running_os.base_url,
            "GET",
            f"/api/v1/workflows/{_WORKFLOW_UUID}",
        )
    )
    assert workflow["uuid"] == _WORKFLOW_UUID
    assert workflow["meta_data"]["package_fqid"].endswith(".complete_workflow")
    prepare_node_uuid = str(uuid4())
    finish_node_uuid = str(uuid4())
    source_handle = _handle(prepare, io_type="source", handle_key="payload")
    target_handle = _handle(finish, io_type="target", handle_key="payload")
    nodes = [
        {
            "uuid": prepare_node_uuid,
            "workflow_node_template_uuid": prepare_summary["uuid"],
            "material_uuid": _DEVICE_UUID,
            "name": "prepare",
            "status": "idle",
            "type": "device_action",
            "pose": {},
            "param": {"batch": 7},
            "action_name": "prepare",
            "action_type": prepare["template"]["type"],
            "execution_policy": {},
            "disabled": False,
            "minimized": False,
            "meta_data": {},
        },
        {
            "uuid": finish_node_uuid,
            "workflow_node_template_uuid": finish_summary["uuid"],
            "material_uuid": _DEVICE_UUID,
            "name": "finish",
            "status": "idle",
            "type": "device_action",
            "pose": {},
            "param": {},
            "action_name": "finish",
            "action_type": finish["template"]["type"],
            "execution_policy": {},
            "disabled": False,
            "minimized": False,
            "meta_data": {},
        },
    ]
    saved = _data(
        _request(
            running_os.base_url,
            "PUT",
            f"/api/v1/workflows/{workflow['uuid']}/graph",
            {
                "revision": workflow["revision"],
                "nodes": nodes,
                "edges": [
                    {
                        "uuid": str(uuid4()),
                        "source_node_uuid": prepare_node_uuid,
                        "target_node_uuid": finish_node_uuid,
                        "source_handle_uuid": source_handle["uuid"],
                        "target_handle_uuid": target_handle["uuid"],
                        "meta_data": {},
                    }
                ],
            },
        )
    )
    assert saved["workflow"]["revision"] == 2

    task = _data(
        _request(
            running_os.base_url,
            "POST",
            "/api/v1/workflow-tasks",
            {
                "workflow_uuid": workflow["uuid"],
                "run_mode": "normal",
                "input": {},
                "meta_data": {},
            },
        ),
        expected_status=201,
    )
    terminal_task = _wait_for_terminal_task(running_os.base_url, task["uuid"])
    jobs = _data(
        _request(
            running_os.base_url,
            "GET",
            f"/api/v1/workflow-tasks/{task['uuid']}/jobs",
        )
    )

    assert terminal_task["status"] == "succeeded", terminal_task
    assert [job["workflow_node_uuid"] for job in jobs] == [
        prepare_node_uuid,
        finish_node_uuid,
    ]
    assert [job["status"] for job in jobs] == ["succeeded", "succeeded"]
    assert jobs[0]["finished_at"] <= jobs[1]["started_at"]

    jobs_by_node = {
        job["workflow_node_uuid"]: {
            **job,
            "action_name": (
                "prepare"
                if job["workflow_node_uuid"] == prepare_node_uuid
                else "finish"
            ),
        }
        for job in jobs
    }
    _assert_correlated_execution_logs(
        running_os.logs(),
        task_uuid=task["uuid"],
        jobs=[jobs_by_node[prepare_node_uuid], jobs_by_node[finish_node_uuid]],
    )
