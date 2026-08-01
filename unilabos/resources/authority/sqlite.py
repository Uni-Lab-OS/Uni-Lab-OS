"""Material Authority 的 SQLite durable adapter。"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import (
    MaterialAuthorityUnavailable,
    MaterialConflict,
    MaterialRecord,
    RuntimeAuthorityCoordinator,
    RuntimeAuthorityUnitOfWork,
)

_SQLITE_BUSY_TIMEOUT_MS = 5000

_SCHEMA_STATEMENTS = (
    """
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
)
""",
    """
CREATE UNIQUE INDEX IF NOT EXISTS ux_material_barcode_active_nonempty
    ON material(LOWER(barcode))
    WHERE deleted_at IS NULL AND barcode <> ''
""",
    """
CREATE INDEX IF NOT EXISTS ix_material_template_active
    ON material(resource_template_uuid)
    WHERE deleted_at IS NULL
""",
    """
CREATE INDEX IF NOT EXISTS ix_material_parent_active
    ON material(parent_uuid)
    WHERE deleted_at IS NULL
""",
)


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
        klass=row["class"],
        barcode=row["barcode"],
        name=row["name"],
        config=_json_object(row["config"]),
        data=_json_object(row["data"]),
        disposition=row["disposition"],
        material_kind=row["material_kind"],
        version=row["version"],
    )


class _StandaloneRuntimeAuthority:
    """仅供独立 Material 部署/测试使用的 SQLite coordinator。"""

    def __init__(self, database_path: str | Path):
        self.path = str(database_path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
            self._connection.execute("PRAGMA foreign_keys = ON")
            if self.path != ":memory:":
                self._connection.execute("PRAGMA journal_mode = WAL")
                self._connection.execute("PRAGMA synchronous = NORMAL")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class SQLiteMaterialAdapter:
    """把 Material repository 装配到一个 runtime-authority coordinator。"""

    def __init__(self, database_path: str | Path):
        coordinator: RuntimeAuthorityCoordinator | None = None
        try:
            coordinator = _StandaloneRuntimeAuthority(database_path)
            self._configure(coordinator, owned=True)
        except sqlite3.Error:
            if coordinator is not None:
                coordinator.close()
            raise MaterialAuthorityUnavailable(
                "failed to initialize Material Authority"
            ) from None

    @classmethod
    def from_runtime_authority(
        cls,
        coordinator: RuntimeAuthorityCoordinator,
    ) -> SQLiteMaterialAdapter:
        """绑定已有 coordinator；adapter 不取得 connection/close ownership。"""

        adapter = cls.__new__(cls)
        try:
            adapter._configure(coordinator, owned=False)
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to initialize Material Authority"
            ) from None
        return adapter

    def _configure(
        self,
        coordinator: RuntimeAuthorityCoordinator,
        *,
        owned: bool,
    ) -> None:
        self._coordinator = coordinator
        self._owned_coordinator = coordinator if owned else None
        with coordinator.transaction() as uow:
            for statement in _SCHEMA_STATEMENTS:
                uow.execute(statement)

    def close(self) -> None:
        if self._owned_coordinator is not None:
            self._owned_coordinator.close()

    def _with_uow(
        self,
        uow: RuntimeAuthorityUnitOfWork | None,
    ) -> AbstractContextManager[RuntimeAuthorityUnitOfWork]:
        if uow is not None:
            return _BorrowedUnitOfWork(uow)
        return self._coordinator.transaction()

    def create_business_material(
        self,
        *,
        material_uuid: str,
        resource_template_uuid: str,
        resource_class: str,
        barcode: str,
        name: str,
        description: str | None,
        meta_data: dict[str, Any],
        config: dict[str, Any],
        data: dict[str, Any],
        now: str,
        uow: RuntimeAuthorityUnitOfWork | None = None,
    ) -> MaterialRecord:
        try:
            with self._with_uow(uow) as active_uow:
                active_uow.execute(
                    """
                        INSERT INTO material(
                            uuid, create_time, update_time, deleted_at,
                            description, meta_data, resource_template_uuid,
                            parent_uuid, class, barcode, name, config, data,
                            disposition, material_kind, version
                        ) VALUES (?, ?, ?, NULL, ?, ?, ?, NULL, ?, ?, ?, ?, ?,
                                  'active', 'business', 1)
                        """,
                    (
                        material_uuid,
                        now,
                        now,
                        description,
                        json.dumps(
                            meta_data, ensure_ascii=False, separators=(",", ":")
                        ),
                        resource_template_uuid,
                        resource_class,
                        barcode,
                        name,
                        json.dumps(config, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                row = active_uow.execute(
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

    def get_material(
        self,
        material_uuid: str,
        *,
        uow: RuntimeAuthorityUnitOfWork | None = None,
    ) -> MaterialRecord | None:
        try:
            with self._with_uow(uow) as active_uow:
                row = active_uow.execute(
                    "SELECT * FROM material WHERE uuid = ? AND deleted_at IS NULL",
                    (material_uuid,),
                ).fetchone()
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable("failed to read material") from None
        return _material_record(row) if row is not None else None


class _BorrowedUnitOfWork:
    """借用调用者 UoW；退出时不 commit、rollback 或 close。"""

    def __init__(self, uow: RuntimeAuthorityUnitOfWork):
        self._uow = uow

    def __enter__(self) -> RuntimeAuthorityUnitOfWork:
        return self._uow

    def __exit__(self, *_exc: object) -> None:
        return None


__all__ = ["SQLiteMaterialAdapter"]
