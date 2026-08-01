"""Material Authority 的 SQLite durable adapter。"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .models import (
    MaterialAuthorityUnavailable,
    MaterialConflict,
    MaterialRecord,
)

_SQLITE_BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
PRAGMA foreign_keys = ON;

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
    class TEXT NOT NULL DEFAULT '',
    barcode TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
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
    ON material(LOWER(barcode))
    WHERE deleted_at IS NULL AND barcode <> '';

CREATE INDEX IF NOT EXISTS ix_material_template_active
    ON material(resource_template_uuid)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_material_parent_active
    ON material(parent_uuid)
    WHERE deleted_at IS NULL;
"""


def _json_object(value: str) -> dict[str, Any]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise MaterialAuthorityUnavailable("material JSON field is not an object")
    return decoded


def _material_record(row: sqlite3.Row) -> MaterialRecord:
    return MaterialRecord(
        uuid=row["uuid"],
        create_time=row["create_time"],
        update_time=row["update_time"],
        deleted_at=row["deleted_at"],
        description=row["description"],
        meta_data=_json_object(row["meta_data"]),
        resource_template_uuid=row["resource_template_uuid"],
        parent_uuid=row["parent_uuid"],
        resource_class=row["class"],
        barcode=row["barcode"],
        name=row["name"],
        config=_json_object(row["config"]),
        data=_json_object(row["data"]),
        disposition=row["disposition"],
        material_kind=row["material_kind"],
        version=row["version"],
    )


class SQLiteMaterialAdapter:
    """以单连接和进程内锁持有 Material SQLite partition。"""

    def __init__(self, database_path: str | Path):
        self.path = str(database_path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._connection = sqlite3.connect(
                self.path,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            with self._lock:
                self._connection.execute(
                    f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}"
                )
                self._connection.execute("PRAGMA foreign_keys = ON")
                if self.path != ":memory:":
                    self._connection.execute("PRAGMA journal_mode = WAL")
                    self._connection.execute("PRAGMA synchronous = NORMAL")
                self._connection.executescript(_SCHEMA)
        except sqlite3.Error:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise MaterialAuthorityUnavailable(
                "failed to initialize Material Authority"
            ) from None

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def create_business_material(
        self,
        *,
        material_uuid: str,
        resource_template_uuid: str,
        barcode: str,
        now: str,
    ) -> MaterialRecord:
        try:
            with self._lock:
                self._connection.execute("BEGIN IMMEDIATE")
                try:
                    self._connection.execute(
                        """
                        INSERT INTO material(
                            uuid, create_time, update_time, deleted_at,
                            description, meta_data, resource_template_uuid,
                            parent_uuid, class, barcode, name, config, data,
                            disposition, material_kind, version
                        ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, NULL, '', ?, '',
                                  '{}', '{}', 'active', 'business', 1)
                        """,
                        (material_uuid, now, now, resource_template_uuid, barcode),
                    )
                except BaseException:
                    self._connection.rollback()
                    raise
                else:
                    self._connection.commit()
                row = self._connection.execute(
                    "SELECT * FROM material WHERE uuid = ? AND deleted_at IS NULL",
                    (material_uuid,),
                ).fetchone()
        except sqlite3.IntegrityError:
            raise MaterialConflict(
                "material identity or barcode already exists"
            ) from None
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable("failed to create material") from None
        if row is None:
            raise MaterialAuthorityUnavailable("created material is not readable")
        return _material_record(row)

    def get_material(self, material_uuid: str) -> MaterialRecord | None:
        try:
            with self._lock:
                row = self._connection.execute(
                    "SELECT * FROM material WHERE uuid = ? AND deleted_at IS NULL",
                    (material_uuid,),
                ).fetchone()
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable("failed to read material") from None
        return _material_record(row) if row is not None else None


__all__ = ["SQLiteMaterialAdapter"]
