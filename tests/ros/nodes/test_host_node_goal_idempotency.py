"""HostNode ROS Goal UUID 幂等下发回归测试。"""

from __future__ import annotations

import threading
import uuid
from types import SimpleNamespace

from unilabos.ros.nodes.presets import host_node as host_node_module
from unilabos.ros.nodes.presets.host_node import HostNode


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def trace(self, _message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        self.warnings.append(message)


class _Future:
    def __init__(self) -> None:
        self.callbacks = []

    def add_done_callback(self, callback) -> None:
        self.callbacks.append(callback)


class _ActionClient:
    _action_type = SimpleNamespace(Goal=lambda: SimpleNamespace())

    def __init__(self) -> None:
        self.send_count = 0

    def wait_for_server(self) -> None:
        pass

    def send_goal_async(self, *_args, **_kwargs) -> _Future:
        self.send_count += 1
        return _Future()


class _RejectedFuture:
    def result(self):
        return SimpleNamespace(accepted=False)


class _Bridge:
    def __init__(self) -> None:
        self.statuses = []

    def publish_job_status(self, *args) -> None:
        self.statuses.append(args)


def test_send_goal_ignores_duplicate_job_uuid(monkeypatch) -> None:
    action_id = "/devices/material/_execute_driver_command_async"
    action_client = _ActionClient()
    logger = _Logger()
    host = object.__new__(HostNode)
    host._action_clients = {action_id: action_client}
    host._goals = {}
    host._pending_goal_ids = set()
    host._goals_lock = threading.RLock()
    host.devices_instances = {}
    host.server_latest_timestamp = 0.0
    host.lab_logger = lambda: logger
    monkeypatch.setattr(
        host_node_module,
        "convert_to_ros_msg",
        lambda _goal, kwargs: kwargs,
    )

    job_uuid = str(uuid.uuid4())
    item = SimpleNamespace(
        job_id=job_uuid,
        task_id=str(uuid.uuid4()),
        device_id="material",
        action_name="run_operation_review_v1",
        trace_context={},
    )
    kwargs = {
        "operation_name": "pf_s7_consumables",
        "inputs_json": "{}",
        "timeout_s": 3600.0,
    }

    HostNode.send_goal(
        host,
        item,
        "UniLabJsonCommandAsync",
        kwargs,
        {},
    )
    HostNode.send_goal(
        host,
        item,
        "UniLabJsonCommandAsync",
        kwargs,
        {},
    )

    assert action_client.send_count == 1
    assert host._pending_goal_ids == {job_uuid}
    assert any("Duplicate goal dispatch ignored" in line for line in logger.warnings)


def test_rejected_duplicate_does_not_fail_accepted_goal() -> None:
    logger = _Logger()
    bridge = _Bridge()
    host = object.__new__(HostNode)
    job_uuid = str(uuid.uuid4())
    host._goals = {job_uuid: object()}
    host._pending_goal_ids = {job_uuid}
    host._goals_lock = threading.RLock()
    host.bridges = [bridge]
    host.lab_logger = lambda: logger
    item = SimpleNamespace(
        job_id=job_uuid,
        action_name="run_operation_review_v1",
    )

    HostNode.goal_response_callback(
        host,
        item,
        "/devices/material/_execute_driver_command_async",
        _RejectedFuture(),
    )

    assert bridge.statuses == []
    assert host._pending_goal_ids == set()
    assert any("Duplicate goal rejection ignored" in line for line in logger.warnings)
