"""Workflow 公共 JSON 资源预算与完整值深度合同测试。"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import pytest
from fastapi import FastAPI

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow import json_codec
from unilabos.workflow.json_codec import decode_json_bytes, encode_json
from unilabos.workflow.schema import (
    WorkflowSchemaError,
    normalize_value,
    parse_input_contract,
    parse_value_schema,
)

_HTTP_BODY_LIMIT = 8 * 1024 * 1024
_EXTERNAL_INTEGER_DIGITS = 4096
_INVALID_INPUT = {
    "code": 400,
    "error": {
        "code": "invalid_input",
        "message": "提交内容格式不正确",
    },
}


@pytest.fixture(autouse=True)
def _preserve_interpreter_int_string_limit() -> Iterator[None]:
    """公共 decoder 和 HTTP adapter 都不能修改进程级整数转换限制。"""

    original = sys.get_int_max_str_digits()
    yield
    assert sys.get_int_max_str_digits() == original


def _integer_token(digits: int, *, negative: bool = False) -> bytes:
    prefix = b"-1" if negative else b"1"
    return prefix + b"0" * (digits - 1)


@pytest.mark.parametrize("negative", [False, True], ids=["positive", "negative"])
def test_external_integer_budget_accepts_4096_digits(negative: bool) -> None:
    token = _integer_token(_EXTERNAL_INTEGER_DIGITS, negative=negative)
    expected = 10 ** (_EXTERNAL_INTEGER_DIGITS - 1)
    if negative:
        expected = -expected

    decoded = decode_json_bytes(
        token,
        max_integer_digits=_EXTERNAL_INTEGER_DIGITS,
    )

    assert type(decoded) is int
    assert decoded == expected


@pytest.mark.parametrize("negative", [False, True], ids=["positive", "negative"])
def test_external_integer_budget_rejects_4097_digits_before_bigint_construction(
    monkeypatch: pytest.MonkeyPatch,
    negative: bool,
) -> None:
    decoder_calls: list[str] = []

    def record_bigint_construction(raw: str) -> int:
        decoder_calls.append(raw)
        return 0

    monkeypatch.setattr(
        json_codec,
        "_decode_json_integer",
        record_bigint_construction,
    )

    with pytest.raises(ValueError):
        decode_json_bytes(
            _integer_token(_EXTERNAL_INTEGER_DIGITS + 1, negative=negative),
            max_integer_digits=_EXTERNAL_INTEGER_DIGITS,
        )

    assert decoder_calls == []


def test_trusted_decoder_default_still_round_trips_5001_digit_integer() -> None:
    value = -(10**5000)

    encoded = encode_json(value)
    decoded = decode_json_bytes(encoded)

    assert encoded == _integer_token(5001, negative=True)
    assert type(decoded) is int
    assert decoded == value


class _RecordingWorkflowService:
    """只记录边界穿透，不执行数据库或文件副作用。"""

    def __init__(self) -> None:
        self.create_calls = 0
        self.side_effects: list[int] = []

    def create_workflow(self, **values: Any) -> dict[str, bool]:
        self.create_calls += 1
        self.side_effects.append(len(values["name"]))
        return {"accepted": True}


class _ReceiveSpy:
    def __init__(self, chunks: Sequence[bytes]) -> None:
        self._chunks = chunks
        self.calls = 0

    async def __call__(self) -> dict[str, Any]:
        if self.calls >= len(self._chunks):
            raise AssertionError("HTTP adapter 在请求结束后仍继续读取 body")
        index = self.calls
        self.calls += 1
        return {
            "type": "http.request",
            "body": self._chunks[index],
            "more_body": index + 1 < len(self._chunks),
        }


def _invoke_asgi_json(
    app: FastAPI,
    *,
    chunks: Sequence[bytes],
    extra_headers: Sequence[tuple[bytes, bytes]] = (),
) -> tuple[int, dict[str, Any], _ReceiveSpy]:
    receive = _ReceiveSpy(chunks)
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "POST",
        "root_path": "",
        "path": "/api/v1/workflows",
        "raw_path": b"/api/v1/workflows",
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            *extra_headers,
        ],
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


def _assert_rejected_without_service_side_effect(
    *,
    result: tuple[int, dict[str, Any], _ReceiveSpy],
    service: _RecordingWorkflowService,
) -> _ReceiveSpy:
    status, payload, receive = result
    assert status == 400
    assert payload == _INVALID_INPUT
    assert service.create_calls == 0
    assert service.side_effects == []
    return receive


def test_workflow_http_rejects_4097_digit_integer_before_bigint_and_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decoder_calls: list[int] = []

    def record_bigint_construction(raw: str) -> int:
        decoder_calls.append(len(raw))
        return 0

    monkeypatch.setattr(
        json_codec,
        "_decode_json_integer",
        record_bigint_construction,
    )
    service = _RecordingWorkflowService()
    body = (
        b'{"name":"must not persist","future_integer":'
        + _integer_token(_EXTERNAL_INTEGER_DIGITS + 1)
        + b"}"
    )

    result = _invoke_asgi_json(
        create_workflow_app(service),
        chunks=[body],
        extra_headers=[(b"content-length", str(len(body)).encode("ascii"))],
    )

    receive = _assert_rejected_without_service_side_effect(
        result=result,
        service=service,
    )
    assert receive.calls == 1
    assert decoder_calls == []


def test_declared_oversized_workflow_body_is_rejected_before_reading() -> None:
    service = _RecordingWorkflowService()
    tiny_valid_body = b'{"name":"must not persist"}'

    result = _invoke_asgi_json(
        create_workflow_app(service),
        chunks=[tiny_valid_body],
        extra_headers=[(b"content-length", str(_HTTP_BODY_LIMIT + 1).encode("ascii"))],
    )

    receive = _assert_rejected_without_service_side_effect(
        result=result,
        service=service,
    )
    assert receive.calls == 0


@pytest.mark.parametrize(
    "extra_headers",
    [
        pytest.param([], id="missing-content-length"),
        pytest.param(
            [(b"transfer-encoding", b"chunked")],
            id="chunked",
        ),
    ],
)
def test_streamed_workflow_body_stops_reading_at_first_oversized_chunk(
    extra_headers: list[tuple[bytes, bytes]],
) -> None:
    service = _RecordingWorkflowService()
    one_mib_chunk = b" " * (1024 * 1024)
    chunks = [one_mib_chunk] * 8 + [b"x", b"must-not-be-read"]

    result = _invoke_asgi_json(
        create_workflow_app(service),
        chunks=chunks,
        extra_headers=extra_headers,
    )

    receive = _assert_rejected_without_service_side_effect(
        result=result,
        service=service,
    )
    assert receive.calls == 9


def test_workflow_http_accepts_body_at_exactly_eight_mib() -> None:
    service = _RecordingWorkflowService()
    prefix = b'{"name":"'
    suffix = b'","tags":[],"description":null,"meta_data":{}}'
    name_length = _HTTP_BODY_LIMIT - len(prefix) - len(suffix)
    body = b"".join((prefix, b"a" * name_length, suffix))
    assert len(body) == _HTTP_BODY_LIMIT

    status, payload, receive = _invoke_asgi_json(
        create_workflow_app(service),
        chunks=[body],
        extra_headers=[(b"content-length", str(_HTTP_BODY_LIMIT).encode("ascii"))],
    )

    assert status == 201
    assert payload == {"code": 0, "data": {"accepted": True}}
    assert receive.calls == 1
    assert service.create_calls == 1
    assert service.side_effects == [name_length]


def _make_deep_object(depth: int) -> dict[str, Any]:
    """迭代构造指定 object 容器深度的单链 JSON。"""

    assert depth > 0
    root: dict[str, Any] = {}
    cursor = root
    for _ in range(depth - 1):
        child: dict[str, Any] = {}
        cursor["next"] = child
        cursor = child
    cursor["value"] = "leaf"
    return root


def _assert_deep_object(value: object, depth: int) -> None:
    """迭代验证深链，避免测试自身触发递归比较。"""

    cursor = value
    for _ in range(depth - 1):
        assert type(cursor) is dict
        assert set(cursor) == {"next"}
        cursor = cursor["next"]
    assert cursor == {"value": "leaf"}


def _assert_schema_error(
    operation: Callable[[], object],
    *,
    code: str,
    path: str,
) -> None:
    with pytest.raises(WorkflowSchemaError) as caught:
        operation()

    assert caught.value.code == code
    assert caught.value.path == path


def test_standalone_opaque_object_accepts_complete_depth_10000() -> None:
    raw = _make_deep_object(10000)

    normalized = normalize_value(
        parse_value_schema({"type": "object"}),
        raw,
    )

    assert normalized is not raw
    _assert_deep_object(normalized, 10000)


def test_standalone_opaque_object_rejects_complete_depth_10001() -> None:
    raw = _make_deep_object(10001)

    _assert_schema_error(
        lambda: normalize_value(
            parse_value_schema({"type": "object"}),
            raw,
        ),
        code="invalid_value",
        path="/next" * 10000,
    )


def test_list_of_opaque_object_accepts_item_depth_9999() -> None:
    raw_item = _make_deep_object(9999)

    normalized = normalize_value(
        parse_value_schema(
            {
                "type": "array",
                "items": {"type": "object"},
            }
        ),
        [raw_item],
    )

    assert type(normalized) is list
    assert normalized[0] is not raw_item
    _assert_deep_object(normalized[0], 9999)


def test_list_of_opaque_object_rejects_item_depth_10000_with_full_pointer() -> None:
    raw_item = _make_deep_object(10000)

    _assert_schema_error(
        lambda: normalize_value(
            parse_value_schema(
                {
                    "type": "array",
                    "items": {"type": "object"},
                }
            ),
            [raw_item],
        ),
        code="invalid_value",
        path="/0" + "/next" * 9999,
    )


def _input_contract_with_default(default: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "parameters": [
            {
                "name": "settings",
                "schema": {"type": "object"},
                "required": False,
                "default": default,
            }
        ],
    }


def test_input_default_depth_9997_can_parse_and_dump() -> None:
    raw_default = _make_deep_object(9997)

    contract = parse_input_contract(_input_contract_with_default(raw_default))
    dumped_default = contract.to_dict()["parameters"][0]["default"]

    assert dumped_default is not raw_default
    _assert_deep_object(dumped_default, 9997)


def test_input_default_depth_9998_is_invalid_contract_with_full_pointer() -> None:
    raw_default = _make_deep_object(9998)

    _assert_schema_error(
        lambda: parse_input_contract(_input_contract_with_default(raw_default)),
        code="invalid_contract",
        path="/parameters/0/default" + "/next" * 9997,
    )
