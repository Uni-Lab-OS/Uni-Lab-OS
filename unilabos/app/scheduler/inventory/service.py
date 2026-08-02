"""仓储业务写操作.

每个写操作 = 单个 SQLite 事务：业务行更新 + inventory_ledger + sync_outbox 一起提交。
领域不变量在此层强制（数量非负 / available+reserved<=total / barcode active 唯一 /
(workflow_id,node_id,attempt) 幂等 / move 不改数量）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

from unilabos.app.scheduler.inventory.domain import (
    InvariantViolation,
    InventoryEvent,
    MaterialAuthorityUnavailable,
    MaterialConflict,
    MaterialInvalidInput,
    MaterialNotFound,
    MaterialRecord,
    ResourceSlotResolution,
    ResourceTemplateIdentity,
    SiteRecord,
    TaskMaterialAdmissionCommand,
    TaskMaterialAdmissionResult,
    TaskMaterialAdmissionSource,
    TaskMaterialBinding,
    TaskMaterialReleaseCommand,
    TaskMaterialReleaseResult,
    check_lot_invariants,
    new_event_id,
)
from unilabos.app.scheduler.inventory.store import InventoryStore

_MAX_SIGNED_64_BIT_INTEGER = (1 << 63) - 1
_SCHEDULER_CURSOR_NAME = "scheduler"
_CLOUD_CURSOR_NAME = "cloud"
_CURSOR_NAMES = frozenset({_SCHEDULER_CURSOR_NAME, _CLOUD_CURSOR_NAME})
_MATERIAL_FLOW_ROLES = frozenset(
    {"primary_sample", "aliquot_sample", "reagent", "consumable"}
)
_LOGGER = logging.getLogger(__name__)


class _AdmissionRejected(MaterialInvalidInput):
    """携带稳定公开诊断的确定性 M2B rejection。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        material_source_node_uuid: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.material_source_node_uuid = material_source_node_uuid


@dataclass(frozen=True, slots=True)
class _AdmissionSite:
    uuid: str
    sort_order: int
    occupied_material_uuid: str | None


@dataclass(frozen=True, slots=True)
class _AdmissionChoice:
    site_uuid: str
    material_uuid: str | None
    member_versions: tuple[tuple[str, int], ...]


