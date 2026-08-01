"""仓储业务写操作.

每个写操作 = 单个 SQLite 事务：业务行更新 + inventory_ledger + sync_outbox 一起提交。
领域不变量在此层强制（数量非负 / available+reserved<=total / barcode active 唯一 /
(workflow_id,node_id,attempt) 幂等 / move 不改数量）。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import UUID

from unilabos.app.scheduler.inventory.domain import (
    ACTIVE_INSTANCE_STATES,
    CommandRejected,
    DuplicateBarcode,
    InstanceState,
    InsufficientStock,
    InvariantViolation,
    MaterialAuthorityUnavailable,
    MaterialConflict,
    MaterialInvalidInput,
    MaterialNotFound,
    MaterialRecord,
    MaterialRequirement,
    NotFound,
    ReservationState,
    ResourceSlotResolution,
    ResourceTemplateIdentity,
    SiteRecord,
    TaskMaterialAdmissionCommand,
    TaskMaterialAdmissionResult,
    TaskMaterialAdmissionSource,
    TaskMaterialBinding,
    TaskMaterialReleaseCommand,
    TaskMaterialReleaseResult,
    VersionConflict,
    check_instance_transition,
    check_lot_invariants,
    new_event_id,
)
from unilabos.app.scheduler.inventory.store import InventoryStore

_ACTIVE_STATES_TUPLE = tuple(s.value for s in ACTIVE_INSTANCE_STATES)
_MAX_SIGNED_64_BIT_INTEGER = (1 << 63) - 1


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
    ):
        self.store = store
        self.edge_id = edge_id
        self.lab_id = lab_id
        self._time_fn = time_fn
        # 实时监控总线（duck-typed emit(channel, type, data)）；None = 关闭
        self._monitor = monitor
        # 事务内暂存的监控事件（提交成功才发布，回滚即丢弃）
        self._tx_local = threading.local()
        canonical_templates: dict[str, ResourceTemplateIdentity] = {}
        for key, identity in (resource_templates or {}).items():
            if not isinstance(identity, ResourceTemplateIdentity):
                raise MaterialInvalidInput(
                    "resource_templates must contain ResourceTemplateIdentity values"
                )
            canonical_key = _canonical_uuid(key, "resource_template key")
            canonical_identity_uuid = _canonical_uuid(
                identity.uuid,
                "resource_template identity uuid",
            )
            if canonical_key != canonical_identity_uuid:
                raise MaterialInvalidInput(
                    "resource_template key must match identity uuid"
                )
            material_class = identity.material_class.strip()
            if not material_class:
                raise MaterialInvalidInput("resource_template class must not be blank")
            canonical_templates[canonical_key] = ResourceTemplateIdentity(
                uuid=canonical_identity_uuid,
                material_class=material_class,
            )
        self._resource_templates = MappingProxyType(canonical_templates)

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
        )

    def close(self) -> None:
        """Close the InventoryService-owned durable store."""

        self.store.close()

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
        """业务事务 + 监控事件缓冲：commit 成功后才把 material 事件发到总线."""
        events: list[dict[str, Any]] = []
        self._tx_local.events = events
        store = self.store
        try:
            with store.transaction() as conn:
                yield conn
        finally:
            self._tx_local.events = None
        # 到这里说明事务已提交（异常路径在 finally 清理后向上抛，不会执行到此）
        if self._monitor is not None:
            for data in events:
                try:
                    self._monitor.emit("material", data.pop("event_type"), data)
                except Exception:  # noqa: BLE001, S110 - 监控故障不影响业务
                    pass

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
                }
            )
        return outbox_sequence

    @staticmethod
    def _tx_get_lot(conn: sqlite3.Connection, lot_id: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM inventory_lot WHERE lot_id = ?", (lot_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"lot {lot_id} not found")
        return dict(row)

    @staticmethod
    def _tx_get_instance(conn: sqlite3.Connection, edge_uuid: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM material_instance WHERE edge_uuid = ?", (edge_uuid,)
        ).fetchone()
        if row is None:
            raise NotFound(f"instance {edge_uuid} not found")
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

    def _tx_set_instance_status(
        self,
        conn: sqlite3.Connection,
        instance: dict[str, Any],
        target: InstanceState,
    ) -> dict[str, Any]:
        check_instance_transition(InstanceState(instance["status"]), target)
        new_version = instance["version"] + 1
        conn.execute(
            "UPDATE material_instance SET status = ?, version = ? WHERE edge_uuid = ?",
            (target.value, new_version, instance["edge_uuid"]),
        )
        instance = dict(instance)
        instance.update(status=target.value, version=new_version)
        return instance

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
                conn.execute(
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
                        canonical_material_uuid,
                        now_iso,
                        now_iso,
                        description,
                        json.dumps(normalized_meta_data, ensure_ascii=False),
                        canonical_template_uuid,
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
            row = self.store.query_one(
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
        """Atomically resolve and reserve all explicit Materials for one Task."""

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
        seen_materials: set[str] = set()
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
                raise MaterialInvalidInput(
                    "material_source_node_uuid values must be unique"
                )
            seen_nodes.add(node_uuid)
            if source.mode != "existing":
                raise MaterialInvalidInput(
                    "M1R admission only supports explicit existing Materials"
                )
            template_uuid = _canonical_uuid(
                source.resource_template_uuid,
                "resource_template_uuid",
            )
            if template_uuid not in self._resource_templates:
                raise MaterialInvalidInput("resource_template_uuid is not registered")
            if source.material_uuid is None:
                raise MaterialInvalidInput(
                    "existing MaterialSource requires material_uuid"
                )
            material_uuid = _canonical_uuid(source.material_uuid, "material_uuid")
            if material_uuid in seen_materials:
                raise MaterialInvalidInput(
                    "a Material may be bound by only one MaterialSource"
                )
            seen_materials.add(material_uuid)
            if not isinstance(source.mount, Mapping):
                raise MaterialInvalidInput("mount must be a ResourceSlot object")
            mount_uuid = _canonical_uuid(
                str(source.mount.get("uuid", "")), "mount.uuid"
            )
            if mount_uuid != material_uuid:
                raise MaterialInvalidInput("mount.uuid must match material_uuid")
            site_uuid = (
                _canonical_uuid(source.site_uuid, "site_uuid")
                if source.site_uuid is not None
                else None
            )
            if type(source.candidate_site_uuids) is not tuple:
                raise MaterialInvalidInput("candidate_site_uuids must be a UUID tuple")
            candidate_site_uuids = tuple(
                _canonical_uuid(value, "candidate_site_uuid")
                for value in source.candidate_site_uuids
            )
            if len(set(candidate_site_uuids)) != len(candidate_site_uuids):
                raise MaterialInvalidInput("candidate_site_uuids must be unique")
            if not isinstance(source.flow_role, str) or not source.flow_role.strip():
                raise MaterialInvalidInput("flow_role must not be blank")
            normalized_sources.append(
                TaskMaterialAdmissionSource(
                    material_source_node_uuid=node_uuid,
                    mode="existing",
                    resource_template_uuid=template_uuid,
                    mount={"uuid": material_uuid},
                    material_uuid=material_uuid,
                    site_uuid=site_uuid,
                    candidate_site_uuids=tuple(sorted(candidate_site_uuids)),
                    flow_role=source.flow_role.strip(),
                )
            )

        normalized_command = TaskMaterialAdmissionCommand(
            schema_version=1,
            command_uuid=canonical_command_uuid,
            idempotency_key=command.idempotency_key,
            workflow_task_uuid=canonical_task_uuid,
            workflow_snapshot_fingerprint=command.workflow_snapshot_fingerprint,
            sources=tuple(normalized_sources),
        )
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
                if processed is not None:
                    if processed["payload_hash"] != payload_hash:
                        raise MaterialConflict(
                            "command_uuid was already used with a different payload"
                        )
                    return _admission_result_from_payload(
                        json.loads(processed["result_json"])
                    )

                bindings: list[TaskMaterialBinding] = []
                members: dict[str, tuple[str, int]] = {}
                for source in normalized_sources:
                    row = conn.execute(
                        """
                        SELECT uuid, resource_template_uuid, material_kind,
                               disposition, version
                        FROM material
                        WHERE uuid = ? AND deleted_at IS NULL
                        """,
                        (source.material_uuid,),
                    ).fetchone()
                    if row is None:
                        raise MaterialNotFound(
                            f"material {source.material_uuid} not found"
                        )
                    if row["resource_template_uuid"] != source.resource_template_uuid:
                        raise MaterialInvalidInput(
                            "Material template does not match MaterialSource"
                        )
                    if row["material_kind"] != "business":
                        raise MaterialInvalidInput(
                            "MaterialSource requires a business Material"
                        )
                    if row["disposition"] != "active":
                        raise MaterialConflict("Material is not runnable")
                    if source.site_uuid is not None:
                        site = conn.execute(
                            """
                            SELECT occupied_material_uuid
                            FROM site
                            WHERE uuid = ? AND deleted_at IS NULL
                            """,
                            (source.site_uuid,),
                        ).fetchone()
                        if site is None:
                            raise MaterialNotFound(f"site {source.site_uuid} not found")
                        if site["occupied_material_uuid"] != source.material_uuid:
                            raise MaterialConflict(
                                "Site does not contain the selected Material"
                            )
                    subtree = conn.execute(
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
                        (source.material_uuid,),
                    ).fetchall()
                    for item in subtree:
                        if item["material_kind"] != "business":
                            raise MaterialInvalidInput(
                                "Material reservation requires business Materials"
                            )
                        if item["disposition"] != "active":
                            raise MaterialConflict("Material is not runnable")
                        existing = members.get(item["uuid"])
                        if existing is None or source.material_uuid < existing[0]:
                            members[item["uuid"]] = (
                                source.material_uuid,
                                int(item["version"]),
                            )
                    bindings.append(
                        TaskMaterialBinding(
                            material_source_node_uuid=source.material_source_node_uuid,
                            resource_slot={
                                "uuid": source.material_uuid,
                                "resource_template_uuid": source.resource_template_uuid,
                            },
                            site_uuid=source.site_uuid,
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
                        raise MaterialConflict(
                            "Task already owns a different Material reservation"
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
            row = self.store.get_processed_command(canonical_command_uuid)
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
            with self.store.transaction() as conn:
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
            with self.store.transaction() as conn:
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
    # template / 品类模板
    # ------------------------------------------------------------------

    def upsert_template(
        self,
        template_id: str,
        name: str = "",
        category: str = "",
        spec: dict[str, Any] | None = None,
        actor: str = "",
        causation_id: str = "",
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """新建或更新资源模板；更新使用乐观版本并产生 ledger/outbox."""
        template_id = template_id.strip()
        if not template_id:
            raise CommandRejected("template_id required")
        now = self._now_ms()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM resource_template WHERE template_id = ?", (template_id,)
            ).fetchone()
            if row is None:
                if expected_version not in (None, 0):
                    raise VersionConflict(
                        f"expected version {expected_version}, current 0"
                    )
                version = 1
                conn.execute(
                    "INSERT INTO resource_template"
                    "(template_id, name, category, spec_json, version) VALUES (?,?,?,?,?)",
                    (
                        template_id,
                        name,
                        category,
                        json.dumps(spec or {}, ensure_ascii=False),
                        version,
                    ),
                )
                event_type = "template.created"
            else:
                current = dict(row)
                self._tx_check_version(current, expected_version)
                version = current["version"] + 1
                conn.execute(
                    "UPDATE resource_template SET name = ?, category = ?, spec_json = ?, "
                    "version = ? WHERE template_id = ?",
                    (
                        name if name != "" else current["name"],
                        category if category != "" else current["category"],
                        json.dumps(
                            spec
                            if spec is not None
                            else json.loads(current["spec_json"]),
                            ensure_ascii=False,
                        ),
                        version,
                        template_id,
                    ),
                )
                event_type = "template.updated"
            result = conn.execute(
                "SELECT * FROM resource_template WHERE template_id = ?", (template_id,)
            ).fetchone()
            assert result is not None
            self._emit(
                conn,
                now,
                "template",
                template_id,
                version,
                event_type,
                {
                    "name": result["name"],
                    "category": result["category"],
                    "spec": json.loads(result["spec_json"]),
                },
                causation_id=causation_id,
                actor=actor,
            )
        return dict(result)

    def delete_template(
        self,
        template_id: str,
        actor: str = "",
        causation_id: str = "",
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """删除无批次/实例引用的模板；有引用时拒绝，避免悬空领域对象."""
        now = self._now_ms()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM resource_template WHERE template_id = ?", (template_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"template {template_id} not found")
            current = dict(row)
            self._tx_check_version(current, expected_version)
            lot_count = conn.execute(
                "SELECT COUNT(*) FROM inventory_lot WHERE template_id = ?",
                (template_id,),
            ).fetchone()[0]
            instance_count = conn.execute(
                "SELECT COUNT(*) FROM material_instance WHERE template_id = ?",
                (template_id,),
            ).fetchone()[0]
            if lot_count or instance_count:
                raise CommandRejected(
                    f"template {template_id} is referenced by "
                    f"{lot_count} lot(s) and {instance_count} instance(s)"
                )
            conn.execute(
                "DELETE FROM resource_template WHERE template_id = ?", (template_id,)
            )
            self._emit(
                conn,
                now,
                "template",
                template_id,
                current["version"] + 1,
                "template.deleted",
                {},
                causation_id=causation_id,
                actor=actor,
            )
        return {"template_id": template_id, "deleted": True}

    # ------------------------------------------------------------------
    # inbound / 登记
    # ------------------------------------------------------------------

    def inbound_lot(
        self,
        template_id: str,
        quantity: float,
        unit: str = "",
        batch_no: str = "",
        expiry: str = "",
        lot_id: str = "",
        warehouse_zone_id: str = "",
        actor: str = "",
        causation_id: str = "",
    ) -> dict[str, Any]:
        """批次入库（数量层）；lot_id 已存在则追加数量."""
        if quantity <= 0:
            raise InvariantViolation(f"inbound quantity must be > 0, got {quantity}")
        now = self._now_ms()
        lot_id = lot_id or f"lot-{uuid.uuid4().hex[:16]}"
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM inventory_lot WHERE lot_id = ?", (lot_id,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO inventory_lot(lot_id, template_id, batch_no, unit, quantity_total, "
                    "quantity_available, quantity_reserved, expiry, quarantined, warehouse_zone_id, "
                    "created_at, version) VALUES (?,?,?,?,?,?,0,?,0,?,?,1)",
                    (
                        lot_id,
                        template_id,
                        batch_no,
                        unit,
                        quantity,
                        quantity,
                        expiry,
                        warehouse_zone_id,
                        now,
                    ),
                )
                lot = self._tx_get_lot(conn, lot_id)
                event_type = "lot.created"
            else:
                lot = self._tx_update_lot_quantities(
                    conn, dict(row), d_total=quantity, d_available=quantity
                )
                event_type = "lot.inbound"
            self._emit(
                conn,
                now,
                "lot",
                lot_id,
                lot["version"],
                event_type,
                {
                    "template_id": template_id,
                    "quantity": quantity,
                    "unit": unit,
                    "batch_no": batch_no,
                    "expiry": expiry,
                    "quantity_total": lot["quantity_total"],
                    "quantity_available": lot["quantity_available"],
                },
                causation_id=causation_id,
                actor=actor,
            )
        return lot

    def register_instance(
        self,
        template_id: str = "",
        lot_id: str = "",
        barcode: str = "",
        edge_uuid: str = "",
        legacy_cloud_id: str = "",
        parent_uuid: str = "",
        slot_id: str = "",
        actor: str = "",
        causation_id: str = "",
    ) -> dict[str, Any]:
        """实例登记（实体层）.

        edge_uuid 由 Edge 生成且永久稳定；cloud UUID 只写入 legacy_cloud_id 映射，
        永远不会覆盖 edge_uuid。
        """
        now = self._now_ms()
        edge_uuid = edge_uuid or f"mi-{uuid.uuid4().hex}"
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT * FROM material_instance WHERE edge_uuid = ?", (edge_uuid,)
            ).fetchone()
            if existing is not None:
                inst = dict(existing)
                # 幂等重放：仅补 legacy mapping，绝不改 edge_uuid/status
                if legacy_cloud_id and not inst["legacy_cloud_id"]:
                    conn.execute(
                        "UPDATE material_instance SET legacy_cloud_id = ? WHERE edge_uuid = ?",
                        (legacy_cloud_id, edge_uuid),
                    )
                    inst["legacy_cloud_id"] = legacy_cloud_id
                return inst
            if barcode:
                placeholders = ",".join("?" for _ in _ACTIVE_STATES_TUPLE)
                dup = conn.execute(
                    f"SELECT edge_uuid FROM material_instance WHERE barcode = ? "
                    f"AND status IN ({placeholders})",
                    (barcode, *_ACTIVE_STATES_TUPLE),
                ).fetchone()
                if dup is not None:
                    raise DuplicateBarcode(
                        f"barcode {barcode} already active on {dup['edge_uuid']}"
                    )
            conn.execute(
                "INSERT INTO material_instance(edge_uuid, legacy_cloud_id, lot_id, template_id, "
                "barcode, status, version) VALUES (?,?,?,?,?,?,1)",
                (
                    edge_uuid,
                    legacy_cloud_id,
                    lot_id,
                    template_id,
                    barcode,
                    InstanceState.WAREHOUSE.value,
                ),
            )
            if parent_uuid:
                conn.execute(
                    "INSERT INTO resource_relation(parent_uuid, slot_id, child_uuid, version) "
                    "VALUES (?,?,?,1) ON CONFLICT(child_uuid) DO UPDATE SET "
                    "parent_uuid = excluded.parent_uuid, slot_id = excluded.slot_id, "
                    "version = resource_relation.version + 1",
                    (parent_uuid, slot_id, edge_uuid),
                )
            self._emit(
                conn,
                now,
                "instance",
                edge_uuid,
                1,
                "instance.registered",
                {
                    "template_id": template_id,
                    "lot_id": lot_id,
                    "barcode": barcode,
                    "legacy_cloud_id": legacy_cloud_id,
                    "parent_uuid": parent_uuid,
                    "slot_id": slot_id,
                },
                causation_id=causation_id,
                actor=actor,
            )
            inst = self._tx_get_instance(conn, edge_uuid)
        return inst

    # ------------------------------------------------------------------
    # reserve / release / consume（workflow 幂等键）
    # ------------------------------------------------------------------

    def reserve_workflow(
        self,
        workflow_id: str,
        node_requirements: dict[str, list[MaterialRequirement]],
        attempt: int = 1,
        actor: str = "",
        causation_id: str = "",
    ) -> dict[str, Any]:
        """整 DAG 预留（all-or-nothing，单事务）.

        每个节点一行 reservation；任一节点不足则整体回滚并抛 InsufficientStock。
        (workflow_id, node_id, attempt) 幂等：已有 active/consumed 预留的节点跳过。
        """
        now = self._now_ms()
        created: list[str] = []
        with self._tx() as conn:
            for node_id, requirements in node_requirements.items():
                if not requirements:
                    continue
                existing = conn.execute(
                    "SELECT * FROM inventory_reservation WHERE workflow_id = ? AND node_id = ? "
                    "AND attempt = ?",
                    (workflow_id, node_id, attempt),
                ).fetchone()
                if existing is not None and existing["status"] in (
                    ReservationState.ACTIVE.value,
                    ReservationState.CONSUMED.value,
                ):
                    continue  # 幂等重放
                amounts = self._tx_allocate(
                    conn, now, workflow_id, node_id, requirements, actor, causation_id
                )
                reservation_id = f"rsv-{uuid.uuid4().hex[:16]}"
                if existing is not None:
                    conn.execute(
                        "UPDATE inventory_reservation SET status = ?, amounts_json = ?, "
                        "version = version + 1 WHERE workflow_id = ? AND node_id = ? AND attempt = ?",
                        (
                            ReservationState.ACTIVE.value,
                            json.dumps(amounts),
                            workflow_id,
                            node_id,
                            attempt,
                        ),
                    )
                    reservation_id = existing["reservation_id"]
                else:
                    conn.execute(
                        "INSERT INTO inventory_reservation(reservation_id, workflow_id, node_id, "
                        "attempt, status, amounts_json, created_at, version) VALUES (?,?,?,?,?,?,?,1)",
                        (
                            reservation_id,
                            workflow_id,
                            node_id,
                            attempt,
                            ReservationState.ACTIVE.value,
                            json.dumps(amounts),
                            now,
                        ),
                    )
                created.append(node_id)
                self._emit(
                    conn,
                    now,
                    "reservation",
                    reservation_id,
                    1,
                    "reservation.created",
                    {
                        "workflow_id": workflow_id,
                        "node_id": node_id,
                        "attempt": attempt,
                        "amounts": amounts,
                    },
                    causation_id=causation_id,
                    actor=actor,
                )
        return {"workflow_id": workflow_id, "reserved_nodes": created}

    def _tx_allocate(
        self,
        conn: sqlite3.Connection,
        now: int,
        workflow_id: str,
        node_id: str,
        requirements: list[MaterialRequirement],
        actor: str,
        causation_id: str,
    ) -> dict[str, Any]:
        """事务内为一个节点分配预留：FIFO 扣 lot available→reserved；实例置 RESERVED."""
        amounts: dict[str, Any] = {"lots": {}, "instances": []}
        for req in requirements:
            if req.is_instance_requirement():
                inst = self._tx_resolve_instance(conn, req)
                if inst["status"] != InstanceState.WAREHOUSE.value:
                    raise InsufficientStock(
                        f"instance {inst['edge_uuid']} not in warehouse (status={inst['status']})"
                    )
                inst = self._tx_set_instance_status(conn, inst, InstanceState.RESERVED)
                amounts["instances"].append(inst["edge_uuid"])
                self._emit(
                    conn,
                    now,
                    "instance",
                    inst["edge_uuid"],
                    inst["version"],
                    "instance.reserved",
                    {"workflow_id": workflow_id, "node_id": node_id},
                    causation_id=causation_id,
                    actor=actor,
                )
            elif req.quantity > 0:
                remaining = req.quantity
                candidates = self._tx_candidate_lots(conn, req)
                for lot in candidates:
                    if remaining <= 1e-9:
                        break
                    take = min(lot["quantity_available"], remaining)
                    if take <= 0:
                        continue
                    lot = self._tx_update_lot_quantities(
                        conn, lot, d_available=-take, d_reserved=take
                    )
                    amounts["lots"][lot["lot_id"]] = (
                        amounts["lots"].get(lot["lot_id"], 0.0) + take
                    )
                    remaining -= take
                    self._emit(
                        conn,
                        now,
                        "lot",
                        lot["lot_id"],
                        lot["version"],
                        "lot.reserved",
                        {
                            "workflow_id": workflow_id,
                            "node_id": node_id,
                            "quantity": take,
                            "quantity_available": lot["quantity_available"],
                            "quantity_reserved": lot["quantity_reserved"],
                        },
                        causation_id=causation_id,
                        actor=actor,
                    )
                if remaining > 1e-9:
                    raise InsufficientStock(
                        f"node {node_id}: short {remaining} of "
                        f"{req.lot_id or 'template:' + req.template_id}"
                    )
        return amounts

    @staticmethod
    def _tx_resolve_instance(
        conn: sqlite3.Connection, req: MaterialRequirement
    ) -> dict[str, Any]:
        if req.instance_uuid:
            row = conn.execute(
                "SELECT * FROM material_instance WHERE edge_uuid = ?",
                (req.instance_uuid,),
            ).fetchone()
        else:
            placeholders = ",".join("?" for _ in _ACTIVE_STATES_TUPLE)
            row = conn.execute(
                f"SELECT * FROM material_instance WHERE barcode = ? AND status IN ({placeholders})",
                (req.barcode, *_ACTIVE_STATES_TUPLE),
            ).fetchone()
        if row is None:
            raise NotFound(f"instance {req.instance_uuid or req.barcode} not found")
        return dict(row)

    def _tx_candidate_lots(
        self, conn: sqlite3.Connection, req: MaterialRequirement
    ) -> list[dict[str, Any]]:
        if req.lot_id:
            row = conn.execute(
                "SELECT * FROM inventory_lot WHERE lot_id = ? AND quarantined = 0",
                (req.lot_id,),
            ).fetchone()
            return [dict(row)] if row is not None else []
        # FIFO：created_at 升序，同毫秒按插入序（rowid）
        rows = conn.execute(
            "SELECT * FROM inventory_lot WHERE template_id = ? AND quarantined = 0 "
            "AND quantity_available > 0 ORDER BY created_at ASC, rowid ASC",
            (req.template_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def consume_reservation(
        self,
        workflow_id: str,
        node_id: str,
        attempt: int = 1,
        parent_uuid: str = "",
        slot_id: str = "",
        actor: str = "",
        causation_id: str = "",
    ) -> dict[str, Any]:
        """节点开始：预留 → 实际消费（lot reserved/total 扣减；实例 deploy 上台）.

        幂等：已 consumed 直接返回；无预留（无物料节点）no-op。
        """
        now = self._now_ms()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM inventory_reservation WHERE workflow_id = ? AND node_id = ? "
                "AND attempt = ?",
                (workflow_id, node_id, attempt),
            ).fetchone()
            if row is None:
                return {"status": "no_reservation"}
            rsv = dict(row)
            if rsv["status"] == ReservationState.CONSUMED.value:
                return {
                    "status": "already_consumed",
                    "reservation_id": rsv["reservation_id"],
                }
            if rsv["status"] != ReservationState.ACTIVE.value:
                raise CommandRejected(
                    f"reservation {rsv['reservation_id']} in {rsv['status']}, cannot consume"
                )
            amounts = json.loads(rsv["amounts_json"])
            for lot_id, qty in amounts.get("lots", {}).items():
                lot = self._tx_get_lot(conn, lot_id)
                lot = self._tx_update_lot_quantities(
                    conn, lot, d_total=-qty, d_reserved=-qty
                )
                self._emit(
                    conn,
                    now,
                    "lot",
                    lot_id,
                    lot["version"],
                    "lot.consumed",
                    {
                        "workflow_id": workflow_id,
                        "node_id": node_id,
                        "quantity": qty,
                        "quantity_total": lot["quantity_total"],
                    },
                    causation_id=causation_id,
                    actor=actor,
                )
            for inst_uuid in amounts.get("instances", []):
                inst = self._tx_get_instance(conn, inst_uuid)
                inst = self._tx_set_instance_status(conn, inst, InstanceState.BENCH)
                if parent_uuid:
                    self._tx_upsert_relation(conn, parent_uuid, slot_id, inst_uuid)
                self._emit(
                    conn,
                    now,
                    "instance",
                    inst_uuid,
                    inst["version"],
                    "instance.deployed",
                    {
                        "workflow_id": workflow_id,
                        "node_id": node_id,
                        "parent_uuid": parent_uuid,
                        "slot_id": slot_id,
                    },
                    causation_id=causation_id,
                    actor=actor,
                )
            conn.execute(
                "UPDATE inventory_reservation SET status = ?, version = version + 1 "
                "WHERE reservation_id = ?",
                (ReservationState.CONSUMED.value, rsv["reservation_id"]),
            )
            self._emit(
                conn,
                now,
                "reservation",
                rsv["reservation_id"],
                rsv["version"] + 1,
                "reservation.consumed",
                {"workflow_id": workflow_id, "node_id": node_id, "attempt": attempt},
                causation_id=causation_id,
                actor=actor,
            )
        return {
            "status": "consumed",
            "reservation_id": rsv["reservation_id"],
            "amounts": amounts,
        }

    def release_reservation(
        self,
        workflow_id: str,
        node_id: str,
        attempt: int = 1,
        reason: str = "",
        actor: str = "",
        causation_id: str = "",
    ) -> dict[str, Any]:
        """释放未消费的预留：lot reserved→available，实例 RESERVED→WAREHOUSE。幂等."""
        now = self._now_ms()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM inventory_reservation WHERE workflow_id = ? AND node_id = ? "
                "AND attempt = ?",
                (workflow_id, node_id, attempt),
            ).fetchone()
            if row is None:
                return {"status": "no_reservation"}
            rsv = dict(row)
            if rsv["status"] != ReservationState.ACTIVE.value:
                return {
                    "status": f"noop_{rsv['status']}",
                    "reservation_id": rsv["reservation_id"],
                }
            self._tx_release_amounts(
                conn,
                now,
                workflow_id,
                node_id,
                json.loads(rsv["amounts_json"]),
                reason,
                actor,
                causation_id,
            )
            conn.execute(
                "UPDATE inventory_reservation SET status = ?, version = version + 1 "
                "WHERE reservation_id = ?",
                (ReservationState.RELEASED.value, rsv["reservation_id"]),
            )
            self._emit(
                conn,
                now,
                "reservation",
                rsv["reservation_id"],
                rsv["version"] + 1,
                "reservation.released",
                {
                    "workflow_id": workflow_id,
                    "node_id": node_id,
                    "attempt": attempt,
                    "reason": reason,
                },
                causation_id=causation_id,
                actor=actor,
                reason=reason,
            )
        return {"status": "released", "reservation_id": rsv["reservation_id"]}

    def _tx_release_amounts(
        self,
        conn: sqlite3.Connection,
        now: int,
        workflow_id: str,
        node_id: str,
        amounts: dict[str, Any],
        reason: str,
        actor: str,
        causation_id: str,
    ) -> None:
        for lot_id, qty in amounts.get("lots", {}).items():
            lot = self._tx_get_lot(conn, lot_id)
            lot = self._tx_update_lot_quantities(
                conn, lot, d_available=qty, d_reserved=-qty
            )
            self._emit(
                conn,
                now,
                "lot",
                lot_id,
                lot["version"],
                "lot.released",
                {
                    "workflow_id": workflow_id,
                    "node_id": node_id,
                    "quantity": qty,
                    "quantity_available": lot["quantity_available"],
                },
                causation_id=causation_id,
                actor=actor,
                reason=reason,
            )
        for inst_uuid in amounts.get("instances", []):
            inst = self._tx_get_instance(conn, inst_uuid)
            if inst["status"] == InstanceState.RESERVED.value:
                inst = self._tx_set_instance_status(conn, inst, InstanceState.WAREHOUSE)
                self._emit(
                    conn,
                    now,
                    "instance",
                    inst_uuid,
                    inst["version"],
                    "instance.released",
                    {"workflow_id": workflow_id, "node_id": node_id},
                    causation_id=causation_id,
                    actor=actor,
                    reason=reason,
                )

    def quarantine_reservation(
        self,
        workflow_id: str,
        node_id: str,
        attempt: int = 1,
        reason: str = "node_failed",
        actor: str = "",
        causation_id: str = "",
    ) -> dict[str, Any]:
        """节点失败但物料已物理使用：实例转 QUARANTINED（人工复核），lot 不虚假加回."""
        now = self._now_ms()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM inventory_reservation WHERE workflow_id = ? AND node_id = ? "
                "AND attempt = ?",
                (workflow_id, node_id, attempt),
            ).fetchone()
            if row is None:
                return {"status": "no_reservation"}
            rsv = dict(row)
            if rsv["status"] != ReservationState.CONSUMED.value:
                return {
                    "status": f"noop_{rsv['status']}",
                    "reservation_id": rsv["reservation_id"],
                }
            amounts = json.loads(rsv["amounts_json"])
            for inst_uuid in amounts.get("instances", []):
                inst = self._tx_get_instance(conn, inst_uuid)
                if inst["status"] in (
                    InstanceState.BENCH.value,
                    InstanceState.IN_USE.value,
                ):
                    inst = self._tx_set_instance_status(
                        conn, inst, InstanceState.QUARANTINED
                    )
                    self._emit(
                        conn,
                        now,
                        "instance",
                        inst_uuid,
                        inst["version"],
                        "instance.quarantined",
                        {
                            "workflow_id": workflow_id,
                            "node_id": node_id,
                            "reason": reason,
                        },
                        causation_id=causation_id,
                        actor=actor,
                        reason=reason,
                    )
            conn.execute(
                "UPDATE inventory_reservation SET status = ?, version = version + 1 "
                "WHERE reservation_id = ?",
                (ReservationState.QUARANTINED.value, rsv["reservation_id"]),
            )
            self._emit(
                conn,
                now,
                "reservation",
                rsv["reservation_id"],
                rsv["version"] + 1,
                "reservation.quarantined",
                {
                    "workflow_id": workflow_id,
                    "node_id": node_id,
                    "attempt": attempt,
                    "reason": reason,
                },
                causation_id=causation_id,
                actor=actor,
                reason=reason,
            )
        return {"status": "quarantined", "reservation_id": rsv["reservation_id"]}

    def release_workflow(
        self,
        workflow_id: str,
        reason: str = "workflow_cancelled",
        actor: str = "",
        causation_id: str = "",
    ) -> dict[str, Any]:
        """cancel/restart：释放该 workflow 全部 active 预留（依据 DB 状态，不依赖内存）."""
        released: list[str] = []
        for rsv in self.store.reservations_for_workflow(workflow_id):
            if rsv["status"] == ReservationState.ACTIVE.value:
                self.release_reservation(
                    workflow_id,
                    rsv["node_id"],
                    rsv["attempt"],
                    reason=reason,
                    actor=actor,
                    causation_id=causation_id,
                )
                released.append(rsv["node_id"])
        return {"workflow_id": workflow_id, "released_nodes": released}

    # ------------------------------------------------------------------
    # deploy / move / consume / discard / adjust / content
    # ------------------------------------------------------------------

    @staticmethod
    def _tx_upsert_relation(
        conn: sqlite3.Connection, parent_uuid: str, slot_id: str, child_uuid: str
    ) -> None:
        """relation 主键是 child_uuid：transfer 时旧父关系被原子替换，源端不残留.

        单一父不变量：`material_instance.parent_uuid` 与 `relation.parent_uuid`
        始终一致——云端 `parent_material_uuid` 就是资源树父物料（≡ ResourceDict
        parent_uuid），relation 只补充「父物料的哪个具名位」（slot_id = PLR site
        名，↔ 云端 sites.label；uuid 仅后端索引）。每次 upsert 同步父列。
        """
        conn.execute(
            "INSERT INTO resource_relation(parent_uuid, slot_id, child_uuid, version) "
            "VALUES (?,?,?,1) ON CONFLICT(child_uuid) DO UPDATE SET "
            "parent_uuid = excluded.parent_uuid, slot_id = excluded.slot_id, "
            "version = resource_relation.version + 1",
            (parent_uuid, slot_id, child_uuid),
        )
        conn.execute(
            "UPDATE material_instance SET parent_uuid = ? WHERE edge_uuid = ?",
            (parent_uuid, child_uuid),
        )

    def deploy_instance(
        self,
        edge_uuid: str,
        parent_uuid: str = "",
        slot_id: str = "",
        actor: str = "",
        causation_id: str = "",
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        now = self._now_ms()
        with self._tx() as conn:
            inst = self._tx_get_instance(conn, edge_uuid)
            self._tx_check_version(inst, expected_version)
            inst = self._tx_set_instance_status(conn, inst, InstanceState.BENCH)
            if parent_uuid:
                self._tx_upsert_relation(conn, parent_uuid, slot_id, edge_uuid)
            self._emit(
                conn,
                now,
                "instance",
                edge_uuid,
                inst["version"],
                "instance.deployed",
                {"parent_uuid": parent_uuid, "slot_id": slot_id},
                causation_id=causation_id,
                actor=actor,
            )
        return inst

    def move_instance(
        self,
        edge_uuid: str,
        parent_uuid: str,
        slot_id: str = "",
        actor: str = "",
        causation_id: str = "",
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """move/transfer：只改物理层级关系，不改任何库存数量."""
        now = self._now_ms()
        with self._tx() as conn:
            inst = self._tx_get_instance(conn, edge_uuid)
            self._tx_check_version(inst, expected_version)
            old = conn.execute(
                "SELECT * FROM resource_relation WHERE child_uuid = ?", (edge_uuid,)
            ).fetchone()
            self._tx_upsert_relation(conn, parent_uuid, slot_id, edge_uuid)
            new_version = inst["version"] + 1
            conn.execute(
                "UPDATE material_instance SET version = ? WHERE edge_uuid = ?",
                (new_version, edge_uuid),
            )
            self._emit(
                conn,
                now,
                "instance",
                edge_uuid,
                new_version,
                "instance.moved",
                {
                    "from_parent": old["parent_uuid"] if old else "",
                    "from_slot": old["slot_id"] if old else "",
                    "to_parent": parent_uuid,
                    "to_slot": slot_id,
                },
                causation_id=causation_id,
                actor=actor,
            )
            inst = self._tx_get_instance(conn, edge_uuid)
        return inst

    def detach_instance(
        self,
        edge_uuid: str,
        actor: str = "",
        causation_id: str = "",
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """解除物理父关系；实例与库存数量保持不变，重复 detach 为幂等 no-op."""
        now = self._now_ms()
        with self._tx() as conn:
            inst = self._tx_get_instance(conn, edge_uuid)
            self._tx_check_version(inst, expected_version)
            old = conn.execute(
                "SELECT * FROM resource_relation WHERE child_uuid = ?", (edge_uuid,)
            ).fetchone()
            if old is None:
                return inst
            conn.execute(
                "DELETE FROM resource_relation WHERE child_uuid = ?", (edge_uuid,)
            )
            version = inst["version"] + 1
            # 单一父不变量：取下即脱离父物料（回到顶层/未分配）
            conn.execute(
                "UPDATE material_instance SET version = ?, parent_uuid = '' WHERE edge_uuid = ?",
                (version, edge_uuid),
            )
            self._emit(
                conn,
                now,
                "instance",
                edge_uuid,
                version,
                "instance.detached",
                {"from_parent": old["parent_uuid"], "from_slot": old["slot_id"]},
                causation_id=causation_id,
                actor=actor,
            )
            inst = self._tx_get_instance(conn, edge_uuid)
        return inst

    def set_instance_parent(
        self,
        edge_uuid: str,
        parent_uuid: str = "",
        slot_id: str | None = None,
        actor: str = "",
        causation_id: str = "",
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """设置/清除父物料（云端 parent_material_uuid ≡ 资源树 parent_uuid，单一父）。

        资源只有一个父层级：父物料 + 可选具名位（slot_id = PLR site 名，
        ↔ 云端 sites.label；uuid 仅后端索引）。语义：

        - parent_uuid 空串：顶层——父与具名位一并清除；
        - parent_uuid 非空、slot_id 空/None：有父但不占具名位（sites 讨论稿场景
          「父子关系不需要用 site 表达」），relation 行删除；
        - parent_uuid 非空、slot_id 非空：父 + 具名位，与 deploy/move 同一不变量
          （relation.parent 始终等于 parent_uuid 列）。

        沿 parent 链防环（云端由业务层校验，Edge 同等语义）。
        """
        now = self._now_ms()
        new_slot = slot_id or ""
        with self._tx() as conn:
            inst = self._tx_get_instance(conn, edge_uuid)
            self._tx_check_version(inst, expected_version)
            old_parent = inst.get("parent_uuid", "")
            old_rel = conn.execute(
                "SELECT slot_id FROM resource_relation WHERE child_uuid = ?",
                (edge_uuid,),
            ).fetchone()
            old_slot = old_rel["slot_id"] if old_rel else ""
            if parent_uuid == old_parent and new_slot == old_slot:
                return inst  # 幂等 no-op
            if parent_uuid:
                if parent_uuid == edge_uuid:
                    raise CommandRejected("instance cannot be its own parent")
                parent = conn.execute(
                    "SELECT edge_uuid, status, parent_uuid FROM material_instance "
                    "WHERE edge_uuid = ?",
                    (parent_uuid,),
                ).fetchone()
                if parent is None:
                    raise NotFound(f"parent instance {parent_uuid} not found")
                if parent["status"] not in {s.value for s in ACTIVE_INSTANCE_STATES}:
                    raise CommandRejected(
                        f"parent instance {parent_uuid} is {parent['status']}, not active"
                    )
                # 沿父链向上防环（链长即遍历深度）
                cursor, seen = parent["parent_uuid"], {parent_uuid}
                while cursor:
                    if cursor == edge_uuid or cursor in seen:
                        raise CommandRejected(
                            f"parent chain of {parent_uuid} would form a cycle"
                        )
                    seen.add(cursor)
                    row = conn.execute(
                        "SELECT parent_uuid FROM material_instance WHERE edge_uuid = ?",
                        (cursor,),
                    ).fetchone()
                    cursor = row["parent_uuid"] if row else ""
            new_version = inst["version"] + 1
            conn.execute(
                "UPDATE material_instance SET parent_uuid = ?, version = ? WHERE edge_uuid = ?",
                (parent_uuid, new_version, edge_uuid),
            )
            if parent_uuid and new_slot:
                self._tx_upsert_relation(conn, parent_uuid, new_slot, edge_uuid)
            else:
                conn.execute(
                    "DELETE FROM resource_relation WHERE child_uuid = ?", (edge_uuid,)
                )
            self._emit(
                conn,
                now,
                "instance",
                edge_uuid,
                new_version,
                "instance.parent_changed",
                {
                    "from_parent": old_parent,
                    "parent_uuid": parent_uuid,
                    "from_slot": old_slot,
                    "slot_id": new_slot,
                },
                causation_id=causation_id,
                actor=actor,
            )
            inst = self._tx_get_instance(conn, edge_uuid)
        return inst

    def consume_instance(
        self,
        edge_uuid: str,
        actor: str = "",
        causation_id: str = "",
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        return self._terminal_instance_op(
            edge_uuid,
            InstanceState.CONSUMED,
            "instance.consumed",
            "",
            actor,
            causation_id,
            expected_version,
        )

    def discard_instance(
        self,
        edge_uuid: str,
        reason: str = "",
        actor: str = "",
        causation_id: str = "",
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        return self._terminal_instance_op(
            edge_uuid,
            InstanceState.DISCARDED,
            "instance.discarded",
            reason,
            actor,
            causation_id,
            expected_version,
        )

    def _terminal_instance_op(
        self,
        edge_uuid: str,
        target: InstanceState,
        event_type: str,
        reason: str,
        actor: str,
        causation_id: str,
        expected_version: int | None,
    ) -> dict[str, Any]:
        """终态操作：删除物理关系（remove 真正持久化）+ 状态迁移."""
        now = self._now_ms()
        with self._tx() as conn:
            inst = self._tx_get_instance(conn, edge_uuid)
            self._tx_check_version(inst, expected_version)
            inst = self._tx_set_instance_status(conn, inst, target)
            conn.execute(
                "DELETE FROM resource_relation WHERE child_uuid = ?", (edge_uuid,)
            )
            # 终态实例不再是任何物料的组成部分（历史保留在 ledger）
            conn.execute(
                "UPDATE material_instance SET parent_uuid = '' WHERE edge_uuid = ?",
                (edge_uuid,),
            )
            inst["parent_uuid"] = ""
            self._emit(
                conn,
                now,
                "instance",
                edge_uuid,
                inst["version"],
                event_type,
                {"reason": reason},
                causation_id=causation_id,
                actor=actor,
                reason=reason,
            )
        return inst

    def adjust_lot(
        self,
        lot_id: str,
        new_total: float,
        reason: str,
        actor: str,
        causation_id: str = "",
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """人工盘点调整：必须带 reason + actor（审计），调整 total 并同步 available."""
        if not reason or not actor:
            raise CommandRejected("adjust requires both reason and actor for audit")
        now = self._now_ms()
        with self._tx() as conn:
            lot = self._tx_get_lot(conn, lot_id)
            self._tx_check_version(lot, expected_version)
            delta = new_total - lot["quantity_total"]
            # available 跟随 total 变化；不允许把 total 调到低于已预留量
            lot = self._tx_update_lot_quantities(
                conn, lot, d_total=delta, d_available=delta
            )
            self._emit(
                conn,
                now,
                "lot",
                lot_id,
                lot["version"],
                "lot.adjusted",
                {
                    "delta": delta,
                    "new_total": lot["quantity_total"],
                    "quantity_available": lot["quantity_available"],
                    "reason": reason,
                },
                causation_id=causation_id,
                actor=actor,
                reason=reason,
            )
        return lot

    def update_content(
        self,
        instance_uuid: str,
        state: dict[str, Any],
        actor: str = "",
        causation_id: str = "",
        expected_version: int | None = None,
        event_type: str = "content.updated",
    ) -> dict[str, Any]:
        """更新内容物状态（substance_content）."""
        now = self._now_ms()
        with self._tx() as conn:
            self._tx_get_instance(conn, instance_uuid)
            row = conn.execute(
                "SELECT * FROM substance_content WHERE instance_uuid = ?",
                (instance_uuid,),
            ).fetchone()
            if row is None and expected_version not in (None, 0):
                raise VersionConflict(f"expected version {expected_version}, current 0")
            if row is not None:
                self._tx_check_version(dict(row), expected_version)
            version = (row["version"] + 1) if row is not None else 1
            conn.execute(
                "INSERT INTO substance_content(instance_uuid, state_json, version) VALUES (?,?,?) "
                "ON CONFLICT(instance_uuid) DO UPDATE SET state_json = excluded.state_json, "
                "version = excluded.version",
                (instance_uuid, json.dumps(state, ensure_ascii=False), version),
            )
            self._emit(
                conn,
                now,
                "content",
                instance_uuid,
                version,
                event_type,
                {"state": state},
                causation_id=causation_id,
                actor=actor,
            )
        return {"instance_uuid": instance_uuid, "version": version, "state": state}

    def clear_content(
        self,
        instance_uuid: str,
        actor: str = "",
        causation_id: str = "",
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """清空内容物但保留行与递增版本，避免 optimistic version 回退."""
        return self.update_content(
            instance_uuid,
            {},
            actor=actor,
            causation_id=causation_id,
            expected_version=expected_version,
            event_type="content.cleared",
        )

    @staticmethod
    def _tx_check_version(row: dict[str, Any], expected_version: int | None) -> None:
        """乐观并发：expected_version 不匹配直接 reject（禁止 Last-Write-Wins）."""
        if expected_version is not None and row["version"] != expected_version:
            raise VersionConflict(
                f"expected version {expected_version}, current {row['version']}"
            )
