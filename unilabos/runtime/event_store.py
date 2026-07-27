"""SQLite event journal for local Quick Debug runs.

Terminal state, result, effects, cursor, and outbox are committed in one SQLite
transaction.  The journal is the local run truth; it never retries a physical
action whose previous process only proved that it had started.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Optional


def _journal_locked(method: Callable[..., Any]) -> Callable[..., Any]:
    """Serialize one SQLite connection across API and executor threads."""

    @wraps(method)
    def guarded(self: "SQLiteEventJournal", *args: Any, **kwargs: Any) -> Any:
        with self._guard:
            return method(self, *args, **kwargs)

    return guarded


class JournalCommitError(RuntimeError):
    """Atomic journal transaction failed and was rolled back."""


class CursorProjection(dict):
    """JSON cursor with attribute access for common projection fields."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


@dataclass(frozen=True)
class NodeProjection:
    run_id: str
    node_id: str
    terminal: Optional[str]
    state: str
    attempt: int
    result: dict[str, Any]
    effects: list[dict[str, Any]]
    runtime_epoch: str


@dataclass(frozen=True)
class OutboxItem:
    topic: str
    event: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class RunEvent:
    sequence: int
    run_id: str
    node_id: Optional[str]
    type: str
    timestamp: float
    payload: dict[str, Any]


@dataclass(frozen=True)
class RunSubmission:
    run_id: str
    source: dict[str, Any]
    profile_ref: str
    compiled_dag: dict[str, Any]
    status: str
    runtime_epoch: str


@dataclass(frozen=True)
class RecoveredNode:
    state: str
    attempt: int


@dataclass(frozen=True)
class RecoveryProjection:
    nodes: dict[str, RecoveredNode]


@dataclass(frozen=True)
class PersistedOpenLease:
    """One lease reconstructed by replaying its persisted lifecycle."""

    run_id: str
    node_id: str
    lease_id: str
    holder_id: str
    claims: tuple[dict[str, Any], ...]


