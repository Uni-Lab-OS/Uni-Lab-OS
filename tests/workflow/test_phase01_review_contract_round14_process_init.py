"""Phase 01 Round14：旧 WorkflowStore schema 必须可跨进程并发初始化。"""

from __future__ import annotations

import multiprocessing
import queue
import sqlite3
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
TIMESTAMP = "2026-07-31T01:02:03Z"
RECOVERY_SOURCE = "normalized_legacy_draft()\n"
EXPECTED_HASH = f"sha256:{'d' * 64}"
PROCESS_COUNT = 4
ROUND_COUNT = 2
ROUND_TIMEOUT_SECONDS = 12
LEGACY_COLUMNS = {
    "workflow_uuid",
    "observed_draft_hash",
    "draft_update_time",
    "diagnostics",
    "candidate_hash",
    "candidate",
    "applied_source",
    "writeback_status",
    "writeback_source",
    "writeback_expected_hash",
    "update_time",
}


def _create_complete_legacy_marker(database_path: Path) -> None:
    connection = sqlite3.connect(database_path)
    try:
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
            INSERT INTO workflow_authoring (
                workflow_uuid, observed_draft_hash, draft_update_time,
                diagnostics, candidate_hash, candidate, applied_source,
                writeback_status, writeback_source,
                writeback_expected_hash, update_time
            ) VALUES (?, 'sha256:observed', ?, '[]', NULL, NULL, NULL,
                      'pending', ?, ?, ?)
            """,
            (
                WORKFLOW_UUID,
                TIMESTAMP,
                RECOVERY_SOURCE,
                EXPECTED_HASH,
                TIMESTAMP,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _initialize_store_process(
    database_path: str,
    worker_index: int,
    start_barrier: Any,
    result_queue: Any,
) -> None:
    store: WorkflowStore | None = None
    try:
        start_barrier.wait(timeout=10)
        store = WorkflowStore(database_path)
        record = store.get_authoring_record(WORKFLOW_UUID)
        result_queue.put(
            {
                "worker": worker_index,
                "status": "opened",
                "generation": record["writeback_generation"],
                "marker_status": record["writeback_status"],
                "marker_source": record["writeback_source"],
                "marker_hash": record["writeback_expected_hash"],
            }
        )
    except BaseException as error:  # noqa: BLE001 - 子进程必须回报并退出
        result_queue.put(
            {
                "worker": worker_index,
                "status": "error",
                "error": type(error).__name__,
                "message": str(error),
            }
        )
    finally:
        if store is not None:
            store.close()


def _run_initialization_round(
    database_path: Path,
) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    start_barrier = context.Barrier(PROCESS_COUNT + 1)
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_initialize_store_process,
            args=(
                str(database_path),
                worker_index,
                start_barrier,
                result_queue,
            ),
            name=f"round14-process-init-{worker_index}",
        )
        for worker_index in range(PROCESS_COUNT)
    ]
    for process in processes:
        process.start()

    barrier_error: str | None = None
    results: dict[int, dict[str, Any]] = {}
    deadline = time.monotonic() + ROUND_TIMEOUT_SECONDS
    try:
        try:
            start_barrier.wait(timeout=10)
        except BaseException as error:  # noqa: BLE001 - 纳入合同结果
            barrier_error = type(error).__name__

        while len(results) < PROCESS_COUNT:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                result = result_queue.get(timeout=min(0.5, remaining))
            except queue.Empty:
                continue
            results[result["worker"]] = result

        for process in processes:
            process.join(timeout=max(0.0, deadline - time.monotonic()))
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join(timeout=2)
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
        result_queue.close()
        result_queue.join_thread()

    return {
        "barrier_error": barrier_error,
        "results": results,
        "missing_workers": sorted(set(range(PROCESS_COUNT)) - set(results)),
        "exitcodes": [process.exitcode for process in processes],
        "all_reaped": all(not process.is_alive() for process in processes),
    }


def _migration_projection(
    database_path: Path,
    process_outcome: dict[str, Any],
) -> dict[str, Any]:
    connection = sqlite3.connect(database_path)
    try:
        columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info(workflow_authoring)")
        ]
        rows = connection.execute(
            """
            SELECT workflow_uuid, observed_draft_hash, diagnostics,
                   writeback_status, writeback_source,
                   writeback_expected_hash, update_time,
                   writeback_generation
            FROM workflow_authoring
            """
        ).fetchall()
    finally:
        connection.close()

    results = process_outcome["results"]
    successful_workers = sorted(
        worker for worker, result in results.items() if result["status"] == "opened"
    )
    errors = sorted(
        (
            result["worker"],
            result["error"],
            result["message"],
        )
        for result in results.values()
        if result["status"] == "error"
    )
    final_generation = rows[0][-1] if len(rows) == 1 else None
    successful_generations = {
        results[worker]["generation"] for worker in successful_workers
    }
    generation_is_uuid4 = False
    try:
        generation_is_uuid4 = UUID(final_generation).version == 4
    except (AttributeError, TypeError, ValueError):
        pass

    return {
        "barrier_error": process_outcome["barrier_error"],
        "all_reaped": process_outcome["all_reaped"],
        "missing_workers": process_outcome["missing_workers"],
        "exitcodes": process_outcome["exitcodes"],
        "successful_workers": successful_workers,
        "errors": errors,
        "schema_complete": set(columns) == LEGACY_COLUMNS | {"writeback_generation"},
        "generation_column_count": columns.count("writeback_generation"),
        "row_count": len(rows),
        "legacy_row": rows[0][:-1] if len(rows) == 1 else None,
        "generation_is_uuid4": generation_is_uuid4,
        "one_stable_generation": (
            successful_generations == {final_generation}
            and final_generation is not None
        ),
    }


def test_processes_concurrently_upgrade_one_legacy_store_without_lock_errors(
    tmp_path: Path,
) -> None:
    rounds = []
    for round_index in range(ROUND_COUNT):
        database_path = tmp_path / f"legacy-process-{round_index}.db"
        _create_complete_legacy_marker(database_path)
        outcome = _run_initialization_round(database_path)
        rounds.append(_migration_projection(database_path, outcome))

    expected_round = {
        "barrier_error": None,
        "all_reaped": True,
        "missing_workers": [],
        "exitcodes": [0] * PROCESS_COUNT,
        "successful_workers": list(range(PROCESS_COUNT)),
        "errors": [],
        "schema_complete": True,
        "generation_column_count": 1,
        "row_count": 1,
        "legacy_row": (
            WORKFLOW_UUID,
            "sha256:observed",
            "[]",
            "pending",
            RECOVERY_SOURCE,
            EXPECTED_HASH,
            TIMESTAMP,
        ),
        "generation_is_uuid4": True,
        "one_stable_generation": True,
    }
    assert rounds == [expected_round] * ROUND_COUNT
