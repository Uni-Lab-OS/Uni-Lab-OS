"""Arize Phoenix OSS 的进程与 HTTP Adapters。"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Callable
from typing import Any, BinaryIO
from urllib.parse import quote

import httpx

from unilabos.observability.config import ObservabilitySettings
from unilabos.observability.gateway import (
    ObservabilityUnavailable,
    ObservabilityUpstreamError,
    OtlpResponse,
    SpanQuery,
    TraceQuery,
)


class PhoenixProcessAdapter:
    """管理可选 Phoenix 子进程，数据库始终由 Phoenix 独占。"""

    def __init__(
        self,
        settings: ObservabilitySettings,
        *,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        health_probe: Callable[[str], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._popen_factory = popen_factory
        self._health_probe = health_probe or self._default_health_probe
        self._sleep = sleep
        self._monotonic = monotonic
        self._process: Any = None
        self._log_handle: BinaryIO | None = None
        self._managed = False

    @property
    def managed(self) -> bool:
        return self._managed

    def start(self) -> None:
        if self._health_probe(self.settings.base_url):
            self._managed = False
            return
        if not self.settings.auto_start:
            raise ObservabilityUnavailable("未连接到外部 Phoenix 服务")

        executable = self._resolve_executable()
        self.settings.working_dir.mkdir(parents=True, exist_ok=True)
        self._log_handle = self.settings.log_path.open("ab", buffering=0)
        command = [executable, "serve"]
        try:
            self._process = self._popen_factory(
                command,
                cwd=str(self.settings.working_dir),
                env=self._build_environment(),
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            self._close_log()
            raise ObservabilityUnavailable(
                "无法启动 Phoenix 进程，请检查可执行文件与 phoenix.log"
            ) from exc

        deadline = self._monotonic() + self.settings.startup_timeout_seconds
        while True:
            exit_code = self._process.poll()
            if exit_code is not None:
                self._close_log()
                self._process = None
                raise ObservabilityUnavailable(
                    f"Phoenix 进程提前退出（{exit_code}），请查看 phoenix.log"
                )
            if self._health_probe(self.settings.base_url):
                self._managed = True
                return
            if self._monotonic() >= deadline:
                self.stop()
                raise ObservabilityUnavailable("等待 Phoenix 启动超时")
            self._sleep(0.2)

    def stop(self) -> None:
        process = self._process
        self._process = None
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=self.settings.shutdown_timeout_seconds)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=self.settings.shutdown_timeout_seconds)
        finally:
            self._managed = False
            self._close_log()

    def _resolve_executable(self) -> str:
        configured = self.settings.phoenix_executable
        if configured:
            return configured
        executable = shutil.which("phoenix")
        if executable:
            return executable
        raise ObservabilityUnavailable(
            "未安装 Arize Phoenix，请安装 Uni-Lab-OS observability 可选依赖"
        )

    def _build_environment(self) -> dict[str, str]:
        database_url = f"sqlite:///{self.settings.database_path.as_posix()}"
        return {
            **os.environ,
            "PHOENIX_HOST": self.settings.host,
            "PHOENIX_PORT": str(self.settings.port),
            "PHOENIX_GRPC_PORT": str(self.settings.grpc_port),
            "PHOENIX_WORKING_DIR": str(self.settings.working_dir),
            "PHOENIX_SQL_DATABASE_URL": database_url,
            "PHOENIX_DEFAULT_RETENTION_POLICY_DAYS": str(self.settings.retention_days),
            "PHOENIX_TELEMETRY_ENABLED": "false",
            "PHOENIX_ALLOW_EXTERNAL_RESOURCES": "false",
            "PHOENIX_ALLOWED_PROVIDERS": "NONE",
            "PHOENIX_ALLOWED_SANDBOX_PROVIDERS": "NONE",
            "PYTHONUNBUFFERED": "1",
        }

    def _close_log(self) -> None:
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

    @staticmethod
    def _default_health_probe(base_url: str) -> bool:
        try:
            response = httpx.get(f"{base_url}/healthz", timeout=1.0)
            return response.status_code == 200
        except httpx.HTTPError:
            return False


class PhoenixHttpAdapter:
    """通过 Phoenix 官方 OTLP/REST Interface 存取 trace。"""

    def __init__(
        self,
        settings: ObservabilitySettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._project_path = quote(settings.project_name, safe="")
        self._client = httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=settings.request_timeout_seconds,
            transport=transport,
        )

    async def health(self) -> bool:
        try:
            response = await self._client.get("/healthz")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def export_traces(
        self,
        payload: bytes,
        *,
        content_type: str,
        content_encoding: str | None,
    ) -> OtlpResponse:
        headers = {
            "Content-Type": content_type,
            "x-project-name": self.settings.project_name,
        }
        if content_encoding:
            headers["Content-Encoding"] = content_encoding
        try:
            response = await self._client.post(
                "/v1/traces",
                content=payload,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise ObservabilityUnavailable() from exc
        return OtlpResponse(
            status_code=response.status_code,
            content=response.content,
            content_type=response.headers.get("content-type"),
            # httpx 已自动解压 response.content，不能把原压缩标记继续转发。
            content_encoding=None,
        )

    async def list_traces(self, query: TraceQuery) -> dict[str, Any]:
        params: list[tuple[str, str]] = [
            ("limit", str(query.limit)),
            ("sort", query.sort),
            ("order", query.order),
            ("include_spans", "true" if query.include_spans else "false"),
        ]
        for key, value in (
            ("cursor", query.cursor),
            ("start_time", query.start_time),
            ("end_time", query.end_time),
        ):
            if value is not None:
                params.append((key, value))
        params.extend(
            ("session_identifier", identifier)
            for identifier in query.session_identifiers
        )
        return await self._get_collection(
            f"/v1/projects/{self._project_path}/traces",
            params,
        )

    async def list_spans(self, query: SpanQuery) -> dict[str, Any]:
        params = [("trace_id", query.trace_id), ("limit", str(query.limit))]
        if query.cursor is not None:
            params.append(("cursor", query.cursor))
        return await self._get_collection(
            f"/v1/projects/{self._project_path}/spans",
            params,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get_collection(
        self,
        path: str,
        params: list[tuple[str, str]],
    ) -> dict[str, Any]:
        try:
            response = await self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise ObservabilityUnavailable() from exc
        if response.status_code == 404:
            return {"data": [], "next_cursor": None}
        if response.status_code != 200:
            raise ObservabilityUpstreamError(
                f"Phoenix 查询失败（HTTP {response.status_code}）"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ObservabilityUpstreamError("Phoenix 返回了无效 JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ObservabilityUpstreamError("Phoenix 返回的数据结构不兼容")
        next_cursor = payload.get("next_cursor")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise ObservabilityUpstreamError("Phoenix 返回的分页游标不兼容")
        return {"data": payload["data"], "next_cursor": next_cursor}
