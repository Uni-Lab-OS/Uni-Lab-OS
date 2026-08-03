"""M1V ``inventory.db`` v5 exact schema 与弃旧策略。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from unilabos.app.scheduler.inventory import (
    InventoryService,
    MaterialAuthorityUnavailable,
)
from unilabos.app.scheduler.inventory.store import _SCHEMA_V5, InventoryStore

SCHEMA_VERSION = 6

REQUIRED_TABLE_COLUMNS = {
    "material": (
        "uuid",
        "create_time",
        "update_time",
        "deleted_at",
        "description",
        "meta_data",
        "resource_template_uuid",
        "parent_uuid",
        "class",
        "barcode",
        "name",
        "config",
        "data",
        "disposition",
        "material_kind",
        "version",
    ),
    "site": (
        "uuid",
        "create_time",
        "update_time",
        "deleted_at",
        "description",
        "meta_data",
        "material_uuid",
        "name",
        "sort_order",
        "occupied_material_uuid",
        "position_x",
        "position_y",
        "position_z",
        "depth",
        "length",
        "width",
        "version",
    ),
    "site_allowed_resource_template": (
        "site_uuid",
        "resource_template_uuid",
    ),
    "relative_position": (
        "uuid",
        "create_time",
        "update_time",
        "deleted_at",
        "description",
        "meta_data",
        "material_uuid",
        "position_x",
        "position_y",
        "position_z",
        "depth",
        "length",
        "width",
        "scale_x",
        "scale_y",
        "scale_z",
        "rotation_x",
        "rotation_y",
        "rotation_z",
    ),
    "material_reservation": (
        "uuid",
        "workflow_task_uuid",
        "set_fingerprint",
        "status",
        "create_time",
        "released_at",
    ),
    "material_reservation_member": (
        "reservation_uuid",
        "material_uuid",
        "root_material_uuid",
        "acquired_version",
        "released_at",
    ),
    "material_content": ("material_uuid", "state_json", "version"),
    "inventory_lot": (
        "lot_id",
        "resource_template_uuid",
        "batch_no",
        "unit",
        "quantity_total",
        "quantity_available",
        "quantity_reserved",
        "expiry",
        "quarantined",
        "warehouse_zone_id",
        "created_at",
        "version",
    ),
    "inventory_ledger": (
        "ledger_id",
        "occurred_at",
        "op_type",
        "aggregate_type",
        "aggregate_id",
        "delta_json",
        "actor",
        "reason",
        "causation_id",
    ),
    "sync_outbox": (
        "sequence",
        "event_id",
        "edge_id",
        "lab_id",
        "aggregate_type",
        "aggregate_id",
        "aggregate_version",
        "event_type",
        "occurred_at",
        "causation_id",
        "payload_json",
    ),
    "processed_command": (
        "command_id",
        "idempotency_key",
        "command_type",
        "payload_hash",
        "result_json",
        "status",
        "processed_at",
    ),
    "sync_cursor": (
        "cursor_name",
        "acked_sequence",
        "updated_at",
    ),
    "lab_meta": ("meta_key", "meta_value"),
    "lab_zone": (
        "zone_id",
        "name",
        "kind",
        "x",
        "y",
        "w",
        "h",
        "meta_json",
        "version",
    ),
    "lab_placement": (
        "subject_id",
        "subject_kind",
        "zone_id",
        "x",
        "y",
        "w",
        "h",
        "rotation",
        "label",
        "meta_json",
        "version",
    ),
    "material_claim": (
        "uuid",
        "workflow_task_uuid",
        "workflow_node_job_uuid",
        "attempt",
        "set_fingerprint",
        "fencing_token",
        "state",
        "uncertainty_reason",
        "acquired_at",
        "create_time",
        "running_at",
        "release_proof_kind",
        "release_proof_fingerprint",
        "release_reason",
        "terminal_changeset_uuid",
        "workflow_terminal_fingerprint",
        "release_command_uuid",
        "released_at",
        "update_time",
    ),
    "material_claim_member": (
        "claim_uuid",
        "resource_kind",
        "resource_uuid",
        "acquired_version",
        "expected_version",
        "released_at",
    ),
    "material_claim_fence_sequence": ("sequence", "claim_uuid"),
    "material_resource_fence": (
        "resource_kind",
        "resource_uuid",
        "fencing_token",
        "claim_uuid",
        "update_time",
    ),
    "material_changeset": (
        "uuid",
        "workflow_task_uuid",
        "workflow_node_job_uuid",
        "attempt",
        "claim_uuid",
        "fencing_token",
        "effect_identity",
        "deterministic_fingerprint",
        "outcome",
        "result_json",
        "outbox_sequence",
        "create_time",
    ),
    "material_changeset_effect": (
        "changeset_uuid",
        "effect_key",
        "resource_kind",
        "resource_uuid",
        "operation",
        "expected_version",
        "before_json",
        "after_json",
    ),
}
FORBIDDEN_TABLES = {
    "resource_template",
    "material_instance",
    "resource_relation",
    "substance_content",
    "inventory_reservation",
}


def _application_tables(connection: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        row[0]
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
    )


def _table_columns(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[str, ...]:
    return tuple(row[1] for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _database_snapshot(database: Path) -> dict[str, Any]:
    connection = sqlite3.connect(database)
    try:
        tables = _application_tables(connection)
        return {
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "schema": tuple(
                connection.execute(
                    """
                    SELECT type, name, tbl_name, sql FROM sqlite_master
                    WHERE name NOT LIKE 'sqlite_%'
                    ORDER BY type, name
                    """
                )
            ),
            "rows": {
                table: tuple(connection.execute(f'SELECT * FROM "{table}"'))
                for table in tables
            },
        }
    finally:
        connection.close()


def _create_legacy_v3(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE resource_template (
                template_id TEXT PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE material_instance (
                edge_uuid TEXT PRIMARY KEY,
                template_id TEXT NOT NULL,
                parent_uuid TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO resource_template(template_id, name)
            VALUES ('legacy-template', 'Legacy template');
            INSERT INTO material_instance(edge_uuid, template_id, parent_uuid)
            VALUES ('legacy-material', 'legacy-template', '');
            PRAGMA user_version = 3;
            """
        )
        connection.commit()
    finally:
        connection.close()


