"""ROS2 动作分发链路的 trace 传播契约。

这些测试只使用内存 fake，不启动 ROS executor、Phoenix 或 OTLP exporter。生产代码
应把可观测性看作 fail-open 的旁路：trace 失败不得影响动作分发与驱动执行。
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Iterator, Mapping

import pytest
from unilabos_msgs.action import StrSingleInput

from unilabos.app.scheduler import backend as backend_module
from unilabos.app.scheduler.backend import JobExecutionBackend
from unilabos.app.scheduler.dispatch import build_job_start_payload
from unilabos.app.ws_client import QueueItem
from unilabos.resources.resource_tracker import JSON_UNILABOS_PARAM
from unilabos.ros.nodes import base_device_node as device_module
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode
from unilabos.ros.nodes.presets import host_node as host_module
from unilabos.ros.nodes.presets.host_node import HostNode


TRACE_ID = "0123456789abcdef0123456789abcdef"
ROOT_SPAN_ID = "0123456789abcdef"
ROOT_CARRIER = {
    "traceparent": f"00-{TRACE_ID}-{ROOT_SPAN_ID}-01",
    "tracestate": "unilab=test",
}
TRACEPARENT_RE = re.compile(
    r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$"
)


@dataclass(frozen=True)
class _RecordedSpan:
    name: str
    parent: dict[str, str]
    attributes: dict[str, Any]
    carrier: dict[str, str]


class _RecordingRuntimeTracing:
    """模拟 production tracing seam，并用 ContextVar 检测线程传播。"""

    def __init__(self) -> None:
        self.spans: list[_RecordedSpan] = []
        self._current: contextvars.ContextVar[dict[str, str]] = (
            contextvars.ContextVar("test_trace_carrier", default={})
        )
        self._span_counter = 1

    @contextmanager
    def start_span(
        self,
        name: str,
        *,
        parent: Mapping[str, str] | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[_RecordedSpan]:
        parent_carrier = dict(parent or self.capture_context())
        trace_id = _trace_id(parent_carrier) or TRACE_ID
        span_id = f"{self._span_counter:016x}"
        self._span_counter += 1
        carrier = {
            "traceparent": f"00-{trace_id}-{span_id}-01",
        }
        if parent_carrier.get("tracestate"):
            carrier["tracestate"] = parent_carrier["tracestate"]
        span = _RecordedSpan(
            name=name,
            parent=parent_carrier,
            attributes=dict(attributes or {}),
            carrier=carrier,
        )
        self.spans.append(span)
        token = self._current.set(carrier)
        try:
            yield span
        finally:
            self._current.reset(token)

    @contextmanager
    def attach_context(
        self, carrier: Mapping[str, str] | None
    ) -> Iterator[None]:
        token = self._current.set(dict(carrier or {}))
        try:
            yield
        finally:
            self._current.reset(token)

    def capture_context(self) -> dict[str, str]:
        return dict(self._current.get())

    # 给实现留一个明确的 ThreadPoolExecutor seam；使用它或等价的
    # ``contextvars.copy_context().run`` 都能满足下面的行为测试。
    def submit(self, executor: Any, function: Any, /, *args: Any, **kwargs: Any):
        copied = contextvars.copy_context()
        return executor.submit(copied.run, function, *args, **kwargs)


class _BrokenRuntimeTracing:
    """模拟 disabled/degraded exporter 或 tracing adapter 全部报错。"""

    def __getattr__(self, _name: str) -> Any:
        def fail(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("trace backend unavailable")

        return fail


class _SilentLogger:
    def debug(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def info(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def error(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def trace(self, *_args: Any, **_kwargs: Any) -> None:
        pass


def _trace_id(carrier: Mapping[str, str]) -> str:
    traceparent = carrier.get("traceparent", "")
    return traceparent.split("-")[1] if TRACEPARENT_RE.fullmatch(traceparent) else ""


def _assert_w3c_carrier(carrier: Mapping[str, str]) -> None:
    assert TRACEPARENT_RE.fullmatch(carrier.get("traceparent", ""))
    if "tracestate" in carrier:
        assert isinstance(carrier["tracestate"], str)


def _queue_item() -> QueueItem:
    return QueueItem(
        task_type="job_call_back_status",
        device_id="arm-1",
        action_name="move",
        task_id="22222222-2222-4222-8222-222222222222",
        job_id="11111111-1111-4111-8111-111111111111",
        notebook_id="",
        device_action_key="/devices/arm-1/move",
    )


def test_runtime_trace_seam_has_a_fail_open_default() -> None:
    """Base 安装未带 OTel/Phoenix 时仍必须提供安全的 no-op seam。"""

    runtime = import_module("unilabos.observability.runtime")

    carrier = runtime.capture_trace_context()
    with runtime.start_runtime_span("test.noop", attributes={"safe": True}):
        pass

    assert isinstance(carrier, dict)


def test_scheduler_dispatch_creates_w3c_context_without_sensitive_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracing = _RecordingRuntimeTracing()
    monkeypatch.setattr(backend_module, "runtime_tracing", tracing, raising=False)

    sent: list[QueueItem] = []

    class FakeHost:
        def send_goal(self, item: QueueItem, **_kwargs: Any) -> None:
            sent.append(item)

    backend = JobExecutionBackend(host_node_getter=FakeHost)
    backend.start()
    try:
        backend.dispatch(
            build_job_start_payload(
                job_id="11111111-1111-4111-8111-111111111111",
                task_id="22222222-2222-4222-8222-222222222222",
                workflow_id="33333333-3333-4333-8333-333333333333",
                node_id="44444444-4444-4444-8444-444444444444",
                device_id="arm-1",
                action_name="move",
                action_type="UniLabJsonCommand",
                action_args={
                    "position": 12,
                    "password": "DO-NOT-EXPORT",
                    "nested": {"token": "TOP-SECRET"},
                },
            )
        )
        assert backend.wait_idle()
    finally:
        backend.stop()

    assert len(sent) == 1
    carrier = sent[0].trace_context
    _assert_w3c_carrier(carrier)

    dispatch_span = next(
        span for span in tracing.spans if span.name == "workflow.node.dispatch"
    )
    assert dispatch_span.attributes["workflow.node_job.uuid"] == sent[0].job_id
    assert dispatch_span.attributes["workflow.task.uuid"] == sent[0].task_id
    assert dispatch_span.attributes["workflow.node.uuid"] == (
        "44444444-4444-4444-8444-444444444444"
    )
    rendered = repr(dispatch_span.attributes)
    assert "DO-NOT-EXPORT" not in rendered
    assert "TOP-SECRET" not in rendered
    assert "action_args" not in dispatch_span.attributes


def test_host_passes_child_context_to_local_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracing = _RecordingRuntimeTracing()
    monkeypatch.setattr(host_module, "runtime_tracing", tracing, raising=False)
    monkeypatch.setattr(host_module.BasicConfig, "test_mode", False)

    registered: list[tuple[str, str, str, dict[str, str]]] = []

    class TargetNode:
        def register_job_context(
            self,
            job_id: str,
            task_id: str,
            action_name: str,
            trace_context: Mapping[str, str] | None = None,
        ) -> None:
            registered.append(
                (job_id, task_id, action_name, dict(trace_context or {}))
            )

    client = _FakeActionClient()
    host = _FakeHostForSendGoal(client, TargetNode())
    item = _queue_item()
    item.trace_context = dict(ROOT_CARRIER)

    HostNode.send_goal(
        host,
        item,
        action_type="UniLabJsonCommand",
        action_kwargs={"position": 12},
        sample_material={},
    )

    assert len(registered) == 1
    local_carrier = registered[0][3]
    _assert_w3c_carrier(local_carrier)
    assert _trace_id(local_carrier) == TRACE_ID
    assert local_carrier["traceparent"] != ROOT_CARRIER["traceparent"]
    assert any(span.name == "ros2.action.send_goal" for span in tracing.spans)


def test_json_command_carries_and_remote_device_extracts_w3c_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracing = _RecordingRuntimeTracing()
    monkeypatch.setattr(host_module, "runtime_tracing", tracing, raising=False)
    monkeypatch.setattr(host_module.BasicConfig, "test_mode", False)

    client = _FakeActionClient()
    host = _FakeHostForSendGoal(client, target_node=None)
    item = _queue_item()
    item.trace_context = dict(ROOT_CARRIER)

    HostNode.send_goal(
        host,
        item,
        action_type="UniLabJsonCommand",
        action_kwargs={"position": 12},
        sample_material={},
    )

    command = json.loads(client.goal.string)
    carrier = command[JSON_UNILABOS_PARAM]["trace_context"]
    _assert_w3c_carrier(carrier)
    assert _trace_id(carrier) == TRACE_ID
    assert carrier["tracestate"] == ROOT_CARRIER["tracestate"]

    remote_node = object.__new__(BaseROS2DeviceNode)
    remote_node._job_contexts = {}
    remote_node._job_contexts_lock = threading.Lock()
    goal_handle = _GoalHandle(client.goal, item.job_id)

    consumed = BaseROS2DeviceNode._consume_job_context(
        remote_node,
        goal_handle,
        "_execute_driver_command",
    )

    assert consumed["trace_context"] == carrier
    assert consumed["job_id"] == item.job_id


def test_sync_driver_threadpool_keeps_parent_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracing = _RecordingRuntimeTracing()
    monkeypatch.setattr(device_module, "runtime_tracing", tracing, raising=False)

    observed: list[dict[str, str]] = []

    class Driver:
        def run(self, string: str) -> str:
            assert string == "sensitive-action-argument"
            observed.append(tracing.capture_context())
            return "done"

    node = object.__new__(BaseROS2DeviceNode)
    node.driver_instance = Driver()
    node._job_contexts = {}
    node._job_contexts_lock = threading.Lock()
    node._executor = ThreadPoolExecutor(max_workers=1)
    node._print_publish = False
    node._time_spent = 0.0
    node._time_remaining = 0.0
    node.lab_logger = lambda: _SilentLogger()
    node._resolve_runtime_error_policy = (
        lambda action_name, _mapping, _action, _kwargs: (None, action_name)
    )
    node.register_job_context(
        _queue_item().job_id,
        _queue_item().task_id,
        "run",
        trace_context=ROOT_CARRIER,
    )

    mapping = {
        "type": StrSingleInput,
        "method_name": "run",
        "goal": {"string": "string"},
        "feedback": {},
        "result": {},
    }
    goal = StrSingleInput.Goal(string="sensitive-action-argument")
    goal_handle = _GoalHandle(goal, _queue_item().job_id)
    callback = BaseROS2DeviceNode._create_execute_callback(node, "run", mapping)

    async def exercise() -> None:
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            device_module.rclpy,
            "get_global_executor",
            lambda: _LoopExecutor(loop),
        )
        result = await asyncio.wait_for(callback(goal_handle), timeout=2.0)
        assert result.success is True

    try:
        asyncio.run(exercise())
    finally:
        node._executor.shutdown(wait=True)

    assert len(observed) == 1
    _assert_w3c_carrier(observed[0])
    assert _trace_id(observed[0]) == TRACE_ID
    driver_span = next(
        span for span in tracing.spans if span.name == "device.driver.execute"
    )
    assert "sensitive-action-argument" not in repr(driver_span.attributes)


def test_degraded_observability_does_not_block_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        backend_module,
        "runtime_tracing",
        _BrokenRuntimeTracing(),
        raising=False,
    )
    sent: list[str] = []

    class FakeHost:
        def send_goal(self, item: QueueItem, **_kwargs: Any) -> None:
            sent.append(item.job_id)

    backend = JobExecutionBackend(host_node_getter=FakeHost)
    backend.start()
    try:
        backend.dispatch(
            build_job_start_payload(
                job_id="11111111-1111-4111-8111-111111111111",
                task_id="22222222-2222-4222-8222-222222222222",
                workflow_id="33333333-3333-4333-8333-333333333333",
                node_id="44444444-4444-4444-8444-444444444444",
                device_id="arm-1",
                action_name="move",
                action_type="UniLabJsonCommand",
                action_args={"position": 12},
            )
        )
        assert backend.wait_idle()
    finally:
        backend.stop()

    assert sent == ["11111111-1111-4111-8111-111111111111"]


class _ImmediateFuture:
    def add_done_callback(self, _callback: Any) -> None:
        # 本测试只验证发送边界，不进入 ROS result 回调。
        pass


class _FakeActionClient:
    _action_type = StrSingleInput

    def __init__(self) -> None:
        self.goal: StrSingleInput.Goal | None = None

    def wait_for_server(self) -> bool:
        return True

    def send_goal_async(self, goal: StrSingleInput.Goal, **_kwargs: Any):
        self.goal = goal
        return _ImmediateFuture()


class _FakeHostForSendGoal:
    feedback_callback = HostNode.feedback_callback
    goal_response_callback = HostNode.goal_response_callback

    def __init__(self, client: _FakeActionClient, target_node: Any) -> None:
        self._action_clients = {
            "/devices/arm-1/_execute_driver_command": client,
        }
        wrapper = type("Wrapper", (), {"_ros_node": target_node})()
        self.devices_instances = {"arm-1": wrapper} if target_node else {}
        self.server_latest_timestamp = 0.0

    def lab_logger(self) -> _SilentLogger:
        return _SilentLogger()


class _GoalId:
    def __init__(self, job_id: str) -> None:
        self.uuid = list(uuid.UUID(job_id).bytes)


class _GoalHandle:
    def __init__(self, request: Any, job_id: str) -> None:
        self.request = request
        self.goal_id = _GoalId(job_id)
        self.succeeded = False

    def succeed(self) -> None:
        self.succeeded = True

    def publish_feedback(self, _feedback: Any) -> None:
        pass


class _LoopExecutor:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def create_task(self, coroutine: Any) -> None:
        self._loop.call_soon_threadsafe(asyncio.create_task, coroutine)
