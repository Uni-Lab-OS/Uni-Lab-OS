"""Phase 01 Round14：旧 writeback marker 的 generation 迁移守护。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID

from unilabos.workflow.store import WorkflowStore

COMPLETE_A_UUID = "11111111-1111-4111-8111-111111111111"
COMPLETE_B_UUID = "22222222-2222-4222-8222-222222222222"
MISSING_SOURCE_UUID = "33333333-3333-4333-8333-333333333333"
MISSING_HASH_UUID = "44444444-4444-4444-8444-444444444444"
BACKFILL_UUID = "55555555-5555-4555-8555-555555555555"
PRESERVED_UUID = "66666666-6666-4666-8666-666666666666"
PRESERVED_GENERATION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _create_legacy_authoring_table(
    database_path: Path,
    *,
    include_generation: bool,
) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    generation_column = ", writeback_generation TEXT" if include_generation else ""
    connection.execute(
        f"""
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
            writeback_expected_hash TEXT
            {generation_column},
            update_time TEXT NOT NULL
        )
        """
    )
    return connection


def _insert_pending_marker(
    connection: sqlite3.Connection,
    *,
    workflow_uuid: str,
    source: str | None,
    expected_hash: str | None,
    generation: str | None = None,
    include_generation: bool,
) -> None:
    columns = [
        "workflow_uuid",
        "diagnostics",
        "writeback_status",
        "writeback_source",
        "writeback_expected_hash",
        "update_time",
    ]
    values: list[str | None] = [
        workflow_uuid,
        "[]",
        "pending",
        source,
        expected_hash,
        "2026-07-31T01:02:03Z",
    ]
    if include_generation:
        columns.append("writeback_generation")
        values.append(generation)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"""
        INSERT INTO workflow_authoring ({", ".join(columns)})
        VALUES ({placeholders})
        """,
        values,
    )


def test_legacy_pending_backfill_is_unique_and_leaves_malformed_markers_null(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-multiple-pending.db"
    legacy = _create_legacy_authoring_table(
        database_path,
        include_generation=False,
    )
    try:
        _insert_pending_marker(
            legacy,
            workflow_uuid=COMPLETE_A_UUID,
            source="normalized_a()\n",
            expected_hash="sha256:expected-a",
            include_generation=False,
        )
        _insert_pending_marker(
            legacy,
            workflow_uuid=COMPLETE_B_UUID,
            source="normalized_b()\n",
            expected_hash="sha256:expected-b",
            include_generation=False,
        )
        _insert_pending_marker(
            legacy,
            workflow_uuid=MISSING_SOURCE_UUID,
            source=None,
            expected_hash="sha256:expected-missing-source",
            include_generation=False,
        )
        _insert_pending_marker(
            legacy,
            workflow_uuid=MISSING_HASH_UUID,
            source="normalized_missing_hash()\n",
            expected_hash=None,
            include_generation=False,
        )
        legacy.commit()
    finally:
        legacy.close()

    store = WorkflowStore(database_path)
    try:
        complete_generations = [
            store.get_authoring_record(workflow_uuid)["writeback_generation"]
            for workflow_uuid in (COMPLETE_A_UUID, COMPLETE_B_UUID)
        ]
        malformed_generations = [
            store.get_authoring_record(workflow_uuid)["writeback_generation"]
            for workflow_uuid in (MISSING_SOURCE_UUID, MISSING_HASH_UUID)
        ]
    finally:
        store.close()

    assert all(
        isinstance(generation, str) and generation
        for generation in complete_generations
    )
    assert len(set(complete_generations)) == 2
    assert all(UUID(generation).version == 4 for generation in complete_generations)
    assert malformed_generations == [None, None]


def test_generation_backfill_is_idempotent_and_preserves_existing_generation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "partially-migrated-pending.db"
    partially_migrated = _create_legacy_authoring_table(
        database_path,
        include_generation=True,
    )
    try:
        _insert_pending_marker(
            partially_migrated,
            workflow_uuid=BACKFILL_UUID,
            source="normalized_backfill()\n",
            expected_hash="sha256:expected-backfill",
            generation=None,
            include_generation=True,
        )
        _insert_pending_marker(
            partially_migrated,
            workflow_uuid=PRESERVED_UUID,
            source="normalized_preserved()\n",
            expected_hash="sha256:expected-preserved",
            generation=PRESERVED_GENERATION,
            include_generation=True,
        )
        partially_migrated.commit()
    finally:
        partially_migrated.close()

    first_store = WorkflowStore(database_path)
    try:
        first_backfill = first_store.get_authoring_record(BACKFILL_UUID)[
            "writeback_generation"
        ]
        first_preserved = first_store.get_authoring_record(PRESERVED_UUID)[
            "writeback_generation"
        ]
    finally:
        first_store.close()

    second_store = WorkflowStore(database_path)
    try:
        second_backfill = second_store.get_authoring_record(BACKFILL_UUID)[
            "writeback_generation"
        ]
        second_preserved = second_store.get_authoring_record(PRESERVED_UUID)[
            "writeback_generation"
        ]
    finally:
        second_store.close()

    assert isinstance(first_backfill, str) and first_backfill
    assert UUID(first_backfill).version == 4
    assert second_backfill == first_backfill
    assert first_preserved == second_preserved == PRESERVED_GENERATION
