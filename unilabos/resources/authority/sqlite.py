"""Material Authority 的 SQLite durable adapter。"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol

from .models import (
    MaterialAuthorityUnavailable,
    MaterialConflict,
    MaterialInvalidInput,
    MaterialNotFound,
    MaterialRecord,
    RuntimeAuthorityUnitOfWork,
    SiteRecord,
)

_SQLITE_BUSY_TIMEOUT_MS = 5000
_UNICODE_CASEFOLD_COLLATION = "UNICODE_CASEFOLD"


class _SQLiteRuntimeAuthorityUnitOfWork(RuntimeAuthorityUnitOfWork, Protocol):
    """SQLite adapter 安装 connection-local capability 的内部 seam。"""

    def create_collation(
        self,
        name: str,
        comparison: Callable[[str, str], int],
    ) -> None: ...


class _SQLiteRuntimeAuthorityCoordinator(Protocol):
    """SQLite adapter 所需的 transaction 与 authority-affinity capability。"""

    def transaction(
        self,
    ) -> AbstractContextManager[_SQLiteRuntimeAuthorityUnitOfWork]: ...

    def owns_unit_of_work(self, uow: RuntimeAuthorityUnitOfWork) -> bool: ...


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
    "DROP INDEX IF EXISTS ux_material_barcode_active_nonempty",
    """
CREATE UNIQUE INDEX IF NOT EXISTS ux_material_barcode_active_nonempty
    ON material(barcode COLLATE UNICODE_CASEFOLD)
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
    """
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
)
""",
    """
CREATE TABLE IF NOT EXISTS site_allowed_resource_template (
    site_uuid TEXT NOT NULL,
    resource_template_uuid TEXT NOT NULL,
    PRIMARY KEY(site_uuid, resource_template_uuid),
    FOREIGN KEY(site_uuid) REFERENCES site(uuid) ON DELETE CASCADE
)
""",
    """
CREATE UNIQUE INDEX IF NOT EXISTS ux_site_material_name_active
    ON site(material_uuid, name COLLATE UNICODE_CASEFOLD)
    WHERE deleted_at IS NULL
""",
    """
CREATE UNIQUE INDEX IF NOT EXISTS ux_site_occupied_material_active
    ON site(occupied_material_uuid)
    WHERE deleted_at IS NULL AND occupied_material_uuid IS NOT NULL
""",
    """
CREATE INDEX IF NOT EXISTS ix_site_material_order_active
    ON site(material_uuid, sort_order, create_time, uuid)
    WHERE deleted_at IS NULL
""",
)


def _unicode_casefold(left: str, right: str) -> int:
    left_folded = left.casefold()
    right_folded = right.casefold()
    return (left_folded > right_folded) - (left_folded < right_folded)


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


def _site_record(
    row: sqlite3.Row,
    allowed_resource_template_uuids: tuple[str, ...],
) -> SiteRecord:
    return SiteRecord(
        uuid=row["uuid"],
        create_time=row["create_time"],
        update_time=row["update_time"],
        deleted_at=row["deleted_at"],
        description=row["description"],
        meta_data=_json_object(row["meta_data"]),
        material_uuid=row["material_uuid"],
        name=row["name"],
        sort_order=row["sort_order"],
        allowed_resource_template_uuids=allowed_resource_template_uuids,
        occupied_material_uuid=row["occupied_material_uuid"],
        position_x=row["position_x"],
        position_y=row["position_y"],
        position_z=row["position_z"],
        depth=row["depth"],
        length=row["length"],
        width=row["width"],
        version=row["version"],
    )


def _read_site(
    uow: RuntimeAuthorityUnitOfWork,
    site_uuid: str,
) -> SiteRecord | None:
    row = uow.execute(
        "SELECT * FROM site WHERE uuid = ? AND deleted_at IS NULL",
        (site_uuid,),
    ).fetchone()
    if row is None:
        return None
    allowed_rows = uow.execute(
        """
        SELECT resource_template_uuid
        FROM site_allowed_resource_template
        WHERE site_uuid = ?
        ORDER BY resource_template_uuid
        """,
        (site_uuid,),
    ).fetchall()
    return _site_record(
        row,
        tuple(allowed_row["resource_template_uuid"] for allowed_row in allowed_rows),
    )


