"""Electron 使用的 observability HTTP Adapter。"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Literal, cast

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import QueryParams

from unilabos.observability.gateway import (
    ObservabilityError,
    ObservabilityGateway,
    SpanQuery,
    TraceQuery,
)

_LOGGER = logging.getLogger(__name__)
_TRACE_ID = re.compile(r"^[0-9a-fA-F]{32}$")
_OTLP_CONTENT_TYPES = {"application/x-protobuf"}


class _RequestError(ValueError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _success(data: Any) -> JSONResponse:
    return JSONResponse(status_code=200, content={"code": 0, "data": data})


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "code": status_code,
            "error": {"code": code, "message": message},
        },
    )


def _observability_error(error: ObservabilityError) -> JSONResponse:
    return _error(error.status_code, error.code, str(error))


def _reject_non_loopback(request: Request) -> JSONResponse | None:
    client = request.client
    if client is not None:
        try:
            if ipaddress.ip_address(client.host).is_loopback:
                return None
        except ValueError:
            pass
    return _error(403, "access_denied", "Trace 日志 Interface 仅允许本机访问")


async def _read_limited_body(request: Request, limit: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length, 10)
        except ValueError:
            raise _RequestError(
                400, "invalid_input", "Content-Length 格式不正确"
            ) from None
        if declared_length < 0:
            raise _RequestError(400, "invalid_input", "Content-Length 格式不正确")
        if declared_length > limit:
            raise _RequestError(413, "payload_too_large", "Trace 上报内容过大")

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise _RequestError(413, "payload_too_large", "Trace 上报内容过大")
        body.extend(chunk)
    return bytes(body)


def _reject_unknown_query(query: QueryParams, allowed: set[str]) -> None:
    unknown = set(query.keys()) - allowed
    if unknown:
        raise _RequestError(400, "invalid_input", "查询参数格式不正确")


def _single(query: QueryParams, name: str) -> str | None:
    values = query.getlist(name)
    if len(values) > 1:
        raise _RequestError(400, "invalid_input", f"{name} 不能重复")
    return values[0] if values else None


def _bounded_int(
    query: QueryParams,
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = _single(query, name)
    if raw is None:
        return default
    try:
        parsed = int(raw, 10)
    except ValueError:
        raise _RequestError(400, "invalid_input", f"{name} 格式不正确") from None
    if not minimum <= parsed <= maximum:
        raise _RequestError(400, "invalid_input", f"{name} 超出允许范围")
    return parsed


def _optional_cursor(query: QueryParams) -> str | None:
    cursor = _single(query, "cursor")
    if cursor is not None and (not cursor or len(cursor) > 4096):
        raise _RequestError(400, "invalid_input", "cursor 格式不正确")
    return cursor


def _optional_datetime(
    query: QueryParams, name: str
) -> tuple[str | None, datetime | None]:
    raw = _single(query, name)
    if raw is None:
        return None, None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise _RequestError(400, "invalid_input", f"{name} 格式不正确") from None
    if parsed.tzinfo is None:
        raise _RequestError(400, "invalid_input", f"{name} 必须包含时区")
    return raw, parsed


def _enum(
    query: QueryParams,
    name: str,
    *,
    default: str,
    allowed: set[str],
) -> str:
    value = _single(query, name) or default
    if value not in allowed:
        raise _RequestError(400, "invalid_input", f"{name} 格式不正确")
    return value


def _boolean(query: QueryParams, name: str, *, default: bool) -> bool:
    raw = _single(query, name)
    if raw is None:
        return default
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise _RequestError(400, "invalid_input", f"{name} 格式不正确")


def _parse_trace_query(query: QueryParams) -> TraceQuery:
    _reject_unknown_query(
        query,
        {
            "limit",
            "cursor",
            "start_time",
            "end_time",
            "sort",
            "order",
            "include_spans",
            "session_identifier",
        },
    )
    start_time, parsed_start = _optional_datetime(query, "start_time")
    end_time, parsed_end = _optional_datetime(query, "end_time")
    if (
        parsed_start is not None
        and parsed_end is not None
        and parsed_start >= parsed_end
    ):
        raise _RequestError(400, "invalid_input", "start_time 必须早于 end_time")
    sessions = tuple(query.getlist("session_identifier"))
    if len(sessions) > 20 or any(not item or len(item) > 256 for item in sessions):
        raise _RequestError(400, "invalid_input", "session_identifier 格式不正确")
    return TraceQuery(
        limit=_bounded_int(query, "limit", default=50, minimum=1, maximum=1000),
        cursor=_optional_cursor(query),
        start_time=start_time,
        end_time=end_time,
        sort=cast(
            Literal["start_time", "latency_ms"],
            _enum(
                query,
                "sort",
                default="start_time",
                allowed={"start_time", "latency_ms"},
            ),
        ),
        order=cast(
            Literal["asc", "desc"],
            _enum(
                query,
                "order",
                default="desc",
                allowed={"asc", "desc"},
            ),
        ),
        include_spans=_boolean(query, "include_spans", default=False),
        session_identifiers=sessions,
    )


def _parse_span_query(query: QueryParams, trace_id: str) -> SpanQuery:
    _reject_unknown_query(query, {"limit", "cursor"})
    if _TRACE_ID.fullmatch(trace_id) is None:
        raise _RequestError(400, "invalid_input", "trace_id 格式不正确")
    return SpanQuery(
        trace_id=trace_id.lower(),
        limit=_bounded_int(query, "limit", default=200, minimum=1, maximum=1000),
        cursor=_optional_cursor(query),
    )


def create_observability_router(gateway: ObservabilityGateway) -> APIRouter:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await gateway.startup()
        try:
            yield
        finally:
            await gateway.shutdown()

    router = APIRouter(
        prefix="/api/v1/observability",
        tags=["observability"],
        lifespan=lifespan,
    )

    @router.get("/status")
    async def status(request: Request) -> JSONResponse:
        if denied := _reject_non_loopback(request):
            return denied
        return _success(await gateway.status())

    @router.post("/otlp/v1/traces")
    async def export_traces(request: Request) -> Response:
        if denied := _reject_non_loopback(request):
            return denied
        try:
            content_type = request.headers.get("content-type", "")
            mime = content_type.split(";", 1)[0].strip().lower()
            if mime not in _OTLP_CONTENT_TYPES:
                raise _RequestError(
                    415,
                    "unsupported_media_type",
                    "仅支持 OTLP protobuf",
                )
            content_encoding = request.headers.get("content-encoding")
            if content_encoding:
                content_encoding = content_encoding.strip().lower()
                if content_encoding not in {"gzip", "identity"}:
                    raise _RequestError(
                        415,
                        "unsupported_media_type",
                        "仅支持 gzip 内容编码",
                    )
                if content_encoding == "identity":
                    content_encoding = None
            payload = await _read_limited_body(
                request,
                gateway.settings.max_ingest_bytes,
            )
            upstream = await gateway.export_traces(
                payload,
                content_type=mime,
                content_encoding=content_encoding,
            )
            headers: dict[str, str] = {}
            if upstream.content_type:
                headers["Content-Type"] = upstream.content_type
            if upstream.content_encoding:
                headers["Content-Encoding"] = upstream.content_encoding
            return Response(
                status_code=upstream.status_code,
                content=upstream.content,
                headers=headers,
            )
        except _RequestError as exc:
            return _error(exc.status_code, exc.code, str(exc))
        except ObservabilityError as exc:
            return _observability_error(exc)

    @router.get("/traces")
    async def list_traces(request: Request) -> JSONResponse:
        if denied := _reject_non_loopback(request):
            return denied
        try:
            query = _parse_trace_query(request.query_params)
            return _success(await gateway.list_traces(query))
        except _RequestError as exc:
            return _error(exc.status_code, exc.code, str(exc))
        except ObservabilityError as exc:
            return _observability_error(exc)
        except Exception:
            _LOGGER.exception("查询 trace 列表失败")
            return _error(500, "internal_error", "查询 Trace 日志失败")

    @router.get("/traces/{trace_id}")
    async def get_trace(trace_id: str, request: Request) -> JSONResponse:
        if denied := _reject_non_loopback(request):
            return denied
        try:
            query = _parse_span_query(request.query_params, trace_id)
            result = await gateway.get_trace(query)
            if not result["spans"]:
                return _error(404, "trace_not_found", "Trace 日志不存在")
            return _success(result)
        except _RequestError as exc:
            return _error(exc.status_code, exc.code, str(exc))
        except ObservabilityError as exc:
            return _observability_error(exc)
        except Exception:
            _LOGGER.exception("查询 trace 详情失败")
            return _error(500, "internal_error", "查询 Trace 日志失败")

    return router


def install_observability_api(
    app: FastAPI,
    gateway: ObservabilityGateway,
) -> None:
    app.include_router(create_observability_router(gateway))


def create_observability_app(gateway: ObservabilityGateway) -> FastAPI:
    app = FastAPI()
    install_observability_api(app, gateway)
    return app
