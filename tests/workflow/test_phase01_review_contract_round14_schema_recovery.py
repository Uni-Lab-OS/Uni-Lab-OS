"""Phase 01 Round14：旧 writeback marker 的 schema 迁移与恢复兼容。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
TIMESTAMP = "2026-07-31T01:02:03Z"
PRE_APPLY_SOURCE = "legacy_draft()"
RECOVERY_SOURCE = "normalized_legacy_draft()\n"
CATALOG_FINGERPRINT = f"sha256:{'c' * 64}"
LEGACY_AUTHORING_COLUMNS = {
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


def _sha256(source: str) -> str:
    return f"sha256:{hashlib.sha256(source.encode()).hexdigest()}"


def _create_legacy_database(
    tmp_path: Path,
    *,
    name: str,
) -> tuple[Path, Path]:
    database_path = tmp_path / name
    package_root = tmp_path / f"{name}-package"
    source_path = package_root / "workflows" / "review.py"
    source_path.parent.mkdir(parents=True)
    applied_source = {
        "python_source": RECOVERY_SOURCE,
        "source_hash": _sha256(RECOVERY_SOURCE),
        "source_map": [],
        "compiler_version": "legacy-compiler-v1",
        "template_catalog_fingerprint": CATALOG_FINGERPRINT,
        "workflow_revision": 1,
        "update_time": TIMESTAMP,
    }

    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            CREATE TABLE workflow (
                uuid TEXT PRIMARY KEY,
                create_time TEXT NOT NULL,
                update_time TEXT NOT NULL,
                deleted_at TEXT,
                description TEXT,
                meta_data TEXT NOT NULL,
                name TEXT NOT NULL,
                tags TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE workflow_source_registration (
                workflow_uuid TEXT PRIMARY KEY,
                package_id TEXT NOT NULL,
                package_root TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                source_uri TEXT NOT NULL,
                create_time TEXT NOT NULL,
                update_time TEXT NOT NULL
            );

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
            INSERT INTO workflow(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, name, tags, revision
            ) VALUES (?, ?, ?, NULL, NULL, '{}', 'legacy workflow', '[]', 1)
            """,
            (WORKFLOW_UUID, TIMESTAMP, TIMESTAMP),
        )
        connection.execute(
            """
            INSERT INTO workflow_source_registration(
                workflow_uuid, package_id, package_root, relative_path,
                source_uri, create_time, update_time
            ) VALUES (?, 'legacy_package', ?, 'workflows/review.py',
                      'package://legacy_package/workflows/review.py', ?, ?)
            """,
            (WORKFLOW_UUID, str(package_root), TIMESTAMP, TIMESTAMP),
        )
        connection.execute(
            """
            INSERT INTO workflow_authoring(
                workflow_uuid, observed_draft_hash, draft_update_time,
                diagnostics, candidate_hash, candidate, applied_source,
                writeback_status, writeback_source,
                writeback_expected_hash, update_time
            ) VALUES (?, ?, ?, '[]', NULL, NULL, ?, 'pending', ?, ?, ?)
            """,
            (
                WORKFLOW_UUID,
                _sha256(PRE_APPLY_SOURCE),
                TIMESTAMP,
                json.dumps(applied_source, sort_keys=True),
                RECOVERY_SOURCE,
                _sha256(PRE_APPLY_SOURCE),
                TIMESTAMP,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return database_path, source_path


def test_legacy_pending_marker_recovers_missing_draft_after_schema_upgrade(
    tmp_path: Path,
) -> None:
    database_path, source_path = _create_legacy_database(
        tmp_path,
        name="legacy-recovery.db",
    )
    assert not source_path.exists()

    store = WorkflowStore(database_path)
    try:
        service = WorkflowService(store)
        recovered = service.reconcile_registered_source(WORKFLOW_UUID)
        record = store.get_authoring_record(WORKFLOW_UUID)
        pending_after_recovery = service.source_reconciliation_pending(WORKFLOW_UUID)
    finally:
        store.close()

    assert {
        "state": recovered["state"],
        "draft_source": (
            recovered["draft"]["python_source"]
            if recovered["draft"] is not None
            else None
        ),
        "candidate": recovered["candidate"],
        "applied_compiler": recovered["applied_source"]["compiler_version"],
        "pending_after_recovery": pending_after_recovery,
        "marker": {
            "status": record["writeback_status"],
            "source": record["writeback_source"],
            "expected_hash": record["writeback_expected_hash"],
            "generation": record["writeback_generation"],
        },
        "canonical": (
            source_path.read_text(encoding="utf-8") if source_path.exists() else None
        ),
    } == {
        "state": "applied",
        "draft_source": RECOVERY_SOURCE,
        "candidate": None,
        "applied_compiler": "legacy-compiler-v1",
        "pending_after_recovery": False,
        "marker": {
            "status": "settled",
            "source": None,
            "expected_hash": None,
            "generation": None,
        },
        "canonical": RECOVERY_SOURCE,
    }


def test_concurrent_store_initialization_upgrades_legacy_schema_once(
    tmp_path: Path,
) -> None:
    database_path, _source_path = _create_legacy_database(
        tmp_path,
        name="legacy-concurrent.db",
    )
    start = threading.Event()
    ready = [threading.Event(), threading.Event()]
    outcomes: dict[int, dict[str, Any]] = {}

    def initialize(index: int) -> None:
        ready[index].set()
        if not start.wait(timeout=3):
            outcomes[index] = {"error": "start_timeout"}
            return
        store: WorkflowStore | None = None
        try:
            store = WorkflowStore(database_path)
            record = store.get_authoring_record(WORKFLOW_UUID)
            outcomes[index] = {
                "status": "opened",
                "legacy_status": record["writeback_status"],
                "legacy_source": record["writeback_source"],
            }
        except Exception as error:  # noqa: BLE001 - 暴露并发迁移异常
            outcomes[index] = {
                "error": type(error).__name__,
                "message": str(error),
            }
        finally:
            if store is not None:
                store.close()

    threads = [
        threading.Thread(
            target=initialize,
            args=(index,),
            name=f"round14-legacy-schema-{index}",
        )
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    try:
        both_ready = all(event.wait(timeout=2) for event in ready)
        assert both_ready
        start.set()
        for thread in threads:
            thread.join(timeout=7)
        all_finished = all(not thread.is_alive() for thread in threads)
    finally:
        start.set()
        for thread in threads:
            thread.join(timeout=7)

    migrated = sqlite3.connect(database_path)
    try:
        columns = [
            row[1] for row in migrated.execute("PRAGMA table_info(workflow_authoring)")
        ]
        legacy_row = migrated.execute(
            """
            SELECT workflow_uuid, observed_draft_hash, writeback_status,
                   writeback_source, writeback_expected_hash
            FROM workflow_authoring
            """
        ).fetchone()
    finally:
        migrated.close()

    assert {
        "both_ready": both_ready,
        "all_finished": all_finished,
        "outcomes": outcomes,
        "columns": set(columns),
        "generation_column_count": columns.count("writeback_generation"),
        "legacy_row": legacy_row,
    } == {
        "both_ready": True,
        "all_finished": True,
        "outcomes": {
            0: {
                "status": "opened",
                "legacy_status": "pending",
                "legacy_source": RECOVERY_SOURCE,
            },
            1: {
                "status": "opened",
                "legacy_status": "pending",
                "legacy_source": RECOVERY_SOURCE,
            },
        },
        "columns": LEGACY_AUTHORING_COLUMNS | {"writeback_generation"},
        "generation_column_count": 1,
        "legacy_row": (
            WORKFLOW_UUID,
            _sha256(PRE_APPLY_SOURCE),
            "pending",
            RECOVERY_SOURCE,
            _sha256(PRE_APPLY_SOURCE),
        ),
    }
