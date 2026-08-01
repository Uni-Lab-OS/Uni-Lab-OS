"""Phoenix Adapter 与本地 SQLite 进程配置测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from unilabos.observability.config import ObservabilitySettings
from unilabos.observability.gateway import SpanQuery, TraceQuery
from unilabos.observability.phoenix import (
    PhoenixHttpAdapter,
    PhoenixProcessAdapter,
)


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
        "phoenix_executable": "/opt/unilab/bin/phoenix",
    }
    values.update(overrides)
    return ObservabilitySettings(**values)


def test_settings_reject_non_loopback_and_unsafe_project(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback"):
        _settings(tmp_path, host="0.0.0.0")
    with pytest.raises(ValueError, match="project_name"):
        _settings(tmp_path, project_name="bad/project")


def test_process_adapter_builds_explicit_sqlite_environment(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    calls: list[tuple[list[str], dict[str, Any]]] = []
    health_results = iter((False, True))

    class FakeProcess:
        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            return None

        def wait(self, timeout: float) -> int:
            del timeout
            return 0

        def kill(self) -> None:
            return None

    def popen(command: list[str], **kwargs: Any) -> FakeProcess:
        calls.append((command, kwargs))
        return FakeProcess()

    adapter = PhoenixProcessAdapter(
        settings,
        popen_factory=popen,
        health_probe=lambda _url: next(health_results),
        sleep=lambda _seconds: None,
    )

    adapter.start()
    adapter.stop()

    command, kwargs = calls[0]
    env = kwargs["env"]
    assert command == ["/opt/unilab/bin/phoenix", "serve"]
    assert kwargs["cwd"] == str(settings.working_dir)
    assert env["PHOENIX_HOST"] == "127.0.0.1"
    assert env["PHOENIX_PORT"] == "6006"
    assert env["PHOENIX_GRPC_PORT"] == "4317"
    assert env["PHOENIX_WORKING_DIR"] == str(settings.working_dir)
    assert env["PHOENIX_SQL_DATABASE_URL"].endswith("/phoenix.sqlite3")
    assert env["PHOENIX_DEFAULT_RETENTION_POLICY_DAYS"] == "30"
    assert env["PHOENIX_TELEMETRY_ENABLED"] == "false"
    assert env["PHOENIX_ALLOW_EXTERNAL_RESOURCES"] == "false"
    assert settings.database_path.parent.is_dir()
    assert adapter.managed is False


def test_http_adapter_uses_only_configured_project_and_phoenix_routes(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/healthz":
            return httpx.Response(200, text="OK")
        if request.method == "POST" and request.url.path == "/v1/traces":
            return httpx.Response(
                200,
                content=b"\x00",
                headers={"Content-Type": "application/x-protobuf"},
            )
        if request.url.path.endswith("/traces"):
            return httpx.Response(
                200,
                json={"data": [{"trace_id": "a" * 32}], "next_cursor": "next"},
            )
        if request.url.path.endswith("/spans"):
            return httpx.Response(
                200,
                json={"data": [{"name": "electron.startup"}], "next_cursor": None},
            )
        return httpx.Response(404)

    async def exercise() -> None:
        adapter = PhoenixHttpAdapter(
            _settings(tmp_path),
            transport=httpx.MockTransport(handler),
        )
        assert await adapter.health() is True
        exported = await adapter.export_traces(
            b"trace",
            content_type="application/x-protobuf",
            content_encoding="gzip",
        )
        assert exported.content == b"\x00"
        assert exported.content_encoding is None
        traces = await adapter.list_traces(
            TraceQuery(
                limit=25,
                cursor=None,
                start_time=None,
                end_time=None,
                sort="start_time",
                order="desc",
                include_spans=False,
                session_identifiers=(),
            )
        )
        spans = await adapter.list_spans(
            SpanQuery(trace_id="a" * 32, limit=100, cursor=None)
        )
        assert traces["next_cursor"] == "next"
        assert spans["data"][0]["name"] == "electron.startup"
        await adapter.close()

    asyncio.run(exercise())

    export_request = requests[1]
    assert export_request.headers["x-project-name"] == "uni-lab-electron"
    assert export_request.headers["content-encoding"] == "gzip"
    assert requests[2].url.path == ("/v1/projects/uni-lab-electron/traces")
    assert requests[2].url.params["limit"] == "25"
    assert requests[3].url.path == ("/v1/projects/uni-lab-electron/spans")
    assert requests[3].url.params["trace_id"] == "a" * 32
