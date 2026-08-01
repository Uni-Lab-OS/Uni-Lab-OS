"""Material Authority 的 SQLite durable adapter。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, Protocol

from .models import (
    MaterialAuthorityUnavailable,
    MaterialConflict,
    MaterialInvalidInput,
    MaterialNotFound,
    MaterialRecord,
    MaterialReservationOutcome,
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

    def current_unit_of_work(self) -> RuntimeAuthorityUnitOfWork | None: ...


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
    """
CREATE TABLE IF NOT EXISTS material_reservation (
    uuid TEXT PRIMARY KEY,
    workflow_task_uuid TEXT NOT NULL,
    set_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'released')),
    create_time TEXT NOT NULL,
    released_at TEXT,
    FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid) ON DELETE CASCADE,
    CHECK (
        (status = 'active' AND released_at IS NULL)
        OR (status = 'released' AND released_at IS NOT NULL)
    )
)
""",
    """
CREATE UNIQUE INDEX IF NOT EXISTS ux_material_reservation_task_active
    ON material_reservation(workflow_task_uuid)
    WHERE status = 'active'
""",
    """
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
)
""",
    """
CREATE UNIQUE INDEX IF NOT EXISTS ux_material_reservation_member_active
    ON material_reservation_member(material_uuid)
    WHERE released_at IS NULL
""",
    """
CREATE INDEX IF NOT EXISTS ix_material_reservation_member_root
    ON material_reservation_member(root_material_uuid)
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


