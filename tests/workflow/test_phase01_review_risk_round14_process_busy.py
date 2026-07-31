"""Phase 01 Round14：跨进程旧库初始化锁等待守护。"""

from __future__ import annotations

import multiprocessing
import queue
import sqlite3
from pathlib import Path
from typing import Any
from uuid import UUID

from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"


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

            INSERT INTO workflow_authoring (
                workflow_uuid, diagnostics, writeback_status,
                writeback_source, writeback_expected_hash, update_time
            ) VALUES (
                '11111111-1111-4111-8111-111111111111',
                '[]', 'pending', 'normalized_legacy_source()',
                'sha256:legacy-observed-source',
                '2026-07-31T01:02:03Z'
            );
            """
        )
        connection.commit()
    finally:
        connection.close()


def _hold_delete_mode_write_transaction(
    database_path: str,
    acquired: Any,
    release: Any,
    outcome: Any,
) -> None:
    connection = sqlite3.connect(database_path)
    try:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        connection.execute("BEGIN IMMEDIATE")
        acquired.set()
        if not release.wait(timeout=5):
            connection.rollback()
            outcome.put(("error", "release_timeout", journal_mode))
            return
        connection.commit()
        outcome.put(("released", journal_mode))
    except Exception as error:  # noqa: BLE001 - 子进程异常必须显式回传
        outcome.put(("error", type(error).__name__, str(error)))
    finally:
        acquired.set()
        connection.close()


def _open_store_while_legacy_writer_is_busy(
    database_path: str,
    attempted: Any,
    outcome: Any,
) -> None:
    store: WorkflowStore | None = None
    attempted.set()
    try:
        store = WorkflowStore(database_path)
        record = store.get_authoring_record(WORKFLOW_UUID)
        outcome.put(("opened", record["writeback_generation"]))
    except Exception as error:  # noqa: BLE001 - 暴露初始化锁错误
        outcome.put(("error", type(error).__name__, str(error)))
    finally:
        if store is not None:
            store.close()


def _queue_result(result_queue: Any) -> tuple[Any, ...] | None:
    try:
        return result_queue.get(timeout=1)
    except queue.Empty:
        return None


def _terminate_if_alive(process: multiprocessing.Process) -> None:
    if not process.is_alive():
        return
    process.terminate()
    process.join(timeout=2)


def test_store_waits_for_cross_process_delete_mode_writer_before_migration(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-process-busy.db"
    _create_delete_mode_legacy_database(database_path)
    context = multiprocessing.get_context("spawn")
    holder_acquired = context.Event()
    release_holder = context.Event()
    opener_attempted = context.Event()
    holder_outcome = context.Queue()
    opener_outcome = context.Queue()
    holder = context.Process(
        target=_hold_delete_mode_write_transaction,
        args=(
            str(database_path),
            holder_acquired,
            release_holder,
            holder_outcome,
        ),
        name="round14-delete-mode-holder",
    )
    opener = context.Process(
        target=_open_store_while_legacy_writer_is_busy,
        args=(str(database_path), opener_attempted, opener_outcome),
        name="round14-workflow-store-opener",
    )

    holder.start()
    try:
        assert holder_acquired.wait(timeout=2)
        opener.start()
        assert opener_attempted.wait(timeout=2)
        opener.join(timeout=0.3)
        waited_for_lock_release = opener.is_alive()
        release_holder.set()
        holder.join(timeout=5)
        opener.join(timeout=5)
        holder_result = _queue_result(holder_outcome) if holder.exitcode == 0 else None
        opener_result = _queue_result(opener_outcome) if opener.exitcode == 0 else None
    finally:
        release_holder.set()
        _terminate_if_alive(holder)
        _terminate_if_alive(opener)
        holder_outcome.close()
        opener_outcome.close()

    repeat_store = WorkflowStore(database_path)
    try:
        repeated_generation = repeat_store.get_authoring_record(WORKFLOW_UUID)[
            "writeback_generation"
        ]
    finally:
        repeat_store.close()

    opened_generation = (
        opener_result[1]
        if opener_result is not None and opener_result[0] == "opened"
        else None
    )
    assert {
        "waited_for_lock_release": waited_for_lock_release,
        "holder_exitcode": holder.exitcode,
        "holder_result": holder_result,
        "opener_exitcode": opener.exitcode,
        "opener_result": opener_result,
        "generation_is_uuid4": (
            isinstance(opened_generation, str) and UUID(opened_generation).version == 4
        ),
        "repeated_generation": repeated_generation,
    } == {
        "waited_for_lock_release": True,
        "holder_exitcode": 0,
        "holder_result": ("released", "delete"),
        "opener_exitcode": 0,
        "opener_result": ("opened", opened_generation),
        "generation_is_uuid4": True,
        "repeated_generation": opened_generation,
    }
