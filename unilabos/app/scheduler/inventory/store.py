"""SQLite WAL 持久化层.

单仓储分区单写者：所有写事务经进程内锁 + BEGIN IMMEDIATE 串行化，
业务变更、ledger、outbox 必须在同一事务提交（由 service 层保证，
store 只提供 transaction() 原语与行级 helper）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from unilabos.app.scheduler.inventory.domain import MaterialAuthorityUnavailable

SCHEMA_VERSION = 5

_UNICODE_CASEFOLD_COLLATION = "UNICODE_CASEFOLD"
_SQLITE_BUSY_TIMEOUT_MS = 5_000

_EXPECTED_TABLE_COLUMNS = {
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
    "sync_cursor": ("cursor_name", "acked_sequence", "updated_at"),
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


def _unicode_casefold(left: str, right: str) -> int:
    left_folded = left.casefold()
    right_folded = right.casefold()
    return (left_folded > right_folded) - (left_folded < right_folded)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS material (
    uuid TEXT PRIMARY KEY,
    create_time TEXT NOT NULL,
    update_time TEXT NOT NULL,
    deleted_at TEXT,
    description TEXT,
    meta_data TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(meta_data) AND json_type(meta_data) = 'object'),
    resource_template_uuid TEXT NOT NULL,
    parent_uuid TEXT,
    class TEXT NOT NULL CHECK (LENGTH(TRIM(class)) > 0),
    barcode TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL CHECK (LENGTH(TRIM(name)) > 0),
    config TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(config) AND json_type(config) = 'object'),
    data TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(data) AND json_type(data) = 'object'),
    disposition TEXT
        CHECK (disposition IS NULL OR disposition IN (
            'active', 'consumed', 'discarded', 'quarantined', 'reconciling'
        )),
    material_kind TEXT NOT NULL
        CHECK (material_kind IN ('business', 'device')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    FOREIGN KEY(parent_uuid) REFERENCES material(uuid) ON DELETE RESTRICT,
    CHECK (
        (material_kind = 'business' AND disposition IS NOT NULL)
        OR (material_kind = 'device' AND disposition IS NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_material_barcode_active_nonempty
    ON material(barcode COLLATE UNICODE_CASEFOLD)
    WHERE deleted_at IS NULL AND barcode <> '';
CREATE INDEX IF NOT EXISTS ix_material_template_active
    ON material(resource_template_uuid) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_material_parent_active
    ON material(parent_uuid) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS site (
    uuid TEXT PRIMARY KEY,
    create_time TEXT NOT NULL,
    update_time TEXT NOT NULL,
    deleted_at TEXT,
    description TEXT,
    meta_data TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(meta_data) AND json_type(meta_data) = 'object'),
    material_uuid TEXT NOT NULL,
    name TEXT NOT NULL CHECK (LENGTH(TRIM(name)) > 0),
    sort_order INTEGER NOT NULL DEFAULT 0 CHECK (sort_order >= 0),
    occupied_material_uuid TEXT,
    position_x REAL NOT NULL,
    position_y REAL NOT NULL,
    position_z REAL NOT NULL,
    depth REAL NOT NULL CHECK (depth >= 0),
    length REAL NOT NULL CHECK (length >= 0),
    width REAL NOT NULL CHECK (width >= 0),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    FOREIGN KEY(material_uuid) REFERENCES material(uuid) ON DELETE RESTRICT,
    FOREIGN KEY(occupied_material_uuid) REFERENCES material(uuid) ON DELETE RESTRICT,
    CHECK (occupied_material_uuid IS NULL OR occupied_material_uuid <> material_uuid)
);
CREATE TABLE IF NOT EXISTS site_allowed_resource_template (
    site_uuid TEXT NOT NULL,
    resource_template_uuid TEXT NOT NULL,
    PRIMARY KEY(site_uuid, resource_template_uuid),
    FOREIGN KEY(site_uuid) REFERENCES site(uuid) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_site_material_name_active
    ON site(material_uuid, name COLLATE UNICODE_CASEFOLD)
    WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_site_occupied_material_active
    ON site(occupied_material_uuid)
    WHERE deleted_at IS NULL AND occupied_material_uuid IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_site_material_order_active
    ON site(material_uuid, sort_order, create_time, uuid)
    WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS relative_position (
    uuid TEXT PRIMARY KEY,
    create_time TEXT NOT NULL,
    update_time TEXT NOT NULL,
    deleted_at TEXT,
    description TEXT,
    meta_data TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(meta_data) AND json_type(meta_data) = 'object'),
    material_uuid TEXT NOT NULL,
    position_x REAL NOT NULL DEFAULT 0,
    position_y REAL NOT NULL DEFAULT 0,
    position_z REAL NOT NULL DEFAULT 0,
    depth REAL NOT NULL CHECK (depth >= 0),
    length REAL NOT NULL CHECK (length >= 0),
    width REAL NOT NULL CHECK (width >= 0),
    scale_x REAL NOT NULL DEFAULT 1 CHECK (scale_x > 0),
    scale_y REAL NOT NULL DEFAULT 1 CHECK (scale_y > 0),
    scale_z REAL NOT NULL DEFAULT 1 CHECK (scale_z > 0),
    rotation_x REAL NOT NULL DEFAULT 0,
    rotation_y REAL NOT NULL DEFAULT 0,
    rotation_z REAL NOT NULL DEFAULT 0,
    FOREIGN KEY(material_uuid) REFERENCES material(uuid) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_relative_position_material_active
    ON relative_position(material_uuid) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS inventory_lot (
    lot_id             TEXT PRIMARY KEY,
    resource_template_uuid TEXT NOT NULL,
    batch_no           TEXT NOT NULL DEFAULT '',
    unit               TEXT NOT NULL DEFAULT '',
    quantity_total     REAL NOT NULL DEFAULT 0,
    quantity_available REAL NOT NULL DEFAULT 0,
    quantity_reserved  REAL NOT NULL DEFAULT 0,
    expiry             TEXT NOT NULL DEFAULT '',
    quarantined        INTEGER NOT NULL DEFAULT 0,
    warehouse_zone_id  TEXT NOT NULL DEFAULT '',
    created_at         INTEGER NOT NULL DEFAULT 0,
    version            INTEGER NOT NULL DEFAULT 1,
    CHECK (quantity_total >= 0),
    CHECK (quantity_available >= 0),
    CHECK (quantity_reserved >= 0),
    CHECK (quantity_available + quantity_reserved <= quantity_total + 1e-9)
);
CREATE INDEX IF NOT EXISTS idx_lot_template
    ON inventory_lot(resource_template_uuid, created_at);

CREATE TABLE IF NOT EXISTS material_content (
    material_uuid TEXT PRIMARY KEY,
    state_json    TEXT NOT NULL DEFAULT '{}',
    version       INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    FOREIGN KEY(material_uuid) REFERENCES material(uuid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS material_reservation (
    uuid TEXT PRIMARY KEY,
    workflow_task_uuid TEXT NOT NULL,
    set_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'released')),
    create_time TEXT NOT NULL,
    released_at TEXT,
    CHECK (
        (status = 'active' AND released_at IS NULL)
        OR (status = 'released' AND released_at IS NOT NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_material_reservation_task_active
    ON material_reservation(workflow_task_uuid) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS material_reservation_member (
    reservation_uuid TEXT NOT NULL,
    material_uuid TEXT NOT NULL,
    root_material_uuid TEXT NOT NULL,
    acquired_version INTEGER NOT NULL CHECK (acquired_version > 0),
    released_at TEXT,
    PRIMARY KEY(reservation_uuid, material_uuid),
    FOREIGN KEY(reservation_uuid) REFERENCES material_reservation(uuid)
        ON DELETE CASCADE,
    FOREIGN KEY(material_uuid) REFERENCES material(uuid) ON DELETE RESTRICT,
    FOREIGN KEY(root_material_uuid) REFERENCES material(uuid) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_material_reservation_member_active
    ON material_reservation_member(material_uuid) WHERE released_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_material_reservation_member_root
    ON material_reservation_member(root_material_uuid);

CREATE TABLE IF NOT EXISTS inventory_ledger (
    ledger_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at   INTEGER NOT NULL,
    op_type       TEXT NOT NULL,
    aggregate_type TEXT NOT NULL DEFAULT '',
    aggregate_id  TEXT NOT NULL DEFAULT '',
    delta_json    TEXT NOT NULL DEFAULT '{}',
    actor         TEXT NOT NULL DEFAULT '',
    reason        TEXT NOT NULL DEFAULT '',
    causation_id  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sync_outbox (
    sequence          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id          TEXT NOT NULL UNIQUE,
    edge_id           TEXT NOT NULL,
    lab_id            TEXT NOT NULL,
    aggregate_type    TEXT NOT NULL,
    aggregate_id      TEXT NOT NULL,
    aggregate_version INTEGER NOT NULL,
    event_type        TEXT NOT NULL,
    occurred_at       INTEGER NOT NULL,
    causation_id      TEXT NOT NULL DEFAULT '',
    payload_json      TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS processed_command (
    command_id      TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL DEFAULT '',
    command_type    TEXT NOT NULL DEFAULT '',
    payload_hash    TEXT NOT NULL DEFAULT '',
    result_json     TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'completed',
    processed_at    INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_processed_command_idempotency
    ON processed_command(idempotency_key) WHERE idempotency_key <> '';

CREATE TABLE IF NOT EXISTS sync_cursor (
    cursor_name    TEXT PRIMARY KEY,
    acked_sequence INTEGER NOT NULL DEFAULT 0,
    updated_at     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS lab_meta (
    meta_key   TEXT PRIMARY KEY,
    meta_value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS lab_zone (
    zone_id   TEXT PRIMARY KEY,
    name      TEXT NOT NULL DEFAULT '',
    kind      TEXT NOT NULL DEFAULT 'bench',
    x         REAL NOT NULL DEFAULT 0,
    y         REAL NOT NULL DEFAULT 0,
    w         REAL NOT NULL DEFAULT 100,
    h         REAL NOT NULL DEFAULT 100,
    meta_json TEXT NOT NULL DEFAULT '{}',
    version   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS lab_placement (
    subject_id   TEXT PRIMARY KEY,
    subject_kind TEXT NOT NULL DEFAULT 'container',
    zone_id      TEXT NOT NULL DEFAULT '',
    x            REAL NOT NULL DEFAULT 0,
    y            REAL NOT NULL DEFAULT 0,
    w            REAL NOT NULL DEFAULT 40,
    h            REAL NOT NULL DEFAULT 40,
    rotation     REAL NOT NULL DEFAULT 0,
    label        TEXT NOT NULL DEFAULT '',
    meta_json    TEXT NOT NULL DEFAULT '{}',
    version      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_placement_zone ON lab_placement(zone_id);
"""


