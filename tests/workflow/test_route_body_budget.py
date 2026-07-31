"""Workflow body route 的 MIME 无关读取预算合同测试。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

import pytest
from fastapi import FastAPI

from unilabos.app.workflow_api import create_workflow_app

_HTTP_BODY_LIMIT = 8 * 1024 * 1024
_WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
_INVALID_INPUT = {
    "code": 400,
    "error": {
        "code": "invalid_input",
        "message": "提交内容格式不正确",
    },
}
_INVALID_SSE_CURSOR = {
    "error": {
        "code": "invalid_input",
        "message": "Last-Event-ID must be a non-negative integer",
    }
}

_BODY_ROUTES = [
    pytest.param(
        "POST",
        "/api/v1/workflows",
        id="post-workflow",
    ),
    pytest.param(
        "PUT",
        f"/api/v1/workflows/{_WORKFLOW_UUID}",
        id="put-workflow",
    ),
]
_NON_JSON_CONTENT_TYPES = [
    pytest.param(b"text/plain", id="text-plain"),
    pytest.param(None, id="missing-content-type"),
    pytest.param(b"application/x-unilab-unknown", id="unknown-mime"),
]
_STREAM_FRAMING = [
    pytest.param([], id="missing-content-length"),
    pytest.param(
        [(b"transfer-encoding", b"chunked")],
        id="chunked",
    ),
]


class _RecordingWorkflowService:
    """记录 route 是否穿透到业务调用，不执行真实持久副作用。"""

    def __init__(self) -> None:
        self.body_calls: list[str] = []
        self.side_effects: list[str] = []
        self.read_calls: list[str] = []

    def create_workflow(self, **_values: Any) -> dict[str, bool]:
        self.body_calls.append("create_workflow")
        self.side_effects.append("created")
        return {"accepted": True}

    def update_workflow(
        self,
        workflow_uuid: str,
        **_values: Any,
    ) -> dict[str, str]:
        self.body_calls.append("update_workflow")
        self.side_effects.append(f"updated:{workflow_uuid}")
        return {"uuid": workflow_uuid}

    def list_workflows(
        self,
        *,
        page: int,
        page_size: int,
        name: str,
    ) -> dict[str, Any]:
        self.read_calls.append("list_workflows")
        return {
            "items": [],
            "total": 0,
            "page": page,
            "page_size": page_size,
            "name": name,
        }


class _ReceiveSpy:
    def __init__(self, chunks: Sequence[bytes]) -> None:
        self._chunks = chunks
        self.calls = 0

    async def __call__(self) -> dict[str, Any]:
        if self.calls >= len(self._chunks):
            raise AssertionError("Workflow route 在 body 结束后仍继续 receive")
        index = self.calls
        self.calls += 1
        return {
            "type": "http.request",
            "body": self._chunks[index],
            "more_body": index + 1 < len(self._chunks),
        }


def _invoke_asgi(
    app: FastAPI,
    *,
    method: str,
    path: str,
    chunks: Sequence[bytes],
    content_type: bytes | None,
    extra_headers: Sequence[tuple[bytes, bytes]] = (),
) -> tuple[int, dict[str, Any], _ReceiveSpy]:
    receive = _ReceiveSpy(chunks)
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    headers: list[tuple[bytes, bytes]] = []
    if content_type is not None:
        headers.append((b"content-type", content_type))
    headers.extend(extra_headers)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "http",
        "method": method,
        "root_path": "",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))

    status = next(
        message["status"]
        for message in sent
        if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return status, json.loads(body), receive


def _assert_no_body_service_side_effect(
    service: _RecordingWorkflowService,
) -> None:
    assert service.body_calls == []
    assert service.side_effects == []


@pytest.mark.parametrize(("method", "path"), _BODY_ROUTES)
@pytest.mark.parametrize("content_type", _NON_JSON_CONTENT_TYPES)
def test_declared_oversized_body_route_rejects_before_receive_for_any_mime(
    method: str,
    path: str,
    content_type: bytes | None,
) -> None:
    service = _RecordingWorkflowService()

    status, payload, receive = _invoke_asgi(
        create_workflow_app(service),
        method=method,
        path=path,
        chunks=[b"{}"],
        content_type=content_type,
        extra_headers=[(b"content-length", str(_HTTP_BODY_LIMIT + 1).encode("ascii"))],
    )

    assert status == 400
    assert payload == _INVALID_INPUT
    assert receive.calls == 0
    _assert_no_body_service_side_effect(service)


@pytest.mark.parametrize(("method", "path"), _BODY_ROUTES)
@pytest.mark.parametrize("content_type", _NON_JSON_CONTENT_TYPES)
@pytest.mark.parametrize("framing_headers", _STREAM_FRAMING)
def test_streamed_oversized_body_route_stops_at_first_excess_byte_for_any_mime(
    method: str,
    path: str,
    content_type: bytes | None,
    framing_headers: list[tuple[bytes, bytes]],
) -> None:
    service = _RecordingWorkflowService()
    one_mib_chunk = b" " * (1024 * 1024)
    chunks = [one_mib_chunk] * 8 + [b"x", b"must-not-be-read"]

    status, payload, receive = _invoke_asgi(
        create_workflow_app(service),
        method=method,
        path=path,
        chunks=chunks,
        content_type=content_type,
        extra_headers=framing_headers,
    )

    assert status == 400
    assert payload == _INVALID_INPUT
    assert receive.calls == 9
    _assert_no_body_service_side_effect(service)


@pytest.mark.parametrize("content_type", _NON_JSON_CONTENT_TYPES)
def test_exact_limit_non_json_body_is_read_once_then_keeps_validation_envelope(
    content_type: bytes | None,
) -> None:
    service = _RecordingWorkflowService()
    body = b"x" * _HTTP_BODY_LIMIT

    status, payload, receive = _invoke_asgi(
        create_workflow_app(service),
        method="POST",
        path="/api/v1/workflows",
        chunks=[body],
        content_type=content_type,
        extra_headers=[(b"content-length", str(_HTTP_BODY_LIMIT).encode("ascii"))],
    )

    assert status == 400
    assert payload == _INVALID_INPUT
    assert receive.calls == 1
    _assert_no_body_service_side_effect(service)


def test_bodyless_get_with_json_content_type_does_not_read_request_body() -> None:
    service = _RecordingWorkflowService()

    status, payload, receive = _invoke_asgi(
        create_workflow_app(service),
        method="GET",
        path="/api/v1/workflows",
        chunks=[b""],
        content_type=b"application/json",
    )

    assert status == 200
    assert payload["code"] == 0
    assert payload["data"]["items"] == []
    assert receive.calls == 0
    assert service.read_calls == ["list_workflows"]
    _assert_no_body_service_side_effect(service)


def test_bodyless_sse_route_with_json_content_type_does_not_read_request_body() -> None:
    service = _RecordingWorkflowService()

    status, payload, receive = _invoke_asgi(
        create_workflow_app(service),
        method="GET",
        path="/api/v1/events",
        chunks=[b""],
        content_type=b"application/json",
        extra_headers=[(b"last-event-id", b"not-an-integer")],
    )

    assert status == 400
    assert payload == _INVALID_SSE_CURSOR
    assert receive.calls == 0
    assert service.read_calls == []
    _assert_no_body_service_side_effect(service)
