"""Uni-Lab-OS 运行时的 fail-open OpenTelemetry seam。

业务模块只依赖本文件暴露的少量函数，不直接依赖 Phoenix 或 OpenTelemetry。
未安装可选依赖、Phoenix 不可用或 exporter 异常时，所有调用自动退化为 no-op，
不得改变调度和设备动作语义。
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import threading
import uuid
from collections.abc import Iterator, Mapping
from concurrent.futures import Executor, Future
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Protocol

from unilabos.observability.config import ObservabilitySettings

_LOGGER = logging.getLogger(__name__)
_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_SENSITIVE_ATTRIBUTE = re.compile(
    r"authorization|cookie|password|secret|token|api[_-]?key",
    re.IGNORECASE,
)
_MAX_ATTRIBUTE_LENGTH = 1024
_MAX_TRACESTATE_LENGTH = 512

TRACE_CONTEXT_KEY = "trace_context"
TRACE_CONTEXT_SERVICE_SUFFIX = "_register_trace_context"
ROS_GOAL_UUID_KEY = "ros_goal_uuid"

_WORKFLOW_EXECUTION_IDENTITY: contextvars.ContextVar[dict[str, str]] = (
    contextvars.ContextVar("unilabos_workflow_execution_identity", default={})
)


class RuntimeTraceBackend(Protocol):
    """具体 trace SDK Adapter 的最小 Interface。"""

    @contextmanager
    def start_span(
        self,
        name: str,
        *,
        parent: Mapping[str, str] | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[Any]: ...

    @contextmanager
    def attach_context(
        self,
        carrier: Mapping[str, str] | None,
    ) -> Iterator[None]: ...

    def capture_context(self) -> dict[str, str]: ...

    def force_flush(self, timeout_seconds: float) -> None: ...

    def shutdown(self, timeout_seconds: float) -> None: ...


class _NoopRuntimeTraceBackend:
    @contextmanager
    def start_span(
        self,
        _name: str,
        *,
        parent: Mapping[str, str] | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[None]:
        del parent, attributes
        yield None

    @contextmanager
    def attach_context(
        self,
        _carrier: Mapping[str, str] | None,
    ) -> Iterator[None]:
        yield None

    def capture_context(self) -> dict[str, str]:
        return {}

    def force_flush(self, _timeout_seconds: float) -> None:
        return

    def shutdown(self, _timeout_seconds: float) -> None:
        return


class _OpenTelemetryRuntimeTraceBackend:
    """延迟导入 Phoenix OTEL，保持 Base 安装可以正常运行。"""

    def __init__(self, settings: ObservabilitySettings) -> None:
        from opentelemetry import context as otel_context
        from opentelemetry import trace
        from opentelemetry.trace.propagation.tracecontext import (
            TraceContextTextMapPropagator,
        )
        from phoenix.otel import (
            BatchSpanProcessor,
            HTTPSpanExporter,
            Resource,
            TracerProvider,
        )

        try:
            service_version = version("unilabos")
        except PackageNotFoundError:
            service_version = "unknown"

        resource = Resource.create(
            {
                "service.name": "uni-lab-os",
                "service.version": service_version,
                "openinference.project.name": settings.project_name,
            }
        )
        self._provider = TracerProvider(resource=resource)
        exporter = HTTPSpanExporter(
            endpoint=f"{settings.base_url}/v1/traces",
            headers={"x-project-name": settings.project_name},
        )
        self._provider.add_span_processor(BatchSpanProcessor(exporter))
        self._tracer = self._provider.get_tracer("unilabos.runtime")
        self._context = otel_context
        self._trace = trace
        self._propagator = TraceContextTextMapPropagator()

    @contextmanager
    def start_span(
        self,
        name: str,
        *,
        parent: Mapping[str, str] | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[Any]:
        parent_context = None
        normalized_parent = normalize_trace_context(parent)
        if normalized_parent:
            parent_context = self._propagator.extract(normalized_parent)
        safe_attributes = sanitize_span_attributes(attributes)
        with self._tracer.start_as_current_span(
            name,
            context=parent_context,
            attributes=safe_attributes,
            # OTel 默认会上报异常消息和完整 stacktrace；驱动异常可能包含动作
            # 参数或凭据，因此这里只保留受控的异常类型和 ERROR 状态。
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            if safe_attributes.get("error.type"):
                span.set_status(self._trace.Status(self._trace.StatusCode.ERROR))
            try:
                yield span
            except BaseException as exc:
                span.set_attribute(
                    "error.type", type(exc).__name__[:_MAX_ATTRIBUTE_LENGTH]
                )
                span.set_status(self._trace.Status(self._trace.StatusCode.ERROR))
                raise

    @contextmanager
    def attach_context(
        self,
        carrier: Mapping[str, str] | None,
    ) -> Iterator[None]:
        normalized = normalize_trace_context(carrier)
        if not normalized:
            yield None
            return
        token = self._context.attach(self._propagator.extract(normalized))
        try:
            yield None
        finally:
            self._context.detach(token)

    def capture_context(self) -> dict[str, str]:
        carrier: dict[str, str] = {}
        self._propagator.inject(carrier)
        return normalize_trace_context(carrier)

    def force_flush(self, timeout_seconds: float) -> None:
        self._provider.force_flush(timeout_millis=int(timeout_seconds * 1000))

    def shutdown(self, timeout_seconds: float) -> None:
        self.force_flush(timeout_seconds)
        self._provider.shutdown()


class RuntimeTracing:
    """线程安全地切换 no-op 与真实 OpenTelemetry Adapter。"""

    def __init__(self) -> None:
        self._backend: RuntimeTraceBackend = _NoopRuntimeTraceBackend()
        self._lock = threading.RLock()

    def start(self, settings: ObservabilitySettings) -> bool:
        if not settings.enabled:
            return False
        try:
            backend: RuntimeTraceBackend = _OpenTelemetryRuntimeTraceBackend(settings)
        except Exception as exc:  # noqa: BLE001 - tracing 必须 fail-open
            _LOGGER.error("初始化运行时 trace 失败，已降级为 no-op：%s", exc)
            return False
        with self._lock:
            previous = self._backend
            self._backend = backend
        try:
            previous.shutdown(settings.shutdown_timeout_seconds)
        except Exception:  # noqa: BLE001 - 旧 Adapter 清理不影响新 Adapter
            _LOGGER.exception("清理旧运行时 trace Adapter 失败")
        return True

    def stop(self, timeout_seconds: float) -> None:
        with self._lock:
            backend = self._backend
            self._backend = _NoopRuntimeTraceBackend()
        try:
            backend.shutdown(timeout_seconds)
        except Exception:  # noqa: BLE001 - trace 关闭不能阻断 OS 退出
            _LOGGER.exception("关闭运行时 trace 失败")

    def force_flush(self, timeout_seconds: float) -> bool:
        try:
            self._backend.force_flush(timeout_seconds)
            return True
        except Exception:  # noqa: BLE001 - trace flush 不能阻断业务
            _LOGGER.exception("刷新运行时 trace 失败")
            return False

    @contextmanager
    def start_span(
        self,
        name: str,
        *,
        parent: Mapping[str, str] | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[Any]:
        with self._backend.start_span(
            name,
            parent=normalize_trace_context(parent),
            attributes=sanitize_span_attributes(attributes),
        ) as span:
            yield span

    @contextmanager
    def attach_context(
        self,
        carrier: Mapping[str, str] | None,
    ) -> Iterator[None]:
        with self._backend.attach_context(normalize_trace_context(carrier)):
            yield None

    def capture_context(self) -> dict[str, str]:
        return normalize_trace_context(self._backend.capture_context())

    def submit(
        self,
        executor: Executor,
        function: Any,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Future[Any]:
        copied = contextvars.copy_context()
        return executor.submit(copied.run, function, *args, **kwargs)


runtime_tracing = RuntimeTracing()


def normalize_trace_context(
    carrier: Mapping[str, Any] | None,
) -> dict[str, str]:
    """只接受可安全跨 ROS 消息传播的 W3C trace carrier。"""

    if not isinstance(carrier, Mapping):
        return {}
    traceparent = carrier.get("traceparent")
    if not isinstance(traceparent, str):
        return {}
    traceparent = traceparent.strip().lower()
    match = _TRACEPARENT.fullmatch(traceparent)
    if match is None or match.group(1) == "0" * 32 or match.group(2) == "0" * 16:
        return {}
    normalized = {"traceparent": traceparent}
    tracestate = carrier.get("tracestate")
    if isinstance(tracestate, str):
        tracestate = tracestate.strip()
        if (
            tracestate
            and len(tracestate) <= _MAX_TRACESTATE_LENGTH
            and "\r" not in tracestate
            and "\n" not in tracestate
        ):
            normalized["tracestate"] = tracestate
    return normalized


def sanitize_span_attributes(
    attributes: Mapping[str, Any] | None,
) -> dict[str, str | bool | int | float]:
    """将业务属性收敛为低基数、无敏感内容的 OTEL 标量。"""

    sanitized: dict[str, str | bool | int | float] = {}
    for raw_key, value in (attributes or {}).items():
        key = str(raw_key)
        if _SENSITIVE_ATTRIBUTE.search(key):
            continue
        if not isinstance(value, (str, bool, int, float)):
            continue
        if isinstance(value, str):
            value = value[:_MAX_ATTRIBUTE_LENGTH]
        sanitized[key] = value
    return sanitized


def encode_job_trace_context(
    *,
    ros_goal_uuid: str | None = None,
    node_job_uuid: str,
    task_uuid: str,
    action_name: str,
    trace_context: Mapping[str, Any] | None,
) -> str:
    """生成原生 ROS Action side-channel 的最小、无业务参数载荷。"""

    carrier = normalize_trace_context(trace_context)
    normalized_job_uuid = ""
    if node_job_uuid:
        try:
            normalized_job_uuid = str(uuid.UUID(node_job_uuid))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("node_job_uuid 格式不正确") from exc
    try:
        normalized_goal_uuid = str(uuid.UUID(ros_goal_uuid or node_job_uuid))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("ros_goal_uuid 格式不正确") from exc
    normalized_task_uuid = ""
    if task_uuid:
        try:
            normalized_task_uuid = str(uuid.UUID(task_uuid))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("task_uuid 格式不正确") from exc
    safe_action_name = str(action_name).strip()[:256]
    if not safe_action_name:
        raise ValueError("action_name 不能为空")
    return json.dumps(
        {
            ROS_GOAL_UUID_KEY: normalized_goal_uuid,
            "node_job_uuid": normalized_job_uuid,
            "task_uuid": normalized_task_uuid,
            "action_name": safe_action_name,
            TRACE_CONTEXT_KEY: carrier,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_job_trace_context(payload: str) -> dict[str, Any]:
    """校验 side-channel 载荷；拒绝未知字段和非 W3C carrier。"""

    if not isinstance(payload, str) or len(payload.encode("utf-8")) > 4096:
        raise ValueError("trace context 载荷格式不正确")
    try:
        decoded = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("trace context 载荷不是有效 JSON") from exc
    expected_keys = {
        ROS_GOAL_UUID_KEY,
        "node_job_uuid",
        "task_uuid",
        "action_name",
        TRACE_CONTEXT_KEY,
    }
    legacy_keys = expected_keys - {ROS_GOAL_UUID_KEY}
    if not isinstance(decoded, dict):
        raise ValueError("trace context 载荷字段不正确")
    decoded_keys = frozenset(decoded)
    if decoded_keys not in {frozenset(expected_keys), frozenset(legacy_keys)}:
        raise ValueError("trace context 载荷字段不正确")
    normalized = json.loads(
        encode_job_trace_context(
            ros_goal_uuid=decoded.get(ROS_GOAL_UUID_KEY) or decoded["node_job_uuid"],
            node_job_uuid=decoded["node_job_uuid"],
            task_uuid=decoded["task_uuid"],
            action_name=decoded["action_name"],
            trace_context=decoded[TRACE_CONTEXT_KEY],
        )
    )
    if not normalized[TRACE_CONTEXT_KEY]:
        raise ValueError("trace context 缺少有效 traceparent")
    return normalized


@contextmanager
def attach_workflow_execution_identity(
    node_job_uuid: str,
    task_uuid: str,
) -> Iterator[None]:
    """让嵌套设备调用沿 Python context 继承原 Workflow 身份。"""

    identity = {
        "node_job_uuid": str(node_job_uuid or ""),
        "task_uuid": str(task_uuid or ""),
    }
    token = _WORKFLOW_EXECUTION_IDENTITY.set(identity)
    try:
        yield None
    finally:
        _WORKFLOW_EXECUTION_IDENTITY.reset(token)


def capture_workflow_execution_identity() -> dict[str, str]:
    """读取当前驱动执行所属的 WorkflowNodeJob/WorkflowTask。"""

    return dict(_WORKFLOW_EXECUTION_IDENTITY.get())


@contextmanager
def fail_open_span(
    tracing: Any,
    name: str,
    *,
    parent: Mapping[str, str] | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Any]:
    """保护业务调用不受任意 tracing 实现初始化/退出异常影响。"""

    try:
        manager = tracing.start_span(
            name,
            parent=normalize_trace_context(parent),
            attributes=sanitize_span_attributes(attributes),
        )
        span = manager.__enter__()
    except Exception:  # noqa: BLE001 - tracing 必须 fail-open
        _LOGGER.exception("创建运行时 span 失败：%s", name)
        yield None
        return

    try:
        yield span
    except BaseException:
        exc_info = sys.exc_info()
        try:
            manager.__exit__(*exc_info)
        except Exception:  # noqa: BLE001 - 保留原业务异常
            _LOGGER.exception("结束异常运行时 span 失败：%s", name)
        raise
    else:
        try:
            manager.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 - tracing 必须 fail-open
            _LOGGER.exception("结束运行时 span 失败：%s", name)


def safe_capture_context(tracing: Any) -> dict[str, str]:
    try:
        return normalize_trace_context(tracing.capture_context())
    except Exception:  # noqa: BLE001 - tracing 必须 fail-open
        _LOGGER.exception("捕获运行时 trace context 失败")
        return {}


def safe_submit(
    tracing: Any,
    executor: Executor,
    function: Any,
    /,
    *args: Any,
    **kwargs: Any,
) -> Future[Any]:
    try:
        return tracing.submit(executor, function, *args, **kwargs)
    except Exception:  # noqa: BLE001 - tracing 必须 fail-open
        _LOGGER.exception("复制运行时 trace context 失败，使用原线程池提交")
        return executor.submit(function, *args, **kwargs)


# 模块级短名便于业务模块依赖，也便于测试替换 module alias。
start_span = runtime_tracing.start_span
attach_context = runtime_tracing.attach_context
capture_context = runtime_tracing.capture_context
submit = runtime_tracing.submit


def capture_trace_context() -> dict[str, str]:
    return safe_capture_context(runtime_tracing)


@contextmanager
def start_runtime_span(
    name: str,
    *,
    parent: Mapping[str, str] | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Any]:
    with fail_open_span(
        runtime_tracing,
        name,
        parent=parent,
        attributes=attributes,
    ) as span:
        yield span


__all__ = [
    "ROS_GOAL_UUID_KEY",
    "TRACE_CONTEXT_KEY",
    "TRACE_CONTEXT_SERVICE_SUFFIX",
    "RuntimeTracing",
    "attach_context",
    "attach_workflow_execution_identity",
    "capture_context",
    "capture_trace_context",
    "capture_workflow_execution_identity",
    "decode_job_trace_context",
    "encode_job_trace_context",
    "fail_open_span",
    "normalize_trace_context",
    "runtime_tracing",
    "safe_capture_context",
    "safe_submit",
    "sanitize_span_attributes",
    "start_runtime_span",
    "start_span",
    "submit",
]