def _schema_objects(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, str, str, str], ...]:
    """Return every application DDL object exactly as SQLite persisted it."""

    return tuple(
        (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
        for row in connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL
            ORDER BY type, name
            """
        )
    )


def _canonical_schema_objects() -> tuple[tuple[str, str, str, str], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.create_collation(
            _UNICODE_CASEFOLD_COLLATION,
            _unicode_casefold,
        )
        connection.executescript(_SCHEMA)
        return _schema_objects(connection)
    finally:
        connection.close()


_EXPECTED_SCHEMA_OBJECTS = _canonical_schema_objects()


class InventoryStore:
    """SQLite WAL 存储：单连接 + 进程内写锁（单写者）."""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._open_exact_schema()
        except BaseException:
            self._conn.close()
            raise

    def _open_exact_schema(self) -> None:
        """Create an empty v5 database or reject every legacy/mixed shape."""

        with self._lock:
            current = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            tables = self._application_tables()
            is_empty = current == 0 and not tables
            if not is_empty and (
                current != SCHEMA_VERSION or not self._has_exact_schema(tables)
            ):
                raise MaterialAuthorityUnavailable(
                    "inventory.db uses an unsupported schema; archive or remove it"
                )

            self._conn.create_collation(
                _UNICODE_CASEFOLD_COLLATION,
                _unicode_casefold,
            )
            self._conn.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
            self._conn.execute("PRAGMA foreign_keys = ON")
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
                self._conn.execute("PRAGMA synchronous = NORMAL")
            if is_empty:
                self._conn.executescript(_SCHEMA)
                self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self._conn.commit()

    def _application_tables(self) -> set[str]:
        return {
            str(row[0])
            for row in self._conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            )
        }

    def _has_exact_schema(self, tables: set[str]) -> bool:
        if tables != set(_EXPECTED_TABLE_COLUMNS):
            return False
        for table, expected in _EXPECTED_TABLE_COLUMNS.items():
            columns = tuple(
                str(row[1])
                for row in self._conn.execute(f'PRAGMA table_info("{table}")')
            )
            if columns != expected:
                return False
        return _schema_objects(self._conn) == _EXPECTED_SCHEMA_OBJECTS

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- 事务原语 -----------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """串行化写事务：业务行 + ledger + outbox 在此上下文内一起提交."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    # -- 只读 helper ---------------------------------------------------------

    def query_one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def query_all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # -- 常用读 -------------------------------------------------------------

    def get_lot(self, lot_id: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM inventory_lot WHERE lot_id = ?", (lot_id,))

    def lots_by_template_fifo(
        self,
        resource_template_uuid: str,
    ) -> list[dict[str, Any]]:
        """FIFO：按 created_at 升序（同毫秒按 rowid 插入序）返回可用批次."""
        return self.query_all(
            "SELECT * FROM inventory_lot WHERE resource_template_uuid = ? "
            "AND quarantined = 0 "
            "AND quantity_available > 0 ORDER BY created_at ASC, rowid ASC",
            (resource_template_uuid,),
        )

    def get_processed_command(self, command_id: str) -> dict[str, Any] | None:
        return self.query_one(
            "SELECT * FROM processed_command WHERE command_id = ?", (command_id,)
        )

    # -- 实验室布局（lab_meta / lab_zone / lab_placement） --------------------

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.query_one(
            "SELECT meta_value FROM lab_meta WHERE meta_key = ?", (key,)
        )
        return str(row["meta_value"]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO lab_meta(meta_key, meta_value) VALUES (?, ?) "
                "ON CONFLICT(meta_key) DO UPDATE SET meta_value = excluded.meta_value",
                (key, value),
            )

    def list_zones(self) -> list[dict[str, Any]]:
        return self.query_all("SELECT * FROM lab_zone ORDER BY zone_id ASC")

    def list_placements(self, zone_id: str = "") -> list[dict[str, Any]]:
        if zone_id:
            return self.query_all(
                "SELECT * FROM lab_placement WHERE zone_id = ? ORDER BY subject_id ASC",
                (zone_id,),
            )
        return self.query_all("SELECT * FROM lab_placement ORDER BY subject_id ASC")

    def get_placement(self, subject_id: str) -> dict[str, Any] | None:
        return self.query_one(
            "SELECT * FROM lab_placement WHERE subject_id = ?", (subject_id,)
        )

    # -- outbox / cursor -----------------------------------------------------

    def pending_outbox(
        self, after_sequence: int, limit: int = 100
    ) -> list[dict[str, Any]]:
        return self.query_all(
            "SELECT * FROM sync_outbox WHERE sequence > ? ORDER BY sequence ASC LIMIT ?",
            (after_sequence, limit),
        )

    def get_cursor(self, name: str = "cloud") -> int:
        row = self.query_one(
            "SELECT acked_sequence FROM sync_cursor WHERE cursor_name = ?", (name,)
        )
        return int(row["acked_sequence"]) if row else 0

    def set_cursor(self, name: str, acked_sequence: int, now_ms: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO sync_cursor(cursor_name, acked_sequence, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(cursor_name) DO UPDATE SET acked_sequence = excluded.acked_sequence, "
                "updated_at = excluded.updated_at",
                (name, acked_sequence, now_ms),
            )

    def max_outbox_sequence(self) -> int:
        row = self.query_one("SELECT COALESCE(MAX(sequence), 0) AS s FROM sync_outbox")
        return int(row["s"]) if row else 0

    # -- 事务内写 helper（必须在 transaction() 上下文中调用） -----------------

    @staticmethod
    def tx_insert_ledger(
        conn: sqlite3.Connection,
        occurred_at: int,
        op_type: str,
        aggregate_type: str,
        aggregate_id: str,
        delta: dict[str, Any],
        actor: str = "",
        reason: str = "",
        causation_id: str = "",
    ) -> None:
        conn.execute(
            "INSERT INTO inventory_ledger(occurred_at, op_type, aggregate_type, aggregate_id, "
            "delta_json, actor, reason, causation_id) VALUES (?,?,?,?,?,?,?,?)",
            (
                occurred_at,
                op_type,
                aggregate_type,
                aggregate_id,
                json.dumps(delta, ensure_ascii=False),
                actor,
                reason,
                causation_id,
            ),
        )

    @staticmethod
    def tx_insert_outbox(
        conn: sqlite3.Connection,
        event_id: str,
        edge_id: str,
        lab_id: str,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_version: int,
        event_type: str,
        occurred_at: int,
        causation_id: str,
        payload: dict[str, Any],
    ) -> int:
        cur = conn.execute(
            "INSERT INTO sync_outbox(event_id, edge_id, lab_id, aggregate_type, aggregate_id, "
            "aggregate_version, event_type, occurred_at, causation_id, payload_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                edge_id,
                lab_id,
                aggregate_type,
                aggregate_id,
                aggregate_version,
                event_type,
                occurred_at,
                causation_id,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        return int(cur.lastrowid or 0)
