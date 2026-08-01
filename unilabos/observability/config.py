"""Phoenix observability 的不可变运行配置。"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ObservabilitySettings:
    """隔离配置系统与运行实现，并集中校验安全约束。"""

    enabled: bool
    auto_start: bool
    host: str
    port: int
    grpc_port: int
    project_name: str
    working_dir: Path
    retention_days: int
    startup_timeout_seconds: float
    request_timeout_seconds: float
    shutdown_timeout_seconds: float
    max_ingest_bytes: int
    phoenix_executable: str

    def __post_init__(self) -> None:
        host = self.host.strip().lower()
        if host != "localhost":
            try:
                is_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError as exc:
                raise ValueError("observability host 必须是 loopback 地址") from exc
            if not is_loopback:
                raise ValueError("observability host 必须是 loopback 地址")
        if not 1 <= self.port <= 65535:
            raise ValueError("observability port 必须在 1 到 65535 之间")
        if not 1 <= self.grpc_port <= 65535:
            raise ValueError("observability grpc_port 必须在 1 到 65535 之间")
        if self.port == self.grpc_port:
            raise ValueError("observability HTTP 与 gRPC 端口不能相同")
        project_name = self.project_name.strip()
        if (
            not project_name
            or len(project_name) > 128
            or any(character in project_name for character in "/?#")
        ):
            raise ValueError("observability project_name 格式不正确")
        if self.retention_days < 0:
            raise ValueError("observability retention_days 不能为负数")
        if self.startup_timeout_seconds <= 0:
            raise ValueError("observability startup_timeout_seconds 必须大于 0")
        if self.request_timeout_seconds <= 0:
            raise ValueError("observability request_timeout_seconds 必须大于 0")
        if self.shutdown_timeout_seconds <= 0:
            raise ValueError("observability shutdown_timeout_seconds 必须大于 0")
        if not 1 <= self.max_ingest_bytes <= 64 * 1024 * 1024:
            raise ValueError("observability max_ingest_bytes 必须在 1B 到 64MiB 之间")

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def database_path(self) -> Path:
        return self.working_dir / "phoenix.sqlite3"

    @property
    def log_path(self) -> Path:
        return self.working_dir / "phoenix.log"

    @classmethod
    def from_runtime_config(
        cls,
        basic_config: type[Any],
        observability_config: type[Any],
    ) -> ObservabilitySettings:
        """从 class-level 配置生成一次性的运行快照。"""

        base_working_dir = Path(
            str(getattr(basic_config, "working_dir", "") or Path.cwd())
        ).expanduser()
        configured_working_dir = str(
            getattr(observability_config, "working_dir", "") or ""
        ).strip()
        if configured_working_dir:
            working_dir = Path(configured_working_dir).expanduser()
            if not working_dir.is_absolute():
                working_dir = base_working_dir / working_dir
        else:
            working_dir = base_working_dir / "observability" / "phoenix"

        return cls(
            enabled=bool(observability_config.enabled),
            auto_start=bool(observability_config.auto_start),
            host=str(observability_config.host).strip(),
            port=int(observability_config.port),
            grpc_port=int(observability_config.grpc_port),
            project_name=str(observability_config.project_name).strip(),
            working_dir=working_dir.resolve(),
            retention_days=int(observability_config.retention_days),
            startup_timeout_seconds=float(observability_config.startup_timeout_seconds),
            request_timeout_seconds=float(observability_config.request_timeout_seconds),
            shutdown_timeout_seconds=float(
                observability_config.shutdown_timeout_seconds
            ),
            max_ingest_bytes=int(observability_config.max_ingest_bytes),
            phoenix_executable=str(
                observability_config.phoenix_executable or ""
            ).strip(),
        )
