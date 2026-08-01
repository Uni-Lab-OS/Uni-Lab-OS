"""Electron 与 trace 存储 Adapter 之间的稳定 Interface。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from unilabos.observability.config import ObservabilitySettings

_LOGGER = logging.getLogger(__name__)


class ObservabilityError(RuntimeError):
    """可安全投影到 Electron 的 observability 错误。"""

    code = "observability_error"
    status_code = 500


class ObservabilityDisabled(ObservabilityError):
    code = "observability_disabled"
    status_code = 503

    def __init__(self, message: str = "Trace 日志功能未启用") -> None:
        super().__init__(message)


class ObservabilityUnavailable(ObservabilityError):
    code = "observability_unavailable"
    status_code = 503

    def __init__(self, message: str = "Trace 日志服务暂不可用") -> None:
        super().__init__(message)


class ObservabilityUpstreamError(ObservabilityError):
    code = "observability_upstream_error"
    status_code = 502

    def __init__(self, message: str = "Trace 日志服务返回异常") -> None:
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class TraceQuery:
    limit: int
    cursor: str | None
    start_time: str | None
    end_time: str | None
    sort: Literal["start_time", "latency_ms"]
    order: Literal["asc", "desc"]
    include_spans: bool
    session_identifiers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SpanQuery:
    trace_id: str
    limit: int
    cursor: str | None


@dataclass(frozen=True, slots=True)
class OtlpResponse:
    status_code: int
    content: bytes
    content_type: str | None
    content_encoding: str | None


class TraceAdapter(Protocol):
    async def health(self) -> bool: ...

    async def export_traces(
        self,
        payload: bytes,
        *,
        content_type: str,
        content_encoding: str | None,
    ) -> OtlpResponse: ...

    async def list_traces(self, query: TraceQuery) -> Mapping[str, Any]: ...

    async def list_spans(self, query: SpanQuery) -> Mapping[str, Any]: ...

    async def close(self) -> None: ...


class ProcessAdapter(Protocol):
    @property
    def managed(self) -> bool: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...


class ObservabilityGateway:
    """隐藏 Phoenix 进程、HTTP 和 SQLite 细节的深 Module。"""

    def __init__(
        self,
        settings: ObservabilitySettings,
        *,
        trace_adapter: TraceAdapter | None = None,
        process_adapter: ProcessAdapter | None = None,
    ) -> None:
        if trace_adapter is None or process_adapter is None:
            from unilabos.observability.phoenix import (
                PhoenixHttpAdapter,
                PhoenixProcessAdapter,
            )

            trace_adapter = trace_adapter or PhoenixHttpAdapter(settings)
            process_adapter = process_adapter or PhoenixProcessAdapter(settings)
        self.settings = settings
        self._trace_adapter = trace_adapter
        self._process_adapter = process_adapter
        self._state = "disabled" if not settings.enabled else "stopped"
        self._last_error: str | None = None
        self._started = False
        self._closed = False
        self._lifecycle_lock = asyncio.Lock()

    async def startup(self) -> None:
        """启动或连接 Phoenix；失败只降级观测功能。"""

        async with self._lifecycle_lock:
            if self._started:
                return
            self._started = True
            if not self.settings.enabled:
                self._state = "disabled"
                return
            self._state = "starting"
            try:
                await asyncio.to_thread(self._process_adapter.start)
                if not await self._trace_adapter.health():
                    raise ObservabilityUnavailable("Phoenix 健康检查未通过")
                self._mark_ready()
                _LOGGER.info("Phoenix trace 日志服务已就绪")
            except Exception as exc:  # noqa: BLE001 - 可观测性不能阻断主运行时
                self._mark_degraded(exc)
                _LOGGER.error("Phoenix trace 日志服务启动失败：%s", exc)

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            try:
                await self._trace_adapter.close()
            finally:
                await asyncio.to_thread(self._process_adapter.stop)
            if self.settings.enabled:
                self._state = "stopped"

    async def status(self) -> dict[str, Any]:
        if self.settings.enabled and self._started and not self._closed:
            try:
                if await self._trace_adapter.health():
                    self._mark_ready()
                else:
                    self._mark_degraded(
                        ObservabilityUnavailable("Phoenix 健康检查未通过")
                    )
            except Exception as exc:  # noqa: BLE001 - 状态查询必须稳定返回
                self._mark_degraded(exc)
        return {
            "enabled": self.settings.enabled,
            "state": self._state,
            "provider": "phoenix",
            "storage": "sqlite",
            "project_name": self.settings.project_name,
            "managed_process": self._process_adapter.managed,
            "last_error": self._last_error,
        }

    async def export_traces(
        self,
        payload: bytes,
        *,
        content_type: str,
        content_encoding: str | None,
    ) -> OtlpResponse:
        self._require_enabled()
        try:
            response = await self._trace_adapter.export_traces(
                payload,
                content_type=content_type,
                content_encoding=content_encoding,
            )
        except ObservabilityError as exc:
            self._mark_degraded(exc)
            raise
        except Exception as exc:
            self._mark_degraded(exc)
            raise ObservabilityUnavailable() from exc
        self._mark_ready()
        return response

    async def list_traces(self, query: TraceQuery) -> dict[str, Any]:
        self._require_enabled()
        payload = await self._query(lambda: self._trace_adapter.list_traces(query))
        return {
            "project_name": self.settings.project_name,
            "traces": list(payload.get("data", [])),
            "next_cursor": payload.get("next_cursor"),
        }

    async def get_trace(self, query: SpanQuery) -> dict[str, Any]:
        self._require_enabled()
        payload = await self._query(lambda: self._trace_adapter.list_spans(query))
        return {
            "project_name": self.settings.project_name,
            "trace_id": query.trace_id,
            "spans": list(payload.get("data", [])),
            "next_cursor": payload.get("next_cursor"),
        }

    async def _query(
        self,
        operation: Callable[[], Awaitable[Mapping[str, Any]]],
    ) -> Mapping[str, Any]:
        try:
            payload = await operation()
        except ObservabilityError as exc:
            self._mark_degraded(exc)
            raise
        except Exception as exc:
            self._mark_degraded(exc)
            raise ObservabilityUnavailable() from exc
        self._mark_ready()
        return payload

    def _require_enabled(self) -> None:
        if not self.settings.enabled:
            raise ObservabilityDisabled()

    def _mark_ready(self) -> None:
        self._state = "ready"
        self._last_error = None

    def _mark_degraded(self, error: Exception) -> None:
        self._state = "degraded"
        message = (
            str(error).strip()
            if isinstance(error, ObservabilityError)
            else "Trace 日志服务内部错误，请查看本地日志"
        )
        self._last_error = message[:500] if message else "Trace 日志服务暂不可用"