def _canonical_uuid(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise MaterialInvalidInput(f"{field} must be a UUID string")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise MaterialInvalidInput(f"{field} must be a valid UUID") from exc
    if parsed.int == 0:
        raise MaterialInvalidInput(f"{field} must not be nil")
    return str(parsed)


def _json_object(value: Mapping[str, Any] | None, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MaterialInvalidInput(f"{field} must be a JSON object")
    try:
        encoded = json.dumps(
            dict(value),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise MaterialInvalidInput(f"{field} must be a JSON object") from exc
    return decoded


def _stored_json_object(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise MaterialAuthorityUnavailable(
            "material JSON field is not an object"
        ) from exc
    if not isinstance(decoded, dict):
        raise MaterialAuthorityUnavailable("material JSON field is not an object")
    return decoded


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MaterialInvalidInput(f"{field} must be a finite number")
    try:
        normalized = float(value)
    except OverflowError:
        raise MaterialInvalidInput(f"{field} must be a finite number") from None
    if not math.isfinite(normalized):
        raise MaterialInvalidInput(f"{field} must be a finite number")
    return normalized


def _material_record(row: Mapping[str, Any]) -> MaterialRecord:
    return MaterialRecord(
        uuid=row["uuid"],
        create_time=row["create_time"],
        update_time=row["update_time"],
        deleted_at=row["deleted_at"],
        description=row["description"],
        meta_data=_stored_json_object(row["meta_data"]),
        resource_template_uuid=row["resource_template_uuid"],
        parent_uuid=row["parent_uuid"],
        klass=row["class"],
        barcode=row["barcode"],
        name=row["name"],
        config=_stored_json_object(row["config"]),
        data=_stored_json_object(row["data"]),
        disposition=row["disposition"],
        material_kind=row["material_kind"],
        version=row["version"],
    )


def _site_record(
    row: Mapping[str, Any],
    allowed_resource_template_uuids: tuple[str, ...],
) -> SiteRecord:
    return SiteRecord(
        uuid=row["uuid"],
        create_time=row["create_time"],
        update_time=row["update_time"],
        deleted_at=row["deleted_at"],
        description=row["description"],
        meta_data=_stored_json_object(row["meta_data"]),
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


def _backend_material(record: MaterialRecord) -> dict[str, Any]:
    """投影冻结 Backend Material；OS runtime 扩展不得泄漏到共享 DTO。"""

    projection: dict[str, Any] = {
        "uuid": record.uuid,
        "create_time": record.create_time,
        "update_time": record.update_time,
        "meta_data": dict(record.meta_data),
        "resource_template_uuid": record.resource_template_uuid,
        "class": record.klass,
        "barcode": record.barcode,
        "name": record.name,
        "config": dict(record.config),
        "data": dict(record.data),
    }
    if record.description is not None:
        projection["description"] = record.description
    if record.parent_uuid is not None:
        projection["parent_uuid"] = record.parent_uuid
    return projection


def _backend_site(record: SiteRecord, *, graph: bool = False) -> dict[str, Any]:
    """投影冻结 Backend Site；Graph 节点固定保留空 occupied 字段。"""

    projection: dict[str, Any] = {
        "uuid": record.uuid,
        "create_time": record.create_time,
        "update_time": record.update_time,
        "meta_data": dict(record.meta_data),
        "material_uuid": record.material_uuid,
        "name": record.name,
        "sort_order": record.sort_order,
        "allowed_resource_template_uuids": list(record.allowed_resource_template_uuids),
        "position_x": record.position_x,
        "position_y": record.position_y,
        "position_z": record.position_z,
        "depth": record.depth,
        "length": record.length,
        "width": record.width,
    }
    if record.description is not None:
        projection["description"] = record.description
    if graph or record.occupied_material_uuid is not None:
        projection["occupied_material_uuid"] = record.occupied_material_uuid
    return projection


def _backend_relative_position(row: Mapping[str, Any]) -> dict[str, Any]:
    projection: dict[str, Any] = {
        "uuid": row["uuid"],
        "create_time": row["create_time"],
        "update_time": row["update_time"],
        "meta_data": _stored_json_object(row["meta_data"]),
        "material_uuid": row["material_uuid"],
        "position_x": row["position_x"],
        "position_y": row["position_y"],
        "position_z": row["position_z"],
        "depth": row["depth"],
        "length": row["length"],
        "width": row["width"],
        "scale_x": row["scale_x"],
        "scale_y": row["scale_y"],
        "scale_z": row["scale_z"],
        "rotation_x": row["rotation_x"],
        "rotation_y": row["rotation_y"],
        "rotation_z": row["rotation_z"],
    }
    if row["description"] is not None:
        projection["description"] = row["description"]
    return projection


def _read_site(conn: sqlite3.Connection, site_uuid: str) -> SiteRecord | None:
    row = conn.execute(
        "SELECT * FROM site WHERE uuid = ? AND deleted_at IS NULL",
        (site_uuid,),
    ).fetchone()
    if row is None:
        return None
    allowed_rows = conn.execute(
        """
        SELECT resource_template_uuid
        FROM site_allowed_resource_template
        WHERE site_uuid = ?
        ORDER BY resource_template_uuid
        """,
        (site_uuid,),
    ).fetchall()
    return _site_record(
        dict(row),
        tuple(item["resource_template_uuid"] for item in allowed_rows),
    )


def _admission_result_payload(result: TaskMaterialAdmissionResult) -> dict[str, Any]:
    return {
        "schema_version": result.schema_version,
        "command_uuid": result.command_uuid,
        "workflow_task_uuid": result.workflow_task_uuid,
        "status": result.status,
        "reservation_uuid": result.reservation_uuid,
        "bindings": [
            {
                "material_source_node_uuid": binding.material_source_node_uuid,
                "resource_slot": dict(binding.resource_slot),
                "site_uuid": binding.site_uuid,
            }
            for binding in result.bindings
        ],
        "diagnostics": [dict(item) for item in result.diagnostics],
        "outbox_sequence": result.outbox_sequence,
    }


def _admission_result_from_payload(
    payload: Mapping[str, Any],
) -> TaskMaterialAdmissionResult:
    return TaskMaterialAdmissionResult(
        schema_version=int(payload["schema_version"]),
        command_uuid=str(payload["command_uuid"]),
        workflow_task_uuid=str(payload["workflow_task_uuid"]),
        status=str(payload["status"]),
        reservation_uuid=(
            str(payload["reservation_uuid"])
            if payload.get("reservation_uuid") is not None
            else None
        ),
        bindings=tuple(
            TaskMaterialBinding(
                material_source_node_uuid=str(item["material_source_node_uuid"]),
                resource_slot=dict(item["resource_slot"]),
                site_uuid=(
                    str(item["site_uuid"])
                    if item.get("site_uuid") is not None
                    else None
                ),
            )
            for item in payload.get("bindings", [])
        ),
        diagnostics=tuple(dict(item) for item in payload.get("diagnostics", [])),
        outbox_sequence=int(payload["outbox_sequence"]),
    )


def _admission_command_payload(command: TaskMaterialAdmissionCommand) -> dict[str, Any]:
    return {
        "schema_version": command.schema_version,
        "command_uuid": command.command_uuid,
        "idempotency_key": command.idempotency_key,
        "workflow_task_uuid": command.workflow_task_uuid,
        "workflow_snapshot_fingerprint": command.workflow_snapshot_fingerprint,
        "sources": [
            {
                "material_source_node_uuid": source.material_source_node_uuid,
                "mode": source.mode,
                "resource_template_uuid": source.resource_template_uuid,
                "mount": dict(source.mount),
                "material_uuid": source.material_uuid,
                "site_uuid": source.site_uuid,
                "candidate_site_uuids": list(source.candidate_site_uuids),
                "flow_role": source.flow_role,
            }
            for source in command.sources
        ],
    }


def _release_result_payload(result: TaskMaterialReleaseResult) -> dict[str, Any]:
    return {
        "schema_version": result.schema_version,
        "command_uuid": result.command_uuid,
        "workflow_task_uuid": result.workflow_task_uuid,
        "status": result.status,
        "reservation_uuid": result.reservation_uuid,
        "outbox_sequence": result.outbox_sequence,
    }


def _release_result_from_payload(
    payload: Mapping[str, Any],
) -> TaskMaterialReleaseResult:
    return TaskMaterialReleaseResult(
        schema_version=int(payload["schema_version"]),
        command_uuid=str(payload["command_uuid"]),
        workflow_task_uuid=str(payload["workflow_task_uuid"]),
        status=str(payload["status"]),
        reservation_uuid=(
            str(payload["reservation_uuid"])
            if payload.get("reservation_uuid") is not None
            else None
        ),
        outbox_sequence=int(payload["outbox_sequence"]),
    )


def _release_command_payload(command: TaskMaterialReleaseCommand) -> dict[str, Any]:
    return {
        "schema_version": command.schema_version,
        "command_uuid": command.command_uuid,
        "idempotency_key": command.idempotency_key,
        "workflow_task_uuid": command.workflow_task_uuid,
        "reason": command.reason,
    }


def _canonical_payload_hash(payload: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            dict(payload),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise MaterialInvalidInput("admission command must be JSON-canonical") from exc
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _reservation_fingerprint(material_uuids: tuple[str, ...]) -> str:
    encoded = json.dumps(
        list(material_uuids),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _inventory_event(row: Mapping[str, Any]) -> InventoryEvent:
    try:
        payload = json.loads(row["payload_json"])
        if not isinstance(payload, dict):
            raise TypeError("outbox payload must be an object")
        return InventoryEvent(
            sequence=int(row["sequence"]),
            event_id=str(row["event_id"]),
            edge_id=str(row["edge_id"]),
            lab_id=str(row["lab_id"]),
            aggregate_type=str(row["aggregate_type"]),
            aggregate_id=str(row["aggregate_id"]),
            aggregate_version=int(row["aggregate_version"]),
            event_type=str(row["event_type"]),
            occurred_at=int(row["occurred_at"]),
            causation_id=str(row["causation_id"]),
            payload=payload,
        )
    except (KeyError, TypeError, ValueError):
        raise MaterialAuthorityUnavailable("stored outbox event is invalid") from None


class InventoryService:
    """Edge 仓储唯一事实源的业务入口."""

    def __init__(
        self,
        store: InventoryStore,
        edge_id: str = "edge-default",
        lab_id: str = "edge-lab",
        time_fn: Callable[[], float] = time.time,
        monitor: Any = None,
        resource_templates: Mapping[str, ResourceTemplateIdentity] | None = None,
        material_shapes: Sequence[Mapping[str, Any]] = (),
    ):
        self._store = store
        self.edge_id = edge_id
        self.lab_id = lab_id
        self._time_fn = time_fn
        # 实时监控总线（duck-typed emit(channel, type, data)）；None = 关闭
        self._monitor = monitor
        # 事务内暂存的监控事件（提交成功才发布，回滚即丢弃）
        self._tx_local = threading.local()
        self._change_listener_lock = threading.Lock()
        self._change_listener: Callable[[Mapping[str, Any]], None] | None = None
        canonical_templates: dict[str, ResourceTemplateIdentity] = {}
        for key, identity in (resource_templates or {}).items():
            if not isinstance(identity, ResourceTemplateIdentity):
                raise MaterialInvalidInput(
                    "resource_templates must contain ResourceTemplateIdentity values"
                )
            canonical_key = _canonical_uuid(key, "ResourceTemplate key")
            canonical_identity_uuid = _canonical_uuid(
                identity.uuid,
                "ResourceTemplate identity uuid",
            )
            if canonical_key != canonical_identity_uuid:
                raise MaterialInvalidInput(
                    "ResourceTemplate key must match identity uuid"
                )
            material_class = identity.material_class.strip()
            if not material_class:
                raise MaterialInvalidInput("ResourceTemplate class must not be blank")
            canonical_templates[canonical_key] = ResourceTemplateIdentity(
                uuid=canonical_identity_uuid,
                material_class=material_class,
            )
        self._resource_templates = MappingProxyType(canonical_templates)
        self._material_shapes = tuple(
            _json_object(shape, "material_shapes item") for shape in material_shapes
        )

    @classmethod
    def open(
        cls,
        *,
        working_dir: str | Path,
        resource_templates: Mapping[str, ResourceTemplateIdentity],
        edge_id: str = "edge-default",
        lab_id: str = "edge-lab",
        time_fn: Callable[[], float] = time.time,
        monitor: Any = None,
        material_shapes: Sequence[Mapping[str, Any]] = (),
    ) -> InventoryService:
        """Open the one ``inventory.db`` owned by a workspace."""

        resolved_working_dir = Path(working_dir).resolve()
        resolved_working_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            InventoryStore(str(resolved_working_dir / "inventory.db")),
            edge_id=edge_id,
            lab_id=lab_id,
            time_fn=time_fn,
            monitor=monitor,
            resource_templates=resource_templates,
            material_shapes=material_shapes,
        )

    def close(self) -> None:
        """Close the InventoryService-owned durable store."""

        self.set_change_listener(None)
        self._store.close()

    def set_change_listener(
        self,
        listener: Callable[[Mapping[str, Any]], None] | None,
    ) -> None:
        """挂载持久 Inventory 变化的唯一提交后 consumer。"""

        if listener is not None and not callable(listener):
            raise MaterialInvalidInput("change listener must be callable or null")
        with self._change_listener_lock:
            self._change_listener = listener

    def _now_ms(self) -> int:
        return int(self._time_fn() * 1000)

    def _now_iso(self) -> str:
        return (
            datetime.fromtimestamp(self._time_fn(), timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """业务事务 + 事件缓冲：commit 成功后才发布 Material 变化。"""
        events: list[dict[str, Any]] = []
        self._tx_local.events = events
        store = self._store
        try:
            with store.transaction() as conn:
                yield conn
        finally:
            self._tx_local.events = None
        # 到这里说明事务已提交（异常路径在 finally 清理后向上抛，不会执行到此）
        with self._change_listener_lock:
            change_listener = self._change_listener
        for data in events:
            event_type = str(data["event_type"])
            if self._monitor is not None:
                try:
                    monitor_data = {
                        key: value for key, value in data.items() if key != "event_type"
                    }
                    self._monitor.emit("material", event_type, monitor_data)
                except Exception:  # noqa: BLE001, S110 - 监控故障不影响业务
                    pass
            if change_listener is not None:
                try:
                    change_listener(MappingProxyType(dict(data)))
                except Exception:
                    _LOGGER.exception(
                        "Inventory 提交后 listener 处理失败：%s",
                        event_type,
                    )

    # ------------------------------------------------------------------
    # 事务内公共 helper
    # ------------------------------------------------------------------

    def _emit(
        self,
        conn: sqlite3.Connection,
        now_ms: int,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_version: int,
        event_type: str,
        payload: dict[str, Any],
        causation_id: str = "",
        actor: str = "",
        reason: str = "",
    ) -> int:
        """同事务写 ledger + outbox."""
        InventoryStore.tx_insert_ledger(
            conn,
            now_ms,
            event_type,
            aggregate_type,
            aggregate_id,
            payload,
            actor=actor,
            reason=reason,
            causation_id=causation_id,
        )
        outbox_sequence = InventoryStore.tx_insert_outbox(
            conn,
            new_event_id(now_ms),
            self.edge_id,
            self.lab_id,
            aggregate_type,
            aggregate_id,
            aggregate_version,
            event_type,
            now_ms,
            causation_id,
            payload,
        )
        # 事务缓冲监控事件：commit 成功后由 _tx 发布到 material 通道
        buffered = getattr(self._tx_local, "events", None)
        if buffered is not None:
            buffered.append(
                {
                    "event_type": event_type,
                    "aggregate_type": aggregate_type,
                    "aggregate_id": aggregate_id,
                    "version": aggregate_version,
                    "payload": payload,
                    "reason": reason,
                    "actor": actor,
                    "causation_id": causation_id,
                }
            )
        return outbox_sequence

    @staticmethod
    def _tx_get_lot(conn: sqlite3.Connection, lot_id: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM inventory_lot WHERE lot_id = ?", (lot_id,)
        ).fetchone()
        if row is None:
            raise MaterialNotFound(f"lot {lot_id} not found")
        return dict(row)

    def _tx_update_lot_quantities(
        self,
        conn: sqlite3.Connection,
        lot: dict[str, Any],
        d_total: float = 0.0,
        d_available: float = 0.0,
        d_reserved: float = 0.0,
    ) -> dict[str, Any]:
        total = lot["quantity_total"] + d_total
        available = lot["quantity_available"] + d_available
        reserved = lot["quantity_reserved"] + d_reserved
        # 浮点残余归零
        total, available, reserved = (
            0.0 if abs(v) < 1e-9 else v for v in (total, available, reserved)
        )
        check_lot_invariants(total, available, reserved)
        new_version = lot["version"] + 1
        conn.execute(
            "UPDATE inventory_lot SET quantity_total = ?, quantity_available = ?, "
            "quantity_reserved = ?, version = ? WHERE lot_id = ?",
            (total, available, reserved, new_version, lot["lot_id"]),
        )
        lot = dict(lot)
        lot.update(
            quantity_total=total,
            quantity_available=available,
            quantity_reserved=reserved,
            version=new_version,
        )
        return lot

    # ------------------------------------------------------------------
    # Material authority
    # ------------------------------------------------------------------

    def create_material(
        self,
        *,
        material_uuid: str,
        resource_template_uuid: str,
        barcode: str,
        name: str,
        parent_uuid: str | None = None,
        description: str | None = None,
        meta_data: Mapping[str, Any] | None = None,
        config: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> MaterialRecord:
        """Create one Backend-aligned business Material."""

        canonical_material_uuid = _canonical_uuid(material_uuid, "material_uuid")
        canonical_template_uuid = _canonical_uuid(
            resource_template_uuid,
            "resource_template_uuid",
        )
        canonical_parent_uuid = (
            _canonical_uuid(parent_uuid, "parent_uuid")
            if parent_uuid is not None
            else None
        )
        if canonical_parent_uuid == canonical_material_uuid:
            raise MaterialConflict("Material cannot be its own parent")
        template = self._resource_templates.get(canonical_template_uuid)
        if template is None:
            raise MaterialInvalidInput("resource_template_uuid is not registered")
        if not isinstance(barcode, str):
            raise MaterialInvalidInput("barcode must be a string")
        if not isinstance(name, str) or not name.strip():
            raise MaterialInvalidInput("name must be a non-blank string")
        if description is not None and not isinstance(description, str):
            raise MaterialInvalidInput("description must be a string or null")
        normalized_meta_data = _json_object(meta_data, "meta_data")
        normalized_config = _json_object(config, "config")
        normalized_data = _json_object(data, "data")
        now_iso = self._now_iso()
        now_ms = self._now_ms()
        try:
            with self._tx() as conn:
                if canonical_parent_uuid is not None:
                    parent = conn.execute(
                        "SELECT material_kind FROM material "
                        "WHERE uuid = ? AND deleted_at IS NULL",
                        (canonical_parent_uuid,),
                    ).fetchone()
                    if parent is None:
                        raise MaterialNotFound("parent Material not found")
                    if parent["material_kind"] != "business":
                        raise MaterialInvalidInput(
                            "business Material requires a business parent"
                        )
                conn.execute(
                    """
                    INSERT INTO material(
                        uuid, create_time, update_time, deleted_at,
                        description, meta_data, resource_template_uuid,
                        parent_uuid, class, barcode, name, config, data,
                        disposition, material_kind, version
                    ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              'active', 'business', 1)
                    """,
                    (
                        canonical_material_uuid,
                        now_iso,
                        now_iso,
                        description,
                        json.dumps(normalized_meta_data, ensure_ascii=False),
                        canonical_template_uuid,
                        canonical_parent_uuid,
                        template.material_class,
                        barcode,
                        name.strip(),
                        json.dumps(normalized_config, ensure_ascii=False),
                        json.dumps(normalized_data, ensure_ascii=False),
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM material WHERE uuid = ? AND deleted_at IS NULL",
                    (canonical_material_uuid,),
                ).fetchone()
                if row is None:
                    raise MaterialAuthorityUnavailable(
                        "created material is not readable"
                    )
                self._emit(
                    conn,
                    now_ms,
                    "material",
                    canonical_material_uuid,
                    1,
                    "material.created",
                    {"material": _material_record(dict(row)).to_dict()},
                )
        except sqlite3.IntegrityError:
            raise MaterialConflict(
                "material identity or barcode already exists"
            ) from None
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable("failed to create material") from None
        return _material_record(dict(row))

    def get_material(self, material_uuid: str) -> MaterialRecord:
        """Read one non-deleted Backend-aligned Material."""

        canonical_material_uuid = _canonical_uuid(material_uuid, "material_uuid")
        try:
            row = self._store.query_one(
                "SELECT * FROM material WHERE uuid = ? AND deleted_at IS NULL",
                (canonical_material_uuid,),
            )
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable("failed to read material") from None
        if row is None:
            raise MaterialNotFound(f"material {canonical_material_uuid} not found")
        return _material_record(row)

    def resolve_resource_slot(
        self,
        *,
        material_uuid: str,
        allowed_resource_template_uuids: tuple[str, ...] | None,
    ) -> ResourceSlotResolution:
        """Resolve one concrete ResourceSlot against durable Material truth."""

        canonical_material_uuid = _canonical_uuid(material_uuid, "material_uuid")
        allowed_templates: set[str] | None = None
        if allowed_resource_template_uuids is not None:
            if type(allowed_resource_template_uuids) is not tuple:
                raise MaterialInvalidInput(
                    "allowed_resource_template_uuids must be a UUID tuple or null"
                )
            if not allowed_resource_template_uuids:
                raise MaterialInvalidInput(
                    "allowed_resource_template_uuids must not be empty"
                )
            canonical_templates = tuple(
                _canonical_uuid(value, "allowed_resource_template_uuid")
                for value in allowed_resource_template_uuids
            )
            allowed_templates = set(canonical_templates)
            if len(allowed_templates) != len(canonical_templates):
                raise MaterialInvalidInput(
                    "allowed_resource_template_uuids must be unique"
                )

        material = self.get_material(canonical_material_uuid)
        template_uuid = _canonical_uuid(
            material.resource_template_uuid,
            "material.resource_template_uuid",
        )
        if material.material_kind != "business":
            raise MaterialInvalidInput("ResourceSlot requires a business Material")
        if material.disposition != "active":
            raise MaterialConflict("Material is not runnable")
        if allowed_templates is not None and template_uuid not in allowed_templates:
            raise MaterialInvalidInput(
                "Material template is not allowed by ResourceSlot"
            )
        return ResourceSlotResolution(
            uuid=canonical_material_uuid,
            resource_template_uuid=template_uuid,
        )

    def admit_task(
        self,
        command: TaskMaterialAdmissionCommand,
    ) -> TaskMaterialAdmissionResult:
        """Return a durable closed result for every well-formed admission command."""

        normalized_command: TaskMaterialAdmissionCommand | None = None
        try:
            normalized_command = self._normalize_admission_command(command)
            return self._admit_task_or_raise(normalized_command)
        except _AdmissionRejected as error:
            return self._persist_admission_rejection(
                normalized_command or command,
                error,
            )
        except (MaterialInvalidInput, MaterialNotFound) as error:
            rejection = _AdmissionRejected("invalid_material_source", str(error))
            return self._persist_admission_rejection(
                normalized_command or command,
                rejection,
            )

    def _normalize_admission_command(
        self,
        command: TaskMaterialAdmissionCommand,
    ) -> TaskMaterialAdmissionCommand:
        if not isinstance(command, TaskMaterialAdmissionCommand):
            raise MaterialInvalidInput("command must be a TaskMaterialAdmissionCommand")
        if command.schema_version != 1:
            raise MaterialInvalidInput("unsupported admission schema_version")
        canonical_command_uuid = _canonical_uuid(command.command_uuid, "command_uuid")
        canonical_task_uuid = _canonical_uuid(
            command.workflow_task_uuid,
            "workflow_task_uuid",
        )
        if not isinstance(command.idempotency_key, str) or not command.idempotency_key:
            raise MaterialInvalidInput("idempotency_key must not be blank")
        if (
            not isinstance(command.workflow_snapshot_fingerprint, str)
            or not command.workflow_snapshot_fingerprint
        ):
            raise MaterialInvalidInput(
                "workflow_snapshot_fingerprint must not be blank"
            )
        if type(command.sources) is not tuple or not command.sources:
            raise MaterialInvalidInput("sources must be a non-empty tuple")

        normalized_sources: list[TaskMaterialAdmissionSource] = []
        seen_nodes: set[str] = set()
        seen_fixed_materials: set[str] = set()
        for source in command.sources:
            if not isinstance(source, TaskMaterialAdmissionSource):
                raise MaterialInvalidInput(
                    "sources must contain TaskMaterialAdmissionSource values"
                )
            node_uuid = _canonical_uuid(
                source.material_source_node_uuid,
                "material_source_node_uuid",
            )
            if node_uuid in seen_nodes:
                raise _AdmissionRejected(
                    "invalid_material_source",
                    "material_source_node_uuid values must be unique",
                    material_source_node_uuid=node_uuid,
                )
            seen_nodes.add(node_uuid)
            if source.mode not in {"existing", "create_new"}:
                raise _AdmissionRejected(
                    "invalid_material_source",
                    "MaterialSource mode must be existing or create_new",
                    material_source_node_uuid=node_uuid,
                )
            template_uuid = _canonical_uuid(
                source.resource_template_uuid,
                "resource_template_uuid",
            )
            if template_uuid not in self._resource_templates:
                raise _AdmissionRejected(
                    "resource_template_not_found",
                    "resource_template_uuid is not registered",
                    material_source_node_uuid=node_uuid,
                )
            if not isinstance(source.mount, Mapping):
                raise _AdmissionRejected(
                    "invalid_material_source",
                    "mount must be a ResourceSlot object",
                    material_source_node_uuid=node_uuid,
                )
            if set(source.mount) != {"uuid"}:
                raise _AdmissionRejected(
                    "invalid_material_source",
                    "mount must contain only the ResourceSlot uuid",
                    material_source_node_uuid=node_uuid,
                )
            mount_uuid = _canonical_uuid(
                str(source.mount.get("uuid", "")),
                "mount.uuid",
            )
            material_uuid = (
                _canonical_uuid(source.material_uuid, "material_uuid")
                if source.material_uuid is not None
                else None
            )
            if source.mode == "create_new" and material_uuid is not None:
                raise _AdmissionRejected(
                    "invalid_material_source",
                    "create_new MaterialSource prohibits material_uuid",
                    material_source_node_uuid=node_uuid,
                )
            if material_uuid is not None:
                if material_uuid in seen_fixed_materials:
                    raise _AdmissionRejected(
                        "invalid_material_source",
                        "a fixed Material may be bound by only one MaterialSource",
                        material_source_node_uuid=node_uuid,
                    )
                seen_fixed_materials.add(material_uuid)
            site_uuid = (
                _canonical_uuid(source.site_uuid, "site_uuid")
                if source.site_uuid is not None
                else None
            )
            if type(source.candidate_site_uuids) is not tuple:
                raise _AdmissionRejected(
                    "invalid_material_source",
                    "candidate_site_uuids must be a UUID tuple",
                    material_source_node_uuid=node_uuid,
                )
            candidate_site_uuids = tuple(
                _canonical_uuid(value, "candidate_site_uuid")
                for value in source.candidate_site_uuids
            )
            if len(set(candidate_site_uuids)) != len(candidate_site_uuids):
                raise _AdmissionRejected(
                    "invalid_material_source",
                    "candidate_site_uuids must be unique",
                    material_source_node_uuid=node_uuid,
                )
            if site_uuid is not None and candidate_site_uuids:
                raise _AdmissionRejected(
                    "invalid_material_source",
                    "site_uuid and candidate_site_uuids are mutually exclusive",
                    material_source_node_uuid=node_uuid,
                )
            if (
                not isinstance(source.flow_role, str)
                or source.flow_role not in _MATERIAL_FLOW_ROLES
            ):
                raise _AdmissionRejected(
                    "invalid_material_source",
                    "flow_role is not in the closed MaterialFlowRole catalog",
                    material_source_node_uuid=node_uuid,
                )
            normalized_sources.append(
                TaskMaterialAdmissionSource(
                    material_source_node_uuid=node_uuid,
                    mode=source.mode,
                    resource_template_uuid=template_uuid,
                    mount={"uuid": mount_uuid},
                    material_uuid=material_uuid,
                    site_uuid=site_uuid,
                    candidate_site_uuids=tuple(sorted(candidate_site_uuids)),
                    flow_role=source.flow_role,
                )
            )
        return TaskMaterialAdmissionCommand(
            schema_version=1,
            command_uuid=canonical_command_uuid,
            idempotency_key=command.idempotency_key,
            workflow_task_uuid=canonical_task_uuid,
            workflow_snapshot_fingerprint=command.workflow_snapshot_fingerprint,
            sources=tuple(
                sorted(
                    normalized_sources,
                    key=lambda item: item.material_source_node_uuid,
                )
            ),
        )

    @staticmethod
    def _admission_sites(
        conn: sqlite3.Connection,
        source: TaskMaterialAdmissionSource,
    ) -> tuple[_AdmissionSite, ...]:
        node_uuid = source.material_source_node_uuid
        mount_uuid = str(source.mount["uuid"])
        if source.site_uuid is not None:
            requested_site_uuids = (source.site_uuid,)
        else:
            requested_site_uuids = source.candidate_site_uuids

        if requested_site_uuids:
            placeholders = ",".join("?" for _ in requested_site_uuids)
            rows = conn.execute(
                f"""
                SELECT uuid, deleted_at, material_uuid, sort_order,
                       occupied_material_uuid
                FROM site
                WHERE uuid IN ({placeholders})
                """,
                requested_site_uuids,
            ).fetchall()
            by_uuid = {str(row["uuid"]): row for row in rows}
            for site_uuid in requested_site_uuids:
                row = by_uuid.get(site_uuid)
                if row is None or row["deleted_at"] is not None:
                    raise _AdmissionRejected(
                        "site_not_found",
                        "selected Site does not exist",
                        material_source_node_uuid=node_uuid,
                    )
                if row["material_uuid"] != mount_uuid:
                    raise _AdmissionRejected(
                        "site_scope_mismatch",
                        "selected Site is not directly owned by mount",
                        material_source_node_uuid=node_uuid,
                    )
            selected_rows = [by_uuid[site_uuid] for site_uuid in requested_site_uuids]
        else:
            selected_rows = conn.execute(
                """
                SELECT uuid, deleted_at, material_uuid, sort_order,
                       occupied_material_uuid
                FROM site
                WHERE material_uuid = ? AND deleted_at IS NULL
                """,
                (mount_uuid,),
            ).fetchall()

        sites: list[_AdmissionSite] = []
        for row in selected_rows:
            allowed = conn.execute(
                """
                SELECT resource_template_uuid
                FROM site_allowed_resource_template
                WHERE site_uuid = ?
                """,
                (row["uuid"],),
            ).fetchall()
            if allowed and source.resource_template_uuid not in {
                str(item["resource_template_uuid"]) for item in allowed
            }:
                if requested_site_uuids:
                    raise _AdmissionRejected(
                        "site_template_mismatch",
                        "selected Site does not allow the Material template",
                        material_source_node_uuid=node_uuid,
                    )
                continue
            sites.append(
                _AdmissionSite(
                    uuid=str(row["uuid"]),
                    sort_order=int(row["sort_order"]),
                    occupied_material_uuid=(
                        str(row["occupied_material_uuid"])
                        if row["occupied_material_uuid"] is not None
                        else None
                    ),
                )
            )
        return tuple(sorted(sites, key=lambda item: (item.sort_order, item.uuid)))

    @staticmethod
    def _admission_material_members(
        conn: sqlite3.Connection,
        *,
        material_uuid: str,
        workflow_task_uuid: str,
        fixed: bool,
        material_source_node_uuid: str,
    ) -> tuple[tuple[tuple[str, int], ...] | None, bool]:
        rows = conn.execute(
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
            SELECT uuid, material_kind, disposition, version
            FROM material
            WHERE uuid IN (SELECT uuid FROM subtree)
            ORDER BY uuid
            """,
            (material_uuid,),
        ).fetchall()
        if not rows:
            return None, False
        for row in rows:
            if row["material_kind"] != "business":
                if fixed:
                    raise _AdmissionRejected(
                        "material_not_runnable",
                        "fixed Material subtree is not runnable",
                        material_source_node_uuid=material_source_node_uuid,
                    )
                return None, False
            if row["disposition"] != "active":
                if fixed and row["disposition"] != "reconciling":
                    raise _AdmissionRejected(
                        "material_not_runnable",
                        "fixed Material subtree is not runnable",
                        material_source_node_uuid=material_source_node_uuid,
                    )
                return None, False
        member_uuids = tuple(str(row["uuid"]) for row in rows)
        placeholders = ",".join("?" for _ in member_uuids)
        reserved = conn.execute(
            f"""
            SELECT 1
            FROM material_reservation_member AS member
            JOIN material_reservation AS reservation
              ON reservation.uuid = member.reservation_uuid
            WHERE member.material_uuid IN ({placeholders})
              AND member.released_at IS NULL
              AND reservation.status = 'active'
              AND reservation.workflow_task_uuid <> ?
            LIMIT 1
            """,
            (*member_uuids, workflow_task_uuid),
        ).fetchone()
        if reserved is not None:
            return None, True
        return tuple((str(row["uuid"]), int(row["version"])) for row in rows), False

    @staticmethod
    def _complete_admission_assignment(
        sources: tuple[TaskMaterialAdmissionSource, ...],
        choices_by_node: Mapping[str, tuple[_AdmissionChoice, ...]],
    ) -> dict[str, _AdmissionChoice] | None:
        assignment: dict[str, _AdmissionChoice] = {}
        used_sites: set[str] = set()
        used_materials: set[str] = set()

        def search(index: int) -> bool:
            if index == len(sources):
                return True
            source = sources[index]
            for choice in choices_by_node[source.material_source_node_uuid]:
                member_uuids = {item[0] for item in choice.member_versions}
                if choice.site_uuid in used_sites or member_uuids & used_materials:
                    continue
                assignment[source.material_source_node_uuid] = choice
                used_sites.add(choice.site_uuid)
                used_materials.update(member_uuids)
                if search(index + 1):
                    return True
                used_materials.difference_update(member_uuids)
                used_sites.remove(choice.site_uuid)
                del assignment[source.material_source_node_uuid]
            return False

        return assignment if search(0) else None

    def _admit_task_or_raise(
        self,
        command: TaskMaterialAdmissionCommand,
    ) -> TaskMaterialAdmissionResult:
        """原子查找并预留一组完整的 Task-wide assignment。"""

        normalized_command = self._normalize_admission_command(command)
        canonical_command_uuid = normalized_command.command_uuid
        canonical_task_uuid = normalized_command.workflow_task_uuid
        payload_hash = _canonical_payload_hash(
            _admission_command_payload(normalized_command)
        )
        now_iso = self._now_iso()
        now_ms = self._now_ms()

        try:
            with self._tx() as conn:
                processed = conn.execute(
                    "SELECT * FROM processed_command WHERE command_id = ?",
                    (canonical_command_uuid,),
                ).fetchone()
                previous_blocked: TaskMaterialAdmissionResult | None = None
                if processed is not None:
                    if processed["payload_hash"] != payload_hash:
                        raise MaterialConflict(
                            "command_uuid was already used with a different payload"
                        )
                    previous_result = _admission_result_from_payload(
                        json.loads(processed["result_json"])
                    )
                    if previous_result.status != "blocked":
                        return previous_result
                    previous_blocked = previous_result

                choices_by_node: dict[str, tuple[_AdmissionChoice, ...]] = {}
                blocked_reason_by_node: dict[str, str] = {}
                for source in normalized_command.sources:
                    mount_uuid = str(source.mount["uuid"])
                    mount = conn.execute(
                        """
                        SELECT uuid
                        FROM material
                        WHERE uuid = ? AND deleted_at IS NULL
                        """,
                        (mount_uuid,),
                    ).fetchone()
                    if mount is None:
                        raise _AdmissionRejected(
                            "mount_not_found",
                            "mount Material does not exist",
                            material_source_node_uuid=source.material_source_node_uuid,
                        )
                    sites = self._admission_sites(conn, source)
                    choices: list[_AdmissionChoice] = []
                    saw_reserved = False
                    if source.mode == "create_new":
                        for site in sites:
                            if site.occupied_material_uuid is None:
                                choices.append(_AdmissionChoice(site.uuid, None, ()))
                        blocked_reason = "site_unavailable"
                    elif source.material_uuid is not None:
                        row = conn.execute(
                            """
                            SELECT resource_template_uuid, material_kind, disposition
                            FROM material
                            WHERE uuid = ? AND deleted_at IS NULL
                            """,
                            (source.material_uuid,),
                        ).fetchone()
                        if row is None:
                            raise _AdmissionRejected(
                                "material_not_found",
                                "fixed Material does not exist",
                                material_source_node_uuid=source.material_source_node_uuid,
                            )
                        if (
                            row["resource_template_uuid"]
                            != source.resource_template_uuid
                        ):
                            raise _AdmissionRejected(
                                "material_template_mismatch",
                                "fixed Material template does not match selector",
                                material_source_node_uuid=source.material_source_node_uuid,
                            )
                        if row["material_kind"] != "business":
                            raise _AdmissionRejected(
                                "material_not_runnable",
                                "fixed Material is not runnable",
                                material_source_node_uuid=source.material_source_node_uuid,
                            )
                        if row["disposition"] != "active":
                            if row["disposition"] == "reconciling":
                                blocked_reason = "material_unavailable"
                                choices_by_node[source.material_source_node_uuid] = ()
                                blocked_reason_by_node[
                                    source.material_source_node_uuid
                                ] = blocked_reason
                                continue
                            raise _AdmissionRejected(
                                "material_not_runnable",
                                "fixed Material is not runnable",
                                material_source_node_uuid=source.material_source_node_uuid,
                            )
                        occupied_sites = [
                            site
                            for site in sites
                            if site.occupied_material_uuid == source.material_uuid
                        ]
                        if len(occupied_sites) != 1:
                            raise _AdmissionRejected(
                                "material_location_mismatch",
                                "fixed Material is not in the selected Site scope",
                                material_source_node_uuid=source.material_source_node_uuid,
                            )
                        member_versions, saw_reserved = (
                            self._admission_material_members(
                                conn,
                                material_uuid=source.material_uuid,
                                workflow_task_uuid=canonical_task_uuid,
                                fixed=True,
                                material_source_node_uuid=source.material_source_node_uuid,
                            )
                        )
                        if member_versions is not None:
                            choices.append(
                                _AdmissionChoice(
                                    occupied_sites[0].uuid,
                                    source.material_uuid,
                                    member_versions,
                                )
                            )
                        blocked_reason = (
                            "material_reserved"
                            if saw_reserved
                            else "material_unavailable"
                        )
                    else:
                        for site in sites:
                            if site.occupied_material_uuid is None:
                                continue
                            row = conn.execute(
                                """
                                SELECT resource_template_uuid, material_kind,
                                       disposition
                                FROM material
                                WHERE uuid = ? AND deleted_at IS NULL
                                """,
                                (site.occupied_material_uuid,),
                            ).fetchone()
                            if (
                                row is None
                                or row["resource_template_uuid"]
                                != source.resource_template_uuid
                                or row["material_kind"] != "business"
                                or row["disposition"] != "active"
                            ):
                                continue
                            member_versions, reserved = (
                                self._admission_material_members(
                                    conn,
                                    material_uuid=site.occupied_material_uuid,
                                    workflow_task_uuid=canonical_task_uuid,
                                    fixed=False,
                                    material_source_node_uuid=source.material_source_node_uuid,
                                )
                            )
                            saw_reserved = saw_reserved or reserved
                            if member_versions is not None:
                                choices.append(
                                    _AdmissionChoice(
                                        site.uuid,
                                        site.occupied_material_uuid,
                                        member_versions,
                                    )
                                )
                        blocked_reason = (
                            "material_reserved"
                            if saw_reserved and not choices
                            else "material_unavailable"
                        )
                    choices_by_node[source.material_source_node_uuid] = tuple(choices)
                    blocked_reason_by_node[source.material_source_node_uuid] = (
                        blocked_reason
                    )

                normalized_sources = normalized_command.sources
                for source in normalized_sources:
                    if not choices_by_node[source.material_source_node_uuid]:
                        return self._blocked_admission_result(
                            conn,
                            normalized_command,
                            payload_hash=payload_hash,
                            now_ms=now_ms,
                            previous_blocked=previous_blocked,
                            reason=blocked_reason_by_node[
                                source.material_source_node_uuid
                            ],
                            material_source_node_uuid=source.material_source_node_uuid,
                        )
                assignment = self._complete_admission_assignment(
                    normalized_sources,
                    choices_by_node,
                )
                if assignment is None:
                    conflict_source = next(
                        (
                            source
                            for source in normalized_sources
                            if source.material_uuid is None
                        ),
                        normalized_sources[0],
                    )
                    reason = (
                        "site_unavailable"
                        if conflict_source.mode == "create_new"
                        else "material_unavailable"
                    )
                    return self._blocked_admission_result(
                        conn,
                        normalized_command,
                        payload_hash=payload_hash,
                        now_ms=now_ms,
                        previous_blocked=previous_blocked,
                        reason=reason,
                        material_source_node_uuid=(
                            conflict_source.material_source_node_uuid
                        ),
                    )

                bindings: list[TaskMaterialBinding] = []
                members: dict[str, tuple[str, int]] = {}
                for source in normalized_sources:
                    choice = assignment[source.material_source_node_uuid]
                    material_uuid = choice.material_uuid
                    member_versions = choice.member_versions
                    if source.mode == "create_new":
                        material_uuid = str(uuid4())
                        template = self._resource_templates[
                            source.resource_template_uuid
                        ]
                        conn.execute(
                            """
                            INSERT INTO material(
                                uuid, create_time, update_time, deleted_at,
                                description, meta_data, resource_template_uuid,
                                parent_uuid, class, barcode, name, config, data,
                                disposition, material_kind, version
                            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, NULL,
                                      ?, '', ?, '{}', '{}',
                                      'active', 'business', 1)
                            """,
                            (
                                material_uuid,
                                now_iso,
                                now_iso,
                                source.resource_template_uuid,
                                template.material_class,
                                template.material_class,
                            ),
                        )
                        material_row = conn.execute(
                            "SELECT * FROM material WHERE uuid = ?",
                            (material_uuid,),
                        ).fetchone()
                        self._emit(
                            conn,
                            now_ms,
                            "material",
                            material_uuid,
                            1,
                            "material.created",
                            {
                                "material": _material_record(
                                    dict(material_row)
                                ).to_dict()
                            },
                            causation_id=canonical_command_uuid,
                        )
                        updated = conn.execute(
                            """
                            UPDATE site
                            SET occupied_material_uuid = ?, update_time = ?,
                                version = version + 1
                            WHERE uuid = ? AND deleted_at IS NULL
                              AND occupied_material_uuid IS NULL
                            """,
                            (material_uuid, now_iso, choice.site_uuid),
                        )
                        if updated.rowcount != 1:
                            raise MaterialConflict(
                                "Site occupancy changed during admission"
                            )
                        site = _read_site(conn, choice.site_uuid)
                        if site is None:
                            raise MaterialAuthorityUnavailable(
                                "updated Site is not readable"
                            )
                        self._emit(
                            conn,
                            now_ms,
                            "site",
                            choice.site_uuid,
                            site.version,
                            "site.occupancy_updated",
                            {"site": site.to_dict()},
                            causation_id=canonical_command_uuid,
                        )
                        member_versions = ((material_uuid, 1),)
                    if material_uuid is None:
                        raise MaterialAuthorityUnavailable(
                            "admission assignment has no Material identity"
                        )
                    for member_uuid, version in member_versions:
                        members[member_uuid] = (material_uuid, version)
                    bindings.append(
                        TaskMaterialBinding(
                            material_source_node_uuid=(
                                source.material_source_node_uuid
                            ),
                            resource_slot={
                                "uuid": material_uuid,
                                "resource_template_uuid": (
                                    source.resource_template_uuid
                                ),
                            },
                            site_uuid=choice.site_uuid,
                        )
                    )

                material_uuids = tuple(sorted(members))
                set_fingerprint = _reservation_fingerprint(material_uuids)
                active = conn.execute(
                    """
                    SELECT uuid, set_fingerprint
                    FROM material_reservation
                    WHERE workflow_task_uuid = ? AND status = 'active'
                    """,
                    (canonical_task_uuid,),
                ).fetchone()
                if active is not None:
                    if active["set_fingerprint"] != set_fingerprint:
                        raise _AdmissionRejected(
                            "task_material_set_conflict",
                            "Task already owns a different Material reservation",
                        )
                    reservation_uuid = str(active["uuid"])
                else:
                    reservation_uuid = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            "unilab:m1r:reservation:"
                            f"{canonical_task_uuid}:{set_fingerprint}:"
                            f"{canonical_command_uuid}",
                        )
                    )
                    conn.execute(
                        """
                        INSERT INTO material_reservation(
                            uuid, workflow_task_uuid, set_fingerprint,
                            status, create_time, released_at
                        ) VALUES (?, ?, ?, 'active', ?, NULL)
                        """,
                        (
                            reservation_uuid,
                            canonical_task_uuid,
                            set_fingerprint,
                            now_iso,
                        ),
                    )
                    for material_uuid, (root_uuid, version) in sorted(members.items()):
                        conn.execute(
                            """
                            INSERT INTO material_reservation_member(
                                reservation_uuid, material_uuid,
                                root_material_uuid, acquired_version, released_at
                            ) VALUES (?, ?, ?, ?, NULL)
                            """,
                            (reservation_uuid, material_uuid, root_uuid, version),
                        )

                outbox_sequence = self._emit(
                    conn,
                    now_ms,
                    "material_reservation",
                    reservation_uuid,
                    1,
                    "material_reservation.admitted",
                    {
                        "workflow_task_uuid": canonical_task_uuid,
                        "set_fingerprint": set_fingerprint,
                        "material_uuids": list(material_uuids),
                    },
                    causation_id=canonical_command_uuid,
                )
                result = TaskMaterialAdmissionResult(
                    schema_version=1,
                    command_uuid=canonical_command_uuid,
                    workflow_task_uuid=canonical_task_uuid,
                    status="admitted",
                    reservation_uuid=reservation_uuid,
                    bindings=tuple(bindings),
                    diagnostics=(),
                    outbox_sequence=outbox_sequence,
                )
                conn.execute(
                    """
                    INSERT INTO processed_command(
                        command_id, idempotency_key, command_type, payload_hash,
                        result_json, status, processed_at
                    ) VALUES (?, ?, 'material.admit', ?, ?, 'completed', ?)
                    ON CONFLICT(command_id) DO UPDATE SET
                        result_json = excluded.result_json,
                        status = excluded.status,
                        processed_at = excluded.processed_at
                    WHERE processed_command.payload_hash = excluded.payload_hash
                    """,
                    (
                        canonical_command_uuid,
                        command.idempotency_key,
                        payload_hash,
                        json.dumps(
                            _admission_result_payload(result),
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        now_ms,
                    ),
                )
        except sqlite3.IntegrityError:
            raise MaterialConflict("Material reservation conflicts") from None
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to admit Task Materials"
            ) from None
        return result

    def _blocked_admission_result(
        self,
        conn: sqlite3.Connection,
        command: TaskMaterialAdmissionCommand,
        *,
        payload_hash: str,
        now_ms: int,
        previous_blocked: TaskMaterialAdmissionResult | None,
        reason: str,
        material_source_node_uuid: str,
    ) -> TaskMaterialAdmissionResult:
        """Persist one transient contention result with no partial reservation."""

        if previous_blocked is not None:
            return previous_blocked
        outbox_sequence = self._emit(
            conn,
            now_ms,
            "material_admission",
            command.workflow_task_uuid,
            1,
            "material_admission.blocked",
            {
                "workflow_task_uuid": command.workflow_task_uuid,
                "reason": reason,
                "material_source_node_uuid": material_source_node_uuid,
            },
            causation_id=command.command_uuid,
        )
        result = TaskMaterialAdmissionResult(
            schema_version=1,
            command_uuid=command.command_uuid,
            workflow_task_uuid=command.workflow_task_uuid,
            status="blocked",
            reservation_uuid=None,
            bindings=(),
            diagnostics=(
                {
                    "code": reason,
                    "material_source_node_uuid": material_source_node_uuid,
                },
            ),
            outbox_sequence=outbox_sequence,
        )
        conn.execute(
            """
            INSERT INTO processed_command(
                command_id, idempotency_key, command_type, payload_hash,
                result_json, status, processed_at
            ) VALUES (?, ?, 'material.admit', ?, ?, 'blocked', ?)
            """,
            (
                command.command_uuid,
                command.idempotency_key,
                payload_hash,
                json.dumps(
                    _admission_result_payload(result),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                now_ms,
            ),
        )
        return result

    def _persist_admission_rejection(
        self,
        command: TaskMaterialAdmissionCommand,
        error: _AdmissionRejected,
    ) -> TaskMaterialAdmissionResult:
        """Freeze one deterministic rejection for replay across process restarts."""

        if not isinstance(command, TaskMaterialAdmissionCommand):
            raise error
        if command.schema_version != 1:
            raise error
        canonical_command_uuid = _canonical_uuid(command.command_uuid, "command_uuid")
        canonical_task_uuid = _canonical_uuid(
            command.workflow_task_uuid,
            "workflow_task_uuid",
        )
        if not isinstance(command.idempotency_key, str) or not command.idempotency_key:
            raise error
        if (
            not isinstance(command.workflow_snapshot_fingerprint, str)
            or not command.workflow_snapshot_fingerprint
        ):
            raise error
        try:
            payload_hash = _canonical_payload_hash(_admission_command_payload(command))
        except (TypeError, ValueError):
            raise error from None
        now_ms = self._now_ms()
        try:
            with self._tx() as conn:
                processed = conn.execute(
                    "SELECT * FROM processed_command WHERE command_id = ?",
                    (canonical_command_uuid,),
                ).fetchone()
                if processed is not None:
                    if processed["payload_hash"] != payload_hash:
                        raise MaterialConflict(
                            "command_uuid was already used with a different payload"
                        )
                    previous_result = _admission_result_from_payload(
                        json.loads(processed["result_json"])
                    )
                    if previous_result.status != "blocked":
                        return previous_result
                diagnostic = {
                    "code": error.code,
                    "material_source_node_uuid": error.material_source_node_uuid,
                }
                outbox_sequence = self._emit(
                    conn,
                    now_ms,
                    "material_admission",
                    canonical_task_uuid,
                    1,
                    "material_admission.rejected",
                    {
                        "workflow_task_uuid": canonical_task_uuid,
                        "diagnostics": [diagnostic],
                    },
                    causation_id=canonical_command_uuid,
                )
                result = TaskMaterialAdmissionResult(
                    schema_version=1,
                    command_uuid=canonical_command_uuid,
                    workflow_task_uuid=canonical_task_uuid,
                    status="rejected",
                    reservation_uuid=None,
                    bindings=(),
                    diagnostics=(diagnostic,),
                    outbox_sequence=outbox_sequence,
                )
                conn.execute(
                    """
                    INSERT INTO processed_command(
                        command_id, idempotency_key, command_type, payload_hash,
                        result_json, status, processed_at
                    ) VALUES (?, ?, 'material.admit', ?, ?, 'rejected', ?)
                    ON CONFLICT(command_id) DO UPDATE SET
                        result_json = excluded.result_json,
                        status = excluded.status,
                        processed_at = excluded.processed_at
                    WHERE processed_command.payload_hash = excluded.payload_hash
                    """,
                    (
                        canonical_command_uuid,
                        command.idempotency_key,
                        payload_hash,
                        json.dumps(
                            _admission_result_payload(result),
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        now_ms,
                    ),
                )
        except sqlite3.IntegrityError:
            raise MaterialConflict("Material admission command conflicts") from None
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to persist Material admission rejection"
            ) from None
        return result

    def release_task(
        self,
        command: TaskMaterialReleaseCommand,
    ) -> TaskMaterialReleaseResult:
        """Idempotently release one Task's complete active Reservation."""

        if not isinstance(command, TaskMaterialReleaseCommand):
            raise MaterialInvalidInput("command must be a TaskMaterialReleaseCommand")
        if command.schema_version != 1:
            raise MaterialInvalidInput("unsupported release schema_version")
        canonical_command_uuid = _canonical_uuid(command.command_uuid, "command_uuid")
        canonical_task_uuid = _canonical_uuid(
            command.workflow_task_uuid,
            "workflow_task_uuid",
        )
        if not isinstance(command.idempotency_key, str) or not command.idempotency_key:
            raise MaterialInvalidInput("idempotency_key must not be blank")
        if not isinstance(command.reason, str) or not command.reason.strip():
            raise MaterialInvalidInput("reason must not be blank")
        normalized_command = TaskMaterialReleaseCommand(
            schema_version=1,
            command_uuid=canonical_command_uuid,
            idempotency_key=command.idempotency_key,
            workflow_task_uuid=canonical_task_uuid,
            reason=command.reason.strip(),
        )
        payload_hash = _canonical_payload_hash(
            _release_command_payload(normalized_command)
        )
        now_iso = self._now_iso()
        now_ms = self._now_ms()

        try:
            with self._tx() as conn:
                processed = conn.execute(
                    "SELECT * FROM processed_command WHERE command_id = ?",
                    (canonical_command_uuid,),
                ).fetchone()
                if processed is not None:
                    if processed["payload_hash"] != payload_hash:
                        raise MaterialConflict(
                            "command_uuid was already used with a different payload"
                        )
                    return _release_result_from_payload(
                        json.loads(processed["result_json"])
                    )

                active = conn.execute(
                    """
                    SELECT uuid
                    FROM material_reservation
                    WHERE workflow_task_uuid = ? AND status = 'active'
                    """,
                    (canonical_task_uuid,),
                ).fetchone()
                reservation_uuid = str(active["uuid"]) if active is not None else None
                if reservation_uuid is not None:
                    conn.execute(
                        """
                        UPDATE material_reservation
                        SET status = 'released', released_at = ?
                        WHERE uuid = ? AND status = 'active'
                        """,
                        (now_iso, reservation_uuid),
                    )
                    conn.execute(
                        """
                        UPDATE material_reservation_member
                        SET released_at = ?
                        WHERE reservation_uuid = ? AND released_at IS NULL
                        """,
                        (now_iso, reservation_uuid),
                    )

                outbox_sequence = self._emit(
                    conn,
                    now_ms,
                    "material_reservation",
                    reservation_uuid or canonical_task_uuid,
                    1,
                    "material_reservation.released",
                    {
                        "workflow_task_uuid": canonical_task_uuid,
                        "reservation_uuid": reservation_uuid,
                        "reason": normalized_command.reason,
                    },
                    causation_id=canonical_command_uuid,
                    reason=normalized_command.reason,
                )
                result = TaskMaterialReleaseResult(
                    schema_version=1,
                    command_uuid=canonical_command_uuid,
                    workflow_task_uuid=canonical_task_uuid,
                    status="released",
                    reservation_uuid=reservation_uuid,
                    outbox_sequence=outbox_sequence,
                )
                conn.execute(
                    """
                    INSERT INTO processed_command(
                        command_id, idempotency_key, command_type, payload_hash,
                        result_json, status, processed_at
                    ) VALUES (?, ?, 'material.release', ?, ?, 'completed', ?)
                    """,
                    (
                        canonical_command_uuid,
                        normalized_command.idempotency_key,
                        payload_hash,
                        json.dumps(
                            _release_result_payload(result),
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        now_ms,
                    ),
                )
        except sqlite3.IntegrityError:
            raise MaterialConflict("Material release conflicts") from None
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to release Task Materials"
            ) from None
        return result

    def get_command_result(
        self,
        command_uuid: str,
    ) -> TaskMaterialAdmissionResult | TaskMaterialReleaseResult:
        """Read one durable Material command result by command UUID."""

        canonical_command_uuid = _canonical_uuid(command_uuid, "command_uuid")
        try:
            row = self._store.get_processed_command(canonical_command_uuid)
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to read command result"
            ) from None
        if row is None:
            raise MaterialNotFound(
                f"inventory command {canonical_command_uuid} not found"
            )
        try:
            payload = json.loads(row["result_json"])
            if row.get("command_type") == "material.admit":
                return _admission_result_from_payload(payload)
            if row.get("command_type") == "material.release":
                return _release_result_from_payload(payload)
            raise MaterialNotFound(
                f"inventory command {canonical_command_uuid} not found"
            )
        except (KeyError, TypeError, ValueError):
            raise MaterialAuthorityUnavailable(
                "stored inventory command result is invalid"
            ) from None

    def read_outbox(
        self,
        *,
        after_sequence: int,
        limit: int,
    ) -> tuple[InventoryEvent, ...]:
        """Read durable Inventory events after one exclusive sequence cursor."""

        if (
            isinstance(after_sequence, bool)
            or not isinstance(after_sequence, int)
            or after_sequence < 0
        ):
            raise MaterialInvalidInput("after_sequence must be a non-negative integer")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise MaterialInvalidInput("limit must be between 1 and 1000")
        try:
            rows = self._store.pending_outbox(after_sequence, limit)
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable("failed to read outbox") from None
        return tuple(_inventory_event(row) for row in rows)

    def get_acknowledged_sequence(self, *, consumer: str = "scheduler") -> int:
        """Return one consumer's independent durable acknowledgement watermark."""

        if consumer not in _CURSOR_NAMES:
            raise MaterialInvalidInput("consumer must be scheduler or cloud")

        try:
            return self._store.get_cursor(consumer)
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to read acknowledgement"
            ) from None

    def acknowledge(self, sequence: int, *, consumer: str = "scheduler") -> None:
        """Advance one consumer watermark monotonically without crossing consumers."""

        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise MaterialInvalidInput("sequence must be a non-negative integer")
        if consumer not in _CURSOR_NAMES:
            raise MaterialInvalidInput("consumer must be scheduler or cloud")
        try:
            with self._tx() as conn:
                maximum_row = conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS value FROM sync_outbox"
                ).fetchone()
                maximum = int(maximum_row["value"])
                if sequence > maximum:
                    raise MaterialConflict(
                        "acknowledgement cannot advance beyond the durable outbox"
                    )
                current_row = conn.execute(
                    "SELECT acked_sequence FROM sync_cursor WHERE cursor_name = ?",
                    (consumer,),
                ).fetchone()
                current = int(current_row["acked_sequence"]) if current_row else 0
                if sequence <= current:
                    return
                conn.execute(
                    """
                    INSERT INTO sync_cursor(cursor_name, acked_sequence, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(cursor_name) DO UPDATE SET
                        acked_sequence = excluded.acked_sequence,
                        updated_at = excluded.updated_at
                    WHERE excluded.acked_sequence > sync_cursor.acked_sequence
                    """,
                    (consumer, sequence, self._now_ms()),
                )
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to persist acknowledgement"
            ) from None

    def has_active_task_reservation(
        self,
        workflow_task_uuid: str,
        reservation_uuid: str,
    ) -> bool:
        """Prove that one Task still owns the named complete active Reservation."""

        canonical_task_uuid = _canonical_uuid(
            workflow_task_uuid,
            "workflow_task_uuid",
        )
        canonical_reservation_uuid = _canonical_uuid(
            reservation_uuid,
            "reservation_uuid",
        )
        try:
            row = self._store.query_one(
                """
                SELECT 1 AS present
                FROM material_reservation AS reservation
                WHERE reservation.uuid = ?
                  AND reservation.workflow_task_uuid = ?
                  AND reservation.status = 'active'
                  AND reservation.released_at IS NULL
                  AND EXISTS (
                      SELECT 1
                      FROM material_reservation_member AS member
                      WHERE member.reservation_uuid = reservation.uuid
                        AND member.released_at IS NULL
                  )
                """,
                (canonical_reservation_uuid, canonical_task_uuid),
            )
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to verify task reservation"
            ) from None
        return row is not None

    def create_site(
        self,
        *,
        site_uuid: str,
        description: str | None,
        meta_data: Mapping[str, Any] | None,
        material_uuid: str,
        name: str,
        sort_order: int,
        allowed_resource_template_uuids: Sequence[str],
        occupied_material_uuid: str | None,
        position_x: float,
        position_y: float,
        position_z: float,
        depth: float,
        length: float,
        width: float,
    ) -> SiteRecord:
        """Create one Backend-aligned Site and its normalized allowlist."""

        if description is not None and not isinstance(description, str):
            raise MaterialInvalidInput("description must be a string or null")
        if not isinstance(name, str) or not name.strip():
            raise MaterialInvalidInput("name must be a non-blank string")
        if isinstance(sort_order, bool) or not isinstance(sort_order, int):
            raise MaterialInvalidInput("sort_order must be a non-negative integer")
        if sort_order < 0 or sort_order > _MAX_SIGNED_64_BIT_INTEGER:
            raise MaterialInvalidInput("sort_order must be a non-negative integer")
        if isinstance(allowed_resource_template_uuids, (str, bytes)) or not isinstance(
            allowed_resource_template_uuids,
            Sequence,
        ):
            raise MaterialInvalidInput(
                "allowed_resource_template_uuids must be a UUID array"
            )

        allowed_templates: set[str] = set()
        for value in allowed_resource_template_uuids:
            canonical_template_uuid = _canonical_uuid(
                value,
                "allowed_resource_template_uuid",
            )
            if canonical_template_uuid not in self._resource_templates:
                raise MaterialInvalidInput(
                    "allowed resource template is not registered"
                )
            allowed_templates.add(canonical_template_uuid)

        geometry = {
            "position_x": _finite_number(position_x, "position_x"),
            "position_y": _finite_number(position_y, "position_y"),
            "position_z": _finite_number(position_z, "position_z"),
            "depth": _finite_number(depth, "depth"),
            "length": _finite_number(length, "length"),
            "width": _finite_number(width, "width"),
        }
        for field in ("depth", "length", "width"):
            if geometry[field] < 0:
                raise MaterialInvalidInput(f"{field} must not be negative")

        canonical_site_uuid = _canonical_uuid(site_uuid, "site_uuid")
        canonical_material_uuid = _canonical_uuid(material_uuid, "material_uuid")
        canonical_occupant_uuid = (
            _canonical_uuid(occupied_material_uuid, "occupied_material_uuid")
            if occupied_material_uuid is not None
            else None
        )
        normalized_meta_data = _json_object(meta_data, "meta_data")
        now_iso = self._now_iso()
        now_ms = self._now_ms()
        try:
            with self._tx() as conn:
                owner = conn.execute(
                    "SELECT 1 FROM material WHERE uuid = ? AND deleted_at IS NULL",
                    (canonical_material_uuid,),
                ).fetchone()
                if owner is None:
                    raise MaterialNotFound("site owner material not found")

                if canonical_occupant_uuid is not None:
                    occupant = conn.execute(
                        """
                        SELECT resource_template_uuid
                        FROM material
                        WHERE uuid = ? AND deleted_at IS NULL
                        """,
                        (canonical_occupant_uuid,),
                    ).fetchone()
                    if occupant is None:
                        raise MaterialNotFound("site occupant material not found")
                    if canonical_occupant_uuid == canonical_material_uuid:
                        raise MaterialConflict("site placement would create a cycle")
                    if (
                        allowed_templates
                        and occupant["resource_template_uuid"] not in allowed_templates
                    ):
                        raise MaterialInvalidInput(
                            "occupied material template is not allowed by site"
                        )
                    would_cycle = conn.execute(
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
                        (canonical_occupant_uuid, canonical_material_uuid),
                    ).fetchone()
                    if would_cycle is not None:
                        raise MaterialConflict("site placement would create a cycle")

                conn.execute(
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
                        canonical_site_uuid,
                        now_iso,
                        now_iso,
                        description,
                        json.dumps(normalized_meta_data, ensure_ascii=False),
                        canonical_material_uuid,
                        name.strip(),
                        sort_order,
                        canonical_occupant_uuid,
                        geometry["position_x"],
                        geometry["position_y"],
                        geometry["position_z"],
                        geometry["depth"],
                        geometry["length"],
                        geometry["width"],
                    ),
                )
                for template_uuid in sorted(allowed_templates):
                    conn.execute(
                        """
                        INSERT INTO site_allowed_resource_template(
                            site_uuid, resource_template_uuid
                        ) VALUES (?, ?)
                        """,
                        (canonical_site_uuid, template_uuid),
                    )
                site = _read_site(conn, canonical_site_uuid)
                if site is None:
                    raise MaterialAuthorityUnavailable("created site is not readable")
                self._emit(
                    conn,
                    now_ms,
                    "site",
                    canonical_site_uuid,
                    1,
                    "site.created",
                    {"site": site.to_dict()},
                )
        except sqlite3.IntegrityError:
            raise MaterialConflict("site identity or placement conflicts") from None
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable("failed to create site") from None
        return site

    def get_site(self, site_uuid: str) -> SiteRecord:
        """Read one non-deleted Site."""

        canonical_site_uuid = _canonical_uuid(site_uuid, "site_uuid")
        try:
            with self._store.transaction() as conn:
                site = _read_site(conn, canonical_site_uuid)
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable("failed to read site") from None
        if site is None:
            raise MaterialNotFound(f"site {canonical_site_uuid} not found")
        return site

    def list_sites(self, material_uuid: str) -> tuple[SiteRecord, ...]:
        """List one Material's active Sites in stable display order."""

        canonical_material_uuid = _canonical_uuid(material_uuid, "material_uuid")
        try:
            with self._store.transaction() as conn:
                owner = conn.execute(
                    "SELECT 1 FROM material WHERE uuid = ? AND deleted_at IS NULL",
                    (canonical_material_uuid,),
                ).fetchone()
                if owner is None:
                    raise MaterialNotFound(
                        f"material {canonical_material_uuid} not found"
                    )
                rows = conn.execute(
                    """
                    SELECT uuid
                    FROM site
                    WHERE material_uuid = ? AND deleted_at IS NULL
                    ORDER BY sort_order, uuid
                    """,
                    (canonical_material_uuid,),
                ).fetchall()
                sites = tuple(_read_site(conn, row["uuid"]) for row in rows)
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable("failed to list sites") from None
        if any(site is None for site in sites):
            raise MaterialAuthorityUnavailable("listed site is not readable")
        return tuple(site for site in sites if site is not None)

    # ------------------------------------------------------------------
    # Canonical stock and read projections
    # ------------------------------------------------------------------

    def inbound_lot(
        self,
        *,
        resource_template_uuid: str,
        quantity: float,
        unit: str = "",
        batch_no: str = "",
        expiry: str = "",
        lot_id: str = "",
        warehouse_zone_id: str = "",
        actor: str = "",
        causation_id: str = "",
    ) -> dict[str, Any]:
        """Register stock against a Registry-owned ResourceTemplate UUID."""

        template_uuid = _canonical_uuid(
            resource_template_uuid,
            "resource_template_uuid",
        )
        if template_uuid not in self._resource_templates:
            raise MaterialInvalidInput("resource_template_uuid is not registered")
        normalized_quantity = _finite_number(quantity, "quantity")
        if normalized_quantity <= 0:
            raise InvariantViolation("inbound quantity must be greater than zero")
        if not all(
            isinstance(value, str)
            for value in (
                unit,
                batch_no,
                expiry,
                lot_id,
                warehouse_zone_id,
                actor,
                causation_id,
            )
        ):
            raise MaterialInvalidInput("lot text fields must be strings")
        canonical_lot_id = lot_id.strip() or f"lot-{uuid4().hex[:16]}"
        now = self._now_ms()
        try:
            with self._tx() as conn:
                row = conn.execute(
                    "SELECT * FROM inventory_lot WHERE lot_id = ?",
                    (canonical_lot_id,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO inventory_lot(
                            lot_id, resource_template_uuid, batch_no, unit,
                            quantity_total, quantity_available, quantity_reserved,
                            expiry, quarantined, warehouse_zone_id, created_at, version
                        ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, 0, ?, ?, 1)
                        """,
                        (
                            canonical_lot_id,
                            template_uuid,
                            batch_no,
                            unit,
                            normalized_quantity,
                            normalized_quantity,
                            expiry,
                            warehouse_zone_id,
                            now,
                        ),
                    )
                    lot = self._tx_get_lot(conn, canonical_lot_id)
                    event_type = "lot.created"
                else:
                    current = dict(row)
                    if current["resource_template_uuid"] != template_uuid:
                        raise MaterialConflict(
                            "lot_id already belongs to another resource template"
                        )
                    if current["unit"] != unit:
                        raise MaterialConflict("lot unit cannot change during inbound")
                    lot = self._tx_update_lot_quantities(
                        conn,
                        current,
                        d_total=normalized_quantity,
                        d_available=normalized_quantity,
                    )
                    event_type = "lot.inbound"
                self._emit(
                    conn,
                    now,
                    "lot",
                    canonical_lot_id,
                    lot["version"],
                    event_type,
                    {
                        "resource_template_uuid": template_uuid,
                        "quantity": normalized_quantity,
                        "unit": unit,
                        "batch_no": batch_no,
                        "expiry": expiry,
                        "quantity_total": lot["quantity_total"],
                        "quantity_available": lot["quantity_available"],
                    },
                    causation_id=causation_id,
                    actor=actor,
                )
        except sqlite3.IntegrityError:
            raise MaterialConflict("lot identity or quantity conflicts") from None
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable("failed to register stock lot") from None
        return lot

    def adjust_lot(
        self,
        *,
        lot_id: str,
        new_total: float,
        reason: str,
        actor: str,
        causation_id: str = "",
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """Apply one audited lot count correction without changing reservations."""

        if not isinstance(lot_id, str) or not lot_id.strip():
            raise MaterialInvalidInput("lot_id must not be blank")
        if not isinstance(reason, str) or not reason.strip():
            raise MaterialInvalidInput("adjust requires a reason")
        if not isinstance(actor, str) or not actor.strip():
            raise MaterialInvalidInput("adjust requires an actor")
        normalized_total = _finite_number(new_total, "new_total")
        now = self._now_ms()
        try:
            with self._tx() as conn:
                lot = self._tx_get_lot(conn, lot_id.strip())
                self._tx_check_version(lot, expected_version)
                delta = normalized_total - float(lot["quantity_total"])
                lot = self._tx_update_lot_quantities(
                    conn,
                    lot,
                    d_total=delta,
                    d_available=delta,
                )
                self._emit(
                    conn,
                    now,
                    "lot",
                    lot["lot_id"],
                    lot["version"],
                    "lot.adjusted",
                    {
                        "delta": delta,
                        "new_total": lot["quantity_total"],
                        "quantity_available": lot["quantity_available"],
                        "reason": reason.strip(),
                    },
                    causation_id=causation_id,
                    actor=actor.strip(),
                    reason=reason.strip(),
                )
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable("failed to adjust stock lot") from None
        return lot

    def inventory_snapshot(self) -> dict[str, Any]:
        """Return one consistent projection without exposing rows or connections."""

        try:
            with self._store.transaction() as conn:
                material_rows = conn.execute(
                    "SELECT * FROM material ORDER BY uuid"
                ).fetchall()
                site_ids = tuple(
                    row["uuid"]
                    for row in conn.execute(
                        "SELECT uuid FROM site ORDER BY uuid"
                    ).fetchall()
                )
                sites = tuple(_read_site(conn, site_uuid) for site_uuid in site_ids)
                reservation_rows = conn.execute(
                    "SELECT * FROM material_reservation ORDER BY uuid"
                ).fetchall()
                reservations: list[dict[str, Any]] = []
                for row in reservation_rows:
                    projection = dict(row)
                    projection["members"] = [
                        dict(member)
                        for member in conn.execute(
                            """
                            SELECT * FROM material_reservation_member
                            WHERE reservation_uuid = ?
                            ORDER BY material_uuid
                            """,
                            (row["uuid"],),
                        ).fetchall()
                    ]
                    reservations.append(projection)
                lots = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT * FROM inventory_lot ORDER BY created_at, rowid"
                    ).fetchall()
                ]
                sequence_row = conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS value FROM sync_outbox"
                ).fetchone()
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to build inventory snapshot"
            ) from None
        if any(site is None for site in sites):
            raise MaterialAuthorityUnavailable("snapshot site is not readable")
        return {
            "materials": [_material_record(row).to_dict() for row in material_rows],
            "sites": [site.to_dict() for site in sites if site is not None],
            "material_reservations": reservations,
            "inventory_lots": lots,
            "snapshot_sequence": int(sequence_row["value"]),
        }

    def bootstrap_resource_graph(self, command: Mapping[str, Any]) -> dict[str, Any]:
        """仅在空库内原子导入一次 ResourceTreeSet；既有 durable truth 永不覆盖。"""

        if not isinstance(command, Mapping):
            raise MaterialInvalidInput("resource graph bootstrap must be an object")
        source_id = str(command.get("source_id") or "").strip()
        fingerprint = str(command.get("fingerprint") or "").strip()
        if not source_id or not fingerprint.startswith("sha256:"):
            raise MaterialInvalidInput("resource graph bootstrap identity is invalid")
        materials = command.get("materials")
        positions = command.get("relative_positions")
        sites = command.get("sites")
        if not isinstance(materials, list) or not materials:
            raise MaterialInvalidInput("resource graph bootstrap requires materials")
        if not isinstance(positions, list) or not isinstance(sites, list):
            raise MaterialInvalidInput(
                "resource graph bootstrap collections are invalid"
            )
        now_iso = self._now_iso()
        now_ms = self._now_ms()
        try:
            with self._tx() as conn:
                existing = int(
                    conn.execute("SELECT COUNT(*) AS value FROM material").fetchone()[
                        "value"
                    ]
                )
                stored_row = conn.execute(
                    "SELECT meta_value FROM lab_meta WHERE meta_key = ?",
                    ("resource_graph_bootstrap_fingerprint",),
                ).fetchone()
                stored = str(stored_row["meta_value"]) if stored_row else ""
                if existing:
                    return {
                        "status": "unchanged" if stored == fingerprint else "preserved",
                        "source_id": source_id,
                        "fingerprint": stored or None,
                        "material_count": existing,
                    }

                material_ids: set[str] = set()
                parent_by_material: dict[str, str | None] = {}
                for raw in materials:
                    if not isinstance(raw, Mapping):
                        raise MaterialInvalidInput(
                            "bootstrap material must be an object"
                        )
                    material_uuid = _canonical_uuid(raw.get("uuid"), "material.uuid")
                    if material_uuid in material_ids:
                        raise MaterialConflict("bootstrap Material UUID must be unique")
                    material_ids.add(material_uuid)
                    template_uuid = _canonical_uuid(
                        raw.get("resource_template_uuid"),
                        "material.resource_template_uuid",
                    )
                    if template_uuid not in self._resource_templates:
                        raise MaterialInvalidInput(
                            "bootstrap resource_template_uuid is not registered"
                        )
                    parent_value = raw.get("parent_uuid")
                    parent_by_material[material_uuid] = (
                        _canonical_uuid(parent_value, "material.parent_uuid")
                        if parent_value is not None
                        else None
                    )
                    material_kind = str(raw.get("material_kind") or "")
                    if material_kind not in {"business", "device"}:
                        raise MaterialInvalidInput("bootstrap material_kind is invalid")
                    name = str(raw.get("name") or "").strip()
                    klass = str(raw.get("class") or "").strip()
                    if not name or not klass:
                        raise MaterialInvalidInput(
                            "bootstrap Material name/class must not be blank"
                        )
                    description = raw.get("description")
                    if description is not None and not isinstance(description, str):
                        raise MaterialInvalidInput(
                            "bootstrap Material description must be string or null"
                        )
                    barcode = str(raw.get("barcode") or "")
                    conn.execute(
                        """
                        INSERT INTO material(
                            uuid, create_time, update_time, deleted_at,
                            description, meta_data, resource_template_uuid,
                            parent_uuid, class, barcode, name, config, data,
                            disposition, material_kind, version
                        ) VALUES (?, ?, ?, NULL, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            material_uuid,
                            now_iso,
                            now_iso,
                            description,
                            json.dumps(_json_object(raw.get("meta_data"), "meta_data")),
                            template_uuid,
                            klass,
                            barcode,
                            name,
                            json.dumps(_json_object(raw.get("config"), "config")),
                            json.dumps(_json_object(raw.get("data"), "data")),
                            "active" if material_kind == "business" else None,
                            material_kind,
                        ),
                    )
                for material_uuid, parent_uuid in parent_by_material.items():
                    if parent_uuid is None:
                        continue
                    if parent_uuid not in material_ids or parent_uuid == material_uuid:
                        raise MaterialInvalidInput(
                            "bootstrap Material parent is invalid"
                        )
                    conn.execute(
                        "UPDATE material SET parent_uuid = ? WHERE uuid = ?",
                        (parent_uuid, material_uuid),
                    )

                for raw in positions:
                    if not isinstance(raw, Mapping):
                        raise MaterialInvalidInput(
                            "bootstrap relative_position must be an object"
                        )
                    position_uuid = _canonical_uuid(
                        raw.get("uuid"), "relative_position.uuid"
                    )
                    material_uuid = _canonical_uuid(
                        raw.get("material_uuid"), "relative_position.material_uuid"
                    )
                    if material_uuid not in material_ids:
                        raise MaterialInvalidInput(
                            "bootstrap relative_position Material is missing"
                        )
                    values = {
                        key: _finite_number(raw.get(key), f"relative_position.{key}")
                        for key in (
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
                        )
                    }
                    if any(values[key] < 0 for key in ("depth", "length", "width")):
                        raise MaterialInvalidInput(
                            "bootstrap relative_position dimensions "
                            "must be non-negative"
                        )
                    if any(
                        values[key] <= 0 for key in ("scale_x", "scale_y", "scale_z")
                    ):
                        raise MaterialInvalidInput(
                            "bootstrap relative_position scale must be positive"
                        )
                    conn.execute(
                        """
                        INSERT INTO relative_position(
                            uuid, create_time, update_time, deleted_at,
                            description, meta_data, material_uuid,
                            position_x, position_y, position_z,
                            depth, length, width, scale_x, scale_y, scale_z,
                            rotation_x, rotation_y, rotation_z
                        ) VALUES (
                            ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        (
                            position_uuid,
                            now_iso,
                            now_iso,
                            raw.get("description"),
                            json.dumps(_json_object(raw.get("meta_data"), "meta_data")),
                            material_uuid,
                            *(
                                values[key]
                                for key in (
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
                                )
                            ),
                        ),
                    )

                for raw in sites:
                    if not isinstance(raw, Mapping):
                        raise MaterialInvalidInput("bootstrap Site must be an object")
                    site_uuid = _canonical_uuid(raw.get("uuid"), "site.uuid")
                    owner_uuid = _canonical_uuid(
                        raw.get("material_uuid"), "site.material_uuid"
                    )
                    if owner_uuid not in material_ids:
                        raise MaterialInvalidInput("bootstrap Site owner is missing")
                    allowed = raw.get("allowed_resource_template_uuids")
                    if not isinstance(allowed, list):
                        raise MaterialInvalidInput(
                            "bootstrap Site allowlist is invalid"
                        )
                    allowed_uuids = tuple(
                        _canonical_uuid(value, "site.allowed_resource_template_uuids")
                        for value in allowed
                    )
                    if any(
                        value not in self._resource_templates for value in allowed_uuids
                    ):
                        raise MaterialInvalidInput(
                            "bootstrap Site allowlist template is not registered"
                        )
                    site_values = {
                        key: _finite_number(raw.get(key), f"site.{key}")
                        for key in (
                            "position_x",
                            "position_y",
                            "position_z",
                            "depth",
                            "length",
                            "width",
                        )
                    }
                    if any(
                        site_values[key] < 0 for key in ("depth", "length", "width")
                    ):
                        raise MaterialInvalidInput(
                            "bootstrap Site dimensions are invalid"
                        )
                    conn.execute(
                        """
                        INSERT INTO site(
                            uuid, create_time, update_time, deleted_at,
                            description, meta_data, material_uuid, name,
                            sort_order, occupied_material_uuid,
                            position_x, position_y, position_z,
                            depth, length, width, version
                        ) VALUES (
                            ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 1
                        )
                        """,
                        (
                            site_uuid,
                            now_iso,
                            now_iso,
                            raw.get("description"),
                            json.dumps(_json_object(raw.get("meta_data"), "meta_data")),
                            owner_uuid,
                            str(raw.get("name") or "").strip(),
                            int(raw.get("sort_order") or 0),
                            *(
                                site_values[key]
                                for key in (
                                    "position_x",
                                    "position_y",
                                    "position_z",
                                    "depth",
                                    "length",
                                    "width",
                                )
                            ),
                        ),
                    )
                    conn.executemany(
                        """
                        INSERT INTO site_allowed_resource_template(
                            site_uuid, resource_template_uuid
                        ) VALUES (?, ?)
                        """,
                        ((site_uuid, value) for value in sorted(set(allowed_uuids))),
                    )

                conn.executemany(
                    "INSERT INTO lab_meta(meta_key, meta_value) VALUES (?, ?)",
                    (
                        ("resource_graph_bootstrap_fingerprint", fingerprint),
                        ("resource_graph_bootstrap_source", source_id),
                    ),
                )
                self._emit(
                    conn,
                    now_ms,
                    "material_graph",
                    source_id,
                    1,
                    "material_graph.bootstrapped",
                    {
                        "source_id": source_id,
                        "fingerprint": fingerprint,
                        "material_count": len(materials),
                        "site_count": len(sites),
                    },
                )
        except (MaterialInvalidInput, MaterialConflict):
            raise
        except sqlite3.IntegrityError as exc:
            raise MaterialConflict("resource graph bootstrap conflicts") from exc
        except (sqlite3.Error, TypeError, ValueError, OverflowError) as exc:
            raise MaterialAuthorityUnavailable(
                "failed to bootstrap resource graph"
            ) from exc
        return {
            "status": "imported",
            "source_id": source_id,
            "fingerprint": fingerprint,
            "material_count": len(materials),
            "site_count": len(sites),
        }

    def list_backend_materials(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        name: str = "",
        barcode: str = "",
        resource_template_uuid: str = "",
    ) -> dict[str, Any]:
        """返回冻结 Backend ``GET /materials`` 的 data 对象。"""

        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise MaterialInvalidInput("page must be a positive integer")
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 100
        ):
            raise MaterialInvalidInput("page_size must be between 1 and 100")
        template_uuid = (
            _canonical_uuid(resource_template_uuid, "resource_template_uuid")
            if resource_template_uuid
            else ""
        )
        clauses = ["deleted_at IS NULL"]
        parameters: list[Any] = []
        if name.strip():
            clauses.append("LOWER(name) LIKE ?")
            parameters.append(f"%{name.strip().lower()}%")
        if barcode.strip():
            clauses.append("LOWER(barcode) LIKE ?")
            parameters.append(f"%{barcode.strip().lower()}%")
        if template_uuid:
            clauses.append("resource_template_uuid = ?")
            parameters.append(template_uuid)
        where = " AND ".join(clauses)
        try:
            with self._store.transaction() as conn:
                total = int(
                    conn.execute(
                        f"SELECT COUNT(*) AS value FROM material WHERE {where}",
                        tuple(parameters),
                    ).fetchone()["value"]
                )
                rows = conn.execute(
                    f"""
                    SELECT * FROM material WHERE {where}
                    ORDER BY create_time DESC, uuid DESC LIMIT ? OFFSET ?
                    """,
                    (*parameters, page_size, (page - 1) * page_size),
                ).fetchall()
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable("failed to list Materials") from None
        return {
            "items": [_backend_material(_material_record(row)) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def backend_material_graph(self) -> dict[str, Any]:
        """返回冻结 Backend ``GET /materials/graph`` 的完整一致快照。"""

        try:
            with self._store.transaction() as conn:
                material_rows = conn.execute(
                    """
                    SELECT * FROM material WHERE deleted_at IS NULL
                    ORDER BY create_time ASC, uuid ASC
                    """
                ).fetchall()
                position_rows = conn.execute(
                    """
                    SELECT * FROM relative_position WHERE deleted_at IS NULL
                    ORDER BY material_uuid ASC, create_time ASC, uuid ASC
                    """
                ).fetchall()
                site_ids = tuple(
                    row["uuid"]
                    for row in conn.execute(
                        """
                        SELECT uuid FROM site WHERE deleted_at IS NULL
                        ORDER BY material_uuid ASC, sort_order ASC,
                                 create_time ASC, uuid ASC
                        """
                    ).fetchall()
                )
                sites = tuple(_read_site(conn, site_uuid) for site_uuid in site_ids)
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to build Backend MaterialGraph"
            ) from None
        position_by_material = {
            row["material_uuid"]: _backend_relative_position(row)
            for row in position_rows
        }
        sites_by_owner: dict[str, list[dict[str, Any]]] = {}
        current_site_by_occupant: dict[str, str] = {}
        for site in sites:
            if site is None:
                raise MaterialAuthorityUnavailable("MaterialGraph Site is unreadable")
            sites_by_owner.setdefault(site.material_uuid, []).append(
                _backend_site(site, graph=True)
            )
            if site.occupied_material_uuid is not None:
                current_site_by_occupant[site.occupied_material_uuid] = site.uuid
        return {
            "nodes": [
                {
                    "material": _backend_material(record),
                    "relative_position": position_by_material.get(record.uuid),
                    "sites": sites_by_owner.get(record.uuid, []),
                    "current_site_uuid": current_site_by_occupant.get(record.uuid),
                    "handles": [],
                }
                for record in (_material_record(row) for row in material_rows)
            ]
        }

    def backend_material_detail(self, material_uuid: str) -> dict[str, Any]:
        """返回冻结 Backend ``GET /materials/{uuid}`` 的 MaterialDetail。"""

        material = self.get_material(material_uuid)
        graph = self.backend_material_graph()
        node = next(
            item for item in graph["nodes"] if item["material"]["uuid"] == material.uuid
        )
        current_site = None
        if node["current_site_uuid"] is not None:
            current_site = _backend_site(
                self.get_site(node["current_site_uuid"]),
            )
        return {
            **node["material"],
            "relative_position": node["relative_position"],
            "sites": node["sites"],
            "current_site": current_site,
        }

    def list_material_shapes(self) -> list[dict[str, Any]]:
        """返回 PackageCatalog 审计过的静态 2.5D shape assets。"""

        return [json.loads(json.dumps(item)) for item in self._material_shapes]

    def read_ledger(
        self, *, after_id: int = 0, limit: int = 200
    ) -> tuple[dict[str, Any], ...]:
        """Read stable audit projections for the private Inventory adapter."""

        if isinstance(after_id, bool) or not isinstance(after_id, int) or after_id < 0:
            raise MaterialInvalidInput("after_id must be a non-negative integer")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise MaterialInvalidInput("limit must be between 1 and 1000")
        try:
            rows = self._store.query_all(
                """
                SELECT * FROM inventory_ledger
                WHERE ledger_id > ? ORDER BY ledger_id LIMIT ?
                """,
                (after_id, limit),
            )
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to read inventory ledger"
            ) from None
        return tuple(rows)

    def outbox_status(self, *, consumer: str = "cloud") -> dict[str, int]:
        """Return durable outbox high-water marks through the public service seam."""

        try:
            maximum = self._store.max_outbox_sequence()
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable("failed to read outbox status") from None
        acknowledged = self.get_acknowledged_sequence(consumer=consumer)
        return {
            "max_sequence": maximum,
            "acked_sequence": acknowledged,
            "backlog": max(0, maximum - acknowledged),
        }

    def get_lab_profile(self) -> dict[str, str]:
        """Read the independent 2D lab-layout profile."""

        try:
            return {
                "name": self._store.get_meta("lab_name", "Uni-Lab 实验室"),
                "domain": self._store.get_meta("lab_domain", "general"),
            }
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable("failed to read lab profile") from None

    def update_lab_profile(
        self,
        *,
        name: str | None = None,
        domain: str | None = None,
    ) -> dict[str, str]:
        """Update lab-layout metadata without changing Material or Site truth."""

        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise MaterialInvalidInput("lab name must not be blank")
        if domain is not None and (not isinstance(domain, str) or not domain.strip()):
            raise MaterialInvalidInput("lab domain must not be blank")
        try:
            with self._tx() as conn:
                for key, value in (("lab_name", name), ("lab_domain", domain)):
                    if value is not None:
                        conn.execute(
                            """
                            INSERT INTO lab_meta(meta_key, meta_value) VALUES (?, ?)
                            ON CONFLICT(meta_key) DO UPDATE SET
                                meta_value = excluded.meta_value
                            """,
                            (key, value.strip()),
                        )
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable("failed to update lab profile") from None
        return self.get_lab_profile()

    def upsert_lab_zone(self, zone: Mapping[str, Any]) -> dict[str, Any]:
        """Upsert one 2D layout zone through the InventoryService boundary."""

        zone_id = str(zone.get("zone_id") or "").strip()
        if not zone_id:
            raise MaterialInvalidInput("zone_id must not be blank")
        meta = _json_object(zone.get("meta"), "zone.meta")
        now = self._now_ms()
        try:
            with self._tx() as conn:
                conn.execute(
                    """
                    INSERT INTO lab_zone(
                        zone_id, name, kind, x, y, w, h, meta_json, version
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(zone_id) DO UPDATE SET
                        name = excluded.name,
                        kind = excluded.kind,
                        x = excluded.x,
                        y = excluded.y,
                        w = excluded.w,
                        h = excluded.h,
                        meta_json = excluded.meta_json,
                        version = lab_zone.version + 1
                    """,
                    (
                        zone_id,
                        str(zone.get("name") or zone_id),
                        str(zone.get("kind") or "bench"),
                        _finite_number(zone.get("x", 0), "zone.x"),
                        _finite_number(zone.get("y", 0), "zone.y"),
                        _finite_number(zone.get("w", 100), "zone.w"),
                        _finite_number(zone.get("h", 100), "zone.h"),
                        json.dumps(meta, ensure_ascii=False),
                    ),
                )
                InventoryStore.tx_insert_ledger(
                    conn,
                    now,
                    "layout.zone_upsert",
                    "zone",
                    zone_id,
                    {"zone_id": zone_id},
                )
                row = conn.execute(
                    "SELECT * FROM lab_zone WHERE zone_id = ?",
                    (zone_id,),
                ).fetchone()
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable("failed to upsert lab zone") from None
        if row is None:
            raise MaterialAuthorityUnavailable("updated lab zone is not readable")
        return dict(row)

    def delete_lab_zone(self, zone_id: str) -> dict[str, Any]:
        """Delete one visual zone and detach its visual placements."""

        if not isinstance(zone_id, str) or not zone_id.strip():
            raise MaterialInvalidInput("zone_id must not be blank")
        canonical_zone_id = zone_id.strip()
        try:
            with self._tx() as conn:
                conn.execute(
                    "DELETE FROM lab_zone WHERE zone_id = ?", (canonical_zone_id,)
                )
                conn.execute(
                    "UPDATE lab_placement SET zone_id = '' WHERE zone_id = ?",
                    (canonical_zone_id,),
                )
                InventoryStore.tx_insert_ledger(
                    conn,
                    self._now_ms(),
                    "layout.zone_delete",
                    "zone",
                    canonical_zone_id,
                    {},
                )
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable("failed to delete lab zone") from None
        return {"zone_id": canonical_zone_id, "deleted": True}

    def upsert_lab_placement(self, placement: Mapping[str, Any]) -> dict[str, Any]:
        """Upsert one visual placement; it never changes Site occupancy."""

        subject_id = str(placement.get("subject_id") or "").strip()
        if not subject_id:
            raise MaterialInvalidInput("subject_id must not be blank")
        meta = _json_object(placement.get("meta"), "placement.meta")
        try:
            with self._tx() as conn:
                conn.execute(
                    """
                    INSERT INTO lab_placement(
                        subject_id, subject_kind, zone_id, x, y, w, h,
                        rotation, label, meta_json, version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(subject_id) DO UPDATE SET
                        subject_kind = excluded.subject_kind,
                        zone_id = excluded.zone_id,
                        x = excluded.x,
                        y = excluded.y,
                        w = excluded.w,
                        h = excluded.h,
                        rotation = excluded.rotation,
                        label = excluded.label,
                        meta_json = excluded.meta_json,
                        version = lab_placement.version + 1
                    """,
                    (
                        subject_id,
                        str(placement.get("subject_kind") or "container"),
                        str(placement.get("zone_id") or ""),
                        _finite_number(placement.get("x", 0), "placement.x"),
                        _finite_number(placement.get("y", 0), "placement.y"),
                        _finite_number(placement.get("w", 40), "placement.w"),
                        _finite_number(placement.get("h", 40), "placement.h"),
                        _finite_number(
                            placement.get("rotation", 0),
                            "placement.rotation",
                        ),
                        str(placement.get("label") or ""),
                        json.dumps(meta, ensure_ascii=False),
                    ),
                )
                InventoryStore.tx_insert_ledger(
                    conn,
                    self._now_ms(),
                    "layout.placement_upsert",
                    "placement",
                    subject_id,
                    {},
                )
                row = conn.execute(
                    "SELECT * FROM lab_placement WHERE subject_id = ?",
                    (subject_id,),
                ).fetchone()
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to upsert lab placement"
            ) from None
        if row is None:
            raise MaterialAuthorityUnavailable("updated lab placement is not readable")
        return dict(row)

    def delete_lab_placement(self, subject_id: str) -> dict[str, Any]:
        """Delete one visual placement without moving its Material."""

        if not isinstance(subject_id, str) or not subject_id.strip():
            raise MaterialInvalidInput("subject_id must not be blank")
        canonical_subject_id = subject_id.strip()
        try:
            with self._tx() as conn:
                conn.execute(
                    "DELETE FROM lab_placement WHERE subject_id = ?",
                    (canonical_subject_id,),
                )
                InventoryStore.tx_insert_ledger(
                    conn,
                    self._now_ms(),
                    "layout.placement_delete",
                    "placement",
                    canonical_subject_id,
                    {},
                )
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to delete lab placement"
            ) from None
        return {"subject_id": canonical_subject_id, "deleted": True}

    def get_lab_layout(self) -> dict[str, Any]:
        """Project visual layout and canonical Material identities together."""

        try:
            with self._store.transaction() as conn:
                zones = [
                    dict(row)
                    for row in conn.execute("SELECT * FROM lab_zone ORDER BY zone_id")
                ]
                placement_rows = conn.execute(
                    "SELECT * FROM lab_placement ORDER BY subject_id"
                ).fetchall()
                placements: list[dict[str, Any]] = []
                for row in placement_rows:
                    projection = dict(row)
                    material_row = conn.execute(
                        "SELECT * FROM material WHERE uuid = ? AND deleted_at IS NULL",
                        (row["subject_id"],),
                    ).fetchone()
                    projection["material"] = (
                        _material_record(material_row).to_dict()
                        if material_row is not None
                        else None
                    )
                    count_row = conn.execute(
                        """
                        SELECT COUNT(*) AS value FROM material
                        WHERE parent_uuid = ? AND deleted_at IS NULL
                        """,
                        (row["subject_id"],),
                    ).fetchone()
                    projection["children_count"] = int(count_row["value"])
                    placements.append(projection)
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable("failed to read lab layout") from None
        return {"zones": zones, "placements": placements}

    def get_material_assembly(self, material_uuid: str) -> dict[str, Any]:
        """Build the canonical Material.parent_uuid composition tree."""

        canonical_uuid = _canonical_uuid(material_uuid, "material_uuid")

        def project(
            conn: sqlite3.Connection, current_uuid: str, depth: int
        ) -> dict[str, Any]:
            row = conn.execute(
                "SELECT * FROM material WHERE uuid = ? AND deleted_at IS NULL",
                (current_uuid,),
            ).fetchone()
            if row is None:
                raise MaterialNotFound(f"material {current_uuid} not found")
            node = _material_record(row).to_dict()
            content = conn.execute(
                "SELECT state_json, version FROM material_content "
                "WHERE material_uuid = ?",
                (current_uuid,),
            ).fetchone()
            node["content"] = (
                {
                    "state": _stored_json_object(content["state_json"]),
                    "version": int(content["version"]),
                }
                if content is not None
                else None
            )
            node["children"] = []
            if depth < 6:
                children = conn.execute(
                    """
                    SELECT uuid FROM material
                    WHERE parent_uuid = ? AND deleted_at IS NULL
                    ORDER BY uuid
                    """,
                    (current_uuid,),
                ).fetchall()
                node["children"] = [
                    project(conn, child["uuid"], depth + 1) for child in children
                ]
            return node

        try:
            with self._store.transaction() as conn:
                root = project(conn, canonical_uuid, 0)
                placement = conn.execute(
                    "SELECT * FROM lab_placement WHERE subject_id = ?",
                    (canonical_uuid,),
                ).fetchone()
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to read material assembly"
            ) from None
        return {"root": root, "placement": dict(placement) if placement else None}

    @staticmethod
    def _tx_check_version(row: Mapping[str, Any], expected_version: int | None) -> None:
        if expected_version is not None and int(row["version"]) != expected_version:
            raise MaterialConflict(
                f"expected version {expected_version}, current {row['version']}"
            )
