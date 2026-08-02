"""SQLite WAL 持久化层.

单仓储分区单写者：所有写事务经进程内锁 + BEGIN IMMEDIATE 串行化，
业务变更、ledger、outbox 必须在同一事务提交（由 service 层保证，
store 只提供 transaction() 原语与行级 helper）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from unilabos.app.scheduler.inventory.domain import MaterialAuthorityUnavailable

SCHEMA_VERSION = 6
_PREVIOUS_SCHEMA_VERSION = 5

_UNICODE_CASEFOLD_COLLATION = "UNICODE_CASEFOLD"
_SQLITE_BUSY_TIMEOUT_MS = 5_000

_EXPECTED_V5_TABLE_COLUMNS = {
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


_SCHEMA_V5 = """
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

_SCHEMA_V6_ADDITIONS = """
CREATE TABLE material_claim (
    uuid TEXT PRIMARY KEY,
    workflow_task_uuid TEXT NOT NULL,
    workflow_node_job_uuid TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt > 0),
    set_fingerprint TEXT NOT NULL,
    fencing_token INTEGER NOT NULL UNIQUE CHECK (fencing_token > 0),
    state TEXT NOT NULL
        CHECK (state IN ('reserved', 'running', 'uncertain', 'released')),
    uncertainty_reason TEXT,
    acquired_at TEXT NOT NULL,
    create_time TEXT NOT NULL,
    running_at TEXT,
    release_proof_kind TEXT,
    release_proof_fingerprint TEXT,
    release_reason TEXT,
    terminal_changeset_uuid TEXT,
    workflow_terminal_fingerprint TEXT,
    release_command_uuid TEXT UNIQUE,
    released_at TEXT,
    update_time TEXT NOT NULL,
    UNIQUE(workflow_node_job_uuid, attempt),
    CHECK (
        (state = 'released'
         AND release_proof_kind IS NOT NULL
         AND release_proof_fingerprint IS NOT NULL
         AND release_reason IS NOT NULL
         AND release_command_uuid IS NOT NULL
         AND released_at IS NOT NULL)
        OR
        (state <> 'released'
         AND release_proof_kind IS NULL
         AND release_proof_fingerprint IS NULL
         AND release_reason IS NULL
         AND release_command_uuid IS NULL
         AND released_at IS NULL)
    ),
    CHECK (
        release_proof_kind IS NULL
        OR release_proof_kind IN (
            'not_submitted', 'terminal_settled', 'reconciled_terminal'
        )
    ),
    CHECK (
        release_proof_kind NOT IN ('terminal_settled', 'reconciled_terminal')
        OR (terminal_changeset_uuid IS NOT NULL
            AND workflow_terminal_fingerprint IS NOT NULL)
    ),
    CHECK (
        release_proof_kind <> 'not_submitted'
        OR terminal_changeset_uuid IS NULL
    )
);
CREATE INDEX ix_material_claim_task_state
    ON material_claim(workflow_task_uuid, state, create_time, uuid);
CREATE INDEX ix_material_claim_state
    ON material_claim(state, create_time, uuid);

CREATE TABLE material_claim_member (
    claim_uuid TEXT NOT NULL,
    resource_kind TEXT NOT NULL
        CHECK (resource_kind IN (
            'device_material', 'business_material', 'site'
        )),
    resource_uuid TEXT NOT NULL,
    acquired_version INTEGER NOT NULL CHECK (acquired_version > 0),
    expected_version INTEGER NOT NULL CHECK (expected_version > 0),
    released_at TEXT,
    PRIMARY KEY(claim_uuid, resource_kind, resource_uuid),
    FOREIGN KEY(claim_uuid) REFERENCES material_claim(uuid) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX ux_material_claim_member_active
    ON material_claim_member(resource_kind, resource_uuid)
    WHERE released_at IS NULL;
CREATE INDEX ix_material_claim_member_claim
    ON material_claim_member(claim_uuid, resource_kind, resource_uuid);

CREATE TABLE material_claim_fence_sequence (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_uuid TEXT NOT NULL UNIQUE,
    FOREIGN KEY(claim_uuid) REFERENCES material_claim(uuid) ON DELETE RESTRICT
);

CREATE TABLE material_resource_fence (
    resource_kind TEXT NOT NULL
        CHECK (resource_kind IN (
            'device_material', 'business_material', 'site'
        )),
    resource_uuid TEXT NOT NULL,
    fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
    claim_uuid TEXT NOT NULL,
    update_time TEXT NOT NULL,
    PRIMARY KEY(resource_kind, resource_uuid),
    FOREIGN KEY(claim_uuid) REFERENCES material_claim(uuid) ON DELETE RESTRICT
);

CREATE TABLE material_changeset (
    uuid TEXT PRIMARY KEY,
    workflow_task_uuid TEXT NOT NULL,
    workflow_node_job_uuid TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt > 0),
    claim_uuid TEXT NOT NULL,
    fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
    effect_identity TEXT NOT NULL,
    deterministic_fingerprint TEXT NOT NULL,
    outcome TEXT NOT NULL
        CHECK (outcome IN ('succeeded', 'failed', 'canceled', 'timeout')),
    result_json TEXT NOT NULL
        CHECK (json_valid(result_json) AND json_type(result_json) = 'object'),
    outbox_sequence INTEGER NOT NULL CHECK (outbox_sequence > 0),
    create_time TEXT NOT NULL,
    UNIQUE(workflow_node_job_uuid, attempt, effect_identity),
    FOREIGN KEY(claim_uuid) REFERENCES material_claim(uuid) ON DELETE RESTRICT
);
CREATE INDEX ix_material_changeset_claim
    ON material_changeset(claim_uuid, create_time, uuid);

CREATE TABLE material_changeset_effect (
    changeset_uuid TEXT NOT NULL,
    effect_key TEXT NOT NULL,
    resource_kind TEXT NOT NULL
        CHECK (resource_kind IN ('business_material', 'site')),
    resource_uuid TEXT NOT NULL,
    operation TEXT NOT NULL
        CHECK (operation IN (
            'create', 'update', 'reparent', 'soft_delete', 'set_occupancy'
        )),
    expected_version INTEGER CHECK (expected_version IS NULL OR expected_version > 0),
    before_json TEXT NOT NULL
        CHECK (json_valid(before_json) AND json_type(before_json) = 'object'),
    after_json TEXT NOT NULL
        CHECK (json_valid(after_json) AND json_type(after_json) = 'object'),
    PRIMARY KEY(changeset_uuid, effect_key),
    FOREIGN KEY(changeset_uuid) REFERENCES material_changeset(uuid)
        ON DELETE RESTRICT
);
"""