class SQLiteEventJournal:
    """Transactional local event store scoped to one runtime epoch."""

    def __init__(
        self,
        path: str | Path,
        *,
        runtime_epoch: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.runtime_epoch = runtime_epoch
        self._clock = clock
        self.failpoint: Optional[str] = None
        self._guard = threading.RLock()
        # FastAPI's TestClient and desktop adapters may enter the same service
        # from a worker thread. SQLite itself remains the cross-process writer
        # coordinator (WAL + BEGIN IMMEDIATE).
        self._db = sqlite3.connect(
            self.path,
            isolation_level=None,
            check_same_thread=False,
        )
        self._db.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        self._db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS node_projection (
                run_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                terminal TEXT,
                state TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                result_json TEXT NOT NULL,
                effects_json TEXT NOT NULL,
                runtime_epoch TEXT NOT NULL,
                PRIMARY KEY (run_id, node_id)
            );
            CREATE TABLE IF NOT EXISTS run_cursor (
                run_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                topic TEXT NOT NULL,
                event TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_event (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                node_id TEXT,
                type TEXT NOT NULL,
                timestamp REAL NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_submission (
                run_id TEXT PRIMARY KEY,
                source_json TEXT NOT NULL,
                profile_ref TEXT NOT NULL,
                compiled_dag_json TEXT NOT NULL,
                status TEXT NOT NULL,
                runtime_epoch TEXT NOT NULL
            );
            """
        )

    @_journal_locked
    def _append_event(
        self,
        *,
        run_id: str,
        event_type: str,
        node_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        self._db.execute(
            "INSERT INTO run_event(run_id,node_id,type,timestamp,payload_json) VALUES(?,?,?,?,?)",
            (
                run_id,
                node_id,
                event_type,
                self._clock(),
                json.dumps(payload or {}, sort_keys=True),
            ),
        )

    @_journal_locked
    def append_runtime_event(
        self,
        *,
        run_id: str,
        event_type: str,
        node_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        """Append a transport/debug event without mutating terminal truth."""

        self._append_event(
            run_id=run_id,
            event_type=event_type,
            node_id=node_id,
            payload=payload,
        )

    @_journal_locked
    def commit_node_terminal(
        self,
        *,
        run_id: str,
        node_id: str,
        terminal: str,
        result: dict[str, Any],
        effects: list[dict[str, Any]],
        cursor: dict[str, Any],
        outbox: list[dict[str, Any]],
    ) -> None:
        try:
            self._db.execute("BEGIN IMMEDIATE")
            previous = self.load_node_projection(run_id, node_id)
            attempt = previous.attempt if previous is not None else 1
            self._db.execute(
                """
                INSERT INTO node_projection(
                    run_id,node_id,terminal,state,attempt,result_json,effects_json,runtime_epoch
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id,node_id) DO UPDATE SET
                    terminal=excluded.terminal,state=excluded.state,attempt=excluded.attempt,
                    result_json=excluded.result_json,effects_json=excluded.effects_json,
                    runtime_epoch=excluded.runtime_epoch
                """,
                (
                    run_id,
                    node_id,
                    terminal,
                    terminal,
                    attempt,
                    json.dumps(result, sort_keys=True),
                    "[]",
                    self.runtime_epoch,
                ),
            )
            if self.failpoint == "after_result_before_effect":
                raise JournalCommitError("injected failure after result")
            self._db.execute(
                "UPDATE node_projection SET effects_json=? WHERE run_id=? AND node_id=?",
                (json.dumps(effects, sort_keys=True), run_id, node_id),
            )
            self._db.execute(
                """
                INSERT INTO run_cursor(run_id,payload_json) VALUES(?,?)
                ON CONFLICT(run_id) DO UPDATE SET payload_json=excluded.payload_json
                """,
                (run_id, json.dumps(cursor, sort_keys=True)),
            )
            for item in outbox:
                self._db.execute(
                    "INSERT INTO outbox(run_id,topic,event,payload_json) VALUES(?,?,?,?)",
                    (
                        run_id,
                        item["topic"],
                        item["event"],
                        json.dumps(item.get("payload", {}), sort_keys=True),
                    ),
                )
            self._append_event(
                run_id=run_id,
                node_id=node_id,
                event_type=f"node_{terminal}",
                payload={"result": result, "effects": effects},
            )
            self._db.execute("COMMIT")
        except Exception as exc:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            if isinstance(exc, JournalCommitError):
                raise
            raise JournalCommitError(str(exc)) from exc

    @_journal_locked
    def record_node_started(self, *, run_id: str, node_id: str, attempt: int) -> None:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute(
                """
                INSERT INTO node_projection(
                    run_id,node_id,terminal,state,attempt,result_json,effects_json,runtime_epoch
                ) VALUES(?,?,NULL,'running',?,'{}','[]',?)
                ON CONFLICT(run_id,node_id) DO UPDATE SET
                    terminal=NULL,state='running',attempt=excluded.attempt,
                    runtime_epoch=excluded.runtime_epoch
                """,
                (run_id, node_id, attempt, self.runtime_epoch),
            )
            self._append_event(
                run_id=run_id,
                node_id=node_id,
                event_type="node_started",
                payload={"attempt": attempt},
            )
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    @_journal_locked
    def record_run_terminal(self, *, run_id: str, terminal: str) -> None:
        if terminal not in {"completed", "failed", "cancelled"}:
            raise ValueError(f"unsupported run terminal: {terminal}")
        existing = self._db.execute(
            """
            SELECT type FROM run_event
            WHERE run_id=? AND type IN ('run_completed','run_failed','run_cancelled')
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if existing is not None:
            existing_status = str(existing["type"]).removeprefix("run_")
            self._db.execute(
                "UPDATE run_submission SET status=?,runtime_epoch=? WHERE run_id=?",
                (existing_status, self.runtime_epoch, run_id),
            )
            return
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute(
                "UPDATE run_submission SET status=?,runtime_epoch=? WHERE run_id=?",
                (terminal, self.runtime_epoch, run_id),
            )
            self._append_event(run_id=run_id, event_type=f"run_{terminal}")
            self._db.execute("COMMIT")
        except Exception as exc:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            if isinstance(exc, JournalCommitError):
                raise
            raise JournalCommitError(str(exc)) from exc

    @_journal_locked
    def record_run_submission(
        self,
        *,
        run_id: str,
        source: dict[str, Any],
        profile_ref: str,
        compiled_dag: dict[str, Any],
        status: str = "pending",
    ) -> None:
        """Atomically persist the accepted source, compiled IR, and first event."""

        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute(
                """
                INSERT INTO run_submission(
                    run_id,source_json,profile_ref,compiled_dag_json,status,runtime_epoch
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    run_id,
                    json.dumps(source, sort_keys=True),
                    profile_ref,
                    json.dumps(compiled_dag, sort_keys=True),
                    status,
                    self.runtime_epoch,
                ),
            )
            self._append_event(
                run_id=run_id,
                event_type="run_submitted",
                payload={
                    "profile_ref": profile_ref,
                    "workflow_revision_hash": compiled_dag.get(
                        "workflow_revision_hash", ""
                    ),
                },
            )
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    @_journal_locked
    def load_run_submission(self, run_id: str) -> RunSubmission | None:
        row = self._db.execute(
            "SELECT * FROM run_submission WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return RunSubmission(
            run_id=str(row["run_id"]),
            source=json.loads(row["source_json"]),
            profile_ref=str(row["profile_ref"]),
            compiled_dag=json.loads(row["compiled_dag_json"]),
            status=str(row["status"]),
            runtime_epoch=str(row["runtime_epoch"]),
        )

    @_journal_locked
    def update_run_status(self, *, run_id: str, status: str) -> bool:
        row = self._db.execute(
            "SELECT status FROM run_submission WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            return False
        previous = str(row["status"])
        if previous == status:
            return True
        if previous in {"completed", "failed", "cancelled"}:
            return False
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute(
                "UPDATE run_submission SET status=?,runtime_epoch=? WHERE run_id=?",
                (status, self.runtime_epoch, run_id),
            )
            self._append_event(
                run_id=run_id,
                event_type="run_status_changed",
                payload={"previous": previous, "status": status},
            )
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise
        return True

    @_journal_locked
    def load_open_lease(
        self, *, run_id: str, lease_id: str
    ) -> PersistedOpenLease | None:
        return next(
            (
                lease
                for lease in self._replay_open_leases(run_id=run_id)
                if lease.lease_id == lease_id
            ),
            None,
        )

    @_journal_locked
    def load_reconcile_resolution(
        self, *, run_id: str, lease_id: str
    ) -> RunEvent | None:
        """Return the durable resolution for a fence, if one already exists."""

        rows = self._db.execute(
            """
            SELECT * FROM run_event
            WHERE run_id=? AND type='reconcile_resolved'
            ORDER BY sequence
            """,
            (run_id,),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if str(payload.get("lease_id") or "") != lease_id:
                continue
            return RunEvent(
                sequence=row["sequence"],
                run_id=row["run_id"],
                node_id=row["node_id"],
                type=row["type"],
                timestamp=row["timestamp"],
                payload=payload,
            )
        return None

    @_journal_locked
    def record_reconcile_resolved(
        self,
        *,
        run_id: str,
        node_id: str,
        lease_id: str,
        resolution: str,
        actor: str,
        reason: str,
    ) -> None:
        """Persist the explicit physical resolution and close the fence."""

        if self.load_reconcile_resolution(run_id=run_id, lease_id=lease_id):
            return

        projection = self.load_node_projection(run_id, node_id)
        restored_state = (
            str(projection.terminal or "") if projection is not None else ""
        )
        has_durable_terminal = restored_state in {
            "succeeded",
            "failed",
            "cancelled",
            "skipped",
        }

        self._db.execute("BEGIN IMMEDIATE")
        try:
            if has_durable_terminal:
                self._db.execute(
                    """
                    UPDATE node_projection
                    SET state=?,runtime_epoch=?
                    WHERE run_id=? AND node_id=?
                    """,
                    (restored_state, self.runtime_epoch, run_id, node_id),
                )
            self._append_event(
                run_id=run_id,
                node_id=node_id,
                event_type="lock_released",
                payload={
                    "lease_id": lease_id,
                    "released_scope": None,
                    "runtime_epoch": self.runtime_epoch,
                },
            )
            self._append_event(
                run_id=run_id,
                node_id=node_id,
                event_type="reconcile_resolved",
                payload={
                    "actor": actor,
                    "lease_id": lease_id,
                    "reason": reason,
                    "resolution": resolution,
                },
            )
            self._db.execute("COMMIT")
        except Exception as exc:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            if isinstance(exc, JournalCommitError):
                raise
            raise JournalCommitError(str(exc)) from exc

    @_journal_locked
    def record_lock_requested(
        self,
        *,
        run_id: str,
        node_id: str,
        holder_id: str,
        claims: list[dict[str, Any]],
    ) -> None:
        self._append_event(
            run_id=run_id,
            node_id=node_id,
            event_type="lock_requested",
            payload={"holder_id": holder_id, "claims": claims},
        )

    @_journal_locked
    def record_lock_acquired(
        self,
        *,
        run_id: str,
        node_id: str,
        lease_id: str,
        holder_id: str,
        claims: list[dict[str, Any]],
    ) -> None:
        self._append_event(
            run_id=run_id,
            node_id=node_id,
            event_type="lock_acquired",
            payload={
                "lease_id": lease_id,
                "holder_id": holder_id,
                "claims": claims,
                "runtime_epoch": self.runtime_epoch,
            },
        )

    @_journal_locked
    def record_lock_unknown(
        self,
        *,
        run_id: str,
        node_id: str,
        lease_id: str,
        holder_id: str,
        claims: list[dict[str, Any]],
        reason: str,
    ) -> None:
        self._append_event(
            run_id=run_id,
            node_id=node_id,
            event_type="lock_unknown",
            payload={
                "lease_id": lease_id,
                "holder_id": holder_id,
                "claims": claims,
                "reason": reason,
                "runtime_epoch": self.runtime_epoch,
            },
        )

    @_journal_locked
    def record_lock_released(
        self,
        *,
        run_id: str,
        node_id: str,
        lease_id: str,
        released_scope: str | None,
        released_resource_id: str | None = None,
    ) -> None:
        self._append_event(
            run_id=run_id,
            node_id=node_id,
            event_type="lock_released",
            payload={
                "lease_id": lease_id,
                "released_scope": released_scope,
                "released_resource_id": released_resource_id,
                "runtime_epoch": self.runtime_epoch,
            },
        )

    @_journal_locked
    def load_node_projection(
        self, run_id: str, node_id: str
    ) -> Optional[NodeProjection]:
        row = self._db.execute(
            "SELECT * FROM node_projection WHERE run_id=? AND node_id=?",
            (run_id, node_id),
        ).fetchone()
        if row is None:
            return None
        return NodeProjection(
            run_id=row["run_id"],
            node_id=row["node_id"],
            terminal=row["terminal"],
            state=row["state"],
            attempt=row["attempt"],
            result=json.loads(row["result_json"]),
            effects=json.loads(row["effects_json"]),
            runtime_epoch=row["runtime_epoch"],
        )

    @_journal_locked
    def load_cursor(self, run_id: str) -> Optional[CursorProjection]:
        row = self._db.execute(
            "SELECT payload_json FROM run_cursor WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return CursorProjection(json.loads(row["payload_json"]))

    @_journal_locked
    def list_outbox(self, run_id: str) -> list[OutboxItem]:
        rows = self._db.execute(
            "SELECT topic,event,payload_json FROM outbox WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
        return [
            OutboxItem(
                topic=row["topic"],
                event=row["event"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    @_journal_locked
    def list_events(self, run_id: str, *, after_sequence: int = 0) -> list[RunEvent]:
        rows = self._db.execute(
            """
            SELECT * FROM run_event
            WHERE run_id=? AND sequence>?
            ORDER BY sequence
            """,
            (run_id, after_sequence),
        ).fetchall()
        return [
            RunEvent(
                sequence=row["sequence"],
                run_id=row["run_id"],
                node_id=row["node_id"],
                type=row["type"],
                timestamp=row["timestamp"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    @_journal_locked
    def list_incomplete_run_ids(self) -> list[str]:
        rows = self._db.execute(
            """
            SELECT DISTINCT run_id FROM node_projection
            WHERE state IN ('running','reconciling')
            ORDER BY run_id
            """
        ).fetchall()
        run_ids = {str(row["run_id"]) for row in rows}
        run_ids.update(lease.run_id for lease in self._replay_open_leases())
        return sorted(run_ids)

    @_journal_locked
    def has_open_unknown_fence(self, run_id: str) -> bool:
        """Return whether an explicitly unknown lease is still unreleased."""

        unknown_lease_ids: set[str] = set()
        for event in self.list_events(run_id):
            lease_id = str(event.payload.get("lease_id") or "")
            if not lease_id:
                continue
            if event.type == "lock_unknown":
                unknown_lease_ids.add(lease_id)
            elif event.type == "lock_released" and (
                event.payload.get("released_scope") is None
                and event.payload.get("released_resource_id") is None
            ):
                unknown_lease_ids.discard(lease_id)
        if not unknown_lease_ids:
            return False
        return any(
            lease.lease_id in unknown_lease_ids
            for lease in self._replay_open_leases(run_id=run_id)
        )

    @_journal_locked
    def reconcile_restart(
        self,
        run_id: str,
        *,
        dispatch: Callable[[str], Any],
        dag: Any | None = None,
        lock_manager: Any | None = None,
    ) -> RecoveryProjection:
        del dispatch  # Explicitly unused: ambiguous physical work is never re-dispatched.
        rows = self._db.execute(
            """
            SELECT node_id,attempt,state FROM node_projection
            WHERE run_id=?
            """,
            (run_id,),
        ).fetchall()
        open_leases = self._replay_open_leases(run_id=run_id)
        unresolved_node_ids = {lease.node_id for lease in open_leases}
        rows = [
            row
            for row in rows
            if row["state"] in {"running", "reconciling"}
            or row["node_id"] in unresolved_node_ids
        ]
        recovered: dict[str, RecoveredNode] = {}
        self._db.execute("BEGIN IMMEDIATE")
        try:
            for row in rows:
                node_id = row["node_id"]
                self._db.execute(
                    "UPDATE node_projection SET state='reconciling',runtime_epoch=? WHERE run_id=? AND node_id=?",
                    (self.runtime_epoch, run_id, node_id),
                )
                self._append_event(
                    run_id=run_id,
                    node_id=node_id,
                    event_type="reconcile_started",
                    payload={"previous_attempt": row["attempt"]},
                )
                recovered[node_id] = RecoveredNode(
                    state="reconciling", attempt=row["attempt"]
                )
            restored_node_ids: set[str] = set()
            if lock_manager is not None:
                for persisted in open_leases:
                    restored = self._install_persisted_unknown_lease(
                        persisted=persisted,
                        lock_manager=lock_manager,
                    )
                    restored_node_ids.add(persisted.node_id)
                    self._append_restored_unknown_event(
                        run_id=run_id,
                        node_id=persisted.node_id,
                        restored=restored,
                    )
                # Defensive fallback for an old journal that recorded a running
                # node before lock lifecycle events were introduced.
                for node_id in recovered:
                    if node_id in restored_node_ids:
                        continue
                    restored = self._restore_unknown_lease_from_dag(
                        run_id=run_id,
                        node_id=node_id,
                        dag=dag,
                        lock_manager=lock_manager,
                    )
                    if restored is not None:
                        self._append_restored_unknown_event(
                            run_id=run_id,
                            node_id=node_id,
                            restored=restored,
                        )
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise
        return RecoveryProjection(nodes=recovered)

    def _replay_open_leases(
        self,
        *,
        run_id: str | None = None,
    ) -> list[PersistedOpenLease]:
        query = """
            SELECT run_id,node_id,type,payload_json FROM run_event
            WHERE type IN ('lock_acquired','lock_unknown','lock_released')
        """
        params: tuple[Any, ...] = ()
        if run_id is not None:
            query += " AND run_id=?"
            params = (run_id,)
        query += " ORDER BY sequence"
        rows = self._db.execute(query, params).fetchall()
        states: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            payload = json.loads(row["payload_json"])
            lease_id = str(payload.get("lease_id") or "")
            if not lease_id:
                continue
            key = (str(row["run_id"]), lease_id)
            if row["type"] in {"lock_acquired", "lock_unknown"}:
                state = states.setdefault(
                    key,
                    {
                        "node_id": str(row["node_id"] or ""),
                        "holder_id": str(payload.get("holder_id") or row["node_id"] or ""),
                        "claims": [],
                    },
                )
                raw_claims = payload.get("claims")
                if isinstance(raw_claims, list) and raw_claims:
                    state["claims"] = [dict(claim) for claim in raw_claims]
                continue
            state = states.get(key)
            if state is None:
                continue
            released_scope = payload.get("released_scope")
            released_resource_id = payload.get("released_resource_id")
            if released_scope is None and released_resource_id is None:
                state["claims"] = []
            else:
                state["claims"] = [
                    claim
                    for claim in state["claims"]
                    if not (
                        (
                            released_scope is None
                            or str(claim.get("scope", "action"))
                            == str(released_scope)
                        )
                        and (
                            released_resource_id is None
                            or str(
                                claim.get("resource_id")
                                or claim.get("resource_uuid")
                                or claim.get("resource_ref")
                                or ""
                            )
                            == str(released_resource_id)
                        )
                    )
                ]
        return [
            PersistedOpenLease(
                run_id=persisted_run_id,
                node_id=str(state["node_id"]),
                lease_id=lease_id,
                holder_id=str(state["holder_id"]),
                claims=tuple(state["claims"]),
            )
            for (persisted_run_id, lease_id), state in states.items()
            if state["claims"]
        ]

    def _install_persisted_unknown_lease(
        self,
        *,
        persisted: PersistedOpenLease,
        lock_manager: Any,
    ) -> Any:
        from unilabos.scheduler.resource_lock import ResolvedResourceClaim

        claims = tuple(
            ResolvedResourceClaim(
                resource_id=str(
                    raw.get("resource_id")
                    or raw.get("resource_uuid")
                    or raw.get("resource_ref")
                    or "resource:unresolved"
                ),
                resource_kind=str(raw.get("resource_kind") or "device"),
                quantity=int(raw.get("quantity", 1) or 1),
                mode=str(raw.get("mode", "exclusive") or "exclusive"),
                scope=str(raw.get("scope", "action") or "action"),
            )
            for raw in persisted.claims
        )
        holder_id = self._run_scoped_holder(
            persisted.run_id,
            persisted.holder_id or persisted.node_id,
        )
        return lock_manager.install_unknown(
            holder_id=holder_id,
            claims=claims,
            reason="runtime restarted before terminal certainty",
            lease_id=persisted.lease_id,
        )

    def _restore_unknown_lease_from_dag(
        self,
        *,
        run_id: str,
        node_id: str,
        dag: Any | None,
        lock_manager: Any,
    ) -> Any | None:
        from unilabos.scheduler.python_fallback import (
            python_fallback_lease_request,
        )

        if dag is None or node_id not in dag.nodes:
            return None

        request = python_fallback_lease_request(
            dag.nodes[node_id],
            run_id=run_id,
        )
        reason = "runtime restarted before terminal certainty"
        return lock_manager.install_unknown(
            holder_id=self._run_scoped_holder(run_id, node_id),
            claims=request.claims,
            reason=reason,
        )

    def _append_restored_unknown_event(
        self,
        *,
        run_id: str,
        node_id: str,
        restored: Any,
    ) -> None:
        self._append_event(
            run_id=run_id,
            node_id=node_id,
            event_type="lock_unknown",
            payload={
                "lease_id": restored.lease_id,
                "holder_id": restored.holder_id,
                "claims": self._serialize_claims(restored.claims),
                "reason": restored.reason,
                "runtime_epoch": self.runtime_epoch,
            },
        )

    @staticmethod
    def _run_scoped_holder(run_id: str, holder_id: str) -> str:
        prefix = f"{run_id}:"
        return holder_id if holder_id.startswith(prefix) else f"{prefix}{holder_id}"

    @staticmethod
    def _serialize_claims(claims: Any) -> list[dict[str, Any]]:
        return [
            {
                "resource_id": claim.resource_id,
                "resource_kind": claim.resource_kind,
                "quantity": claim.quantity,
                "mode": claim.mode,
                "scope": claim.scope,
            }
            for claim in claims
        ]

    @_journal_locked
    def record_callback(
        self,
        *,
        run_id: str,
        node_id: str,
        runtime_epoch: str,
        terminal: str,
        result: dict[str, Any],
    ) -> bool:
        if runtime_epoch != self.runtime_epoch:
            return False
        cursor = self.load_cursor(run_id) or CursorProjection(completed=[])
        completed = list(cursor.get("completed", []))
        if terminal == "succeeded" and node_id not in completed:
            completed.append(node_id)
        self.commit_node_terminal(
            run_id=run_id,
            node_id=node_id,
            terminal=terminal,
            result=result,
            effects=[],
            cursor={"completed": completed},
            outbox=[],
        )
        return True

    @_journal_locked
    def close(self) -> None:
        self._db.close()
