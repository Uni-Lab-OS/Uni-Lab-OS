"""Phase 01 Round14：WorkflowStore 初始化锁必须按数据库路径隔离。"""

from __future__ import annotations

import multiprocessing
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

import unilabos.workflow.store as store_module
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
INITIALIZATION_DEADLINE_SECONDS = 0.6
INDEPENDENT_STORE_WINDOW_SECONDS = 0.2


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
                '[]', 'pending', 'normalized_busy_source()',
                'sha256:busy-observed-source',
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


def _open_store_in_thread(
    database_path: Path,
    outcome: dict[str, Any],
    finished: threading.Event,
) -> None:
    store: WorkflowStore | None = None
    started_at = time.monotonic()
    try:
        store = WorkflowStore(database_path)
        outcome.update(
            {
                "status": "opened",
                "record": store.get_authoring_record(WORKFLOW_UUID),
            }
        )
    except Exception as error:  # noqa: BLE001 - 将初始化失败纳入断言
        outcome.update(
            {
                "status": "error",
                "error": type(error).__name__,
                "message": str(error),
            }
        )
    finally:
        if store is not None:
            store.close()
        outcome["elapsed"] = time.monotonic() - started_at
        finished.set()


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
    if process.is_alive():
        process.kill()
        process.join(timeout=2)


def test_busy_store_retry_does_not_block_initialization_of_independent_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    busy_database_path = tmp_path / "busy-legacy.db"
    independent_database_path = tmp_path / "independent.db"
    _create_delete_mode_legacy_database(busy_database_path)

    retry_entered = threading.Event()
    original_sleep = time.sleep

    def observe_retry_sleep(seconds: float) -> None:
        retry_entered.set()
        original_sleep(seconds)

    monkeypatch.setattr(
        store_module,
        "_STORE_INITIALIZATION_BUSY_TIMEOUT_SECONDS",
        INITIALIZATION_DEADLINE_SECONDS,
    )
    monkeypatch.setattr(
        store_module,
        "_STORE_INITIALIZATION_SQLITE_BUSY_TIMEOUT_MS",
        20,
    )
    monkeypatch.setattr(
        store_module,
        "_STORE_INITIALIZATION_RETRY_INTERVAL_SECONDS",
        0.01,
    )
    monkeypatch.setattr(store_module, "sleep", observe_retry_sleep)

    context = multiprocessing.get_context("spawn")
    holder_acquired = context.Event()
    release_holder = context.Event()
    holder_outcome = context.Queue()
    holder = context.Process(
        target=_hold_delete_mode_write_transaction,
        args=(
            str(busy_database_path),
            holder_acquired,
            release_holder,
            holder_outcome,
        ),
        name="round14-init-isolation-holder",
    )
    busy_outcome: dict[str, Any] = {}
    independent_outcome: dict[str, Any] = {}
    busy_finished = threading.Event()
    independent_finished = threading.Event()
    busy_thread = threading.Thread(
        target=_open_store_in_thread,
        args=(busy_database_path, busy_outcome, busy_finished),
        name="round14-busy-store-init",
    )
    independent_thread = threading.Thread(
        target=_open_store_in_thread,
        args=(
            independent_database_path,
            independent_outcome,
            independent_finished,
        ),
        name="round14-independent-store-init",
    )
    busy_thread_started = False
    independent_thread_started = False

    holder.start()
    holder_result: tuple[Any, ...] | None = None
    try:
        holder_lock_acquired = holder_acquired.wait(timeout=2)
        if holder_lock_acquired:
            busy_thread.start()
            busy_thread_started = True
            retry_observed = retry_entered.wait(timeout=2)
            if retry_observed:
                independent_thread.start()
                independent_thread_started = True
                independent_finished_in_window = independent_finished.wait(
                    timeout=INDEPENDENT_STORE_WINDOW_SECONDS
                )
                busy_still_retrying = not busy_finished.is_set()
            else:
                independent_finished_in_window = False
                busy_still_retrying = False

            busy_thread.join(timeout=2)
            if independent_thread_started:
                independent_thread.join(timeout=2)
            all_store_threads_finished = (
                not busy_thread.is_alive()
                and independent_thread_started
                and not independent_thread.is_alive()
            )
        else:
            retry_observed = False
            independent_finished_in_window = False
            busy_still_retrying = False
            all_store_threads_finished = False
    finally:
        release_holder.set()
        if busy_thread_started:
            busy_thread.join(timeout=2)
        if independent_thread_started:
            independent_thread.join(timeout=2)
        holder.join(timeout=5)
        if holder.exitcode == 0:
            holder_result = _queue_result(holder_outcome)
        _terminate_if_alive(holder)
        holder_outcome.close()

    recovered_store = WorkflowStore(busy_database_path)
    try:
        recovered_generation = recovered_store.get_authoring_record(WORKFLOW_UUID)[
            "writeback_generation"
        ]
    finally:
        recovered_store.close()
    repeated_store = WorkflowStore(busy_database_path)
    try:
        repeated_generation = repeated_store.get_authoring_record(WORKFLOW_UUID)[
            "writeback_generation"
        ]
    finally:
        repeated_store.close()

    busy_elapsed = busy_outcome.get("elapsed")
    assert {
        "holder_lock_acquired": holder_lock_acquired,
        "retry_observed": retry_observed,
        "independent_finished_in_window": independent_finished_in_window,
        "busy_still_retrying": busy_still_retrying,
        "all_store_threads_finished": all_store_threads_finished,
        "holder_exitcode": holder.exitcode,
        "holder_result": holder_result,
        "busy_result": {
            "status": busy_outcome.get("status"),
            "error": busy_outcome.get("error"),
            "message": busy_outcome.get("message"),
        },
        "busy_timeout_bounded": (
            isinstance(busy_elapsed, float)
            and INITIALIZATION_DEADLINE_SECONDS * 0.7
            <= busy_elapsed
            <= INITIALIZATION_DEADLINE_SECONDS + 1
        ),
        "independent_result": independent_outcome.get("status"),
        "independent_elapsed_fast": (
            isinstance(independent_outcome.get("elapsed"), float)
            and independent_outcome["elapsed"] < INDEPENDENT_STORE_WINDOW_SECONDS
        ),
        "recovered_generation_is_uuid4": (
            isinstance(recovered_generation, str)
            and UUID(recovered_generation).version == 4
        ),
        "repeated_generation": repeated_generation,
    } == {
        "holder_lock_acquired": True,
        "retry_observed": True,
        "independent_finished_in_window": True,
        "busy_still_retrying": True,
        "all_store_threads_finished": True,
        "holder_exitcode": 0,
        "holder_result": ("released", "delete"),
        "busy_result": {
            "status": "error",
            "error": "OperationalError",
            "message": "database is locked",
        },
        "busy_timeout_bounded": True,
        "independent_result": "opened",
        "independent_elapsed_fast": True,
        "recovered_generation_is_uuid4": True,
        "repeated_generation": recovered_generation,
    }
