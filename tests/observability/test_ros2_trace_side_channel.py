"""原生 ROS Action 的 trace context side-channel 回归。"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest

from unilabos.app.ws_client import QueueItem
from unilabos.observability.runtime import (
    TRACE_CONTEXT_KEY,
    decode_job_trace_context,
    encode_job_trace_context,
)
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode
from unilabos.ros.nodes.presets.host_node import HostNode
from unilabos_msgs.srv import SerialCommand

_JOB_UUID = "11111111-1111-4111-8111-111111111111"
_TASK_UUID = "22222222-2222-4222-8222-222222222222"
_CARRIER = {
    "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
    "tracestate": "unilab=test",
}


class _Logger:
    def debug(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        pass


class _ImmediateServiceFuture:
    def __init__(self, response: SerialCommand.Response) -> None:
        self._response = response

    def done(self) -> bool:
        return True

    def result(self) -> SerialCommand.Response:
        return self._response

    def cancel(self) -> None:
        pass


class _TraceContextClient:
    def __init__(self) -> None:
        self.request: SerialCommand.Request | None = None

    def wait_for_service(self, timeout_sec: float) -> bool:
        assert timeout_sec <= 0.05
        return True

    def call_async(
        self,
        request: SerialCommand.Request,
    ) -> _ImmediateServiceFuture:
        self.request = request
        return _ImmediateServiceFuture(
            SerialCommand.Response(response=json.dumps({"accepted": True}))
        )


class _RemoteHost:
    callback_group = object()

    def __init__(self) -> None:
        self.client = _TraceContextClient()
        self.created_service = ""

    def create_client(
        self,
        _service_type: Any,
        service_name: str,
        *,
        callback_group: Any,
    ) -> _TraceContextClient:
        assert callback_group is self.callback_group
        self.created_service = service_name
        return self.client

    def lab_logger(self) -> _Logger:
        return _Logger()


def _item() -> QueueItem:
    return QueueItem(
        task_type="job_call_back_status",
        device_id="arm-1",
        action_name="move",
        task_id=_TASK_UUID,
        job_id=_JOB_UUID,
        notebook_id="",
        device_action_key="/devices/arm-1/move",
    )


def test_native_action_registers_context_before_goal_by_service() -> None:
    host = _RemoteHost()

    assert HostNode._register_remote_trace_context(host, _item(), _CARRIER)
    assert host.created_service == ("/srv/devices/arm-1/_register_trace_context")
    assert host.client.request is not None
    decoded = decode_job_trace_context(host.client.request.command)
    assert decoded == {
        "node_job_uuid": _JOB_UUID,
        "task_uuid": _TASK_UUID,
        "action_name": "move",
        TRACE_CONTEXT_KEY: _CARRIER,
    }


def test_device_service_registers_only_valid_context() -> None:
    node = object.__new__(BaseROS2DeviceNode)
    node._job_contexts = {}
    node._job_contexts_lock = threading.Lock()
    node.lab_logger = lambda: _Logger()
    payload = encode_job_trace_context(
        node_job_uuid=_JOB_UUID,
        task_uuid=_TASK_UUID,
        action_name="move",
        trace_context=_CARRIER,
    )

    response = BaseROS2DeviceNode._register_trace_context_service(
        node,
        SerialCommand.Request(command=payload),
        SerialCommand.Response(),
    )

    assert json.loads(response.response) == {"accepted": True}
    assert node._job_contexts[_JOB_UUID][TRACE_CONTEXT_KEY] == _CARRIER


def test_side_channel_rejects_unknown_or_sensitive_fields() -> None:
    payload = json.loads(
        encode_job_trace_context(
            node_job_uuid=_JOB_UUID,
            task_uuid=_TASK_UUID,
            action_name="move",
            trace_context=_CARRIER,
        )
    )
    payload["password"] = "must-not-cross-ros"

    with pytest.raises(ValueError, match="字段"):
        decode_job_trace_context(json.dumps(payload))


def test_pruning_does_not_evict_a_live_context_at_the_capacity_limit() -> None:
    node = object.__new__(BaseROS2DeviceNode)
    registered_at = time.monotonic()
    node._job_contexts = {
        f"job-{index}": {"registered_at": registered_at} for index in range(2048)
    }

    BaseROS2DeviceNode._prune_job_contexts_locked(node)

    assert len(node._job_contexts) == 2048


def test_pruning_removes_expired_contexts() -> None:
    node = object.__new__(BaseROS2DeviceNode)
    registered_at = time.monotonic()
    node._job_contexts = {
        "expired": {"registered_at": registered_at - 61},
        "live": {"registered_at": registered_at},
    }

    BaseROS2DeviceNode._prune_job_contexts_locked(node)

    assert set(node._job_contexts) == {"live"}


def test_pruning_evicts_only_the_oldest_contexts_over_capacity() -> None:
    node = object.__new__(BaseROS2DeviceNode)
    registered_at = time.monotonic()
    node._job_contexts = {
        f"job-{index}": {"registered_at": registered_at + index / 10_000}
        for index in range(2050)
    }

    BaseROS2DeviceNode._prune_job_contexts_locked(node)

    assert len(node._job_contexts) == 2048
    assert "job-0" not in node._job_contexts
    assert "job-1" not in node._job_contexts
    assert "job-2" in node._job_contexts
