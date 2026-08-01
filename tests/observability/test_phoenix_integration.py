"""显式开启的真实 Phoenix OSS 冒烟测试。"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from unilabos.app.observability_api import create_observability_app
from unilabos.observability.config import ObservabilitySettings
from unilabos.observability.gateway import ObservabilityGateway
from unilabos.observability.runtime import runtime_tracing, start_runtime_span

PHOENIX_EXECUTABLE = os.environ.get("UNILABOS_PHOENIX_EXECUTABLE", "")


def _build_otlp_protobuf(trace_id: bytes, span_id: bytes, now_ns: int) -> bytes:
    """使用 Phoenix 所在环境的官方 OTLP proto 生成测试载荷。"""

    python_name = "python.exe" if os.name == "nt" else "python"
    python_executable = str(Path(PHOENIX_EXECUTABLE).parent / python_name)
    script = """
import sys
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status

request = ExportTraceServiceRequest()
resource_spans = request.resource_spans.add()
scope_spans = resource_spans.scope_spans.add()
scope_spans.scope.name = "uni-lab-electron-integration"
span = scope_spans.spans.add()
span.trace_id = bytes.fromhex(sys.argv[1])
span.span_id = bytes.fromhex(sys.argv[2])
span.name = "electron.integration.smoke"
span.kind = Span.SPAN_KIND_INTERNAL
span.start_time_unix_nano = int(sys.argv[3])
span.end_time_unix_nano = int(sys.argv[3]) + 1_000_000
span.status.code = Status.STATUS_CODE_OK
sys.stdout.buffer.write(request.SerializeToString())
"""
    completed = subprocess.run(
        [python_executable, "-c", script, trace_id.hex(), span_id.hex(), str(now_ns)],
        check=True,
        capture_output=True,
    )
    return completed.stdout


@pytest.mark.skipif(
    not PHOENIX_EXECUTABLE,
    reason="需要通过 UNILABOS_PHOENIX_EXECUTABLE 显式指定真实 Phoenix",
)
def test_real_phoenix_sqlite_otlp_and_query(tmp_path: Path) -> None:
    trace_id = bytes.fromhex("0123456789abcdef0123456789abcdef")
    span_id = bytes.fromhex("0123456789abcdef")
    now_ns = time.time_ns()
    settings = ObservabilitySettings(
        enabled=True,
        auto_start=True,
        host="127.0.0.1",
        port=16006,
        grpc_port=14317,
        project_name="uni-lab-electron-integration",
        working_dir=tmp_path / "phoenix",
        retention_days=1,
        startup_timeout_seconds=60.0,
        request_timeout_seconds=10.0,
        shutdown_timeout_seconds=10.0,
        max_ingest_bytes=1024 * 1024,
        phoenix_executable=PHOENIX_EXECUTABLE,
    )
    gateway = ObservabilityGateway(settings)
    otlp_protobuf = _build_otlp_protobuf(trace_id, span_id, now_ns)

    with TestClient(
        create_observability_app(gateway),
        client=("127.0.0.1", 31000),
    ) as client:
        status = client.get("/api/v1/observability/status")
        assert status.json()["data"]["state"] == "ready"

        with start_runtime_span(
            "ros2.integration.smoke",
            attributes={
                "workflow.node_job.uuid": ("11111111-1111-4111-8111-111111111111"),
                "device.action.name": "move",
            },
        ) as runtime_span:
            assert runtime_span is not None
            runtime_trace_id = f"{runtime_span.get_span_context().trace_id:032x}"

        secret = "password=DO-NOT-EXPORT"
        with pytest.raises(RuntimeError, match="DO-NOT-EXPORT"):
            with start_runtime_span("ros2.integration.error") as error_span:
                assert error_span is not None
                error_trace_id = f"{error_span.get_span_context().trace_id:032x}"
                raise RuntimeError(secret)
        assert runtime_tracing.force_flush(10.0)

        exported = client.post(
            "/api/v1/observability/otlp/v1/traces",
            content=otlp_protobuf,
            headers={"Content-Type": "application/x-protobuf"},
        )
        assert exported.status_code == 200

        deadline = time.monotonic() + 15
        while True:
            traces = client.get(
                "/api/v1/observability/traces",
                params={"include_spans": "true"},
            )
            assert traces.status_code == 200
            returned_trace_ids = {
                item.get("trace_id") for item in traces.json()["data"]["traces"]
            }
            if {trace_id.hex(), runtime_trace_id, error_trace_id} <= returned_trace_ids:
                break
            if time.monotonic() >= deadline:
                pytest.fail("Phoenix 未在期限内返回刚上报的 trace")
            time.sleep(0.25)

        detail = client.get(f"/api/v1/observability/traces/{trace_id.hex()}")
        assert detail.status_code == 200
        assert detail.json()["data"]["spans"][0]["name"] == (
            "electron.integration.smoke"
        )
        runtime_detail = client.get(f"/api/v1/observability/traces/{runtime_trace_id}")
        assert runtime_detail.status_code == 200
        assert runtime_detail.json()["data"]["spans"][0]["name"] == (
            "ros2.integration.smoke"
        )
        error_detail = client.get(f"/api/v1/observability/traces/{error_trace_id}")
        assert error_detail.status_code == 200
        error_payload = json.dumps(error_detail.json(), ensure_ascii=False)
        assert secret not in error_payload
        assert "RuntimeError" in error_payload

    assert settings.database_path.is_file()