def _create_mixed_v5(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE material (
                uuid TEXT PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE material_instance (
                edge_uuid TEXT PRIMARY KEY,
                template_id TEXT NOT NULL
            );
            INSERT INTO material(uuid, name)
            VALUES ('50000000-0000-4000-8000-000000000404', 'Mixed material');
            INSERT INTO material_instance(edge_uuid, template_id)
            VALUES ('legacy-material', 'legacy-template');
            PRAGMA user_version = 5;
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_empty_inventory_opens_exact_v6_schema_and_sqlite_configuration(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory.db"
    inventory = InventoryService.open(
        working_dir=tmp_path,
        resource_templates={},
    )
    inventory.close()

    connection = sqlite3.connect(database)
    try:
        settings = {
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        }
        tables = _application_tables(connection)
        columns = {table: _table_columns(connection, table) for table in tables}
        reservation_foreign_keys = tuple(
            connection.execute('PRAGMA foreign_key_list("material_reservation")')
        )
    finally:
        connection.close()

    assert database.is_file()
    assert settings == {
        "user_version": SCHEMA_VERSION,
        "journal_mode": "wal",
    }
    assert set(tables) == set(REQUIRED_TABLE_COLUMNS)
    assert FORBIDDEN_TABLES.isdisjoint(tables)
    assert {
        table: columns[table] for table in REQUIRED_TABLE_COLUMNS
    } == REQUIRED_TABLE_COLUMNS
    assert reservation_foreign_keys == ()


@pytest.mark.parametrize(
    "legacy_builder",
    [_create_legacy_v3, _create_mixed_v5],
    ids=["legacy-v3", "mixed-v5"],
)
def test_inventory_open_rejects_legacy_or_mixed_schema_without_mutation(
    tmp_path: Path,
    legacy_builder: Callable[[Path], None],
) -> None:
    database = tmp_path / "inventory.db"
    legacy_builder(database)
    before = _database_snapshot(database)
    unexpectedly_opened: InventoryService | None = None

    try:
        with pytest.raises(MaterialAuthorityUnavailable):
            unexpectedly_opened = InventoryService.open(
                working_dir=tmp_path,
                resource_templates={},
            )
    finally:
        if unexpectedly_opened is not None:
            unexpectedly_opened.close()

    assert _database_snapshot(database) == before


def test_inventory_open_rejects_v6_with_missing_required_index(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory.db"
    inventory = InventoryService.open(
        working_dir=tmp_path,
        resource_templates={},
    )
    inventory.close()
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP INDEX ux_material_barcode_active_nonempty")
        connection.commit()
    finally:
        connection.close()
    before = _database_snapshot(database)

    with pytest.raises(MaterialAuthorityUnavailable):
        InventoryService.open(
            working_dir=tmp_path,
            resource_templates={},
        )

    assert _database_snapshot(database) == before


def test_exact_v5_migrates_to_v6_without_losing_accepted_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory.db"
    connection = sqlite3.connect(database)
    try:
        connection.create_collation(
            "UNICODE_CASEFOLD",
            lambda left, right: (
                (left.casefold() > right.casefold())
                - (left.casefold() < right.casefold())
            ),
        )
        connection.executescript(_SCHEMA_V5)
        connection.execute(
            """
            INSERT INTO material(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, resource_template_uuid, parent_uuid, class,
                barcode, name, config, data, disposition, material_kind,
                version
            ) VALUES (?, '2026-08-02T00:00:00Z', '2026-08-02T00:00:00Z',
                      NULL, 'preserved', '{}', ?, NULL, 'SampleTube',
                      'M1EF-MIGRATION', 'preserved sample', '{}',
                      '{"temperature":20}', 'active', 'business', 7)
            """,
            (
                "50000000-0000-4000-8000-000000000505",
                "20000000-0000-4000-8000-000000000505",
            ),
        )
        connection.execute(
            """
            INSERT INTO inventory_lot(
                lot_id, resource_template_uuid, batch_no, unit,
                quantity_total, quantity_available, quantity_reserved,
                expiry, quarantined, warehouse_zone_id, created_at, version
            ) VALUES ('lot-505', ?, 'batch-505', 'mL', 10, 6, 4,
                      '2030-01-01', 0, 'cold-room', 505, 3)
            """,
            ("20000000-0000-4000-8000-000000000505",),
        )
        connection.execute("PRAGMA user_version = 5")
        connection.commit()
    finally:
        connection.close()

    before = _database_snapshot(database)
    inventory = InventoryService.open(working_dir=tmp_path, resource_templates={})
    inventory.close()
    after = _database_snapshot(database)

    assert before["rows"]["material"] == after["rows"]["material"]
    assert before["rows"]["inventory_lot"] == after["rows"]["inventory_lot"]
    assert after["user_version"] == 6
    assert set(after["rows"]) == set(REQUIRED_TABLE_COLUMNS)
    assert after["rows"]["material_claim"] == ()
    assert after["rows"]["material_changeset"] == ()


@pytest.mark.parametrize(
    "fault_stage",
    [
        "before_v6_ddl",
        "after_first_v6_ddl",
        "after_v6_ddl",
        "after_schema_receipt",
        "after_user_version",
        "after_exact_schema_audit",
    ],
)
def test_v5_to_v6_crash_windows_roll_back_to_exact_v5(
    tmp_path: Path,
    fault_stage: str,
) -> None:
    database = tmp_path / "inventory.db"
    connection = sqlite3.connect(database)
    try:
        connection.create_collation(
            "UNICODE_CASEFOLD",
            lambda left, right: (
                (left.casefold() > right.casefold())
                - (left.casefold() < right.casefold())
            ),
        )
        connection.executescript(_SCHEMA_V5)
        connection.execute(
            "INSERT INTO lab_meta(meta_key, meta_value) VALUES ('fixture', 'kept')"
        )
        connection.execute("PRAGMA user_version = 5")
        connection.commit()
    finally:
        connection.close()
    before = _database_snapshot(database)

    def fail(stage: str) -> None:
        if stage == fault_stage:
            raise RuntimeError(f"simulated migration crash at {stage}")

    with pytest.raises(RuntimeError, match="simulated migration crash"):
        InventoryStore(str(database), migration_fault_hook=fail)

    assert _database_snapshot(database) == before
    inventory = InventoryService.open(working_dir=tmp_path, resource_templates={})
    inventory.close()
    assert _database_snapshot(database)["user_version"] == SCHEMA_VERSION
