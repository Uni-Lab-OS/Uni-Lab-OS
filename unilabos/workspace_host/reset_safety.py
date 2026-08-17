"""Fail-closed inspection for destructive Local Domain state rebuilds."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import WorkspacePaths


_TERMINAL_WORKFLOW_TASK_STATUSES = frozenset(
    {"succeeded", "failed", "canceled", "timeout"}
)
_TERMINAL_EDGE_RUNTIME_STATUSES = frozenset(
    {"completed", "outcome_committed", "outcome_retired"}
)


class LocalResetInspectionError(RuntimeError):
    """The safety facts could not be read, so reset must fail closed."""


@dataclass(frozen=True)
class LocalResetBlocker:
    source: str
    kind: str
    identity: str
    status: str
    unknown_command_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "source": self.source,
            "kind": self.kind,
            "identity": self.identity,
            "status": self.status,
        }
        if self.unknown_command_ids:
            payload["unknownCommandIds"] = list(self.unknown_command_ids)
        return payload


def inspect_local_reset_blockers(paths: WorkspacePaths) -> list[LocalResetBlocker]:
    """Return every durable fact that makes ``local.reset-state`` unsafe.

    The inspector never creates a database and never mutates live state. Missing
    databases mean that component has no durable facts yet; an unreadable or
    incompatible existing database fails closed via ``LocalResetInspectionError``.
    """

    local_domain = paths.runtime / "backend" / "local-domain"
    try:
        blockers = [
            *_workflow_task_blockers(local_domain / "workflow_history.db"),
            *_local_edge_authority_blockers(local_domain / "edge_authority.db"),
            *_edge_runtime_blockers(paths.runtime / "edge" / "edge_control.db"),
        ]
    except sqlite3.Error as error:
        raise LocalResetInspectionError(
            f"无法检查本地状态是否安全：{error}"
        ) from error
    return sorted(
        blockers,
        key=lambda item: (item.source, item.kind, item.identity, item.status),
    )


def _workflow_task_blockers(path: Path) -> list[LocalResetBlocker]:
    with _read_database(path) as connection:
        if connection is None or not _table_exists(connection, "workflow_task"):
            return []
        placeholders = ",".join("?" for _ in _TERMINAL_WORKFLOW_TASK_STATUSES)
        rows = connection.execute(
            f"""
            SELECT uuid, status FROM workflow_task
            WHERE deleted_at IS NULL AND status NOT IN ({placeholders})
            ORDER BY create_time, uuid
            """,
            tuple(sorted(_TERMINAL_WORKFLOW_TASK_STATUSES)),
        ).fetchall()
    return [
        LocalResetBlocker(
            source="local-domain",
            kind="workflow-task",
            identity=str(row["uuid"]),
            status=str(row["status"]),
        )
        for row in rows
    ]


def _local_edge_authority_blockers(path: Path) -> list[LocalResetBlocker]:
    with _read_database(path) as connection:
        if connection is None or not _table_exists(connection, "local_edge_job"):
            return []
        rows = connection.execute(
            """
            SELECT job_uuid, status, unknown_command_ids_json
            FROM local_edge_job
            WHERE status != 'completed'
            ORDER BY updated_at, job_uuid
            """
        ).fetchall()
    return [
        LocalResetBlocker(
            source="local-edge-authority",
            kind="edge-job",
            identity=str(row["job_uuid"]),
            status=str(row["status"]),
            unknown_command_ids=_unknown_command_ids(
                row["unknown_command_ids_json"], path
            ),
        )
        for row in rows
    ]


def _edge_runtime_blockers(path: Path) -> list[LocalResetBlocker]:
    with _read_database(path) as connection:
        if connection is None:
            return []
        blockers: list[LocalResetBlocker] = []
        if _table_exists(connection, "edge_job_runtime"):
            placeholders = ",".join("?" for _ in _TERMINAL_EDGE_RUNTIME_STATUSES)
            rows = connection.execute(
                f"""
                SELECT job_uuid, status FROM edge_job_runtime
                WHERE status NOT IN ({placeholders})
                ORDER BY updated_at, job_uuid
                """,
                tuple(sorted(_TERMINAL_EDGE_RUNTIME_STATUSES)),
            ).fetchall()
            blockers.extend(
                LocalResetBlocker(
                    source="edge-runtime",
                    kind="edge-job",
                    identity=str(row["job_uuid"]),
                    status=str(row["status"]),
                )
                for row in rows
            )
        if _table_exists(connection, "edge_job_outcome_pending"):
            rows = connection.execute(
                """
                SELECT job_uuid, unknown_command_ids_json
                FROM edge_job_outcome_pending
                ORDER BY updated_at, job_uuid
                """
            ).fetchall()
            for row in rows:
                unknown_command_ids = _unknown_command_ids(
                    row["unknown_command_ids_json"], path
                )
                blockers.append(
                    LocalResetBlocker(
                        source="edge-runtime",
                        kind="edge-outcome",
                        identity=str(row["job_uuid"]),
                        status="unknown" if unknown_command_ids else "outcome-pending",
                        unknown_command_ids=unknown_command_ids,
                    )
                )
        if _table_exists(connection, "edge_event_outbox"):
            # 设备遥测已由 HTTP 数据面先提交；这里的 WebSocket 记录只是可丢弃
            # 短通知。若把持续产生的通知当成重建阻塞项，运行中的 Edge Runtime
            # 将使正常/Dry-run 模式永远无法切换。作业终态等控制事实仍严格阻塞。
            rows = connection.execute(
                """
                SELECT event_uuid, type FROM edge_event_outbox
                WHERE acked_at IS NULL AND type != 'device.telemetry_committed'
                ORDER BY created_at, event_uuid
                """
            ).fetchall()
            blockers.extend(
                LocalResetBlocker(
                    source="edge-runtime",
                    kind="edge-event",
                    identity=str(row["event_uuid"]),
                    status=f"unacknowledged:{row['type']}",
                )
                for row in rows
            )
        return blockers


class _ReadDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection | None:
        if not self.path.exists():
            return None
        try:
            self.connection = sqlite3.connect(
                f"file:{self.path}?mode=ro",
                uri=True,
                timeout=1.0,
            )
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA query_only=ON")
            return self.connection
        except sqlite3.Error as error:
            raise LocalResetInspectionError(
                f"无法读取本地状态库 {self.path}: {error}"
            ) from error

    def __exit__(self, *_args: Any) -> None:
        if self.connection is not None:
            self.connection.close()


def _read_database(path: Path) -> _ReadDatabase:
    return _ReadDatabase(path)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    try:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    except sqlite3.Error as error:
        raise LocalResetInspectionError(f"无法检查本地状态表 {table}: {error}") from error
    return row is not None


def _unknown_command_ids(value: object, path: Path) -> tuple[str, ...]:
    try:
        decoded = json.loads(str(value or "[]"))
    except (TypeError, ValueError) as error:
        raise LocalResetInspectionError(
            f"本地状态库 {path} 的 UNKNOWN 命令不是合法 JSON"
        ) from error
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise LocalResetInspectionError(
            f"本地状态库 {path} 的 UNKNOWN 命令格式无效"
        )
    return tuple(decoded)
