"""Phoenix observability HTTP Interface 回归测试。"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from unilabos.app.observability_api import create_observability_app
from unilabos.observability.config import ObservabilitySettings
from unilabos.observability.gateway import (
    ObservabilityGateway,
    ObservabilityUnavailable,
    OtlpResponse,
)


class FakeProcessAdapter:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.managed = False

    def start(self) -> None:
        self.started = True
        self.managed = True

    def stop(self) -> None:
        self.stopped = True


class FakeTraceAdapter:
    def __init__(self) -> None:
        self.healthy = True
        self.health_error: Exception | None = None
        self.closed = False
        self.export_error: Exception | None = None
        self.last_export: dict[str, Any] | None = None
        self.last_trace_query: Any = None
        self.last_span_query: Any = None

    async def health(self) -> bool:
        if self.health_error is not None:
            raise self.health_error
        return self.healthy

    async def export_traces(
        self,
        payload: bytes,
        *,
        content_type: str,
        content_encoding: str | None,
    ) -> OtlpResponse:
        if self.export_error is not None:
            raise self.export_error
        self.last_export = {
            "payload": payload,
            "content_type": content_type,
            "content_encoding": content_encoding,
        }
        return OtlpResponse(
            status_code=200,
            content=b"\x00",
            content_type="application/x-protobuf",
            content_encoding=None,
        )

    async def list_traces(self, query: Any) -> dict[str, Any]:
        self.last_trace_query = query
        return {
            "data": [
                {
                    "trace_id": "0123456789abcdef0123456789abcdef",
                    "start_time": "2026-08-01T10:00:00Z",
                    "end_time": "2026-08-01T10:00:01Z",
                }
            ],
            "next_cursor": "next-trace-page",
        }

    async def list_spans(self, query: Any) -> dict[str, Any]:
        self.last_span_query = query
        return {
            "data": [
                {
                    "name": "electron.startup",
                    "context": {
                        "trace_id": "0123456789abcdef0123456789abcdef",
                        "span_id": "0123456789abcdef",
                    },
                }
            ],
            "next_cursor": None,
        }

    async def close(self) -> None:
        self.closed = True


def _settings(tmp_path: Path, **overrides: Any) -> ObservabilitySettings:
    values: dict[str, Any] = {
        "enabled": True,
        "auto_start": True,
        "host": "127.0.0.1",
        "port": 6006,
        "grpc_port": 4317,
        "project_name": "uni-lab-electron",
        "working_dir": tmp_path / "phoenix",
        "retention_days": 30,
        "startup_timeout_seconds": 5.0,
        "request_timeout_seconds": 2.0,
        "shutdown_timeout_seconds": 2.0,
        "max_ingest_bytes": 1024,
        "phoenix_executable": "",
    }
    values.update(overrides)
    return ObservabilitySettings(**values)


def _client(
    tmp_path: Path,
    **settings_overrides: Any,
) -> tuple[TestClient, FakeTraceAdapter, FakeProcessAdapter]:
    trace_adapter = FakeTraceAdapter()
    process_adapter = FakeProcessAdapter()
    gateway = ObservabilityGateway(
        _settings(tmp_path, **settings_overrides),
        trace_adapter=trace_adapter,
        process_adapter=process_adapter,
    )
    return (
        TestClient(
            create_observability_app(gateway),
            client=("127.0.0.1", 31000),
        ),
        trace_adapter,
        process_adapter,
    )


def test_lifecycle_status_and_otlp_forwarding(tmp_path: Path) -> None:
    client, trace_adapter, process_adapter = _client(tmp_path)
    payload = b"\x0a\x03abc"

    with client:
        status = client.get("/api/v1/observability/status")
        response = client.post(
            "/api/v1/observability/otlp/v1/traces",
            content=payload,
            headers={
                "Content-Type": "application/x-protobuf",
                "Content-Encoding": "gzip",
            },
        )

    assert status.status_code == 200
    assert status.json()["data"] == {
        "enabled": True,
        "state": "ready",
        "provider": "phoenix",
        "storage": "sqlite",
        "project_name": "uni-lab-electron",
        "managed_process": True,
        "last_error": None,
    }
    assert response.status_code == 200
    assert response.content == b"\x00"
    assert response.headers["content-type"] == "application/x-protobuf"
    assert trace_adapter.last_export == {
        "payload": payload,
        "content_type": "application/x-protobuf",
        "content_encoding": "gzip",
    }
    assert process_adapter.started is True
    assert process_adapter.stopped is True
    assert trace_adapter.closed is True


def test_trace_list_and_detail_hide_phoenix_query_shape(tmp_path: Path) -> None:
    client, trace_adapter, _process_adapter = _client(tmp_path)

    with client:
        listed = client.get(
            "/api/v1/observability/traces",
            params=[
                ("limit", "25"),
                ("include_spans", "true"),
                ("sort", "latency_ms"),
                ("order", "asc"),
                ("session_identifier", "desktop-session"),
                ("session_identifier", "workflow-session"),
            ],
        )
        detail = client.get(
            "/api/v1/observability/traces/0123456789ABCDEF0123456789ABCDEF",
            params={"limit": "200"},
        )

    assert listed.status_code == 200
    assert listed.json()["data"]["project_name"] == "uni-lab-electron"
    assert listed.json()["data"]["next_cursor"] == "next-trace-page"
    assert listed.json()["data"]["traces"][0]["trace_id"].startswith("012345")
    assert trace_adapter.last_trace_query.limit == 25
    assert trace_adapter.last_trace_query.include_spans is True
    assert trace_adapter.last_trace_query.sort == "latency_ms"
    assert trace_adapter.last_trace_query.order == "asc"
    assert trace_adapter.last_trace_query.session_identifiers == (
        "desktop-session",
        "workflow-session",
    )

    assert detail.status_code == 200
    assert detail.json()["data"]["trace_id"] == ("0123456789abcdef0123456789abcdef")
    assert detail.json()["data"]["spans"][0]["name"] == "electron.startup"
    assert trace_adapter.last_span_query.trace_id == (
        "0123456789abcdef0123456789abcdef"
    )
    assert trace_adapter.last_span_query.limit == 200


def test_disabled_or_unavailable_observability_does_not_break_status(
    tmp_path: Path,
) -> None:
    client, trace_adapter, process_adapter = _client(tmp_path, enabled=False)

    with client:
        status = client.get("/api/v1/observability/status")
        rejected = client.post(
            "/api/v1/observability/otlp/v1/traces",
            content=b"trace",
            headers={"Content-Type": "application/x-protobuf"},
        )

    assert status.status_code == 200
    assert status.json()["data"]["state"] == "disabled"
    assert rejected.status_code == 503
    assert rejected.json()["error"]["code"] == "observability_disabled"
    assert process_adapter.started is False
    assert trace_adapter.closed is True

    client, trace_adapter, _process_adapter = _client(tmp_path)
    trace_adapter.export_error = ObservabilityUnavailable("Phoenix 暂不可用")
    with client:
        unavailable = client.post(
            "/api/v1/observability/otlp/v1/traces",
            content=b"trace",
            headers={"Content-Type": "application/x-protobuf"},
        )
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "observability_unavailable"


def test_status_does_not_expose_unexpected_local_error(tmp_path: Path) -> None:
    client, trace_adapter, _process_adapter = _client(tmp_path)
    trace_adapter.health_error = PermissionError("/private/lab/phoenix.sqlite3")

    with client:
        status = client.get("/api/v1/observability/status")

    assert status.status_code == 200
    assert status.json()["data"]["state"] == "degraded"
    assert status.json()["data"]["last_error"] == (
        "Trace 日志服务内部错误，请查看本地日志"
    )


def test_observability_rejects_non_loopback_client(tmp_path: Path) -> None:
    trace_adapter = FakeTraceAdapter()
    gateway = ObservabilityGateway(
        _settings(tmp_path),
        trace_adapter=trace_adapter,
        process_adapter=FakeProcessAdapter(),
    )
    client = TestClient(
        create_observability_app(gateway),
        client=("192.0.2.10", 31000),
    )

    with client:
        status = client.get("/api/v1/observability/status")
        exported = client.post(
            "/api/v1/observability/otlp/v1/traces",
            content=b"trace",
            headers={"Content-Type": "application/x-protobuf"},
        )

    assert status.status_code == 403
    assert status.json()["error"]["code"] == "access_denied"
    assert exported.status_code == 403
    assert trace_adapter.last_export is None


def test_ingest_limits_and_query_validation(tmp_path: Path) -> None:
    client, _trace_adapter, _process_adapter = _client(
        tmp_path,
        max_ingest_bytes=4,
    )

    with client:
        too_large = client.post(
            "/api/v1/observability/otlp/v1/traces",
            content=b"12345",
            headers={"Content-Type": "application/x-protobuf"},
        )
        wrong_media = client.post(
            "/api/v1/observability/otlp/v1/traces",
            content=b"{}",
            headers={"Content-Type": "text/plain"},
        )
        bad_limit = client.get(
            "/api/v1/observability/traces",
            params={"limit": "1001"},
        )
        bad_trace_id = client.get("/api/v1/observability/traces/not-a-trace-id")

    assert too_large.status_code == 413
    assert too_large.json()["error"]["code"] == "payload_too_large"
    assert wrong_media.status_code == 415
    assert wrong_media.json()["error"]["code"] == "unsupported_media_type"
    assert bad_limit.status_code == 400
    assert bad_limit.json()["error"]["code"] == "invalid_input"
    assert bad_trace_id.status_code == 400
    assert bad_trace_id.json()["error"]["code"] == "invalid_input"


def test_main_web_server_mounts_observability_once(monkeypatch: Any) -> None:
    from unilabos.app.web import server as web_server
    from unilabos.config.config import BasicConfig, ObservabilityConfig

    monkeypatch.setattr(BasicConfig, "working_dir", "")
    monkeypatch.setattr(ObservabilityConfig, "enabled", False)
    web_server = importlib.reload(web_server)

    app = web_server.setup_server()
    assert web_server.setup_server() is app
    assert web_server.observability_routes_mounted is True

    with TestClient(app, client=("127.0.0.1", 31000)) as client:
        status = client.get("/api/v1/observability/status")
    assert status.status_code == 200
    assert status.json()["data"]["state"] == "disabled"
