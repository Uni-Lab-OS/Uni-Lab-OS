"""WorkflowTask/WorkflowNodeJob 的 durable runtime state kernel。"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Protocol
from uuid import uuid4

from unilabos.resources.authority import MaterialModule
from unilabos.resources.authority.sqlite import SQLiteMaterialAdapter
from unilabos.workflow.json_codec import decode_json_bytes, encode_json
from unilabos.workflow.material_resolver import MaterialResourceSlotResolver
from unilabos.workflow.store import (
    StoreConflict,
    StoreNotFound,
    WorkflowStore,
    utc_now,
)
from unilabos.workflow.task_input import (
    TaskInputError,
    material_root_uuids_from_task_snapshot,
)

_LOGGER = logging.getLogger(__name__)

TASK_TRANSITIONS = {
    "pending": frozenset({"running", "canceled"}),
    "running": frozenset({"succeeded", "failed", "canceling", "timeout"}),
    "canceling": frozenset({"canceled", "failed", "timeout"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "canceled": frozenset(),
    "timeout": frozenset(),
}
JOB_TRANSITIONS = {
    "pending": frozenset({"dispatched", "failed", "skipped", "canceled"}),
    "dispatched": frozenset(
        {
            "running",
            "cancel_requested",
            "succeeded",
            "failed",
            "canceled",
            "timeout",
            "execution_unknown",
        }
    ),
    "running": frozenset(
        {
            "intervention_required",
            "cancel_requested",
            "succeeded",
            "failed",
            "canceled",
            "timeout",
            "execution_unknown",
        }
    ),
    "intervention_required": frozenset(
        {
            "running",
            "cancel_requested",
            "failed",
            "timeout",
            "execution_unknown",
        }
    ),
    "cancel_requested": frozenset(
        {"canceled", "failed", "timeout", "execution_unknown"}
    ),
    "execution_unknown": frozenset(
        {"running", "succeeded", "failed", "canceled", "timeout"}
    ),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "skipped": frozenset(),
    "canceled": frozenset(),
    "timeout": frozenset(),
}

_TASK_TERMINAL = frozenset({"succeeded", "failed", "canceled", "timeout"})
_JOB_TERMINAL = frozenset({"succeeded", "failed", "skipped", "canceled", "timeout"})
_JOB_IN_FLIGHT = frozenset(
    {"dispatched", "running", "intervention_required", "cancel_requested"}
)


class TaskMaterialReservationGuard(Protocol):
    """Runtime dispatch 所需的最小 Material capability。"""

    def has_complete_task_reservation(
        self,
        uow: Any,
        *,
        task_uuid: str,
        root_material_uuids: tuple[str, ...],
    ) -> bool: ...


class WorkflowJobDispatcher(Protocol):
    """ROS execution backend capability required by the durable worker."""

    def dispatch(self, payload: Mapping[str, Any]) -> None: ...

    def add_job_finished_listener(self, listener: Callable[..., None]) -> None: ...

    def remove_job_finished_listener(self, listener: Callable[..., None]) -> None: ...

    def execution_ready(self) -> bool: ...


def _json(value: Any) -> str:
    return encode_json(value, sort_keys=True).decode("utf-8")


def _load(value: str) -> Any:
    return decode_json_bytes(value.encode("utf-8"))


def _normalized_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise StoreConflict("feedback observed_at must include a timezone")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if not isinstance(value, str) or not value.strip():
        raise StoreConflict("feedback observed_at must be a timestamp")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise StoreConflict("feedback observed_at must be a timestamp") from None
    if parsed.tzinfo is None:
        raise StoreConflict("feedback observed_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class WorkflowRuntimeCoordinator:
    """Task/Job runtime mutation 的唯一公开领域入口。"""

    def __init__(
        self,
        store: WorkflowStore,
        *,
        material_reservations: TaskMaterialReservationGuard | None = None,
    ):
        self._store = store
        if material_reservations is None:
            materials = MaterialModule(
                SQLiteMaterialAdapter.from_runtime_authority(store),
                resource_templates={},
            )
            material_reservations = MaterialResourceSlotResolver(materials)
        self._material_reservations = material_reservations

    def _require_complete_material_reservation(
        self,
        connection: sqlite3.Connection,
        task: sqlite3.Row,
    ) -> None:
        try:
            roots = material_root_uuids_from_task_snapshot(
                _load(task["workflow_snapshot"]),
                _load(task["input"]),
                _load(task["execution_plan"]),
            )
            if not roots:
                return
            complete = self._material_reservations.has_complete_task_reservation(
                connection,
                task_uuid=task["uuid"],
                root_material_uuids=roots,
            )
        except (TaskInputError, TypeError, ValueError):
            raise StoreConflict("task Material reservation is not verifiable") from None
        if not complete:
            raise StoreConflict("task does not own a complete Material reservation")

    @staticmethod
    def _task_row(
        connection: sqlite3.Connection,
        task_uuid: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM workflow_task
            WHERE uuid = ? AND deleted_at IS NULL
            """,
            (task_uuid,),
        ).fetchone()
        if row is None:
            raise StoreNotFound(f"workflow task {task_uuid} not found")
        return row

    @staticmethod
    def _job_row(
        connection: sqlite3.Connection,
        job_uuid: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM workflow_node_job
            WHERE uuid = ? AND deleted_at IS NULL
            """,
            (job_uuid,),
        ).fetchone()
        if row is None:
            raise StoreNotFound(f"workflow node job {job_uuid} not found")
        return row

    @staticmethod
    def _append_journal(
        connection: sqlite3.Connection,
        *,
        task_uuid: str,
        kind: str,
        now: str,
        job_uuid: Optional[str] = None,
        command_uuid: Optional[str] = None,
        from_status: Optional[str] = None,
        to_status: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO workflow_runtime_journal(
                workflow_task_uuid, workflow_node_job_uuid,
                workflow_task_command_uuid, kind, from_status, to_status,
                data, create_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_uuid,
                job_uuid,
                command_uuid,
                kind,
                from_status,
                to_status,
                _json(data or {}),
                now,
            ),
        )

    @staticmethod
    def _append_invalidation(
        connection: sqlite3.Connection,
        *,
        task_uuid: str,
        now: str,
    ) -> None:
        WorkflowStore._append_event(
            connection,
            event="workflow.runtime.changed",
            data={"workflow_task_uuid": task_uuid},
            now=now,
        )

    @staticmethod
    def _project_task(row: sqlite3.Row) -> Dict[str, Any]:
        return WorkflowStore._task_row(row)

    @staticmethod
    def _project_job(row: sqlite3.Row) -> Dict[str, Any]:
        return WorkflowStore._job_row(row)

    @staticmethod
    def _project_command(row: sqlite3.Row) -> Dict[str, Any]:
        return WorkflowStore._task_command_row(row)

    @staticmethod
    def _require_transition(
        transitions: Dict[str, frozenset[str]],
        source: str,
        target: str,
    ) -> None:
        if target not in transitions.get(source, frozenset()):
            raise StoreConflict(f"invalid runtime transition {source!r} -> {target!r}")

    def start_task(self, task_uuid: str) -> Dict[str, Any]:
        return self.transition_task(task_uuid, "running")

    def transition_task(
        self,
        task_uuid: str,
        status: str,
        *,
        error_info: Optional[list[Any]] = None,
    ) -> Dict[str, Any]:
        with self._store.transaction() as connection:
            now = utc_now()
            row = self._task_row(connection, task_uuid)
            source = row["status"]
            self._require_transition(TASK_TRANSITIONS, source, status)
            assignments = ["status = ?", "update_time = ?"]
            values: list[Any] = [status, now]
            if status == "running" and row["started_at"] is None:
                assignments.append("started_at = ?")
                values.append(now)
            if status in _TASK_TERMINAL:
                assignments.append("finished_at = ?")
                values.append(now)
            if error_info is not None:
                if not isinstance(error_info, list):
                    raise StoreConflict("task error_info must be an array")
                assignments.append("error_info = ?")
                values.append(_json(error_info))
            values.append(task_uuid)
            connection.execute(
                f"UPDATE workflow_task SET {', '.join(assignments)} WHERE uuid = ?",
                values,
            )
            self._append_journal(
                connection,
                task_uuid=task_uuid,
                kind="task_transition",
                from_status=source,
                to_status=status,
                now=now,
            )
            self._append_invalidation(connection, task_uuid=task_uuid, now=now)
            return self._project_task(self._task_row(connection, task_uuid))

    def transition_job(
        self,
        job_uuid: str,
        status: str,
        *,
        feedback_data: Optional[Dict[str, Any]] = None,
        return_info: Optional[Dict[str, Any]] = None,
        error_info: Optional[list[Any]] = None,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        if status == "execution_unknown":
            return self.mark_job_unknown(
                job_uuid,
                reason or "execution outcome unknown",
            )
        with self._store.transaction() as connection:
            now = utc_now()
            row = self._job_row(connection, job_uuid)
            source = row["status"]
            if source == "execution_unknown":
                raise StoreConflict(
                    "execution_unknown requires explicit reconciliation"
                )
            self._require_transition(JOB_TRANSITIONS, source, status)
            task = self._task_row(connection, row["workflow_task_uuid"])
            if task["status"] in _TASK_TERMINAL:
                raise StoreConflict("terminal task cannot mutate a job")
            if status == "dispatched":
                self._require_complete_material_reservation(connection, task)
            assignments = ["status = ?", "update_time = ?"]
            values: list[Any] = [status, now]
            if status == "running" and row["started_at"] is None:
                assignments.append("started_at = ?")
                values.append(now)
            if status in _JOB_TERMINAL:
                assignments.append("finished_at = ?")
                values.append(now)
            for column, value, expected in (
                ("feedback_data", feedback_data, dict),
                ("return_info", return_info, dict),
                ("error_info", error_info, list),
            ):
                if value is None:
                    continue
                if not isinstance(value, expected):
                    raise StoreConflict(f"job {column} has an invalid JSON shape")
                assignments.append(f"{column} = ?")
                values.append(_json(value))
            values.append(job_uuid)
            connection.execute(
                f"UPDATE workflow_node_job SET {', '.join(assignments)} WHERE uuid = ?",
                values,
            )
            self._append_journal(
                connection,
                task_uuid=row["workflow_task_uuid"],
                job_uuid=job_uuid,
                kind="job_transition",
                from_status=source,
                to_status=status,
                now=now,
            )
            self._append_invalidation(
                connection,
                task_uuid=row["workflow_task_uuid"],
                now=now,
            )
            return self._project_job(self._job_row(connection, job_uuid))

    def mark_job_unknown(self, job_uuid: str, reason: str) -> Dict[str, Any]:
        reason = reason.strip() if isinstance(reason, str) else ""
        if not reason:
            raise StoreConflict("execution uncertainty requires a reason")
        with self._store.transaction() as connection:
            now = utc_now()
            job = self._job_row(connection, job_uuid)
            source = job["status"]
            self._require_transition(JOB_TRANSITIONS, source, "execution_unknown")
            task_uuid = job["workflow_task_uuid"]
            task = self._task_row(connection, task_uuid)
            if task["status"] in _TASK_TERMINAL:
                raise StoreConflict("terminal task cannot become uncertain")
            resume_status = task["reconciliation_resume_control_status"]
            if task["control_status"] != "waiting_reconciliation":
                resume_status = (
                    task["control_status"]
                    if task["control_status"] in {"active", "paused"}
                    else "active"
                )
            connection.execute(
                """
                UPDATE workflow_node_job
                SET status = 'execution_unknown', uncertainty_reason = ?,
                    update_time = ?
                WHERE uuid = ?
                """,
                (reason, now, job_uuid),
            )
            connection.execute(
                """
                UPDATE workflow_task
                SET control_status = 'waiting_reconciliation',
                    cleanup_status = 'requires_attention',
                    reconciliation_resume_control_status = ?,
                    attention_reason = ?, update_time = ?
                WHERE uuid = ?
                """,
                (resume_status, reason, now, task_uuid),
            )
            self._append_journal(
                connection,
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                kind="uncertainty_opened",
                from_status=source,
                to_status="execution_unknown",
                data={"reason": reason},
                now=now,
            )
            self._append_invalidation(connection, task_uuid=task_uuid, now=now)
            return self._project_job(self._job_row(connection, job_uuid))

    def resolve_job_uncertainty(
        self,
        job_uuid: str,
        status: str,
        *,
        reason: str,
    ) -> Dict[str, Any]:
        reason = reason.strip() if isinstance(reason, str) else ""
        if not reason:
            raise StoreConflict("reconciliation requires a reason")
        with self._store.transaction() as connection:
            now = utc_now()
            job = self._job_row(connection, job_uuid)
            source = job["status"]
            self._require_transition(JOB_TRANSITIONS, source, status)
            task_uuid = job["workflow_task_uuid"]
            task = self._task_row(connection, task_uuid)
            assignments = [
                "status = ?",
                "uncertainty_reason = NULL",
                "update_time = ?",
            ]
            values: list[Any] = [status, now]
            if status == "running" and job["started_at"] is None:
                assignments.append("started_at = ?")
                values.append(now)
            if status in _JOB_TERMINAL:
                assignments.append("finished_at = ?")
                values.append(now)
            values.append(job_uuid)
            connection.execute(
                f"UPDATE workflow_node_job SET {', '.join(assignments)} WHERE uuid = ?",
                values,
            )
            remaining_unknown = connection.execute(
                """
                SELECT COUNT(*) FROM workflow_node_job
                WHERE workflow_task_uuid = ? AND deleted_at IS NULL
                  AND status = 'execution_unknown'
                """,
                (task_uuid,),
            ).fetchone()[0]
            if remaining_unknown == 0:
                resume_status = (
                    task["reconciliation_resume_control_status"]
                    if task["reconciliation_resume_control_status"]
                    in {"active", "paused"}
                    else "active"
                )
                task_status = task["status"]
                cleanup_status = "none"
                if task_status == "canceling":
                    active = connection.execute(
                        """
                        SELECT COUNT(*) FROM workflow_node_job
                        WHERE workflow_task_uuid = ? AND deleted_at IS NULL
                          AND status IN (
                              'dispatched', 'running', 'intervention_required',
                              'cancel_requested', 'execution_unknown'
                          )
                        """,
                        (task_uuid,),
                    ).fetchone()[0]
                    if active:
                        cleanup_status = "canceling"
                    else:
                        task_status = "canceled"
                        cleanup_status = "settled"
                connection.execute(
                    """
                    UPDATE workflow_task
                    SET status = ?, control_status = ?, cleanup_status = ?,
                        reconciliation_resume_control_status = NULL,
                        attention_reason = NULL, update_time = ?,
                        finished_at = CASE
                            WHEN ? = 'canceled' THEN COALESCE(finished_at, ?)
                            ELSE finished_at
                        END
                    WHERE uuid = ?
                    """,
                    (
                        task_status,
                        resume_status,
                        cleanup_status,
                        now,
                        task_status,
                        now,
                        task_uuid,
                    ),
                )
            else:
                remaining = connection.execute(
                    """
                    SELECT uncertainty_reason FROM workflow_node_job
                    WHERE workflow_task_uuid = ? AND deleted_at IS NULL
                      AND status = 'execution_unknown'
                    ORDER BY topological_index, create_time, uuid
                    LIMIT 1
                    """,
                    (task_uuid,),
                ).fetchone()
                assert remaining is not None
                connection.execute(
                    """
                    UPDATE workflow_task
                    SET attention_reason = ?, update_time = ?
                    WHERE uuid = ?
                    """,
                    (remaining["uncertainty_reason"], now, task_uuid),
                )
            self._append_journal(
                connection,
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                kind="uncertainty_resolved",
                from_status=source,
                to_status=status,
                data={"reason": reason},
                now=now,
            )
            self._append_invalidation(connection, task_uuid=task_uuid, now=now)
            return self._project_job(self._job_row(connection, job_uuid))

    def consume_next_command(self, task_uuid: str) -> Optional[Dict[str, Any]]:
        with self._store.transaction() as connection:
            now = utc_now()
            task = self._task_row(connection, task_uuid)
            command = connection.execute(
                """
                SELECT * FROM workflow_task_command
                WHERE workflow_task_uuid = ? AND deleted_at IS NULL
                  AND status = 'pending'
                ORDER BY create_time, uuid
                LIMIT 1
                """,
                (task_uuid,),
            ).fetchone()
            if command is None:
                return None
            outcome = "applied"
            if task["status"] in _TASK_TERMINAL:
                outcome = "rejected"
            else:
                command_type = command["type"]
                if command_type == "pause":
                    self._apply_control_command(
                        connection,
                        task,
                        control_status="paused",
                        now=now,
                    )
                elif command_type == "resume":
                    self._apply_control_command(
                        connection,
                        task,
                        control_status="active",
                        now=now,
                    )
                elif command_type == "step":
                    connection.execute(
                        """
                        INSERT INTO workflow_task_step_permit(
                            workflow_task_command_uuid, workflow_task_uuid,
                            target_node_uuid, status, create_time, consumed_at
                        ) VALUES (?, ?, ?, 'available', ?, NULL)
                        """,
                        (
                            command["uuid"],
                            task_uuid,
                            command["target_node_uuid"],
                            now,
                        ),
                    )
                elif command_type == "cancel":
                    self._apply_cancel(connection, task, now=now)
                else:
                    outcome = "rejected"
            if outcome == "applied":
                result = {"outcome": "applied"}
                status = "succeeded"
            else:
                result = {
                    "outcome": "rejected",
                    "error_code": "invalid_transition",
                }
                status = "rejected"
            connection.execute(
                """
                UPDATE workflow_task_command
                SET status = ?, result = ?, consumed_at = ?, update_time = ?
                WHERE uuid = ? AND status = 'pending'
                """,
                (status, _json(result), now, now, command["uuid"]),
            )
            self._append_journal(
                connection,
                task_uuid=task_uuid,
                command_uuid=command["uuid"],
                kind="command_consumed",
                from_status="pending",
                to_status=status,
                data={"command_type": command["type"], **result},
                now=now,
            )
            self._append_invalidation(connection, task_uuid=task_uuid, now=now)
            updated = connection.execute(
                "SELECT * FROM workflow_task_command WHERE uuid = ?",
                (command["uuid"],),
            ).fetchone()
            assert updated is not None
            return self._project_command(updated)

    @staticmethod
    def _apply_control_command(
        connection: sqlite3.Connection,
        task: sqlite3.Row,
        *,
        control_status: str,
        now: str,
    ) -> None:
        if task["control_status"] == "waiting_reconciliation":
            connection.execute(
                """
                UPDATE workflow_task
                SET reconciliation_resume_control_status = ?, update_time = ?
                WHERE uuid = ?
                """,
                (control_status, now, task["uuid"]),
            )
        else:
            connection.execute(
                """
                UPDATE workflow_task
                SET control_status = ?, update_time = ? WHERE uuid = ?
                """,
                (control_status, now, task["uuid"]),
            )

    def _apply_cancel(
        self,
        connection: sqlite3.Connection,
        task: sqlite3.Row,
        *,
        now: str,
    ) -> None:
        task_uuid = task["uuid"]
        jobs = connection.execute(
            """
            SELECT * FROM workflow_node_job
            WHERE workflow_task_uuid = ? AND deleted_at IS NULL
            ORDER BY topological_index, create_time, uuid
            """,
            (task_uuid,),
        ).fetchall()
        if task["status"] == "pending":
            for job in jobs:
                if job["status"] != "pending":
                    continue
                connection.execute(
                    """
                    UPDATE workflow_node_job
                    SET status = 'canceled', update_time = ?, finished_at = ?
                    WHERE uuid = ?
                    """,
                    (now, now, job["uuid"]),
                )
                self._append_journal(
                    connection,
                    task_uuid=task_uuid,
                    job_uuid=job["uuid"],
                    kind="job_transition",
                    from_status="pending",
                    to_status="canceled",
                    now=now,
                )
            connection.execute(
                """
                UPDATE workflow_task
                SET status = 'canceled', control_status = 'paused',
                    cleanup_status = 'settled', update_time = ?, finished_at = ?
                WHERE uuid = ?
                """,
                (now, now, task_uuid),
            )
            self._append_journal(
                connection,
                task_uuid=task_uuid,
                kind="task_transition",
                from_status="pending",
                to_status="canceled",
                now=now,
            )
            return

        for job in jobs:
            source = job["status"]
            target: Optional[str] = None
            if source == "pending":
                target = "canceled"
            elif source in {"dispatched", "running", "intervention_required"}:
                target = "cancel_requested"
            if target is None:
                continue
            finished_at = now if target == "canceled" else None
            connection.execute(
                """
                UPDATE workflow_node_job
                SET status = ?, update_time = ?,
                    finished_at = COALESCE(?, finished_at)
                WHERE uuid = ?
                """,
                (target, now, finished_at, job["uuid"]),
            )
            self._append_journal(
                connection,
                task_uuid=task_uuid,
                job_uuid=job["uuid"],
                kind="job_transition",
                from_status=source,
                to_status=target,
                now=now,
            )
        counts = {
            row["status"]: row["count"]
            for row in connection.execute(
                """
                SELECT status, COUNT(*) AS count FROM workflow_node_job
                WHERE workflow_task_uuid = ? AND deleted_at IS NULL
                GROUP BY status
                """,
                (task_uuid,),
            )
        }
        unknown = counts.get("execution_unknown", 0)
        active = sum(
            counts.get(status, 0)
            for status in {
                "dispatched",
                "running",
                "intervention_required",
                "cancel_requested",
            }
        )
        if unknown:
            next_status = "canceling"
            cleanup = "requires_attention"
        elif active:
            next_status = "canceling"
            cleanup = "canceling"
        else:
            next_status = "canceled"
            cleanup = "settled"
        journal_source = task["status"]
        if journal_source == "running":
            self._append_journal(
                connection,
                task_uuid=task_uuid,
                kind="task_transition",
                from_status="running",
                to_status="canceling",
                now=now,
            )
            journal_source = "canceling"
        connection.execute(
            """
            UPDATE workflow_task
            SET status = ?, cleanup_status = ?,
                control_status = CASE
                    WHEN control_status = 'waiting_reconciliation'
                    THEN control_status
                    ELSE 'paused'
                END,
                reconciliation_resume_control_status = CASE
                    WHEN control_status = 'waiting_reconciliation'
                    THEN 'paused'
                    ELSE reconciliation_resume_control_status
                END,
                update_time = ?,
                finished_at = CASE
                    WHEN ? = 'canceled' THEN COALESCE(finished_at, ?)
                    ELSE finished_at
                END
            WHERE uuid = ?
            """,
            (next_status, cleanup, now, next_status, now, task_uuid),
        )
        if journal_source != next_status:
            self._append_journal(
                connection,
                task_uuid=task_uuid,
                kind="task_transition",
                from_status=journal_source,
                to_status=next_status,
                now=now,
            )

    def commit_job_feedback(
        self,
        job_uuid: str,
        samples: Iterable[Dict[str, Any]],
    ) -> Dict[str, int]:
        normalized: list[Dict[str, Any]] = []
        seen_sequence: dict[int, Dict[str, Any]] = {}
        seen_key: dict[str, Dict[str, Any]] = {}
        for raw in samples:
            if not isinstance(raw, dict):
                raise StoreConflict("feedback sample must be an object")
            sequence = raw.get("sequence")
            feedback_type = raw.get("feedback_type")
            data = raw.get("data")
            key = raw.get("idempotency_key")
            if (
                isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence < 1
            ):
                raise StoreConflict("feedback sequence must be positive")
            feedback_type = (
                feedback_type.strip() if isinstance(feedback_type, str) else ""
            )
            key = key.strip() if isinstance(key, str) else ""
            if not feedback_type or not key or not isinstance(data, dict):
                raise StoreConflict("feedback sample has an invalid shape")
            sample = {
                "sequence": sequence,
                "feedback_type": feedback_type,
                "data": data,
                "observed_at": _normalized_timestamp(raw.get("observed_at")),
                "idempotency_key": key,
            }
            previous_sequence = seen_sequence.get(sequence)
            previous_key = seen_key.get(key)
            if (previous_sequence is not None and previous_sequence != sample) or (
                previous_key is not None and previous_key != sample
            ):
                raise StoreConflict("feedback batch reuses an idempotency identity")
            if previous_sequence is None and previous_key is None:
                normalized.append(sample)
            seen_sequence[sequence] = sample
            seen_key[key] = sample
        if not normalized:
            raise StoreConflict("feedback batch must not be empty")
        normalized.sort(key=lambda item: (item["sequence"], item["idempotency_key"]))
        with self._store.transaction() as connection:
            now = utc_now()
            job = self._job_row(connection, job_uuid)
            if job["status"] in _JOB_TERMINAL:
                raise StoreConflict("terminal job cannot accept feedback")
            created: list[Dict[str, Any]] = []
            for sample in normalized:
                matches = connection.execute(
                    """
                    SELECT * FROM workflow_node_job_feedback_history
                    WHERE workflow_node_job_uuid = ? AND deleted_at IS NULL
                      AND (sequence = ? OR idempotency_key = ?)
                    ORDER BY sequence
                    """,
                    (job_uuid, sample["sequence"], sample["idempotency_key"]),
                ).fetchall()
                if matches:
                    if len(matches) != 1 or not self._feedback_matches(
                        matches[0], sample
                    ):
                        raise StoreConflict(
                            "feedback identity already has other content"
                        )
                    continue
                connection.execute(
                    """
                    INSERT INTO workflow_node_job_feedback_history(
                        uuid, create_time, update_time, deleted_at, description,
                        meta_data, workflow_node_job_uuid, sequence,
                        feedback_type, data, observed_at, received_at,
                        published_at, idempotency_key
                    ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        now,
                        now,
                        job_uuid,
                        sample["sequence"],
                        sample["feedback_type"],
                        _json(sample["data"]),
                        sample["observed_at"],
                        now,
                        now,
                        sample["idempotency_key"],
                    ),
                )
                created.append(sample)
            if created:
                latest = max(created, key=lambda item: item["sequence"])
                if latest["sequence"] > job["feedback_sequence"]:
                    connection.execute(
                        """
                        UPDATE workflow_node_job
                        SET feedback_sequence = ?, feedback_data = ?, update_time = ?
                        WHERE uuid = ?
                        """,
                        (
                            latest["sequence"],
                            _json(latest["data"]),
                            now,
                            job_uuid,
                        ),
                    )
                for sample in created:
                    self._append_journal(
                        connection,
                        task_uuid=job["workflow_task_uuid"],
                        job_uuid=job_uuid,
                        kind="feedback_committed",
                        data={
                            "sequence": sample["sequence"],
                            "feedback_type": sample["feedback_type"],
                        },
                        now=now,
                    )
                self._append_invalidation(
                    connection,
                    task_uuid=job["workflow_task_uuid"],
                    now=now,
                )
            through_sequence = max(
                job["feedback_sequence"],
                max(sample["sequence"] for sample in normalized),
            )
            return {"through_sequence": through_sequence, "created": len(created)}

    @staticmethod
    def _feedback_matches(
        row: sqlite3.Row,
        sample: Dict[str, Any],
    ) -> bool:
        return (
            row["sequence"] == sample["sequence"]
            and row["feedback_type"] == sample["feedback_type"]
            and _load(row["data"]) == sample["data"]
            and row["observed_at"] == sample["observed_at"]
            and row["idempotency_key"] == sample["idempotency_key"]
        )

    def recover_startup(self) -> Dict[str, int]:
        with self._store.transaction() as connection:
            now = utc_now()
            jobs = connection.execute(
                """
                SELECT * FROM workflow_node_job
                WHERE deleted_at IS NULL
                  AND status IN (
                      'dispatched', 'running', 'intervention_required',
                      'cancel_requested'
                  )
                ORDER BY workflow_task_uuid, topological_index, create_time, uuid
                """
            ).fetchall()
            affected_tasks: set[str] = set()
            for job in jobs:
                task_uuid = job["workflow_task_uuid"]
                task = self._task_row(connection, task_uuid)
                resume_status = task["reconciliation_resume_control_status"]
                if task["control_status"] != "waiting_reconciliation":
                    resume_status = (
                        task["control_status"]
                        if task["control_status"] in {"active", "paused"}
                        else "active"
                    )
                connection.execute(
                    """
                    UPDATE workflow_node_job
                    SET status = 'execution_unknown',
                        uncertainty_reason = 'runtime_restarted_in_flight',
                        update_time = ?
                    WHERE uuid = ?
                    """,
                    (now, job["uuid"]),
                )
                connection.execute(
                    """
                    UPDATE workflow_task
                    SET control_status = 'waiting_reconciliation',
                        cleanup_status = 'requires_attention',
                        reconciliation_resume_control_status = ?,
                        attention_reason = 'runtime_restarted_in_flight',
                        update_time = ?
                    WHERE uuid = ?
                    """,
                    (resume_status, now, task_uuid),
                )
                self._append_journal(
                    connection,
                    task_uuid=task_uuid,
                    job_uuid=job["uuid"],
                    kind="startup_recovered",
                    from_status=job["status"],
                    to_status="execution_unknown",
                    data={"reason": "runtime_restarted_in_flight"},
                    now=now,
                )
                affected_tasks.add(task_uuid)
            for task_uuid in sorted(affected_tasks):
                self._append_invalidation(connection, task_uuid=task_uuid, now=now)
            return {
                "recovered_jobs": len(jobs),
                "affected_tasks": len(affected_tasks),
            }

    def _pending_command_task_uuids(self) -> list[str]:
        with self._store.transaction() as connection:
            return [
                row["workflow_task_uuid"]
                for row in connection.execute(
                    """
                    SELECT workflow_task_uuid, MIN(create_time) AS oldest
                    FROM workflow_task_command
                    WHERE deleted_at IS NULL AND status = 'pending'
                    GROUP BY workflow_task_uuid
                    ORDER BY oldest, workflow_task_uuid
                    """
                )
            ]

    def _execution_tasks(self) -> list[Dict[str, Any]]:
        """Return frozen pending/running Tasks in deterministic creation order."""

        with self._store.transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workflow_task
                WHERE deleted_at IS NULL AND status IN ('pending', 'running')
                ORDER BY create_time, uuid
                """
            ).fetchall()
            return [self._project_task(row) for row in rows]

    def _execution_jobs(self, task_uuid: str) -> list[Dict[str, Any]]:
        return self._store.list_jobs(task_uuid)

    def _execution_job(self, job_uuid: str) -> Dict[str, Any]:
        return self._store.get_job(job_uuid)


class WorkflowRuntimeWorker:
    """Consume durable commands and, when configured, execute frozen DAG Jobs."""

    def __init__(
        self,
        coordinator: WorkflowRuntimeCoordinator,
        *,
        dispatcher: WorkflowJobDispatcher | None = None,
        device_identity_resolver: Callable[[str], str | None] | None = None,
        poll_interval_seconds: float = 0.25,
    ):
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._coordinator = coordinator
        if (dispatcher is None) != (device_identity_resolver is None):
            raise ValueError(
                "dispatcher and device_identity_resolver must be configured together"
            )
        self._dispatcher = dispatcher
        self._device_identity_resolver = device_identity_resolver
        self._poll_interval_seconds = poll_interval_seconds
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._listener_registered = False
        if dispatcher is not None:
            dispatcher.add_job_finished_listener(self._on_job_finished)
            self._listener_registered = True

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._dispatcher is not None and not self._listener_registered:
                self._dispatcher.add_job_finished_listener(self._on_job_finished)
                self._listener_registered = True
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="workflow-runtime-worker",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        dispatcher = self._dispatcher
        with self._lock:
            remove_listener = dispatcher is not None and self._listener_registered
            self._listener_registered = False
        if remove_listener:
            assert dispatcher is not None
            dispatcher.remove_job_finished_listener(self._on_job_finished)

    def join(self, timeout: Optional[float] = None) -> None:
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def _run(self) -> None:
        while True:
            try:
                task_uuids = self._coordinator._pending_command_task_uuids()
                for task_uuid in task_uuids:
                    self._coordinator.consume_next_command(task_uuid)
                if self._dispatcher is not None:
                    self._sweep_execution_tasks()
            except (sqlite3.Error, StoreConflict, StoreNotFound):
                _LOGGER.exception("Workflow runtime sweep failed")
            if self._stop_event.is_set():
                return
            self._wake_event.wait(self._poll_interval_seconds)
            self._wake_event.clear()

    def _sweep_execution_tasks(self) -> None:
        dispatcher = self._dispatcher
        assert dispatcher is not None
        if not dispatcher.execution_ready():
            return
        for task in self._coordinator._execution_tasks():
            if task["control_status"] != "active":
                continue
            if task["status"] == "pending":
                task = self._coordinator.start_task(task["uuid"])
                _LOGGER.info(
                    "Workflow Task running task_uuid=%s workflow_uuid=%s",
                    task["uuid"],
                    task["workflow_uuid"],
                )
            self._advance_task(task)

    def _advance_task(self, task: Dict[str, Any]) -> None:
        task_uuid = task["uuid"]
        jobs = self._coordinator._execution_jobs(task_uuid)
        if any(job["status"] == "failed" for job in jobs):
            self._coordinator.transition_task(
                task_uuid,
                "failed",
                error_info=[{"code": "job_failed"}],
            )
            _LOGGER.error("Workflow Task failed task_uuid=%s", task_uuid)
            return
        if jobs and all(job["status"] in {"succeeded", "skipped"} for job in jobs):
            self._coordinator.transition_task(task_uuid, "succeeded")
            _LOGGER.info("Workflow Task succeeded task_uuid=%s", task_uuid)
            return
        if any(
            job["status"]
            in {
                "dispatched",
                "running",
                "intervention_required",
                "cancel_requested",
                "execution_unknown",
            }
            for job in jobs
        ):
            return

        plan = task.get("execution_plan")
        snapshot = task.get("workflow_snapshot")
        if not isinstance(plan, dict) or not isinstance(snapshot, dict):
            self._fail_task(task_uuid, None, "invalid_execution_snapshot")
            return
        nodes = {
            node.get("uuid"): node
            for node in snapshot.get("nodes", [])
            if isinstance(node, dict) and isinstance(node.get("uuid"), str)
        }
        plan_nodes = {
            node.get("uuid"): node
            for node in plan.get("nodes", [])
            if isinstance(node, dict) and isinstance(node.get("uuid"), str)
        }
        templates = {
            template.get("uuid"): template
            for template in snapshot.get("node_templates", [])
            if isinstance(template, dict) and isinstance(template.get("uuid"), str)
        }
        jobs_by_node = {job["workflow_node_uuid"]: job for job in jobs}
        incoming: dict[str, list[dict[str, Any]]] = {}
        for edge in plan.get("edges", []):
            if not isinstance(edge, dict):
                self._fail_task(task_uuid, None, "invalid_execution_plan")
                return
            incoming.setdefault(str(edge.get("target_node_uuid") or ""), []).append(
                edge
            )

        pending = sorted(
            (job for job in jobs if job["status"] == "pending"),
            key=lambda item: (item["topological_index"], item["uuid"]),
        )
        for job in pending:
            predecessors = [
                jobs_by_node.get(edge.get("source_node_uuid"))
                for edge in incoming.get(job["workflow_node_uuid"], [])
            ]
            if any(
                item is None or item["status"] != "succeeded" for item in predecessors
            ):
                continue
            node = nodes.get(job["workflow_node_uuid"])
            planned_node = plan_nodes.get(job["workflow_node_uuid"])
            if node is None or planned_node is None:
                self._fail_task(task_uuid, job["uuid"], "invalid_execution_snapshot")
                return
            try:
                payload = self._dispatch_payload(
                    task,
                    job,
                    node,
                    planned_node,
                    templates.get(node.get("workflow_node_template_uuid")),
                    incoming.get(job["workflow_node_uuid"], []),
                    jobs_by_node,
                )
                self._coordinator.transition_job(job["uuid"], "dispatched")
                self._coordinator.transition_job(job["uuid"], "running")
                _LOGGER.info(
                    "Workflow Job dispatch task_uuid=%s job_uuid=%s "
                    "device_id=%s action_name=%s",
                    task_uuid,
                    job["uuid"],
                    payload["device_id"],
                    payload["action_name"],
                )
            except (KeyError, TypeError, ValueError, StoreConflict) as error:
                self._fail_task(
                    task_uuid,
                    job["uuid"],
                    "dispatch_invalid",
                    detail=str(error),
                )
                return
            assert self._dispatcher is not None
            try:
                self._dispatcher.dispatch(payload)
            except Exception as error:  # noqa: BLE001
                # 传输层可能已接受动作、却在确认阶段失败；保留 durable
                # uncertainty fence，不能凭空生成物理失败事实。
                _LOGGER.exception(
                    "Workflow Job dispatch outcome unknown task_uuid=%s job_uuid=%s",
                    task_uuid,
                    job["uuid"],
                )
                uncertainty_reason = str(error).strip() or "dispatch_outcome_unknown"
                self._coordinator.mark_job_unknown(
                    job["uuid"],
                    uncertainty_reason,
                )
            return

    def _dispatch_payload(
        self,
        task: Dict[str, Any],
        job: Dict[str, Any],
        node: Dict[str, Any],
        planned_node: Dict[str, Any],
        node_template: Dict[str, Any] | None,
        incoming_edges: list[dict[str, Any]],
        jobs_by_node: dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        if job["executor_kind"] != "device_action":
            raise ValueError(f"unsupported executor kind {job['executor_kind']!r}")
        device_identity = planned_node.get("material_uuid") or node.get("material_uuid")
        if not isinstance(device_identity, str) or not device_identity:
            metadata = node.get("meta_data")
            unilab = metadata.get("unilab") if isinstance(metadata, dict) else None
            binding = (
                unilab.get("executor_binding") if isinstance(unilab, dict) else None
            )
            if isinstance(binding, dict) and binding.get("mode") == "fixed":
                device_identity = binding.get("device_id")
        if not isinstance(device_identity, str) or not device_identity:
            raise ValueError("device action is missing a frozen executor identity")
        assert self._device_identity_resolver is not None
        device_id = self._device_identity_resolver(device_identity)
        if not isinstance(device_id, str) or not device_id:
            raise ValueError(f"executor {device_identity!r} is not a graph device")
        action_name = node.get("action_name")
        action_type = node.get("action_type") or (
            node_template.get("type") if isinstance(node_template, dict) else None
        )
        if not isinstance(action_name, str) or not action_name:
            raise ValueError("device action is missing action_name")
        if not isinstance(action_type, str) or not action_type:
            raise ValueError("device action is missing action_type")
        action_args = dict(job.get("param") or {})
        for edge in incoming_edges:
            if edge.get("dependency_only") is True:
                continue
            source_job = jobs_by_node.get(edge.get("source_node_uuid"))
            if source_job is None:
                raise ValueError("execution edge source job is missing")
            source_key = edge.get("source_data_key")
            target_key = edge.get("target_data_key")
            if not isinstance(source_key, str) or not source_key:
                raise ValueError("execution edge source key is missing")
            if not isinstance(target_key, str) or not target_key:
                raise ValueError("execution edge target key is missing")
            action_args[target_key.split("@@@")[-1]] = self._read_result_value(
                source_job.get("return_info"), source_key
            )
        return {
            "job_uuid": job["uuid"],
            "task_uuid": task["uuid"],
            "node_uuid": job["workflow_node_uuid"],
            "workflow_uuid": task["workflow_uuid"],
            "device_id": device_id,
            "action_name": action_name,
            "action_type": action_type,
            "param": action_args,
            "sample_material": {},
        }

    @staticmethod
    def _read_result_value(return_info: Any, data_key: str) -> Any:
        value = return_info
        for part in data_key.split("@@@"):
            if not isinstance(value, dict) or part not in value:
                raise ValueError(f"result does not contain {data_key!r}")
            value = value[part]
        return value

    def _on_job_finished(
        self,
        job_uuid: str,
        success: bool,
        return_value: Any,
        _success_type: str = "normal",
    ) -> None:
        if self._stop_event.is_set() or not self._listener_registered:
            return
        try:
            job = self._coordinator._execution_job(job_uuid)
            if job["status"] in _JOB_TERMINAL:
                return
            task_uuid = job["workflow_task_uuid"]
            if success:
                result = (
                    return_value
                    if isinstance(return_value, dict)
                    else {"value": return_value}
                )
                self._coordinator.transition_job(
                    job_uuid,
                    "succeeded",
                    return_info=result,
                )
                _LOGGER.info(
                    "Workflow Job succeeded task_uuid=%s job_uuid=%s",
                    task_uuid,
                    job_uuid,
                )
            else:
                self._coordinator.transition_job(
                    job_uuid,
                    "failed",
                    error_info=[{"code": "device_action_failed"}],
                )
                _LOGGER.error(
                    "Workflow Job failed task_uuid=%s job_uuid=%s",
                    task_uuid,
                    job_uuid,
                )
        except (sqlite3.Error, StoreConflict, StoreNotFound):
            _LOGGER.exception("Workflow Job completion could not be committed")

    def _fail_task(
        self,
        task_uuid: str,
        job_uuid: str | None,
        code: str,
        *,
        detail: str = "",
    ) -> None:
        error = {"code": code}
        if detail:
            error["detail"] = detail
        if job_uuid is not None:
            job = self._coordinator._execution_job(job_uuid)
            if job["status"] in {"pending", "dispatched", "running"}:
                self._coordinator.transition_job(
                    job_uuid,
                    "failed",
                    error_info=[error],
                )
                _LOGGER.error(
                    "Workflow Job failed task_uuid=%s job_uuid=%s code=%s",
                    task_uuid,
                    job_uuid,
                    code,
                )
        task = next(
            (
                item
                for item in self._coordinator._execution_tasks()
                if item["uuid"] == task_uuid
            ),
            None,
        )
        if task is not None and task["status"] == "running":
            self._coordinator.transition_task(
                task_uuid,
                "failed",
                error_info=[error],
            )
        _LOGGER.error("Workflow Task failed task_uuid=%s code=%s", task_uuid, code)


__all__ = [
    "JOB_TRANSITIONS",
    "TASK_TRANSITIONS",
    "WorkflowRuntimeCoordinator",
    "WorkflowRuntimeWorker",
    "WorkflowJobDispatcher",
]
