"""Round O1 独立审查 finding 的回归测试。"""

from __future__ import annotations

import asyncio
import contextvars
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Iterator, Mapping

import pytest

from unilabos.app.ws_client import QueueItem
from unilabos.observability.runtime import (
    ROS_GOAL_UUID_KEY,
    TRACE_CONTEXT_KEY,
    _OpenTelemetryRuntimeTraceBackend,
    attach_workflow_execution_identity,
    decode_job_trace_context,
)
from unilabos.ros.nodes import base_device_node as device_module
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode, ROS2DeviceNode
from unilabos.ros.nodes.presets import host_node as host_module
from unilabos.ros.nodes.presets.host_node import HostNode
from unilabos_msgs.action import EmptyIn, StrSingleInput
from unilabos_msgs.srv import SerialCommand

_JOB_UUID = "11111111-1111-4111-8111-111111111111"
_TASK_UUID = "22222222-2222-4222-8222-222222222222"
_NESTED_GOAL_UUID = "33333333-3333-4333-8333-333333333333"
_CARRIER = {
    "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
    "tracestate": "unilab=test",
}


class _Logger:
    def trace(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def debug(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def info(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def error(self, *_args: Any, **_kwargs: Any) -> None:
        pass


class _GoalId:
    def __init__(self, value: str) -> None:
        self.uuid = list(uuid.UUID(value).bytes)


class _ServerGoal:
    request = object()

    def __init__(self, value: str) -> None:
        self.goal_id = _GoalId(value)


def _item() -> QueueItem:
    return QueueItem(
        task_type="job_call_back_status",
        device_id="arm-1",
        action_name="move",
        task_id=_TASK_UUID,
        job_id=_JOB_UUID,
        notebook_id="",
        device_action_key="/devices/arm-1/move",
        trace_context=dict(_CARRIER),
    )


def test_scheduler_hex_job_uuid_matches_local_goal_uuid() -> None:
    node = object.__new__(BaseROS2DeviceNode)
    node._job_contexts = {}
    node._job_contexts_lock = threading.Lock()
    node.register_job_context(
        _JOB_UUID.replace("-", ""),
        _TASK_UUID,
        "move",
        trace_context=_CARRIER,
    )

    consumed = node._consume_job_context(_ServerGoal(_JOB_UUID), "move")

    assert consumed["job_id"] == _JOB_UUID.replace("-", "")
    assert consumed["task_id"] == _TASK_UUID
    assert consumed[TRACE_CONTEXT_KEY] == _CARRIER


class _SafeSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}
        self.status: Any = None
        self.events: list[str] = []

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_status(self, status: Any) -> None:
        self.status = status


class _FakeTracer:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}
        self.span = _SafeSpan()

    @contextmanager
    def start_as_current_span(self, _name: str, **kwargs: Any) -> Iterator[_SafeSpan]:
        self.kwargs = kwargs
        try:
            yield self.span
        except BaseException as exc:
            if kwargs.get("record_exception", True):
                self.span.events.append(str(exc))
            raise


class _FakeTraceApi:
    class StatusCode:
        ERROR = "ERROR"

    @staticmethod
    def Status(code: str) -> tuple[str, str]:
        return ("status", code)


def test_runtime_span_never_records_exception_message_or_stacktrace() -> None:
    backend = object.__new__(_OpenTelemetryRuntimeTraceBackend)
    backend._tracer = _FakeTracer()
    backend._propagator = SimpleNamespace(extract=lambda _carrier: None)
    backend._trace = _FakeTraceApi()
    secret = "password=DO-NOT-EXPORT"

    with pytest.raises(RuntimeError, match="DO-NOT-EXPORT"):
        with backend.start_span("device.driver.execute"):
            raise RuntimeError(secret)

    assert backend._tracer.kwargs["record_exception"] is False
    assert backend._tracer.kwargs["set_status_on_exception"] is False
    assert backend._tracer.span.attributes == {"error.type": "RuntimeError"}
    assert secret not in repr(backend._tracer.span.events)
    assert backend._tracer.span.status == ("status", "ERROR")


def test_error_type_attribute_marks_callback_span_as_error() -> None:
    backend = object.__new__(_OpenTelemetryRuntimeTraceBackend)
    backend._tracer = _FakeTracer()
    backend._propagator = SimpleNamespace(extract=lambda _carrier: None)
    backend._trace = _FakeTraceApi()

    with backend.start_span(
        "ros2.action.cancel_response",
        attributes={"error.type": "TimeoutError"},
    ):
        pass

    assert backend._tracer.span.status == ("status", "ERROR")


class _RecordedSpan:
    def __init__(
        self,
        name: str,
        parent: Mapping[str, str],
        carrier: Mapping[str, str],
        attributes: Mapping[str, Any],
    ) -> None:
        self.name = name
        self.parent = dict(parent)
        self.carrier = dict(carrier)
        self.attributes = dict(attributes)


class _Tracing:
    def __init__(self) -> None:
        self._current: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
            "o1_review_trace", default=dict(_CARRIER)
        )
        self._counter = 10
        self.spans: list[_RecordedSpan] = []

    @contextmanager
    def start_span(
        self,
        name: str,
        *,
        parent: Mapping[str, str] | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[_RecordedSpan]:
        parent_carrier = dict(parent or self._current.get())
        trace_id = parent_carrier["traceparent"].split("-")[1]
        carrier = {
            "traceparent": f"00-{trace_id}-{self._counter:016x}-01",
        }
        self._counter += 1
        span = _RecordedSpan(name, parent_carrier, carrier, attributes or {})
        self.spans.append(span)
        token = self._current.set(carrier)
        try:
            yield span
        finally:
            self._current.reset(token)

    def capture_context(self) -> dict[str, str]:
        return dict(self._current.get())


class _ImmediateFuture:
    def __init__(self, result: Any) -> None:
        self._result = result

    def done(self) -> bool:
        return True

    def result(self) -> Any:
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result

    def cancel(self) -> None:
        pass

    def add_done_callback(self, callback: Any) -> None:
        callback(self)

    def __await__(self):
        async def resolved() -> Any:
            return self.result()

        return resolved().__await__()


class _TraceServiceClient:
    def __init__(self, *, hanging: bool = False) -> None:
        self.hanging = hanging
        self.request: SerialCommand.Request | None = None

    def wait_for_service(self, timeout_sec: float) -> bool:
        assert timeout_sec <= 0.05
        return True

    def service_is_ready(self) -> bool:
        return True

    def call_async(self, request: SerialCommand.Request) -> _ImmediateFuture:
        self.request = request
        if self.hanging:
            return _HangingFuture()
        return _ImmediateFuture(
            SerialCommand.Response(response=json.dumps({"accepted": True}))
        )


class _HangingFuture:
    def done(self) -> bool:
        return False

    def cancel(self) -> None:
        pass


class _ActionGoalHandle:
    accepted = True

    def get_result_async(self) -> _ImmediateFuture:
        return _ImmediateFuture(SimpleNamespace(result=object()))


class _ActionClient:
    _action_type = EmptyIn

    def __init__(self) -> None:
        self.goal_uuid: Any = None
        self.send_count = 0

    def wait_for_server(self, **_kwargs: Any) -> bool:
        return True

    def server_is_ready(self) -> bool:
        return True

    def send_goal_async(self, _goal: Any, **kwargs: Any) -> _ImmediateFuture:
        self.send_count += 1
        self.goal_uuid = kwargs.get("goal_uuid")
        return _ImmediateFuture(_ActionGoalHandle())


def _nested_node(action_client: _ActionClient, service_client: _TraceServiceClient):
    node = object.__new__(BaseROS2DeviceNode)
    node.callback_group = object()
    node._trace_context_clients = {}
    node.create_client = lambda *_args, **_kwargs: service_client
    node.lab_logger = lambda: _Logger()
    node._build_action_call = lambda *_args, **_kwargs: (
        "/devices/arm-2/move",
        action_client,
        object(),
        True,
    )
    node._wait_future_blocking = lambda *_args, **_kwargs: None
    node._parse_action_result = lambda *_args, **_kwargs: "ok"

    async def sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    node.sleep = sleep
    return node


def _assert_registered_uuid_matches_goal(
    action_client: _ActionClient,
    service_client: _TraceServiceClient,
) -> None:
    assert action_client.goal_uuid is not None
    assert service_client.request is not None
    registered = decode_job_trace_context(service_client.request.command)
    goal_uuid = str(uuid.UUID(bytes=bytes(action_client.goal_uuid.uuid)))
    assert registered[ROS_GOAL_UUID_KEY] == goal_uuid
    assert registered["node_job_uuid"] == _JOB_UUID
    assert registered["task_uuid"] == _TASK_UUID
    assert registered[TRACE_CONTEXT_KEY]["traceparent"].startswith(
        "00-0123456789abcdef0123456789abcdef-"
    )

    target = object.__new__(BaseROS2DeviceNode)
    target._job_contexts = {}
    target._job_contexts_lock = threading.Lock()
    target.lab_logger = lambda: _Logger()
    response = BaseROS2DeviceNode._register_trace_context_service(
        target,
        service_client.request,
        SerialCommand.Response(),
    )
    assert json.loads(response.response) == {"accepted": True}
    consumed = BaseROS2DeviceNode._consume_job_context(
        target,
        _ServerGoal(goal_uuid),
        "move",
    )
    assert consumed[ROS_GOAL_UUID_KEY] == goal_uuid
    assert consumed["job_id"] == _JOB_UUID
    assert consumed["task_id"] == _TASK_UUID


def test_nested_sync_native_action_uses_side_channel_and_matching_goal_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracing = _Tracing()
    monkeypatch.setattr(device_module, "runtime_tracing", tracing)
    action_client = _ActionClient()
    service_client = _TraceServiceClient()
    node = _nested_node(action_client, service_client)

    with attach_workflow_execution_identity(_JOB_UUID, _TASK_UUID):
        assert BaseROS2DeviceNode.call_device_action(node, "arm-2", "move") == "ok"

    _assert_registered_uuid_matches_goal(action_client, service_client)


def test_nested_async_native_action_uses_side_channel_and_matching_goal_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracing = _Tracing()
    monkeypatch.setattr(device_module, "runtime_tracing", tracing)
    action_client = _ActionClient()
    service_client = _TraceServiceClient()
    node = _nested_node(action_client, service_client)

    async def exercise() -> Any:
        with attach_workflow_execution_identity(_JOB_UUID, _TASK_UUID):
            return await BaseROS2DeviceNode.call_device_action_async(
                node, "arm-2", "move"
            )

    result = asyncio.run(exercise())

    assert result == "ok"
    _assert_registered_uuid_matches_goal(action_client, service_client)


class _CancelGoal:
    def __init__(self, response: Any) -> None:
        self.response = response

    def cancel_goal_async(self) -> _ImmediateFuture:
        return _ImmediateFuture(self.response)


@pytest.mark.parametrize(
    ("response", "accepted", "error_type"),
    [
        (SimpleNamespace(goals_canceling=[object()]), True, None),
        (SimpleNamespace(goals_canceling=[]), False, None),
        (RuntimeError("secret cancel failure"), False, "RuntimeError"),
    ],
)
def test_cancel_request_and_response_stay_in_job_trace(
    monkeypatch: pytest.MonkeyPatch,
    response: Any,
    accepted: bool,
    error_type: str | None,
) -> None:
    tracing = _Tracing()
    monkeypatch.setattr(host_module, "runtime_tracing", tracing)
    host = SimpleNamespace(
        _goals={_JOB_UUID: _CancelGoal(response)},
        _goal_trace_contexts={_JOB_UUID: dict(_CARRIER)},
        lab_logger=lambda: _Logger(),
    )
    host._cancel_goal_callback = lambda goal_uuid, future, *args: (
        HostNode._cancel_goal_callback(host, goal_uuid, future, *args)
    )

    assert HostNode.cancel_goal(host, _JOB_UUID)

    request_span = next(
        span for span in tracing.spans if span.name == "ros2.action.cancel"
    )
    response_span = next(
        span for span in tracing.spans if span.name == "ros2.action.cancel_response"
    )
    assert request_span.parent == _CARRIER
    assert response_span.parent == request_span.carrier
    assert response_span.attributes["ros.cancel.accepted"] is accepted
    assert response_span.attributes.get("error.type") == error_type


def test_hanging_trace_registration_does_not_block_scheduler_send_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracing = _Tracing()
    monkeypatch.setattr(host_module, "runtime_tracing", tracing)
    service_client = _TraceServiceClient(hanging=True)
    action_client = _ActionClient()
    executor = ThreadPoolExecutor(max_workers=1)
    host = SimpleNamespace(
        _action_clients={"/devices/arm-1/move": action_client},
        devices_instances={},
        server_latest_timestamp=0.0,
        callback_group=object(),
        _trace_context_clients={},
        _trace_registration_executor=executor,
        bridges=[],
        create_client=lambda *_args, **_kwargs: service_client,
        lab_logger=lambda: _Logger(),
        goal_response_callback=lambda *_args, **_kwargs: None,
        feedback_callback=lambda *_args, **_kwargs: None,
    )

    started_at = time.monotonic()
    deferred = HostNode.send_goal(
        host,
        _item(),
        action_type="NativeAction",
        action_kwargs={},
        sample_material={},
    )
    elapsed = time.monotonic() - started_at

    try:
        assert elapsed < 0.1
        assert deferred is not None
        deferred.result(timeout=1.5)
        assert action_client.send_count == 1
    finally:
        executor.shutdown(wait=True)


class _UnavailableActionClient(_ActionClient):
    def __init__(self) -> None:
        super().__init__()
        self.wait_timeout: float | None = None

    def wait_for_server(self, **kwargs: Any) -> bool:
        self.wait_timeout = kwargs.get("timeout_sec")
        return False


def _remote_host(
    action_client: _ActionClient,
    service_client: _TraceServiceClient,
    executor: Any,
) -> SimpleNamespace:
    return SimpleNamespace(
        _action_clients={"/devices/arm-1/move": action_client},
        devices_instances={},
        server_latest_timestamp=0.0,
        callback_group=object(),
        _trace_context_clients={},
        _trace_registration_executor=executor,
        _shutting_down=False,
        bridges=[],
        create_client=lambda *_args, **_kwargs: service_client,
        lab_logger=lambda: _Logger(),
        goal_response_callback=lambda *_args, **_kwargs: None,
        feedback_callback=lambda *_args, **_kwargs: None,
    )


def test_unavailable_action_server_finishes_deferred_send_with_failure() -> None:
    action_client = _UnavailableActionClient()
    executor = ThreadPoolExecutor(max_workers=1)
    host = _remote_host(action_client, _TraceServiceClient(), executor)
    failure_reported = threading.Event()
    reported_statuses: list[str] = []

    def publish_job_status(
        _payload: Any,
        _item: Any,
        status: str,
        _return_info: Any,
    ) -> None:
        reported_statuses.append(status)
        failure_reported.set()

    host.bridges = [SimpleNamespace(publish_job_status=publish_job_status)]

    deferred = HostNode._send_goal_with_trace(
        host,
        _item(),
        action_type="NativeAction",
        action_kwargs={},
        sample_material={},
        trace_context=_CARRIER,
    )

    try:
        assert deferred is not None
        with pytest.raises(TimeoutError, match="unavailable"):
            deferred.result(timeout=1.0)
        assert action_client.wait_timeout == pytest.approx(5.0)
        assert action_client.send_count == 0
        assert failure_reported.wait(1.0)
        assert reported_statuses == ["failed"]
    finally:
        executor.shutdown(wait=True)


class _ForbiddenExecutor:
    def submit(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("no carrier must not defer business send")


def test_disabled_tracing_preserves_synchronous_native_send() -> None:
    action_client = _ActionClient()
    host = _remote_host(
        action_client,
        _TraceServiceClient(),
        _ForbiddenExecutor(),
    )

    deferred = HostNode._send_goal_with_trace(
        host,
        _item(),
        action_type="NativeAction",
        action_kwargs={},
        sample_material={},
        trace_context={},
    )

    assert deferred is None
    assert action_client.send_count == 1


def test_shutdown_cancels_executor_and_prevents_deferred_physical_goal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action_client = _ActionClient()
    executor = ThreadPoolExecutor(max_workers=1)
    host = _remote_host(
        action_client,
        _TraceServiceClient(hanging=True),
        executor,
    )
    monkeypatch.setattr(HostNode, "_instance", host)

    deferred = HostNode._send_goal_with_trace(
        host,
        _item(),
        action_type="NativeAction",
        action_kwargs={},
        sample_material={},
        trace_context=_CARRIER,
    )
    assert deferred is not None

    HostNode._shutdown_trace_registration_executor()

    with pytest.raises(RuntimeError, match="shutting down"):
        deferred.result(timeout=1.5)
    assert host._trace_registration_executor is None
    assert host._shutting_down is True
    assert action_client.send_count == 0


class _ExecutableGoal:
    def __init__(self, request: Any, value: str) -> None:
        self.request = request
        self.goal_id = _GoalId(value)
        self.succeeded = False

    def succeed(self) -> None:
        self.succeeded = True

    def publish_feedback(self, _feedback: Any) -> None:
        pass


def test_async_driver_execution_keeps_ros_parent_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracing = _Tracing()
    monkeypatch.setattr(device_module, "runtime_tracing", tracing)
    monkeypatch.setattr(device_module, "Task", asyncio.Task)
    monkeypatch.setattr(
        ROS2DeviceNode,
        "run_async_func",
        staticmethod(lambda function, **_kwargs: asyncio.create_task(function())),
    )
    observed: list[dict[str, str]] = []

    class Driver:
        async def run(self, string: str) -> str:
            assert string == "argument-not-exported"
            observed.append(tracing.capture_context())
            await asyncio.sleep(0)
            return "done"

    node = object.__new__(BaseROS2DeviceNode)
    node.driver_instance = Driver()
    node.device_id = "arm-1"
    node._job_contexts = {}
    node._job_contexts_lock = threading.Lock()
    node._executor = ThreadPoolExecutor(max_workers=1)
    node._print_publish = False
    node._time_spent = 0.0
    node._time_remaining = 0.0
    node.lab_logger = lambda: _Logger()
    node._resolve_runtime_error_policy = (
        lambda action_name, _mapping, _action, _kwargs: (None, action_name)
    )
    node.register_job_context(
        _JOB_UUID,
        _TASK_UUID,
        "run",
        trace_context=_CARRIER,
        ros_goal_uuid=_NESTED_GOAL_UUID,
    )
    mapping = {
        "type": StrSingleInput,
        "method_name": "run",
        "goal": {"string": "string"},
        "feedback": {},
        "result": {},
    }
    callback = BaseROS2DeviceNode._create_execute_callback(node, "run", mapping)
    goal_handle = _ExecutableGoal(
        StrSingleInput.Goal(string="argument-not-exported"),
        _NESTED_GOAL_UUID,
    )

    try:
        result = asyncio.run(callback(goal_handle))
    finally:
        node._executor.shutdown(wait=True)

    assert result.success is True
    assert goal_handle.succeeded
    assert len(observed) == 1
    assert (
        observed[0]["traceparent"].split("-")[1]
        == (_CARRIER["traceparent"].split("-")[1])
    )
    driver_span = next(
        span for span in tracing.spans if span.name == "device.driver.execute"
    )
    execute_span = next(
        span for span in tracing.spans if span.name == "ros2.action.execute"
    )
    assert execute_span.attributes["workflow.node_job.uuid"] == _JOB_UUID
    assert execute_span.attributes["workflow.task.uuid"] == _TASK_UUID
    assert execute_span.attributes["ros.goal.uuid"] == _NESTED_GOAL_UUID
    assert driver_span.attributes["workflow.node_job.uuid"] == _JOB_UUID
    assert "argument-not-exported" not in repr(driver_span.attributes)
