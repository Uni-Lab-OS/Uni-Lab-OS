"""Round 02E pure Authoring HTTP Interface 的独立合同测试。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.workflow.test_authoring_engine import (
    PREPARE_NODE_UUID,
    WORKFLOW_UUID,
    EngineContext,
    _compile,
    _empty_graph,
    _opened_engine,
    _source,
)
from unilabos.app import workflow_api
from unilabos.workflow.models import CandidateCompilation

SOURCE_URI = "package://lab/workflows/round02e.py"
OTHER_WORKFLOW_UUID = "10000000-0000-4000-8000-000000000002"
FINGERPRINT = "sha256:" + "e" * 64
HTTP_BODY_LIMIT = 8 * 1024 * 1024
EXTERNAL_INTEGER_DIGITS = 4096
MAX_JSON_DEPTH = 10_000

INVALID_INPUT = {
    "code": 400,
    "error": {
        "code": "invalid_input",
        "message": "提交内容格式不正确",
    },
}
CATALOG_UNAVAILABLE = {
    "code": 503,
    "error": {
        "code": "template_catalog_unavailable",
        "message": "设备动作模板暂不可用，请稍后重试",
    },
}
INTERNAL_ERROR = {
    "code": 500,
    "error": {
        "code": "internal_error",
        "message": "本地工作流服务出现错误，请重试或查看日志",
    },
}


def _changeset() -> dict[str, Any]:
    return {
        "kind": "source_only",
        "created_node_uuids": [],
        "updated_node_uuids": [],
        "deleted_node_uuids": [],
        "created_edge_uuids": [],
        "updated_edge_uuids": [],
        "deleted_edge_uuids": [],
        "reserved_metadata_changed": False,
    }


def _diagnostic_result(
    code: str,
    *,
    source_range: dict[str, int] | None = None,
) -> CandidateCompilation:
    diagnostic: dict[str, Any] = {
        "severity": "error",
        "code": code,
        "message": f"stable {code} diagnostic",
    }
    if source_range is not None:
        diagnostic["source_range"] = source_range
    return CandidateCompilation(
        diagnostics=[diagnostic],
        graph=None,
        normalized_python_source=None,
        source_map=[],
        changeset=None,
        compiler_version="round02e-spy/v1",
        template_catalog_fingerprint=FINGERPRINT,
    )


class RecordingTransformEngine:
    """只实现冻结的三个 transform，并记录唯一允许的边界调用。"""

    compiler_version = "round02e-spy/v1"
    template_catalog_fingerprint = FINGERPRINT

    def __init__(
        self,
        mode: Literal[
            "valid",
            "diagnostic",
            "catalog-unavailable",
            "internal-error",
            "invalid-dto",
            "invalid-range",
        ] = "valid",
    ) -> None:
        self.mode = mode
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _result(
        self,
        method: str,
        values: dict[str, Any],
    ) -> CandidateCompilation:
        self.calls.append((method, values))
        if self.mode == "diagnostic":
            code = (
                "python_syntax_error"
                if values.get("python_source") == "syntax error"
                else "template_catalog_mismatch"
            )
            return _diagnostic_result(code)
        if self.mode == "catalog-unavailable":
            return _diagnostic_result("template_catalog_unavailable")
        if self.mode == "internal-error":
            raise RuntimeError("secret database password must never cross HTTP")
        if self.mode == "invalid-dto":
            result = _diagnostic_result("invalid_action_call")
            result.diagnostics[0]["private_detail"] = "must not leak"
            return result
        if self.mode == "invalid-range":
            # “中”只有一个 UTF-16 code unit，公开 end column 最大为 2；UTF-8 byte
            # 长度却为 3。这个出站值专门防止 adapter 继续按 byte 验证。
            return _diagnostic_result(
                "invalid_action_call",
                source_range={
                    "start_line": 1,
                    "start_column": 1,
                    "end_line": 1,
                    "end_column": 3,
                },
            )

        graph = values.get("applied_graph", values.get("graph"))
        source = values.get("python_source", "")
        return CandidateCompilation(
            diagnostics=[],
            graph=deepcopy(graph),
            normalized_python_source=(
                source if source.endswith("\n") else source + "\n"
            ),
            source_map=[],
            changeset=_changeset(),
            compiler_version=self.compiler_version,
            template_catalog_fingerprint=self.template_catalog_fingerprint,
        )

    def compile(self, **values: Any) -> CandidateCompilation:
        return self._result("compile", values)

    def generate_python(self, **values: Any) -> CandidateCompilation:
        return self._result("generate_python", values)

    def validate(self, **values: Any) -> CandidateCompilation:
        return self._result("validate", values)


def _transform_router(engine: Any):
    factory = getattr(workflow_api, "create_authoring_transform_router", None)
    assert callable(factory), "缺少冻结 seam: create_authoring_transform_router(engine)"
    return factory(engine)


def _transform_app(engine: Any) -> FastAPI:
    factory = getattr(workflow_api, "create_authoring_transform_app", None)
    assert callable(factory), (
        "缺少 focused seam: create_authoring_transform_app(engine)"
    )
    app = factory(engine)
    assert isinstance(app, FastAPI)
    return app


def _compile_body(
    *,
    python_source: str = "value = 1\n",
    applied_graph: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "workflow_uuid": WORKFLOW_UUID,
        "revision": 7,
        "python_source": python_source,
        "source_uri": SOURCE_URI,
        "applied_graph": _empty_graph() if applied_graph is None else applied_graph,
    }


def _generate_body(graph: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "workflow_uuid": WORKFLOW_UUID,
        "revision": 7,
        "graph": _empty_graph() if graph is None else graph,
        "source_uri": SOURCE_URI,
    }


def _validate_body(
    graph: dict[str, Any] | None = None,
    *,
    python_source: str = "value = 1\n",
) -> dict[str, Any]:
    return {
        "workflow_uuid": WORKFLOW_UUID,
        "revision": 7,
        "graph": _empty_graph() if graph is None else graph,
        "python_source": python_source,
        "source_uri": SOURCE_URI,
    }


def _assert_success_shape(payload: dict[str, Any]) -> dict[str, Any]:
    assert set(payload) == {"code", "data"}
    assert payload["code"] == 0
    data = payload["data"]
    assert set(data) == {
        "diagnostics",
        "graph",
        "normalized_python_source",
        "source_map",
        "changeset",
        "compiler_version",
        "template_catalog_fingerprint",
    }
    assert "candidate_hash" not in data
    assert "draft_hash" not in data
    assert "expected_workflow_revision" not in data
    return data


@pytest.mark.parametrize(
    ("path", "body", "method", "expected_keys"),
    [
        pytest.param(
            "/api/v1/authoring/compile",
            _compile_body(),
            "compile",
            {
                "workflow_uuid",
                "workflow_revision",
                "python_source",
                "source_uri",
                "applied_graph",
            },
            id="compile",
        ),
        pytest.param(
            "/api/v1/authoring/generate-python",
            _generate_body(),
            "generate_python",
            {"workflow_uuid", "workflow_revision", "graph", "source_uri"},
            id="generate-python",
        ),
        pytest.param(
            "/api/v1/authoring/validate",
            _validate_body(),
            "validate",
            {
                "workflow_uuid",
                "workflow_revision",
                "graph",
                "python_source",
                "source_uri",
            },
            id="validate",
        ),
    ],
)
def test_three_routes_use_backend_identity_closed_success_and_one_engine_call(
    path: str,
    body: dict[str, Any],
    method: str,
    expected_keys: set[str],
) -> None:
    engine = RecordingTransformEngine()

    with TestClient(_transform_app(engine)) as client:
        response = client.post(path, json=body)

    assert response.status_code == 200
    data = _assert_success_shape(response.json())
    assert data["diagnostics"] == []
    assert len(engine.calls) == 1
    called_method, values = engine.calls[0]
    assert called_method == method
    assert set(values) == expected_keys
    assert values["workflow_uuid"] == WORKFLOW_UUID
    assert values["workflow_revision"] == 7
    assert "workflow_id" not in values
    assert "base_revision_id" not in values
    assert "candidate_hash" not in values


def test_transform_router_contains_only_three_pure_routes_and_no_apply() -> None:
    router = _transform_router(RecordingTransformEngine())
    routes = {
        (route.path, frozenset(route.methods or set())) for route in router.routes
    }

    assert routes == {
        ("/api/v1/authoring/compile", frozenset({"POST"})),
        ("/api/v1/authoring/generate-python", frozenset({"POST"})),
        ("/api/v1/authoring/validate", frozenset({"POST"})),
    }
    assert all("apply" not in path and "draft" not in path for path, _ in routes)


@pytest.mark.parametrize(
    ("path", "body"),
    [
        pytest.param(
            "/api/v1/authoring/compile",
            {**_compile_body(), "candidate_hash": "sha256:" + "a" * 64},
            id="compile-extra-apply-token",
        ),
        pytest.param(
            "/api/v1/authoring/generate-python",
            {**_generate_body(), "workflow_id": WORKFLOW_UUID},
            id="generate-workflow-id-alias",
        ),
        pytest.param(
            "/api/v1/authoring/validate",
            {**_validate_body(), "base_revision_id": 7},
            id="validate-revision-alias",
        ),
        pytest.param(
            "/api/v1/authoring/compile",
            {**_compile_body(), "revision": True},
            id="bool-revision",
        ),
        pytest.param(
            "/api/v1/authoring/compile",
            {**_compile_body(), "revision": 0},
            id="zero-revision",
        ),
        pytest.param(
            "/api/v1/authoring/compile",
            {
                **_compile_body(),
                "workflow_uuid": "00000000-0000-0000-0000-000000000000",
            },
            id="nil-workflow-uuid",
        ),
        pytest.param(
            "/api/v1/authoring/compile",
            {**_compile_body(), "source_uri": "  "},
            id="blank-source-uri",
        ),
        pytest.param(
            "/api/v1/authoring/compile",
            {**_compile_body(), "applied_graph": []},
            id="non-object-applied-graph",
        ),
        pytest.param(
            "/api/v1/authoring/generate-python",
            {**_generate_body(), "graph": []},
            id="non-object-graph",
        ),
    ],
)
def test_malformed_or_noncanonical_request_is_400_without_engine_call(
    path: str,
    body: dict[str, Any],
) -> None:
    engine = RecordingTransformEngine()

    with TestClient(_transform_app(engine)) as client:
        response = client.post(path, json=body)

    assert response.status_code == 400
    assert response.json() == INVALID_INPUT
    assert engine.calls == []


@pytest.mark.parametrize(
    "body",
    [
        b"{not-json",
        (
            b'{"workflow_uuid":"'
            + WORKFLOW_UUID.encode("ascii")
            + b'","revision":7,"python_source":"\\ud800",'
            b'"source_uri":"package://lab/bad.py","applied_graph":{}}'
        ),
    ],
    ids=["malformed-json", "unpaired-surrogate-source"],
)
def test_invalid_json_or_non_utf8_encodable_source_is_400_before_engine(
    body: bytes,
) -> None:
    engine = RecordingTransformEngine()

    with TestClient(_transform_app(engine)) as client:
        response = client.post(
            "/api/v1/authoring/compile",
            content=body,
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json() == INVALID_INPUT
    assert engine.calls == []


@pytest.mark.parametrize(
    ("source", "graph", "expected_code"),
    [
        ("syntax error", _empty_graph(), "python_syntax_error"),
        ("valid source", {"not_five_sets": True}, "template_catalog_mismatch"),
    ],
    ids=["syntax", "semantic-graph"],
)
def test_well_formed_transform_diagnostic_stays_http_200_data(
    source: str,
    graph: dict[str, Any],
    expected_code: str,
) -> None:
    engine = RecordingTransformEngine("diagnostic")

    with TestClient(_transform_app(engine)) as client:
        response = client.post(
            "/api/v1/authoring/compile",
            json=_compile_body(python_source=source, applied_graph=graph),
        )

    assert response.status_code == 200
    data = _assert_success_shape(response.json())
    assert data["diagnostics"][0]["code"] == expected_code
    assert data["graph"] is None
    assert data["normalized_python_source"] is None
    assert data["source_map"] == []
    assert data["changeset"] is None
    assert len(engine.calls) == 1


def test_catalog_unavailable_is_503_not_a_compilation_diagnostic() -> None:
    engine = RecordingTransformEngine("catalog-unavailable")

    with TestClient(_transform_app(engine)) as client:
        response = client.post("/api/v1/authoring/compile", json=_compile_body())

    assert response.status_code == 503
    assert response.json() == CATALOG_UNAVAILABLE
    assert len(engine.calls) == 1


@pytest.mark.parametrize(
    "mode",
    ["internal-error", "invalid-dto", "invalid-range"],
    ids=["engine-exception", "open-diagnostic-dto", "illegal-outbound-range"],
)
def test_internal_or_illegal_engine_result_is_sanitized_500(
    mode: Literal["internal-error", "invalid-dto", "invalid-range"],
) -> None:
    engine = RecordingTransformEngine(mode)
    body = _compile_body(python_source="中")

    with TestClient(_transform_app(engine), raise_server_exceptions=False) as client:
        response = client.post("/api/v1/authoring/compile", json=body)

    assert response.status_code == 500
    assert response.json() == INTERNAL_ERROR
    assert "secret database password" not in response.text
    assert len(engine.calls) == 1


@pytest.fixture()
def engine_context(tmp_path: Path) -> Iterator[EngineContext]:
    with _opened_engine(tmp_path / "workflow.db") as context:
        yield context


def _post(client: TestClient, path: str, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post(path, json=body)
    assert response.status_code == 200, response.text
    return _assert_success_shape(response.json())


def _semantic_node_uuids(graph: dict[str, Any]) -> set[str]:
    return {node["uuid"] for node in graph["nodes"]}


def test_real_engine_generate_compile_validate_is_pure_proof_roundtrip(
    engine_context: EngineContext,
) -> None:
    store = engine_context.store
    persistent_tables = (
        "workflow",
        "workflow_node",
        "workflow_edge",
        "workflow_authoring",
        "workflow_task",
        "workflow_node_job",
    )
    before = {table: store.count_rows(table) for table in persistent_tables}

    with TestClient(_transform_app(engine_context.engine)) as client:
        initial = _post(
            client, "/api/v1/authoring/compile", _compile_body(python_source=_source())
        )
        assert initial["graph"] is not None
        generated = _post(
            client,
            "/api/v1/authoring/generate-python",
            _generate_body(initial["graph"]),
        )
        recompiled = _post(
            client,
            "/api/v1/authoring/compile",
            _compile_body(
                python_source=generated["normalized_python_source"],
                applied_graph=initial["graph"],
            ),
        )
        validated = _post(
            client,
            "/api/v1/authoring/validate",
            _validate_body(
                initial["graph"],
                python_source=generated["normalized_python_source"],
            ),
        )

    assert _semantic_node_uuids(recompiled["graph"]) == _semantic_node_uuids(
        initial["graph"]
    )
    assert PREPARE_NODE_UUID in _semantic_node_uuids(initial["graph"])
    assert (
        recompiled["normalized_python_source"] == generated["normalized_python_source"]
    )
    assert validated["graph"] == initial["graph"]
    assert (
        validated["normalized_python_source"] == generated["normalized_python_source"]
    )
    assert recompiled["changeset"]["kind"] == "source_only"
    assert {table: store.count_rows(table) for table in persistent_tables} == before


def _utf16_column(line: str, codepoint_index: int) -> int:
    """把 Python 的零基 code-point index 独立换算成公开一基 UTF-16 column。"""

    return len(line[:codepoint_index].encode("utf-16-le")) // 2 + 1


def _substring_range(line_number: int, line: str, text: str) -> dict[str, int]:
    start = line.index(text)
    return {
        "start_line": line_number,
        "start_column": _utf16_column(line, start),
        "end_line": line_number,
        "end_column": _utf16_column(line, start + len(text)),
    }


def _unicode_semantic_source() -> str:
    action = f"""    # unilab:node_uuid={PREPARE_NODE_UUID}
    prepared = reactor.prepare(
        sample=sample,
        cycles=cycles,
        note=note,
    )"""
    return _source().replace(action, '    "中😀\t"; invalid = unknown()', 1)


def test_engine_diagnostic_uses_utf16_for_chinese_emoji_and_tab(
    engine_context: EngineContext,
) -> None:
    source = _unicode_semantic_source()
    line_number = next(
        index
        for index, line in enumerate(source.splitlines(), 1)
        if "invalid = unknown()" in line
    )
    line = source.splitlines()[line_number - 1]

    result = _compile(engine_context.engine, source)

    diagnostic = result.diagnostics[0]
    assert diagnostic["code"] == "invalid_action_call"
    assert diagnostic["source_range"] == _substring_range(
        line_number,
        line,
        "unknown()",
    )


def test_engine_syntax_range_uses_utf16(
    engine_context: EngineContext,
) -> None:
    syntax_source = 'value = "中😀\t"; )\n'
    syntax = _compile(engine_context.engine, syntax_source)
    assert syntax.diagnostics[0]["code"] == "python_syntax_error"
    assert syntax.diagnostics[0]["source_range"] == {
        "start_line": 1,
        "start_column": 17,
        "end_line": 1,
        "end_column": 17,
    }


def test_engine_duplicate_anchor_repair_ranges_use_utf16(
    engine_context: EngineContext,
) -> None:
    duplicate_source = (
        _source()
        .replace(
            f"    # unilab:node_uuid={PREPARE_NODE_UUID}",
            f'    "中😀\t"; # unilab:node_uuid={PREPARE_NODE_UUID}',
        )
        .replace(
            "20000000-0000-4000-8000-000000000002",
            PREPARE_NODE_UUID,
        )
    )
    duplicate = _compile(engine_context.engine, duplicate_source)
    diagnostic = duplicate.diagnostics[0]
    assert diagnostic["code"] == "DUPLICATE_NODE_UUID"
    occurrences = diagnostic["occurrence_ranges"]
    assert len(occurrences) == 2
    for occurrence in occurrences:
        line_number = occurrence["start_line"]
        line = duplicate_source.splitlines()[line_number - 1]
        assert occurrence == _substring_range(
            line_number,
            line,
            f"# unilab:node_uuid={PREPARE_NODE_UUID}",
        )
    assert {
        tuple(repair["retained_range"].values())
        for repair in diagnostic["repair_alternatives"]
    } == {tuple(item.values()) for item in occurrences}


def test_real_http_source_map_end_is_utf16_end_exclusive(
    engine_context: EngineContext,
) -> None:
    source = _source().replace("note=note,", 'note="中😀\t",', 1)

    with TestClient(_transform_app(engine_context.engine)) as client:
        data = _post(
            client,
            "/api/v1/authoring/compile",
            _compile_body(python_source=source),
        )

    normalized = data["normalized_python_source"]
    entry = next(
        item
        for item in data["source_map"]
        if item["workflow_node_uuid"] == PREPARE_NODE_UUID
    )
    end_line = normalized.splitlines()[entry["end_line"] - 1]
    assert "中😀\\t" in end_line
    assert entry["start_column"] == 5
    assert entry["end_column"] == len(end_line.encode("utf-16-le")) // 2 + 1


class _ReceiveSpy:
    def __init__(self, chunks: Sequence[bytes]) -> None:
        self.chunks = chunks
        self.calls = 0

    async def __call__(self) -> dict[str, Any]:
        if self.calls >= len(self.chunks):
            raise AssertionError("Authoring route 在 body 结束后仍继续 receive")
        index = self.calls
        self.calls += 1
        return {
            "type": "http.request",
            "body": self.chunks[index],
            "more_body": index + 1 < len(self.chunks),
        }


def _invoke_asgi(
    app: FastAPI,
    *,
    body: bytes,
    content_length: int | None = None,
) -> tuple[int, dict[str, Any], _ReceiveSpy]:
    receive = _ReceiveSpy([body])
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    headers = [(b"content-type", b"application/json")]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "POST",
        "root_path": "",
        "path": "/api/v1/authoring/compile",
        "raw_path": b"/api/v1/authoring/compile",
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
    raw = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return status, json.loads(raw), receive


def _raw_compile_body(graph: bytes) -> bytes:
    return (
        b'{"workflow_uuid":"'
        + WORKFLOW_UUID.encode("ascii")
        + b'","revision":7,"python_source":"semantic_error",'
        b'"source_uri":"package://lab/limits.py","applied_graph":' + graph + b"}"
    )


def test_authoring_body_budget_rejects_declared_oversize_before_receive() -> None:
    engine = RecordingTransformEngine()
    app = _transform_app(engine)

    status, payload, receive = _invoke_asgi(
        app,
        body=_raw_compile_body(b"{}"),
        content_length=HTTP_BODY_LIMIT + 1,
    )

    assert status == 400
    assert payload == INVALID_INPUT
    assert receive.calls == 0
    assert engine.calls == []


@pytest.mark.parametrize(
    ("digits", "expected_status", "expected_calls"),
    [
        (EXTERNAL_INTEGER_DIGITS, 200, 1),
        (EXTERNAL_INTEGER_DIGITS + 1, 400, 0),
    ],
    ids=["4096-accepted", "4097-rejected"],
)
def test_authoring_external_integer_budget(
    digits: int,
    expected_status: int,
    expected_calls: int,
) -> None:
    engine = RecordingTransformEngine("diagnostic")
    integer = b"1" + b"0" * (digits - 1)
    body = _raw_compile_body(b'{"external":' + integer + b"}")

    status, payload, _receive = _invoke_asgi(
        _transform_app(engine),
        body=body,
        content_length=len(body),
    )

    assert status == expected_status
    assert len(engine.calls) == expected_calls
    if expected_status == 400:
        assert payload == INVALID_INPUT
    else:
        assert payload["code"] == 0
        assert payload["data"]["diagnostics"][0]["code"] == "template_catalog_mismatch"


@pytest.mark.parametrize(
    ("depth", "expected_status", "expected_calls"),
    [
        (MAX_JSON_DEPTH, 200, 1),
        (MAX_JSON_DEPTH + 1, 400, 0),
    ],
    ids=["depth-10000-accepted", "depth-10001-rejected"],
)
def test_authoring_complete_json_depth_budget(
    depth: int,
    expected_status: int,
    expected_calls: int,
) -> None:
    engine = RecordingTransformEngine("diagnostic")
    # request root + applied_graph object + arrays == complete document depth.
    array_depth = depth - 2
    graph = b'{"deep":' + b"[" * array_depth + b"0" + b"]" * array_depth + b"}"
    body = _raw_compile_body(graph)

    status, payload, _receive = _invoke_asgi(
        _transform_app(engine),
        body=body,
        content_length=len(body),
    )

    assert status == expected_status
    assert len(engine.calls) == expected_calls
    if expected_status == 400:
        assert payload == INVALID_INPUT
    else:
        assert payload["code"] == 0