def _reservation_fingerprint(material_uuids: tuple[str, ...]) -> str:
    payload = json.dumps(
        list(material_uuids),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _active_reservation_outcome(
    uow: RuntimeAuthorityUnitOfWork,
    task_uuid: str,
) -> MaterialReservationOutcome | None:
    header = uow.execute(
        """
        SELECT uuid, set_fingerprint
        FROM material_reservation
        WHERE workflow_task_uuid = ? AND status = 'active'
        """,
        (task_uuid,),
    ).fetchone()
    if header is None:
        return None
    rows = uow.execute(
        """
        SELECT material_uuid, root_material_uuid
        FROM material_reservation_member
        WHERE reservation_uuid = ? AND released_at IS NULL
        ORDER BY material_uuid
        """,
        (header["uuid"],),
    ).fetchall()
    material_uuids = tuple(row["material_uuid"] for row in rows)
    return MaterialReservationOutcome(
        acquired=True,
        reservation_uuid=header["uuid"],
        set_fingerprint=header["set_fingerprint"],
        material_uuids=material_uuids,
    )


def _expand_reservation_roots(
    uow: RuntimeAuthorityUnitOfWork,
    root_material_uuids: tuple[str, ...],
) -> tuple[tuple[str, str, int], ...]:
    members: dict[str, tuple[str, int]] = {}
    for root_uuid in root_material_uuids:
        rows = uow.execute(
            """
            WITH RECURSIVE subtree(uuid) AS (
                SELECT uuid
                FROM material
                WHERE uuid = ? AND deleted_at IS NULL
                UNION
                SELECT child.uuid
                FROM material AS child
                JOIN subtree ON child.parent_uuid = subtree.uuid
                WHERE child.deleted_at IS NULL
            )
            SELECT material.uuid, material.material_kind,
                   material.disposition, material.version
            FROM material
            JOIN subtree ON subtree.uuid = material.uuid
            ORDER BY material.uuid
            """,
            (root_uuid,),
        ).fetchall()
        if not rows or all(row["uuid"] != root_uuid for row in rows):
            raise MaterialNotFound(f"material {root_uuid} not found")
        for row in rows:
            if row["material_kind"] != "business":
                raise MaterialInvalidInput(
                    "Material reservation requires business Materials"
                )
            if row["disposition"] != "active":
                raise MaterialConflict("Material is not runnable")
            existing = members.get(row["uuid"])
            if existing is None or root_uuid < existing[0]:
                members[row["uuid"]] = (root_uuid, int(row["version"]))
    return tuple(
        (material_uuid, root_uuid, version)
        for material_uuid, (root_uuid, version) in sorted(members.items())
    )


@contextmanager
def _site_create_savepoint(
    uow: RuntimeAuthorityUnitOfWork,
) -> Iterator[None]:
    """让多语句 Site 创建在借用的 UoW 内保持原子性。"""

    uow.execute("SAVEPOINT unilab_site_create")
    try:
        yield
    except BaseException:
        try:
            uow.execute("ROLLBACK TO SAVEPOINT unilab_site_create")
        finally:
            uow.execute("RELEASE SAVEPOINT unilab_site_create")
        raise
    else:
        uow.execute("RELEASE SAVEPOINT unilab_site_create")


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

    def current_unit_of_work(self) -> RuntimeAuthorityUnitOfWork | None:
        with self._lock:
            return self._connection if self._connection.in_transaction else None

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
        current_uow = self._coordinator.current_unit_of_work()
        if current_uow is not None:
            return _BorrowedUnitOfWork(current_uow)
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
            with (
                self._with_uow(uow) as active_uow,
                _site_create_savepoint(active_uow),
            ):
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
                    if occupied_material_uuid == material_uuid:
                        raise MaterialConflict("site placement would create a cycle")
                    if (
                        allowed_resource_template_uuids
                        and occupant["resource_template_uuid"]
                        not in allowed_resource_template_uuids
                    ):
                        raise MaterialInvalidInput(
                            "occupied material template is not allowed by site"
                        )
                    would_cycle = active_uow.execute(
                        """
                            WITH RECURSIVE
                            edges(source_uuid, target_uuid) AS (
                                SELECT parent_uuid, uuid
                                FROM material
                                WHERE parent_uuid IS NOT NULL AND deleted_at IS NULL
                                UNION ALL
                                SELECT material_uuid, occupied_material_uuid
                                FROM site
                                WHERE occupied_material_uuid IS NOT NULL
                                  AND deleted_at IS NULL
                            ),
                            reachable(uuid) AS (
                                SELECT ?
                                UNION
                                SELECT edges.target_uuid
                                FROM edges
                                JOIN reachable ON edges.source_uuid = reachable.uuid
                            )
                            SELECT 1 FROM reachable WHERE uuid = ? LIMIT 1
                            """,
                        (occupied_material_uuid, material_uuid),
                    ).fetchone()
                    if would_cycle is not None:
                        raise MaterialConflict("site placement would create a cycle")

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
                            meta_data,
                            ensure_ascii=False,
                            separators=(",", ":"),
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

    def list_sites(
        self,
        material_uuid: str,
        *,
        uow: RuntimeAuthorityUnitOfWork | None = None,
    ) -> tuple[SiteRecord, ...]:
        try:
            with self._with_uow(uow) as active_uow:
                rows = active_uow.execute(
                    """
                    SELECT uuid
                    FROM site
                    WHERE material_uuid = ? AND deleted_at IS NULL
                    ORDER BY sort_order, uuid
                    """,
                    (material_uuid,),
                ).fetchall()
                sites = tuple(_read_site(active_uow, str(row["uuid"])) for row in rows)
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable("failed to list sites") from None
        if any(site is None for site in sites):
            raise MaterialAuthorityUnavailable("listed site is not readable")
        return tuple(site for site in sites if site is not None)

    def reserve_task_materials(
        self,
        *,
        reservation_uuid: str,
        task_uuid: str,
        root_material_uuids: tuple[str, ...],
        now: str,
        uow: RuntimeAuthorityUnitOfWork,
    ) -> MaterialReservationOutcome:
        """在借用的 Task transaction 内写入一个 all-or-none Reservation。"""

        try:
            with self._with_uow(uow) as active_uow:
                task = active_uow.execute(
                    """
                    SELECT 1 FROM workflow_task
                    WHERE uuid = ? AND deleted_at IS NULL
                    """,
                    (task_uuid,),
                ).fetchone()
                if task is None:
                    raise MaterialNotFound(f"workflow task {task_uuid} not found")

                members = _expand_reservation_roots(
                    active_uow,
                    root_material_uuids,
                )
                material_uuids = tuple(member[0] for member in members)
                fingerprint = _reservation_fingerprint(material_uuids)
                existing = _active_reservation_outcome(active_uow, task_uuid)
                if existing is not None:
                    if (
                        existing.set_fingerprint != fingerprint
                        or existing.material_uuids != material_uuids
                    ):
                        raise MaterialConflict(
                            "task already owns a different Material reservation"
                        )
                    return existing

                active_uow.execute("SAVEPOINT unilab_task_material_reservation")
                try:
                    active_uow.execute(
                        """
                        INSERT INTO material_reservation(
                            uuid, workflow_task_uuid, set_fingerprint,
                            status, create_time, released_at
                        ) VALUES (?, ?, ?, 'active', ?, NULL)
                        """,
                        (reservation_uuid, task_uuid, fingerprint, now),
                    )
                    for material_uuid, root_uuid, acquired_version in members:
                        active_uow.execute(
                            """
                            INSERT INTO material_reservation_member(
                                reservation_uuid, material_uuid,
                                root_material_uuid, acquired_version, released_at
                            ) VALUES (?, ?, ?, ?, NULL)
                            """,
                            (
                                reservation_uuid,
                                material_uuid,
                                root_uuid,
                                acquired_version,
                            ),
                        )
                except sqlite3.IntegrityError:
                    try:
                        active_uow.execute(
                            "ROLLBACK TO SAVEPOINT unilab_task_material_reservation"
                        )
                    finally:
                        active_uow.execute(
                            "RELEASE SAVEPOINT unilab_task_material_reservation"
                        )
                    marks = ",".join("?" for _ in material_uuids)
                    conflict = active_uow.execute(
                        f"""
                        SELECT 1
                        FROM material_reservation_member
                        WHERE released_at IS NULL
                          AND material_uuid IN ({marks})
                        LIMIT 1
                        """,
                        material_uuids,
                    ).fetchone()
                    if conflict is None:
                        raise MaterialConflict(
                            "Material reservation violates durable constraints"
                        ) from None
                    return MaterialReservationOutcome(
                        acquired=False,
                        reservation_uuid=None,
                        set_fingerprint=fingerprint,
                        material_uuids=material_uuids,
                    )
                except BaseException:
                    try:
                        active_uow.execute(
                            "ROLLBACK TO SAVEPOINT unilab_task_material_reservation"
                        )
                    finally:
                        active_uow.execute(
                            "RELEASE SAVEPOINT unilab_task_material_reservation"
                        )
                    raise
                else:
                    active_uow.execute(
                        "RELEASE SAVEPOINT unilab_task_material_reservation"
                    )
                    return MaterialReservationOutcome(
                        acquired=True,
                        reservation_uuid=reservation_uuid,
                        set_fingerprint=fingerprint,
                        material_uuids=material_uuids,
                    )
        except MaterialConflict:
            raise
        except (MaterialInvalidInput, MaterialNotFound):
            raise
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to reserve task Materials"
            ) from None

    def has_complete_task_reservation(
        self,
        *,
        task_uuid: str,
        root_material_uuids: tuple[str, ...],
        uow: RuntimeAuthorityUnitOfWork,
    ) -> bool:
        """检查活动 header、完整 members 与 acquired version。"""

        try:
            with self._with_uow(uow) as active_uow:
                existing = _active_reservation_outcome(active_uow, task_uuid)
                if existing is None:
                    return False
                members = _expand_reservation_roots(
                    active_uow,
                    root_material_uuids,
                )
                expected_material_uuids = tuple(member[0] for member in members)
                if (
                    not existing.material_uuids
                    or existing.material_uuids != expected_material_uuids
                ):
                    return False
                if existing.set_fingerprint != _reservation_fingerprint(
                    expected_material_uuids
                ):
                    return False
                marks = ",".join("?" for _ in existing.material_uuids)
                valid_count = active_uow.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM material_reservation_member AS member
                    JOIN material
                      ON material.uuid = member.material_uuid
                     AND material.deleted_at IS NULL
                     AND material.material_kind = 'business'
                     AND material.disposition = 'active'
                     AND material.version = member.acquired_version
                    WHERE member.reservation_uuid = ?
                      AND member.released_at IS NULL
                      AND member.material_uuid IN ({marks})
                    """,
                    (existing.reservation_uuid, *existing.material_uuids),
                ).fetchone()[0]
                return int(valid_count) == len(existing.material_uuids)
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to check task Material reservation"
            ) from None


class _BorrowedUnitOfWork:
    """借用调用者 UoW；退出时不 commit、rollback 或 close。"""

    def __init__(self, uow: RuntimeAuthorityUnitOfWork):
        self._uow = uow

    def __enter__(self) -> RuntimeAuthorityUnitOfWork:
        return self._uow

    def __exit__(self, *_exc: object) -> None:
        return None


__all__ = ["SQLiteMaterialAdapter"]
