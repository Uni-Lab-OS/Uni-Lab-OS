"""Phase 01 Round14：WorkflowStore 初始化 timeout 是调用级 deadline。"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import UUID

import unilabos.workflow.store as workflow_store
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
RECOVERY_SOURCE = "normalized_legacy_source()\n"
EXPECTED_HASH = f"sha256:{'e' * 64}"
INITIALIZATION_TIMEOUT_SECONDS = 0.35
CALL_DEADLINE_FACTOR = 1.5


def _create_delete_mode_legacy_database(database_path: Path) -> None:
    connection = sqlite3.connect(database_path)
    try:
        journal_mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
        assert journal_mode == "delete"
        connection.executescript(
            """
            CREATE TABLE workflow_authoring (
                workflow_uuid TEXT PRIMARY KEY,
                observed_draft_hash TEXT,
                draft_update_time TEXT,
                diagnostics TEXT NOT NULL,
                candidate_hash TEXT,
                candidate TEXT,
                applied_source TEXT,
                writeback_status TEXT NOT NULL DEFAULT 'settled',
                writeback_source TEXT,
                writeback_expected_hash TEXT,
                update_time TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO workflow_authoring(
                workflow_uuid, diagnostics, writeback_status,
                writeback_source, writeback_expected_hash, update_time
            ) VALUES (?, '[]', 'pending', ?, ?, '2026-07-31T01:02:03Z')
            """,
            (WORKFLOW_UUID, RECOVERY_SOURCE, EXPECTED_HASH),
        )
        connection.commit()
    finally:
        connection.close()


def test_concurrent_initialization_timeout_is_measured_from_each_call_entry(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        workflow_store,
        "_STORE_INITIALIZATION_BUSY_TIMEOUT_SECONDS",
        INITIALIZATION_TIMEOUT_SECONDS,
    )
    monkeypatch.setattr(
        workflow_store,
        "_STORE_INITIALIZATION_SQLITE_BUSY_TIMEOUT_MS",
        20,
    )
    monkeypatch.setattr(
        workflow_store,
        "_STORE_INITIALIZATION_RETRY_INTERVAL_SECONDS",
        0.005,
    )

    database_path = tmp_path / "legacy-init-deadline.db"
    _create_delete_mode_legacy_database(database_path)
    holder = sqlite3.connect(database_path)
    holder.execute("BEGIN IMMEDIATE")

    start_barrier = threading.Barrier(3)
    outcomes: dict[int, dict[str, Any]] = {}

    def initialize(worker_index: int) -> None:
        store: WorkflowStore | None = None
        try:
            start_barrier.wait(timeout=2)
            call_started = monotonic()
            try:
                store = WorkflowStore(database_path)
            except Exception as error:  # noqa: BLE001 - 冻结 timeout 结果
                outcomes[worker_index] = {
                    "status": "error",
                    "error": type(error).__name__,
                    "message": str(error),
                    "duration": monotonic() - call_started,
                }
            else:
                outcomes[worker_index] = {
                    "status": "opened",
                    "duration": monotonic() - call_started,
                }
        except BaseException as error:  # noqa: BLE001 - 暴露 worker 泄漏
            outcomes[worker_index] = {
                "status": "worker_error",
                "error": type(error).__name__,
                "message": str(error),
            }
        finally:
            if store is not None:
                store.close()

    threads = [
        threading.Thread(
            target=initialize,
            args=(worker_index,),
            name=f"round14-init-deadline-{worker_index}",
        )
        for worker_index in range(2)
    ]
    for thread in threads:
        thread.start()

    try:
        start_barrier.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=2)
        finished_while_locked = all(not thread.is_alive() for thread in threads)
    finally:
        holder.rollback()
        holder.close()
        for thread in threads:
            thread.join(timeout=2)
    all_workers_reaped = all(not thread.is_alive() for thread in threads)

    recovery_store = WorkflowStore(database_path)
    try:
        recovered_record = recovery_store.get_authoring_record(WORKFLOW_UUID)
    finally:
        recovery_store.close()

    durations = [
        outcome["duration"] for outcome in outcomes.values() if "duration" in outcome
    ]
    timeout_results = sorted(
        (outcome.get("error"), outcome.get("message")) for outcome in outcomes.values()
    )
    slowest_timeout_window = "missing"
    if len(durations) == 2:
        if max(durations) >= INITIALIZATION_TIMEOUT_SECONDS * 1.8:
            slowest_timeout_window = "serialized_double"
        elif max(durations) <= INITIALIZATION_TIMEOUT_SECONDS * CALL_DEADLINE_FACTOR:
            slowest_timeout_window = "single_call"
        else:
            slowest_timeout_window = "ambiguous"
    generation = recovered_record["writeback_generation"]
    assert {
        "finished_while_locked": finished_while_locked,
        "all_workers_reaped": all_workers_reaped,
        "worker_count": len(outcomes),
        "timeout_results": timeout_results,
        "both_waited_for_contention": (
            len(durations) == 2
            and min(durations) >= INITIALIZATION_TIMEOUT_SECONDS * 0.8
        ),
        "both_respected_call_deadline": (
            len(durations) == 2
            and max(durations) <= INITIALIZATION_TIMEOUT_SECONDS * CALL_DEADLINE_FACTOR
        ),
        "slowest_timeout_window": slowest_timeout_window,
        "database_recovered": {
            "status": recovered_record["writeback_status"],
            "source": recovered_record["writeback_source"],
            "expected_hash": recovered_record["writeback_expected_hash"],
            "generation_is_uuid4": (
                isinstance(generation, str) and UUID(generation).version == 4
            ),
        },
    } == {
        "finished_while_locked": True,
        "all_workers_reaped": True,
        "worker_count": 2,
        "timeout_results": [
            ("OperationalError", "database is locked"),
            ("OperationalError", "database is locked"),
        ],
        "both_waited_for_contention": True,
        "both_respected_call_deadline": True,
        "slowest_timeout_window": "single_call",
        "database_recovered": {
            "status": "pending",
            "source": RECOVERY_SOURCE,
            "expected_hash": EXPECTED_HASH,
            "generation_is_uuid4": True,
        },
    }
