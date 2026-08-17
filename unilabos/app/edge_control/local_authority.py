"""正式 Edge 协议形状的本地实现。

工作区后端（Workspace Backend）拥有此适配器并持久化调度意图和结果；可独立
重启的边缘运行时（Edge Runtime）消费与正式后端（Backend）一致的 HTTP 与
WebSocket 合同。本模块不导入设备驱动或 ROS 对象。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, WebSocket
from fastapi.websockets import WebSocketDisconnect

from unilabos.app.scheduler.dispatch import DispatchPayload

_PROTOCOL_VERSION = 1
_COMMAND_RETRY_SECONDS = 0.5


class LocalEdgeAuthorityStore:
    """SQLite facts for one Local Backend's Edge sessions and jobs."""

    def __init__(self, path: str | Path) -> None:
        target = Path(path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(target)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS local_edge_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_edge_session (
                    session_uuid TEXT PRIMARY KEY,
                    edge_uuid TEXT NOT NULL,
                    instance_uuid TEXT NOT NULL,
                    edge_key TEXT NOT NULL,
                    devices_json TEXT NOT NULL,
                    connected INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_edge_command (
                    command_uuid TEXT PRIMARY KEY,
                    sequence INTEGER NOT NULL UNIQUE,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_sent_at REAL,
                    created_at REAL NOT NULL,
                    acked_at REAL
                );
                CREATE TABLE IF NOT EXISTS local_edge_job (
                    job_uuid TEXT PRIMARY KEY,
                    task_uuid TEXT NOT NULL,
                    node_uuid TEXT NOT NULL,
                    command_uuid TEXT NOT NULL UNIQUE,
                    local_device_id TEXT NOT NULL,
                    action_name TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    param_json TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    device_action_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    feedback_sequence INTEGER NOT NULL DEFAULT 0,
                    outcome_json TEXT,
                    unknown_command_ids_json TEXT NOT NULL DEFAULT '[]',
                    projected_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def register_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        edge_key = _required_text(payload, "edge_key")
        instance_uuid = str(uuid.UUID(_required_text(payload, "instance_uuid")))
        devices = payload.get("devices")
        if not isinstance(devices, list) or any(
            not isinstance(device, dict) for device in devices
        ):
            raise ValueError("devices must be a list of objects")
        edge_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"unilab:{edge_key}"))
        session_uuid = str(uuid.uuid4())
        now = time.time()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO local_edge_session(
                    session_uuid, edge_uuid, instance_uuid, edge_key,
                    devices_json, connected, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    session_uuid,
                    edge_uuid,
                    instance_uuid,
                    edge_key,
                    json.dumps(devices, ensure_ascii=False, separators=(",", ":")),
                    now,
                    now,
                ),
            )
            self._connection.commit()
        return {"edge_uuid": edge_uuid, "session_uuid": session_uuid}

    def set_session_connected(self, session_uuid: str, connected: bool) -> None:
        with self._lock:
            changed = self._connection.execute(
                """
                UPDATE local_edge_session SET connected = ?, updated_at = ?
                WHERE session_uuid = ?
                """,
                (1 if connected else 0, time.time(), session_uuid),
            ).rowcount
            self._connection.commit()
        if changed != 1:
            raise ValueError("unknown Edge session")

    def reconcile_hello(self, payload: dict[str, Any]) -> None:
        """Apply the Edge's durable resume cursor before sending commands."""

        last_ack = payload.get("last_ack_command_sequence", 0)
        if isinstance(last_ack, bool) or not isinstance(last_ack, int) or last_ack < 0:
            raise ValueError("last_ack_command_sequence is invalid")
        running_jobs = payload.get("running_jobs") or []
        if not isinstance(running_jobs, list) or any(
            not isinstance(job, dict) for job in running_jobs
        ):
            raise ValueError("running_jobs must be a list of objects")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    UPDATE local_edge_command
                    SET status = 'acked', acked_at = COALESCE(acked_at, ?)
                    WHERE sequence <= ?
                    """,
                    (time.time(), last_ack),
                )
                self._connection.execute(
                    """
                    UPDATE local_edge_job
                    SET status = 'dispatched', updated_at = ?
                    WHERE status = 'pending' AND command_uuid IN (
                        SELECT command_uuid FROM local_edge_command
                        WHERE sequence <= ? AND status = 'acked'
                    )
                    """,
                    (time.time(), last_ack),
                )
                for reported in running_jobs:
                    job_uuid = str(uuid.UUID(_required_text(reported, "job_uuid")))
                    command_uuid = str(
                        uuid.UUID(_required_text(reported, "command_uuid"))
                    )
                    changed = self._connection.execute(
                        """
                        UPDATE local_edge_job
                        SET status = 'running', unknown_command_ids_json = '[]',
                            updated_at = ?
                        WHERE job_uuid = ? AND command_uuid = ?
                          AND outcome_json IS NULL
                        """,
                        (time.time(), job_uuid, command_uuid),
                    ).rowcount
                    if changed != 1:
                        raise ValueError("running Edge job identity is unknown")
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def mark_disconnected_jobs_unknown(self) -> list[str]:
        """Lock jobs which may already have crossed the physical-action boundary."""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT job_uuid FROM local_edge_job
                WHERE status IN ('dispatched', 'running') AND outcome_json IS NULL
                """
            ).fetchall()
            job_uuids = [str(row["job_uuid"]) for row in rows]
            for job_uuid in job_uuids:
                self._connection.execute(
                    """
                    UPDATE local_edge_job
                    SET status = 'unknown', unknown_command_ids_json = ?, updated_at = ?
                    WHERE job_uuid = ?
                    """,
                    (
                        json.dumps([f"workflow-node-job:{job_uuid}"]),
                        time.time(),
                        job_uuid,
                    ),
                )
            self._connection.commit()
        return job_uuids

    def dispatch(self, payload: DispatchPayload) -> dict[str, Any]:
        job_uuid = str(uuid.UUID(_required_text(payload, "job_id")))
        task_uuid = str(uuid.UUID(_required_text(payload, "task_id")))
        node_uuid = str(uuid.UUID(_required_text(payload, "node_id")))
        local_device_id = _required_text(payload, "device_id")
        action_name = _required_text(payload, "action")
        action_type = str(payload.get("action_type") or "")
        param = payload.get("action_args") or {}
        if not isinstance(param, dict):
            raise ValueError("action_args must be an object")
        device_action_key = f"/devices/{local_device_id}/{action_name}"
        now = time.time()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self._connection.execute(
                "SELECT * FROM local_edge_job WHERE job_uuid = ?", (job_uuid,)
            ).fetchone()
            if existing is not None:
                self._connection.rollback()
                if (
                    existing["task_uuid"] != task_uuid
                    or existing["node_uuid"] != node_uuid
                    or existing["local_device_id"] != local_device_id
                    or existing["action_name"] != action_name
                ):
                    raise ValueError("duplicate local Edge job identity changed")
                return _job_projection(existing)
            blocked = self._connection.execute(
                """
                SELECT job_uuid FROM local_edge_job
                WHERE local_device_id = ? AND status = 'unknown'
                ORDER BY created_at LIMIT 1
                """,
                (local_device_id,),
            ).fetchone()
            if blocked is not None:
                self._connection.rollback()
                raise RuntimeError(
                    "device is locked by unresolved UNKNOWN job: "
                    f"{blocked['job_uuid']}"
                )
            sequence = self._next_sequence_locked()
            command_uuid = str(uuid.uuid4())
            job_token = secrets.token_urlsafe(32)
            command_payload = {
                "job_uuid": job_uuid,
                "task_uuid": task_uuid,
                "node_uuid": node_uuid,
                "job_access_token": job_token,
                "executor_kind": "device_action",
            }
            try:
                self._connection.execute(
                    """
                    INSERT INTO local_edge_job(
                        job_uuid, task_uuid, node_uuid, command_uuid,
                        local_device_id, action_name, action_type, param_json,
                        token_hash, device_action_key, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        job_uuid,
                        task_uuid,
                        node_uuid,
                        command_uuid,
                        local_device_id,
                        action_name,
                        action_type,
                        json.dumps(param, ensure_ascii=False, separators=(",", ":")),
                        _token_hash(job_token),
                        device_action_key,
                        now,
                        now,
                    ),
                )
                self._connection.execute(
                    """
                    INSERT INTO local_edge_command(
                        command_uuid, sequence, type, payload_json, status, created_at
                    ) VALUES (?, ?, 'job.start', ?, 'pending', ?)
                    """,
                    (
                        command_uuid,
                        sequence,
                        json.dumps(
                            command_payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        now,
                    ),
                )
                self._connection.execute(
                    """
                    INSERT INTO local_edge_meta(key, value) VALUES ('sequence', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (str(sequence),),
                )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
            row = self._connection.execute(
                "SELECT * FROM local_edge_job WHERE job_uuid = ?", (job_uuid,)
            ).fetchone()
        assert row is not None
        return _job_projection(row)

    def pending_commands(self) -> list[dict[str, Any]]:
        retry_before = time.time() - _COMMAND_RETRY_SECONDS
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT command_uuid, sequence, type, payload_json
                FROM local_edge_command
                WHERE status != 'acked'
                  AND (last_sent_at IS NULL OR last_sent_at <= ?)
                ORDER BY sequence
                """,
                (retry_before,),
            ).fetchall()
        return [
            {
                "protocol_version": _PROTOCOL_VERSION,
                "message_uuid": str(row["command_uuid"]),
                "sequence": int(row["sequence"]),
                "type": str(row["type"]),
                "sent_at": _utc_now(),
                "payload": json.loads(str(row["payload_json"])),
            }
            for row in rows
        ]

    def mark_command_sent(self, command_uuid: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE local_edge_command SET last_sent_at = ? WHERE command_uuid = ?",
                (time.time(), command_uuid),
            )
            self._connection.commit()

    def acknowledge_command(self, command_uuid: str) -> None:
        normalized = str(uuid.UUID(command_uuid))
        with self._lock:
            self._connection.execute(
                """
                UPDATE local_edge_command
                SET status = 'acked', acked_at = COALESCE(acked_at, ?)
                WHERE command_uuid = ?
                """,
                (time.time(), normalized),
            )
            self._connection.execute(
                """
                UPDATE local_edge_job SET status = CASE
                    WHEN status = 'pending' THEN 'dispatched' ELSE status END,
                    updated_at = ? WHERE command_uuid = ?
                """,
                (time.time(), normalized),
            )
            self._connection.commit()

    def fetch_job(
        self,
        job_uuid: str,
        *,
        command_uuid: str,
        job_token: str,
    ) -> dict[str, Any]:
        normalized_job = str(uuid.UUID(job_uuid))
        normalized_command = str(uuid.UUID(command_uuid))
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM local_edge_job WHERE job_uuid = ?", (normalized_job,)
            ).fetchone()
        if row is None:
            raise KeyError(normalized_job)
        if row["command_uuid"] != normalized_command or not hmac.compare_digest(
            str(row["token_hash"]), _token_hash(job_token)
        ):
            raise PermissionError("job credential rejected")
        return {
            "job_uuid": normalized_job,
            "task_uuid": str(row["task_uuid"]),
            "node_uuid": str(row["node_uuid"]),
            "command_uuid": normalized_command,
            "local_device_id": str(row["local_device_id"]),
            "action_name": str(row["action_name"]),
            "action_type": str(row["action_type"]),
            "param": json.loads(str(row["param_json"])),
        }

    def mark_job_started(self, job_uuid: str) -> None:
        with self._lock:
            self._connection.execute(
                """
                UPDATE local_edge_job SET status = CASE
                    WHEN status IN ('pending', 'dispatched') THEN 'running'
                    ELSE status END, updated_at = ? WHERE job_uuid = ?
                """,
                (time.time(), str(uuid.UUID(job_uuid))),
            )
            self._connection.commit()

    def commit_feedback(
        self,
        job_uuid: str,
        *,
        command_uuid: str,
        job_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        row = self._authorized_job(job_uuid, command_uuid, job_token)
        sequence = payload.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ValueError("feedback sequence is invalid")
        with self._lock:
            through = max(int(row["feedback_sequence"]), sequence)
            self._connection.execute(
                """
                UPDATE local_edge_job SET feedback_sequence = ?, status = CASE
                    WHEN status IN ('pending', 'dispatched') THEN 'running'
                    ELSE status END, updated_at = ? WHERE job_uuid = ?
                """,
                (through, time.time(), str(uuid.UUID(job_uuid))),
            )
            self._connection.commit()
        return {"through_sequence": through}

    def save_outcome(
        self,
        job_uuid: str,
        *,
        command_uuid: str,
        job_token: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        row = self._authorized_job(job_uuid, command_uuid, job_token)
        outcome = str(payload.get("outcome") or "")
        if outcome not in {"succeeded", "failed", "canceled", "timeout"}:
            raise ValueError("outcome is invalid")
        unknown_ids = payload.get("unknown_command_ids") or []
        if not isinstance(unknown_ids, list) or any(
            not isinstance(value, str) or not value for value in unknown_ids
        ):
            raise ValueError("unknown_command_ids is invalid")
        normalized = {
            "outcome": outcome,
            "return_info": payload.get("return_info") or {},
            "error_info": payload.get("error_info") or [],
            "unknown_command_ids": unknown_ids,
        }
        encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        existing = row["outcome_json"]
        if existing is not None and str(existing) != encoded:
            raise ValueError("job outcome conflicts with its first committed result")
        is_new = existing is None
        status = "unknown" if unknown_ids else "outcome_pending"
        with self._lock:
            self._connection.execute(
                """
                UPDATE local_edge_job
                SET outcome_json = COALESCE(outcome_json, ?),
                    unknown_command_ids_json = ?, status = CASE
                        WHEN projected_at IS NOT NULL THEN status ELSE ? END,
                    updated_at = ?
                WHERE job_uuid = ?
                """,
                (
                    encoded,
                    json.dumps(unknown_ids, separators=(",", ":")),
                    status,
                    time.time(),
                    str(uuid.UUID(job_uuid)),
                ),
            )
            self._connection.commit()
        return normalized, is_new

    def mark_outcome_projected(self, job_uuid: str) -> None:
        with self._lock:
            self._connection.execute(
                """
                UPDATE local_edge_job SET status = 'completed',
                    projected_at = COALESCE(projected_at, ?), updated_at = ?
                WHERE job_uuid = ?
                """,
                (time.time(), time.time(), str(uuid.UUID(job_uuid))),
            )
            self._connection.commit()

    def is_outcome_projected(self, job_uuid: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT projected_at FROM local_edge_job WHERE job_uuid = ?",
                (str(uuid.UUID(job_uuid)),),
            ).fetchone()
        return row is not None and row["projected_at"] is not None

    def create_unknown_resolution(
        self, job_uuid: str, *, reason: str
    ) -> dict[str, Any]:
        normalized_job = str(uuid.UUID(job_uuid))
        if not reason.strip():
            raise ValueError("reason is required")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT * FROM local_edge_job WHERE job_uuid = ?", (normalized_job,)
            ).fetchone()
            if row is None:
                self._connection.rollback()
                raise KeyError(normalized_job)
            unknown_ids = json.loads(str(row["unknown_command_ids_json"]))
            if row["status"] != "unknown" or not unknown_ids:
                self._connection.rollback()
                raise ValueError("job has no unresolved UNKNOWN command")
            sequence = self._next_sequence_locked()
            command_uuid = str(uuid.uuid4())
            payload = {
                "job_uuid": normalized_job,
                "local_device_id": str(row["local_device_id"]),
                "device_command_id": str(unknown_ids[0]),
                "resolution": "canceled",
                "reason": reason.strip(),
            }
            try:
                self._connection.execute(
                    """
                    INSERT INTO local_edge_command(
                        command_uuid, sequence, type, payload_json, status, created_at
                    ) VALUES (?, ?, 'job.resolve_unknown', ?, 'pending', ?)
                    """,
                    (
                        command_uuid,
                        sequence,
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        time.time(),
                    ),
                )
                self._connection.execute(
                    """
                    INSERT INTO local_edge_meta(key, value) VALUES ('sequence', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (str(sequence),),
                )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return {"command_uuid": command_uuid, "sequence": sequence}

    def resolve_unknown_committed(self, job_uuid: str) -> bool:
        normalized = str(uuid.UUID(job_uuid))
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM local_edge_job WHERE job_uuid = ?", (normalized,)
            ).fetchone()
            if row is None or row["status"] != "unknown":
                return False
            self._connection.execute(
                """
                UPDATE local_edge_job SET status = 'outcome_pending',
                    unknown_command_ids_json = '[]', updated_at = ?
                WHERE job_uuid = ?
                """,
                (time.time(), normalized),
            )
            self._connection.commit()
        return True

    def busy_device_action_keys(self) -> set[str]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT device_action_key FROM local_edge_job
                WHERE status IN ('pending', 'dispatched', 'running', 'unknown')
                """
            ).fetchall()
        return {str(row["device_action_key"]) for row in rows}

    def job(self, job_uuid: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM local_edge_job WHERE job_uuid = ?",
                (str(uuid.UUID(job_uuid)),),
            ).fetchone()
        if row is None:
            raise KeyError(job_uuid)
        return _job_projection(row)

    def online_devices(self) -> dict[str, dict[str, Any]]:
        """Project the currently connected Edge registration as online facts."""

        with self._lock:
            row = self._connection.execute(
                """
                SELECT devices_json FROM local_edge_session
                WHERE connected = 1 ORDER BY updated_at DESC LIMIT 1
                """
            ).fetchone()
        if row is None:
            return {}
        devices = json.loads(str(row["devices_json"]))
        return {
            str(device["local_id"]): {
                "device_key": f"/devices/{device['local_id']}/{device['local_id']}",
                "namespace": f"/devices/{device['local_id']}",
                "machine_name": "managed-local-edge",
                "uuid": str(device.get("material_uuid") or ""),
                "node_name": str(device["local_id"]),
                "transport": "edge-control",
            }
            for device in devices
            if isinstance(device, dict) and str(device.get("local_id") or "")
        }

    def latest_registration(self) -> dict[str, Any] | None:
        """返回最近一次 Edge 注册的脱离副本，不把 SQLite 行泄漏给调用方。

        参数：无。返回：尚未注册时为 ``None``，否则包含 Edge/实例身份、连接
        状态、时间戳和设备声明。异常：持久的设备声明不是对象数组时按空数组
        失败关闭，数据库错误原样传播。
        """

        with self._lock:
            row = self._connection.execute(
                """
                SELECT edge_uuid,instance_uuid,edge_key,devices_json,connected,
                       created_at,updated_at
                FROM local_edge_session
                ORDER BY connected DESC,updated_at DESC,created_at DESC LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        decoded = json.loads(str(row["devices_json"]))
        devices = (
            [dict(device) for device in decoded if isinstance(device, dict)]
            if isinstance(decoded, list)
            else []
        )
        return {
            "edge_uuid": str(row["edge_uuid"]),
            "instance_uuid": str(row["instance_uuid"]),
            "edge_key": str(row["edge_key"]),
            "connected": bool(row["connected"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
            "devices": devices,
        }

    def has_device_binding(self, local_device_id: str, material_uuid: str) -> bool:
        """校验最近 Edge 会话声明的设备绑定（EdgeDeviceBinding）。

        参数：``local_device_id`` 是 Edge 本地设备身份，``material_uuid`` 是
        正式设备物料 UUID。返回：两者是否出现在同一最近注册设备声明中。异常：
        UUID 非法时返回 ``False``，数据库错误原样传播。
        """

        local_identity = str(local_device_id or "").strip()
        try:
            material_identity = str(uuid.UUID(str(material_uuid)))
        except (TypeError, ValueError, AttributeError):
            return False
        registration = self.latest_registration()
        if registration is None:
            return False
        return any(
            str(device.get("local_id") or "").strip() == local_identity
            and str(device.get("material_uuid") or "").strip() == material_identity
            for device in registration["devices"]
        )

    def has_local_device(self, local_device_id: str) -> bool:
        """校验本地设备身份存在于当前 Edge 注册快照。

        参数：``local_device_id`` 是候选 Edge 本地设备身份。返回：规范身份是否
        出现在最近注册设备声明中。异常：数据库错误原样传播；空身份返回假。
        """

        identity = str(local_device_id or "").strip()
        if not identity:
            return False
        registration = self.latest_registration()
        return registration is not None and any(
            str(device.get("local_id") or "").strip() == identity
            for device in registration["devices"]
        )

    def _authorized_job(
        self, job_uuid: str, command_uuid: str, job_token: str
    ) -> sqlite3.Row:
        normalized = str(uuid.UUID(job_uuid))
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM local_edge_job WHERE job_uuid = ?", (normalized,)
            ).fetchone()
        if row is None:
            raise KeyError(normalized)
        if row["command_uuid"] != str(uuid.UUID(command_uuid)) or not hmac.compare_digest(
            str(row["token_hash"]), _token_hash(job_token)
        ):
            raise PermissionError("job credential rejected")
        return row

    def _next_sequence_locked(self) -> int:
        row = self._connection.execute(
            "SELECT value FROM local_edge_meta WHERE key = 'sequence'"
        ).fetchone()
        return int(row["value"]) + 1 if row is not None else 1


class LocalEdgeControlAuthority:
    """本地后端（Local Backend）拥有的无 API key 调度与协议权威。"""

    def __init__(self, store: LocalEdgeAuthorityStore) -> None:
        self.store = store
        from unilabos.app.edge_control.device_telemetry import DeviceTelemetryHub

        self.telemetry = DeviceTelemetryHub(store.has_device_binding)
        self._telemetry_event_lock = threading.RLock()
        self._telemetry_events: OrderedDict[str, str] = OrderedDict()
        self.device_state = None
        self._listeners: list[Callable[[str, bool, Any, str], None]] = []

    def start(self) -> None:
        return

    def stop(self) -> None:
        self.store.close()

    def accept_telemetry_event(
        self,
        event_uuid: str,
        sent_at: str,
        payload: dict[str, Any],
    ) -> bool:
        """在当前微后端进程内幂等消费设备遥测短通知。

        参数：事件 UUID、发送时间与严格短通知载荷。返回：是否发布新的 SSE
        latest。异常：同一事件 UUID 改写身份时拒绝；不写 SQLite、不负责重启恢复。
        """

        normalized_uuid = str(uuid.UUID(event_uuid))
        normalized_sent_at = _required_text({"sent_at": sent_at}, "sent_at")
        identity = json.dumps(
            {"sent_at": normalized_sent_at, "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._telemetry_event_lock:
            existing = self._telemetry_events.get(normalized_uuid)
            if existing is not None:
                if existing != identity:
                    raise ValueError("duplicate Edge event identity changed")
                self._telemetry_events.move_to_end(normalized_uuid)
                return False
            changed = self.telemetry.notify(payload)
            self._telemetry_events[normalized_uuid] = identity
            if len(self._telemetry_events) > 4096:
                self._telemetry_events.popitem(last=False)
            return changed

    def dispatch(self, payload: DispatchPayload) -> None:
        self.store.dispatch(payload)

    def add_job_finished_listener(
        self, listener: Callable[[str, bool, Any, str], None]
    ) -> None:
        self._listeners.append(listener)

    def busy_device_action_keys(self) -> set[str]:
        return self.store.busy_device_action_keys()

    def commit_outcome(
        self,
        job_uuid: str,
        *,
        command_uuid: str,
        job_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        outcome, _is_new = self.store.save_outcome(
            job_uuid,
            command_uuid=command_uuid,
            job_token=job_token,
            payload=payload,
        )
        if outcome["unknown_command_ids"]:
            return {"uuid": job_uuid, "status": "unknown"}
        if not self.store.is_outcome_projected(job_uuid):
            succeeded = outcome["outcome"] == "succeeded"
            return_info = outcome["return_info"]
            result = (
                return_info.get("return_value")
                if isinstance(return_info, dict) and "return_value" in return_info
                else return_info
            )
            for listener in tuple(self._listeners):
                listener(job_uuid, succeeded, result, "normal")
            self.store.mark_outcome_projected(job_uuid)
        return {"uuid": job_uuid, "status": "completed"}

    def resolve_unknown_committed(self, job_uuid: str) -> None:
        if not self.store.resolve_unknown_committed(job_uuid):
            return
        if not self.store.is_outcome_projected(job_uuid):
            for listener in tuple(self._listeners):
                listener(job_uuid, False, None, "operator_intervention")
            self.store.mark_outcome_projected(job_uuid)


def create_local_edge_control_router(
    authority: LocalEdgeControlAuthority,
) -> APIRouter:
    """创建与正式协议同形的本地后端（Local Backend）适配器。

    本地边缘控制权威（LocalEdgeControlAuthority）依赖工作区部署边界，不复制
    正式后端（Backend）的 API key 认证。设备会话仍携带稳定 ``edge_key``；单个
    作业仍由命令 UUID 和作业 token 共同约束。
    """

    router = APIRouter(prefix="/api/v1/edge", tags=["local-edge-control"])

    @router.post("/sessions")
    def register_session(
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            result = authority.store.register_session(payload)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return _envelope(result)

    @router.get("/jobs/{job_uuid}")
    def fetch_job(
        job_uuid: str,
        task_uuid: str,
        node_uuid: str,
        x_command_uuid: str = Header(alias="X-Command-UUID"),
        x_job_token: str = Header(alias="X-Job-Token"),
    ) -> dict[str, Any]:
        try:
            result = authority.store.fetch_job(
                job_uuid,
                command_uuid=x_command_uuid,
                job_token=x_job_token,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Job not found") from error
        except (PermissionError, ValueError) as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        if result["task_uuid"] != task_uuid or result["node_uuid"] != node_uuid:
            raise HTTPException(status_code=409, detail="Job identity changed")
        return _envelope(result)

    @router.post("/jobs/{job_uuid}/feedback")
    def commit_feedback(
        job_uuid: str,
        payload: dict[str, Any],
        x_command_uuid: str = Header(alias="X-Command-UUID"),
        x_job_token: str = Header(alias="X-Job-Token"),
    ) -> dict[str, Any]:
        try:
            result = authority.store.commit_feedback(
                job_uuid,
                command_uuid=x_command_uuid,
                job_token=x_job_token,
                payload=payload,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Job not found") from error
        except (PermissionError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return _envelope(result)

    @router.put("/jobs/{job_uuid}/outcome")
    def commit_outcome(
        job_uuid: str,
        payload: dict[str, Any],
        x_command_uuid: str = Header(alias="X-Command-UUID"),
        x_job_token: str = Header(alias="X-Job-Token"),
    ) -> dict[str, Any]:
        try:
            result = authority.commit_outcome(
                job_uuid,
                command_uuid=x_command_uuid,
                job_token=x_job_token,
                payload=payload,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Job not found") from error
        except PermissionError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        except (RuntimeError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _envelope(result)

    @router.post("/jobs/{job_uuid}/resolve-unknown")
    def resolve_unknown(
        job_uuid: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            result = authority.store.create_unknown_resolution(
                job_uuid, reason=str(payload.get("reason") or "")
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Job not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _envelope(result)

    @router.websocket("/ws")
    async def edge_websocket(websocket: WebSocket) -> None:
        await websocket.accept()
        session_uuid = ""
        try:
            hello = json.loads(await asyncio.wait_for(websocket.receive_text(), 10))
            if hello.get("type") != "hello" or not isinstance(
                hello.get("payload"), dict
            ):
                await websocket.close(code=4400)
                return
            session_uuid = _required_text(hello["payload"], "session_uuid")
            authority.store.reconcile_hello(hello["payload"])
            authority.store.set_session_connected(session_uuid, True)
            await _write_event_ack(
                websocket,
                str(uuid.UUID(_required_text(hello, "message_uuid"))),
            )
            while True:
                for command in authority.store.pending_commands():
                    await websocket.send_text(json.dumps(command, ensure_ascii=False))
                    authority.store.mark_command_sent(str(command["message_uuid"]))
                try:
                    encoded = await asyncio.wait_for(
                        websocket.receive_text(), timeout=0.1
                    )
                except TimeoutError:
                    continue
                event = json.loads(encoded)
                await _handle_edge_event(authority, websocket, event)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            if session_uuid:
                authority.store.mark_disconnected_jobs_unknown()
                try:
                    authority.store.set_session_connected(session_uuid, False)
                except ValueError:
                    pass

    return router


async def _handle_edge_event(
    authority: LocalEdgeControlAuthority,
    websocket: WebSocket,
    event: dict[str, Any],
) -> None:
    if event.get("protocol_version") != _PROTOCOL_VERSION:
        raise ValueError("unsupported Edge protocol version")
    event_uuid = str(uuid.UUID(_required_text(event, "message_uuid")))
    event_type = _required_text(event, "type")
    sent_at = _required_text(event, "sent_at")
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("event payload must be an object")
    if event_type == "command.ack":
        authority.store.acknowledge_command(_required_text(payload, "command_uuid"))
    elif event_type == "job.started":
        authority.store.mark_job_started(_required_text(payload, "job_uuid"))
    elif event_type == "job.unknown_resolution_committed":
        authority.resolve_unknown_committed(_required_text(payload, "job_uuid"))
    elif event_type == "device.telemetry_committed":
        authority.accept_telemetry_event(
            event_uuid,
            sent_at,
            payload,
        )
    elif event_type not in {
        "job.feedback_committed",
        "job.outcome_committed",
    }:
        raise ValueError(f"unsupported Edge event {event_type!r}")
    await _write_event_ack(websocket, event_uuid)


async def _write_event_ack(websocket: WebSocket, event_uuid: str) -> None:
    """发送正式 Edge 信封形状的 ``event.ack``。

    参数：当前 WebSocket 和已处理事件 UUID。返回：无。异常：序列化或连接
    写入错误原样传播，使连接关闭并由 Edge outbox 重放。
    """

    await websocket.send_text(
        json.dumps(
            {
                "protocol_version": _PROTOCOL_VERSION,
                "message_uuid": str(uuid.uuid4()),
                "sequence": 0,
                "type": "event.ack",
                "sent_at": _utc_now(),
                "payload": {"event_uuid": event_uuid},
            },
            ensure_ascii=False,
        )
    )


def _required_text(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _job_projection(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "job_uuid": str(row["job_uuid"]),
        "task_uuid": str(row["task_uuid"]),
        "node_uuid": str(row["node_uuid"]),
        "command_uuid": str(row["command_uuid"]),
        "local_device_id": str(row["local_device_id"]),
        "action_name": str(row["action_name"]),
        "device_action_key": str(row["device_action_key"]),
        "status": str(row["status"]),
    }


def _envelope(data: dict[str, Any]) -> dict[str, Any]:
    return {"code": 0, "data": data}


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000000Z"


__all__ = [
    "LocalEdgeAuthorityStore",
    "LocalEdgeControlAuthority",
    "create_local_edge_control_router",
]
