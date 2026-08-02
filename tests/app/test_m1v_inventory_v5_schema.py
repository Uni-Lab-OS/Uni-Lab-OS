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

SCHEMA_VERSION = 5

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


def test_empty_inventory_opens_exact_v5_schema_and_sqlite_configuration(
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


def test_inventory_open_rejects_v5_with_missing_required_index(
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