class _StandaloneRuntimeAuthority:
    """仅供独立 Material 部署/测试使用的 SQLite coordinator。"""

    def __init__(self, database_path: str | Path):
        self.path = str(database_path)
        connection: sqlite3.Connection | None = None
        try:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._lock = threading.RLock()
            connection = sqlite3.connect(
                self.path,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            with self._lock:
                connection.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
                connection.execute("PRAGMA foreign_keys = ON")
                if self.path != ":memory:":
                    connection.execute("PRAGMA journal_mode = WAL")
                    connection.execute("PRAGMA synchronous = NORMAL")
            self._connection = connection
        except BaseException:
            if connection is not None:
                connection.close()
            raise

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

    def owns_unit_of_work(self, uow: RuntimeAuthorityUnitOfWork) -> bool:
        with self._lock:
            return uow is self._connection and self._connection.in_transaction

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class SQLiteMaterialAdapter:
    """把 Material repository 装配到一个 runtime-authority coordinator。"""

    def __init__(self, database_path: str | Path):
        coordinator: _SQLiteRuntimeAuthorityCoordinator | None = None
        try:
            coordinator = _StandaloneRuntimeAuthority(database_path)
            self._configure(coordinator, owned=True)
        except (OSError, ValueError, sqlite3.Error):
            if coordinator is not None:
                coordinator.close()
            raise MaterialAuthorityUnavailable(
                "failed to initialize Material Authority"
            ) from None

    @classmethod
    def from_runtime_authority(
        cls,
        coordinator: _SQLiteRuntimeAuthorityCoordinator,
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
        coordinator: _SQLiteRuntimeAuthorityCoordinator,
        *,
        owned: bool,
    ) -> None:
        self._coordinator = coordinator
        self._owned_coordinator = coordinator if owned else None
        with coordinator.transaction() as uow:
            uow.create_collation(
                _UNICODE_CASEFOLD_COLLATION,
                _unicode_casefold,
            )
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
            if not self._coordinator.owns_unit_of_work(uow):
                raise MaterialAuthorityUnavailable(
                    "unit of work does not belong to Material Authority"
                )
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

    def create_site(
        self,
        *,
        site_uuid: str,
        description: str | None,
        meta_data: dict[str, Any],
        material_uuid: str,
        name: str,
        sort_order: int,
        allowed_resource_template_uuids: tuple[str, ...],
        occupied_material_uuid: str | None,
        position_x: float,
        position_y: float,
        position_z: float,
        depth: float,
        length: float,
        width: float,
        now: str,
        uow: RuntimeAuthorityUnitOfWork | None = None,
    ) -> SiteRecord:
        try:
            with self._with_uow(uow) as active_uow:
                owner = active_uow.execute(
                    """
                    SELECT resource_template_uuid
                    FROM material
                    WHERE uuid = ? AND deleted_at IS NULL
                    """,
                    (material_uuid,),
                ).fetchone()
                if owner is None:
                    raise MaterialNotFound("site owner material not found")

                if occupied_material_uuid is not None:
                    occupant = active_uow.execute(
                        """
                        SELECT resource_template_uuid
                        FROM material
                        WHERE uuid = ? AND deleted_at IS NULL
                        """,
                        (occupied_material_uuid,),
                    ).fetchone()
                    if occupant is None:
                        raise MaterialNotFound("site occupant material not found")
                    if (
                        allowed_resource_template_uuids
                        and occupant["resource_template_uuid"]
                        not in allowed_resource_template_uuids
                    ):
                        raise MaterialInvalidInput(
                            "occupied material template is not allowed by site"
                        )

                active_uow.execute(
                    """
                    INSERT INTO site(
                        uuid, create_time, update_time, deleted_at,
                        description, meta_data, material_uuid, name,
                        sort_order, occupied_material_uuid,
                        position_x, position_y, position_z,
                        depth, length, width, version
                    ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        site_uuid,
                        now,
                        now,
                        description,
                        json.dumps(
                            meta_data, ensure_ascii=False, separators=(",", ":")
                        ),
                        material_uuid,
                        name,
                        sort_order,
                        occupied_material_uuid,
                        position_x,
                        position_y,
                        position_z,
                        depth,
                        length,
                        width,
                    ),
                )
                for template_uuid in allowed_resource_template_uuids:
                    active_uow.execute(
                        """
                        INSERT INTO site_allowed_resource_template(
                            site_uuid, resource_template_uuid
                        ) VALUES (?, ?)
                        """,
                        (site_uuid, template_uuid),
                    )
                site = _read_site(active_uow, site_uuid)
        except sqlite3.IntegrityError:
            raise MaterialConflict("site identity or placement conflicts") from None
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable("failed to create site") from None
        if site is None:
            raise MaterialAuthorityUnavailable("created site is not readable")
        return site

    def get_site(
        self,
        site_uuid: str,
        *,
        uow: RuntimeAuthorityUnitOfWork | None = None,
    ) -> SiteRecord | None:
        try:
            with self._with_uow(uow) as active_uow:
                return _read_site(active_uow, site_uuid)
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable("failed to read site") from None


class _BorrowedUnitOfWork:
    """借用调用者 UoW；退出时不 commit、rollback 或 close。"""

    def __init__(self, uow: RuntimeAuthorityUnitOfWork):
        self._uow = uow

    def __enter__(self) -> RuntimeAuthorityUnitOfWork:
        return self._uow

    def __exit__(self, *_exc: object) -> None:
        return None


__all__ = ["SQLiteMaterialAdapter"]