_SCHEMA = _SCHEMA_V5 + _SCHEMA_V6_ADDITIONS

_EXPECTED_TABLE_COLUMNS = {
    **_EXPECTED_V5_TABLE_COLUMNS,
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


def _canonical_schema_objects(
    schema: str,
) -> tuple[tuple[str, str, str, str], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.create_collation(
            _UNICODE_CASEFOLD_COLLATION,
            _unicode_casefold,
        )
        connection.executescript(schema)
        return _schema_objects(connection)
    finally:
        connection.close()


_EXPECTED_V5_SCHEMA_OBJECTS = _canonical_schema_objects(_SCHEMA_V5)
_EXPECTED_SCHEMA_OBJECTS = _canonical_schema_objects(_SCHEMA)


class InventoryStore:
    """SQLite WAL 存储：单连接 + 进程内写锁（单写者）."""

    def __init__(
        self,
        path: str = ":memory:",
        *,
        migration_fault_hook: Callable[[str], None] | None = None,
    ):
        self.path = path
        self._lock = threading.RLock()
        self._migration_fault_hook = migration_fault_hook
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._open_exact_schema()
        except BaseException:
            self._conn.close()
            raise

    def _open_exact_schema(self) -> None:
        """Create v6, migrate exact v5 atomically, or reject mixed/corrupt data."""

        with self._lock:
            self._conn.create_collation(
                _UNICODE_CASEFOLD_COLLATION,
                _unicode_casefold,
            )
            self._conn.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
            self._conn.execute("PRAGMA foreign_keys = ON")
            current = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            tables = self._application_tables()
            is_empty = current == 0 and not tables
            is_v6 = current == SCHEMA_VERSION and self._has_exact_schema(
                tables,
                _EXPECTED_TABLE_COLUMNS,
                _EXPECTED_SCHEMA_OBJECTS,
            )
            is_v5 = current == _PREVIOUS_SCHEMA_VERSION and self._has_exact_schema(
                tables,
                _EXPECTED_V5_TABLE_COLUMNS,
                _EXPECTED_V5_SCHEMA_OBJECTS,
            )
            if not is_empty and not is_v6 and not is_v5:
                raise MaterialAuthorityUnavailable(
                    "inventory.db uses an unsupported schema; archive or remove it"
                )

            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
                self._conn.execute("PRAGMA synchronous = NORMAL")
            if is_empty:
                self._conn.executescript(_SCHEMA)
                self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self._conn.commit()
            elif is_v5:
                self._migrate_v5_to_v6()

    @staticmethod
    def _statements(script: str) -> tuple[str, ...]:
        """Split trusted DDL without letting ``executescript`` commit our tx."""

        statements: list[str] = []
        pending = ""
        for line in script.splitlines(keepends=True):
            pending += line
            if sqlite3.complete_statement(pending):
                statement = pending.strip()
                if statement:
                    statements.append(statement)
                pending = ""
        if pending.strip():
            raise MaterialAuthorityUnavailable("inventory v6 migration DDL is invalid")
        return tuple(statements)

    def _migrate_v5_to_v6(self) -> None:
        """Add M1EF authority tables while preserving every accepted v5 row."""

        try:
            self._conn.execute("BEGIN EXCLUSIVE")
            self._inject_migration_fault("before_v6_ddl")
            for index, statement in enumerate(self._statements(_SCHEMA_V6_ADDITIONS)):
                self._conn.execute(statement)
                if index == 0:
                    self._inject_migration_fault("after_first_v6_ddl")
            self._inject_migration_fault("after_v6_ddl")
            self._conn.execute(
                """
                INSERT INTO lab_meta(meta_key, meta_value) VALUES (?, ?)
                ON CONFLICT(meta_key) DO UPDATE SET meta_value = excluded.meta_value
                """,
                ("inventory_schema_migration", "v5-to-v6:m1ef"),
            )
            self._inject_migration_fault("after_schema_receipt")
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._inject_migration_fault("after_user_version")
            tables = self._application_tables()
            if not self._has_exact_schema(
                tables,
                _EXPECTED_TABLE_COLUMNS,
                _EXPECTED_SCHEMA_OBJECTS,
            ):
                raise MaterialAuthorityUnavailable(
                    "inventory.db v5-to-v6 migration did not produce exact schema"
                )
            self._inject_migration_fault("after_exact_schema_audit")
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def _inject_migration_fault(self, stage: str) -> None:
        """Expose deterministic crash windows without changing normal startup."""

        if self._migration_fault_hook is not None:
            self._migration_fault_hook(stage)

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

    def _has_exact_schema(
        self,
        tables: set[str],
        expected_columns: dict[str, tuple[str, ...]],
        expected_objects: tuple[tuple[str, str, str, str], ...],
    ) -> bool:
        if tables != set(expected_columns):
            return False
        for table, expected in expected_columns.items():
            columns = tuple(
                str(row[1])
                for row in self._conn.execute(f'PRAGMA table_info("{table}")')
            )
            if columns != expected:
                return False
        return _schema_objects(self._conn) == expected_objects

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
            "SELECT * FROM sync_outbox WHERE sequence > ? "
            "ORDER BY sequence ASC LIMIT ?",
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
                "INSERT INTO sync_cursor(cursor_name, acked_sequence, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(cursor_name) DO UPDATE SET "
                "acked_sequence = excluded.acked_sequence, "
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
            "INSERT INTO inventory_ledger(occurred_at, op_type, aggregate_type, "
            "aggregate_id, "
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
            "INSERT INTO sync_outbox(event_id, edge_id, lab_id, aggregate_type, "
            "aggregate_id, "
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
