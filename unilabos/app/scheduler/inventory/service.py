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
    JobClaimAcquireCommand,
    JobClaimMemberRecord,
    JobClaimRecord,
    JobClaimReleaseCommand,
    JobClaimResolutionCommand,
    JobClaimResult,
    JobClaimStateCommand,
    JobClaimUncertainCommand,
    MaterialAuthorityUnavailable,
    MaterialChangeSetCommand,
    MaterialChangeSetEffect,
    MaterialChangeSetReceipt,
    MaterialClaimCorrupt,
    MaterialConflict,
    MaterialInvalidInput,
    MaterialModelAsset,
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


def _claim_member_payload(member: JobClaimMemberRecord) -> dict[str, Any]:
    return {
        "resource_kind": member.resource_kind,
        "resource_uuid": member.resource_uuid,
        "acquired_version": member.acquired_version,
        "expected_version": member.expected_version,
        "released_at": member.released_at,
    }


def _claim_payload(claim: JobClaimRecord) -> dict[str, Any]:
    return {
        "uuid": claim.uuid,
        "workflow_task_uuid": claim.workflow_task_uuid,
        "workflow_node_job_uuid": claim.workflow_node_job_uuid,
        "attempt": claim.attempt,
        "set_fingerprint": claim.set_fingerprint,
        "fencing_token": claim.fencing_token,
        "state": claim.state,
        "uncertainty_reason": claim.uncertainty_reason,
        "acquired_at": claim.acquired_at,
        "create_time": claim.create_time,
        "running_at": claim.running_at,
        "release_proof_kind": claim.release_proof_kind,
        "release_proof_fingerprint": claim.release_proof_fingerprint,
        "release_reason": claim.release_reason,
        "terminal_changeset_uuid": claim.terminal_changeset_uuid,
        "workflow_terminal_fingerprint": claim.workflow_terminal_fingerprint,
        "release_command_uuid": claim.release_command_uuid,
        "released_at": claim.released_at,
        "update_time": claim.update_time,
        "members": [_claim_member_payload(member) for member in claim.members],
    }


def _claim_from_payload(payload: Mapping[str, Any]) -> JobClaimRecord:
    return JobClaimRecord(
        uuid=str(payload["uuid"]),
        workflow_task_uuid=str(payload["workflow_task_uuid"]),
        workflow_node_job_uuid=str(payload["workflow_node_job_uuid"]),
        attempt=int(payload["attempt"]),
        set_fingerprint=str(payload["set_fingerprint"]),
        fencing_token=int(payload["fencing_token"]),
        state=str(payload["state"]),
        uncertainty_reason=payload.get("uncertainty_reason"),
        acquired_at=str(payload["acquired_at"]),
        create_time=str(payload["create_time"]),
        running_at=payload.get("running_at"),
        release_proof_kind=payload.get("release_proof_kind"),
        release_proof_fingerprint=payload.get("release_proof_fingerprint"),
        release_reason=payload.get("release_reason"),
        terminal_changeset_uuid=payload.get("terminal_changeset_uuid"),
        workflow_terminal_fingerprint=payload.get("workflow_terminal_fingerprint"),
        release_command_uuid=payload.get("release_command_uuid"),
        released_at=payload.get("released_at"),
        update_time=str(payload["update_time"]),
        members=tuple(
            JobClaimMemberRecord(
                resource_kind=str(member["resource_kind"]),
                resource_uuid=str(member["resource_uuid"]),
                acquired_version=int(member["acquired_version"]),
                expected_version=int(member["expected_version"]),
                released_at=member.get("released_at"),
            )
            for member in payload.get("members", [])
        ),
    )


def _claim_result_payload(result: JobClaimResult) -> dict[str, Any]:
    return {
        "schema_version": result.schema_version,
        "command_uuid": result.command_uuid,
        "status": result.status,
        "claim": _claim_payload(result.claim) if result.claim is not None else None,
        "diagnostics": [dict(item) for item in result.diagnostics],
        "outbox_sequence": result.outbox_sequence,
    }


def _claim_result_from_payload(payload: Mapping[str, Any]) -> JobClaimResult:
    raw_claim = payload.get("claim")
    return JobClaimResult(
        schema_version=int(payload["schema_version"]),
        command_uuid=str(payload["command_uuid"]),
        status=str(payload["status"]),
        claim=(
            _claim_from_payload(raw_claim) if isinstance(raw_claim, Mapping) else None
        ),
        diagnostics=tuple(dict(item) for item in payload.get("diagnostics", [])),
        outbox_sequence=(
            int(payload["outbox_sequence"])
            if payload.get("outbox_sequence") is not None
            else None
        ),
    )


def _effect_payload(effect: MaterialChangeSetEffect) -> dict[str, Any]:
    return {
        "effect_key": effect.effect_key,
        "resource_kind": effect.resource_kind,
        "resource_uuid": effect.resource_uuid,
        "operation": effect.operation,
        "expected_version": effect.expected_version,
        "before": dict(effect.before),
        "after": dict(effect.after),
    }


def _changeset_command_payload(
    command: MaterialChangeSetCommand,
) -> dict[str, Any]:
    return {
        "schema_version": command.schema_version,
        "command_uuid": command.command_uuid,
        "idempotency_key": command.idempotency_key,
        "workflow_task_uuid": command.workflow_task_uuid,
        "workflow_node_job_uuid": command.workflow_node_job_uuid,
        "attempt": command.attempt,
        "claim_uuid": command.claim_uuid,
        "fencing_token": command.fencing_token,
        "effect_identity": command.effect_identity,
        "outcome": command.outcome,
        "result": dict(command.result),
        "effects": [_effect_payload(effect) for effect in command.effects],
        "expected_claim_state": command.expected_claim_state,
    }


def _changeset_receipt_payload(
    receipt: MaterialChangeSetReceipt,
) -> dict[str, Any]:
    return {
        "schema_version": receipt.schema_version,
        "command_uuid": receipt.command_uuid,
        "uuid": receipt.uuid,
        "workflow_task_uuid": receipt.workflow_task_uuid,
        "workflow_node_job_uuid": receipt.workflow_node_job_uuid,
        "attempt": receipt.attempt,
        "claim_uuid": receipt.claim_uuid,
        "fencing_token": receipt.fencing_token,
        "effect_identity": receipt.effect_identity,
        "deterministic_fingerprint": receipt.deterministic_fingerprint,
        "outcome": receipt.outcome,
        "result": dict(receipt.result),
        "effects": [_effect_payload(effect) for effect in receipt.effects],
        "create_time": receipt.create_time,
        "outbox_sequence": receipt.outbox_sequence,
    }


def _changeset_receipt_from_payload(
    payload: Mapping[str, Any],
) -> MaterialChangeSetReceipt:
    return MaterialChangeSetReceipt(
        schema_version=int(payload["schema_version"]),
        command_uuid=str(payload["command_uuid"]),
        uuid=str(payload["uuid"]),
        workflow_task_uuid=str(payload["workflow_task_uuid"]),
        workflow_node_job_uuid=str(payload["workflow_node_job_uuid"]),
        attempt=int(payload["attempt"]),
        claim_uuid=str(payload["claim_uuid"]),
        fencing_token=int(payload["fencing_token"]),
        effect_identity=str(payload["effect_identity"]),
        deterministic_fingerprint=str(payload["deterministic_fingerprint"]),
        outcome=str(payload["outcome"]),
        result=dict(payload["result"]),
        effects=tuple(
            MaterialChangeSetEffect(
                effect_key=str(effect["effect_key"]),
                resource_kind=str(effect["resource_kind"]),
                resource_uuid=str(effect["resource_uuid"]),
                operation=str(effect["operation"]),
                expected_version=(
                    int(effect["expected_version"])
                    if effect.get("expected_version") is not None
                    else None
                ),
                before=dict(effect["before"]),
                after=dict(effect["after"]),
            )
            for effect in payload.get("effects", [])
        ),
        create_time=str(payload["create_time"]),
        outbox_sequence=int(payload["outbox_sequence"]),
    )


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
        material_model_assets: Sequence[MaterialModelAsset] = (),
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
        model_assets_by_path: dict[str, MaterialModelAsset] = {}
        for asset in material_model_assets:
            if not isinstance(asset, MaterialModelAsset):
                raise MaterialInvalidInput(
                    "material_model_assets must contain MaterialModelAsset values"
                )
            if not asset.public_path.startswith("/api/v1/material-models/"):
                raise MaterialInvalidInput(
                    "material model asset path is outside its API"
                )
            if asset.public_path in model_assets_by_path:
                raise MaterialInvalidInput("duplicate material model asset path")
            model_assets_by_path[asset.public_path] = asset
        self._material_model_assets = MappingProxyType(model_assets_by_path)

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
        material_model_assets: Sequence[MaterialModelAsset] = (),
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
            material_model_assets=material_model_assets,
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

    def resolve_material_ref(
        self,
        resource_id: str,
        *,
        uow: object | None = None,
    ) -> MaterialRecord:
        """Resolve a deployment resource id to one visible durable Material."""

        del uow
        if (
            not isinstance(resource_id, str)
            or not resource_id.strip()
            or (resource_id != resource_id.strip())
        ):
            raise MaterialInvalidInput("resource_id must be a non-empty string")
        try:
            rows = self._store.query_all(
                "SELECT * FROM material WHERE deleted_at IS NULL "
                "AND json_extract(meta_data, '$.source_node_id') = ? "
                "ORDER BY uuid LIMIT 2",
                (resource_id,),
            )
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to resolve material reference"
            ) from None
        if not rows:
            raise MaterialNotFound(f"resource {resource_id} not found")
        if len(rows) != 1:
            raise MaterialConflict(f"resource {resource_id} is ambiguous")
        return _material_record(rows[0])

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
                blocked_replay = False
                if processed is not None:
                    if processed["payload_hash"] != payload_hash:
                        raise MaterialConflict(
                            "command_uuid was already used with a different payload"
                        )
                    if processed["status"] != "blocked":
                        return _release_result_from_payload(
                            json.loads(processed["result_json"])
                        )
                    blocked_replay = True

                live_claim = conn.execute(
                    """
                    SELECT uuid, workflow_node_job_uuid, attempt, state
                    FROM material_claim
                    WHERE workflow_task_uuid = ?
                      AND state IN ('reserved', 'running', 'uncertain')
                    ORDER BY create_time, uuid LIMIT 1
                    """,
                    (canonical_task_uuid,),
                ).fetchone()
                if live_claim is not None:
                    if blocked_replay:
                        return _release_result_from_payload(
                            json.loads(processed["result_json"])
                        )
                    active = conn.execute(
                        """
                        SELECT uuid FROM material_reservation
                        WHERE workflow_task_uuid = ? AND status = 'active'
                        """,
                        (canonical_task_uuid,),
                    ).fetchone()
                    reservation_uuid = (
                        str(active["uuid"]) if active is not None else None
                    )
                    outbox_sequence = self._emit(
                        conn,
                        now_ms,
                        "material_reservation",
                        reservation_uuid or canonical_task_uuid,
                        1,
                        "material_reservation.release_blocked",
                        {
                            "workflow_task_uuid": canonical_task_uuid,
                            "reservation_uuid": reservation_uuid,
                            "blocking_claim_uuid": str(live_claim["uuid"]),
                            "workflow_node_job_uuid": str(
                                live_claim["workflow_node_job_uuid"]
                            ),
                            "attempt": int(live_claim["attempt"]),
                            "claim_state": str(live_claim["state"]),
                            "reason": normalized_command.reason,
                        },
                        causation_id=canonical_command_uuid,
                        reason=normalized_command.reason,
                    )
                    result = TaskMaterialReleaseResult(
                        schema_version=1,
                        command_uuid=canonical_command_uuid,
                        workflow_task_uuid=canonical_task_uuid,
                        status="blocked",
                        reservation_uuid=reservation_uuid,
                        outbox_sequence=outbox_sequence,
                    )
                    conn.execute(
                        """
                        INSERT INTO processed_command(
                            command_id, idempotency_key, command_type,
                            payload_hash, result_json, status, processed_at
                        ) VALUES (?, ?, 'material.release', ?, ?, 'blocked', ?)
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
                    return result

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
                encoded_result = json.dumps(
                    _release_result_payload(result),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                if blocked_replay:
                    conn.execute(
                        """
                        UPDATE processed_command
                        SET result_json = ?, status = 'completed', processed_at = ?
                        WHERE command_id = ? AND status = 'blocked'
                        """,
                        (encoded_result, now_ms, canonical_command_uuid),
                    )
                else:
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
                            encoded_result,
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

    # ------------------------------------------------------------------
    # M1EF Job Claim / fencing / terminal Material ChangeSet authority
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_attempt(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise MaterialInvalidInput("attempt must be a positive integer")
        return value

    @staticmethod
    def _validate_fencing_token(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise MaterialInvalidInput("fencing_token must be a positive integer")
        return value

    @staticmethod
    def _validate_nonblank(value: str, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise MaterialInvalidInput(f"{field} must not be blank")
        return value.strip()

    @staticmethod
    def _validate_fingerprint(value: str, field: str) -> str:
        normalized = InventoryService._validate_nonblank(value, field)
        digest = normalized.removeprefix("sha256:")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise MaterialInvalidInput(f"{field} must be a SHA-256 fingerprint")
        return normalized

    @staticmethod
    def _read_job_claim(
        conn: sqlite3.Connection,
        *,
        job_uuid: str | None = None,
        attempt: int | None = None,
        claim_uuid: str | None = None,
    ) -> JobClaimRecord | None:
        if claim_uuid is not None:
            row = conn.execute(
                "SELECT * FROM material_claim WHERE uuid = ?",
                (claim_uuid,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM material_claim
                WHERE workflow_node_job_uuid = ? AND attempt = ?
                """,
                (job_uuid, attempt),
            ).fetchone()
        if row is None:
            return None
        members = conn.execute(
            """
            SELECT resource_kind, resource_uuid, acquired_version,
                   expected_version, released_at
            FROM material_claim_member
            WHERE claim_uuid = ?
            ORDER BY CASE resource_kind
                       WHEN 'device_material' THEN 0
                       WHEN 'business_material' THEN 1
                       ELSE 2 END,
                     resource_uuid
            """,
            (row["uuid"],),
        ).fetchall()
        return JobClaimRecord(
            uuid=str(row["uuid"]),
            workflow_task_uuid=str(row["workflow_task_uuid"]),
            workflow_node_job_uuid=str(row["workflow_node_job_uuid"]),
            attempt=int(row["attempt"]),
            set_fingerprint=str(row["set_fingerprint"]),
            fencing_token=int(row["fencing_token"]),
            state=str(row["state"]),
            uncertainty_reason=row["uncertainty_reason"],
            acquired_at=str(row["acquired_at"]),
            create_time=str(row["create_time"]),
            running_at=row["running_at"],
            release_proof_kind=row["release_proof_kind"],
            release_proof_fingerprint=row["release_proof_fingerprint"],
            release_reason=row["release_reason"],
            terminal_changeset_uuid=row["terminal_changeset_uuid"],
            workflow_terminal_fingerprint=row["workflow_terminal_fingerprint"],
            release_command_uuid=row["release_command_uuid"],
            released_at=row["released_at"],
            update_time=str(row["update_time"]),
            members=tuple(
                JobClaimMemberRecord(
                    resource_kind=str(member["resource_kind"]),
                    resource_uuid=str(member["resource_uuid"]),
                    acquired_version=int(member["acquired_version"]),
                    expected_version=int(member["expected_version"]),
                    released_at=member["released_at"],
                )
                for member in members
            ),
        )

    @staticmethod
    def _processed_payload(
        conn: sqlite3.Connection,
        command_uuid: str,
        payload_hash: str,
    ) -> Mapping[str, Any] | None:
        processed = conn.execute(
            "SELECT payload_hash, result_json FROM processed_command "
            "WHERE command_id = ?",
            (command_uuid,),
        ).fetchone()
        if processed is None:
            return None
        if processed["payload_hash"] != payload_hash:
            raise MaterialConflict(
                "command_uuid was already used with a different payload"
            )
        try:
            payload = json.loads(processed["result_json"])
        except (TypeError, ValueError):
            raise MaterialAuthorityUnavailable(
                "stored Inventory command result is invalid"
            ) from None
        if not isinstance(payload, dict):
            raise MaterialAuthorityUnavailable(
                "stored Inventory command result is invalid"
            )
        return payload

    @staticmethod
    def _insert_processed(
        conn: sqlite3.Connection,
        *,
        command_uuid: str,
        idempotency_key: str,
        command_type: str,
        payload_hash: str,
        result: Mapping[str, Any],
        status: str,
        now_ms: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO processed_command(
                command_id, idempotency_key, command_type, payload_hash,
                result_json, status, processed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                command_uuid,
                idempotency_key,
                command_type,
                payload_hash,
                json.dumps(
                    dict(result),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                status,
                now_ms,
            ),
        )

    def resolve_executor_material(self, device_id: str) -> MaterialRecord:
        """精确解析 ResourceTreeSet device identity，不做 fallback guessing。"""

        canonical_device_id = self._validate_nonblank(device_id, "device_id")
        try:
            rows = self._store.query_all(
                """
                SELECT * FROM material
                WHERE material_kind = 'device' AND deleted_at IS NULL
                  AND json_extract(meta_data, '$.source') = 'resource-tree-set'
                  AND json_extract(meta_data, '$.source_node_id') = ?
                ORDER BY uuid
                """,
                (canonical_device_id,),
            )
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to resolve executor Material"
            ) from None
        if not rows:
            raise MaterialNotFound("executor Material not found")
        if len(rows) != 1:
            raise MaterialConflict("executor Material mapping is ambiguous")
        return _material_record(rows[0])

    def acquire_job_claim(
        self,
        command: JobClaimAcquireCommand,
    ) -> JobClaimResult:
        """为一个 attempt 原子获取完整 physical resource set。"""

        if not isinstance(command, JobClaimAcquireCommand):
            raise MaterialInvalidInput("command must be a JobClaimAcquireCommand")
        if command.schema_version != 1:
            raise MaterialInvalidInput("unsupported Job Claim schema_version")
        command_uuid = _canonical_uuid(command.command_uuid, "command_uuid")
        task_uuid = _canonical_uuid(command.workflow_task_uuid, "workflow_task_uuid")
        job_uuid = _canonical_uuid(
            command.workflow_node_job_uuid,
            "workflow_node_job_uuid",
        )
        attempt = self._validate_attempt(command.attempt)
        idempotency_key = self._validate_nonblank(
            command.idempotency_key,
            "idempotency_key",
        )
        device_uuid = _canonical_uuid(
            command.device_material_uuid,
            "device_material_uuid",
        )
        if type(command.mutable_material_root_uuids) is not tuple:
            raise MaterialInvalidInput("mutable_material_root_uuids must be a tuple")
        if type(command.occupancy_changing_site_uuids) is not tuple:
            raise MaterialInvalidInput("occupancy_changing_site_uuids must be a tuple")
        roots = tuple(
            sorted(
                {
                    _canonical_uuid(value, "mutable_material_root_uuid")
                    for value in command.mutable_material_root_uuids
                }
            )
        )
        sites = tuple(
            sorted(
                {
                    _canonical_uuid(value, "occupancy_changing_site_uuid")
                    for value in command.occupancy_changing_site_uuids
                }
            )
        )
        if len(roots) != len(command.mutable_material_root_uuids) or len(sites) != len(
            command.occupancy_changing_site_uuids
        ):
            raise MaterialInvalidInput("Job Claim resource roots must be unique")
        normalized_payload = {
            "schema_version": 1,
            "command_uuid": command_uuid,
            "idempotency_key": idempotency_key,
            "workflow_task_uuid": task_uuid,
            "workflow_node_job_uuid": job_uuid,
            "attempt": attempt,
            "device_material_uuid": device_uuid,
            "mutable_material_root_uuids": list(roots),
            "occupancy_changing_site_uuids": list(sites),
        }
        payload_hash = _canonical_payload_hash(normalized_payload)
        now_iso = self._now_iso()
        now_ms = self._now_ms()
        try:
            with self._tx() as conn:
                replay = self._processed_payload(conn, command_uuid, payload_hash)
                if replay is not None:
                    stored_result = _claim_result_from_payload(replay)
                    if stored_result.claim is None:
                        return stored_result
                    current_claim = self._read_job_claim(
                        conn,
                        job_uuid=job_uuid,
                        attempt=attempt,
                    )
                    if (
                        current_claim is None
                        or current_claim.uuid != stored_result.claim.uuid
                        or current_claim.workflow_task_uuid != task_uuid
                        or current_claim.workflow_node_job_uuid != job_uuid
                    ):
                        raise MaterialClaimCorrupt(
                            "stored acquire result does not match the durable Claim"
                        )
                    return JobClaimResult(
                        schema_version=stored_result.schema_version,
                        command_uuid=stored_result.command_uuid,
                        status=(
                            "acquired"
                            if current_claim.state != "released"
                            else "rejected"
                        ),
                        claim=current_claim,
                        diagnostics=stored_result.diagnostics,
                        outbox_sequence=stored_result.outbox_sequence,
                    )

                device = conn.execute(
                    """
                    SELECT uuid, version FROM material
                    WHERE uuid = ? AND deleted_at IS NULL
                      AND material_kind = 'device'
                    """,
                    (device_uuid,),
                ).fetchone()
                if device is None:
                    raise MaterialNotFound("selected device Material not found")

                business: dict[str, int] = {}
                for root_uuid in roots:
                    root = conn.execute(
                        """
                        SELECT uuid, disposition FROM material
                        WHERE uuid = ? AND deleted_at IS NULL
                          AND material_kind = 'business'
                        """,
                        (root_uuid,),
                    ).fetchone()
                    if root is None:
                        raise MaterialNotFound("mutable business Material not found")
                    if root["disposition"] != "active":
                        raise MaterialConflict(
                            "mutable business Material is not active"
                        )
                    descendants = conn.execute(
                        """
                        WITH RECURSIVE subtree(uuid) AS (
                            SELECT ?
                            UNION ALL
                            SELECT m.uuid FROM material AS m
                            JOIN subtree AS s ON m.parent_uuid = s.uuid
                            WHERE m.deleted_at IS NULL
                              AND m.material_kind = 'business'
                        )
                        SELECT m.uuid, m.version, m.disposition
                        FROM material AS m JOIN subtree AS s ON s.uuid = m.uuid
                        ORDER BY m.uuid
                        """,
                        (root_uuid,),
                    ).fetchall()
                    for material in descendants:
                        if material["disposition"] != "active":
                            raise MaterialConflict(
                                "mutable business subtree is not active"
                            )
                        business[str(material["uuid"])] = int(material["version"])

                site_versions: dict[str, int] = {}
                for site_uuid in sites:
                    site = conn.execute(
                        """
                        SELECT uuid, version, occupied_material_uuid
                        FROM site WHERE uuid = ? AND deleted_at IS NULL
                        """,
                        (site_uuid,),
                    ).fetchone()
                    if site is None:
                        raise MaterialNotFound("occupancy-changing Site not found")
                    site_versions[site_uuid] = int(site["version"])
                    occupant_uuid = site["occupied_material_uuid"]
                    if occupant_uuid is not None:
                        occupant = conn.execute(
                            """
                            SELECT uuid, version, disposition, material_kind
                            FROM material WHERE uuid = ? AND deleted_at IS NULL
                            """,
                            (occupant_uuid,),
                        ).fetchone()
                        if occupant is None or occupant["material_kind"] != "business":
                            raise MaterialConflict("Site occupant is not runnable")
                        if occupant["disposition"] != "active":
                            raise MaterialConflict("Site occupant is not active")
                        business[str(occupant["uuid"])] = int(occupant["version"])

                if business:
                    placeholders = ",".join("?" for _ in business)
                    reserved_rows = conn.execute(
                        f"""
                        SELECT rm.material_uuid
                        FROM material_reservation AS r
                        JOIN material_reservation_member AS rm
                          ON rm.reservation_uuid = r.uuid
                        WHERE r.workflow_task_uuid = ? AND r.status = 'active'
                          AND rm.released_at IS NULL
                          AND rm.material_uuid IN ({placeholders})
                        """,
                        (task_uuid, *sorted(business)),
                    ).fetchall()
                    covered = {str(row["material_uuid"]) for row in reserved_rows}
                    if covered != set(business):
                        raise MaterialConflict("claim_set_not_reserved")

                members: list[tuple[str, str, int]] = [
                    ("device_material", device_uuid, int(device["version"]))
                ]
                members.extend(
                    ("business_material", material_uuid, business[material_uuid])
                    for material_uuid in sorted(business)
                )
                members.extend(
                    ("site", site_uuid, site_versions[site_uuid])
                    for site_uuid in sorted(site_versions)
                )
                set_fingerprint = _canonical_payload_hash(
                    {
                        "members": [
                            [resource_kind, resource_uuid, version]
                            for resource_kind, resource_uuid, version in members
                        ]
                    }
                )
                existing = self._read_job_claim(
                    conn,
                    job_uuid=job_uuid,
                    attempt=attempt,
                )
                if existing is not None:
                    if (
                        existing.workflow_task_uuid != task_uuid
                        or existing.set_fingerprint != set_fingerprint
                    ):
                        raise MaterialConflict(
                            "Job attempt was already bound to a different Claim set"
                        )
                    result = JobClaimResult(
                        schema_version=1,
                        command_uuid=command_uuid,
                        status=(
                            "acquired" if existing.state != "released" else "rejected"
                        ),
                        claim=existing,
                        diagnostics=(),
                        outbox_sequence=None,
                    )
                    self._insert_processed(
                        conn,
                        command_uuid=command_uuid,
                        idempotency_key=idempotency_key,
                        command_type="material.claim.acquire",
                        payload_hash=payload_hash,
                        result=_claim_result_payload(result),
                        status="completed",
                        now_ms=now_ms,
                    )
                    return result

                job_claims = conn.execute(
                    """
                    SELECT uuid, workflow_task_uuid, attempt, state
                    FROM material_claim
                    WHERE workflow_node_job_uuid = ?
                    ORDER BY attempt, uuid
                    """,
                    (job_uuid,),
                ).fetchall()
                if job_claims:
                    latest_attempt = int(job_claims[-1]["attempt"])
                    if attempt != latest_attempt + 1:
                        raise MaterialConflict(
                            "Job Claim attempt is stale or out of order"
                        )
                    if any(row["state"] != "released" for row in job_claims):
                        raise MaterialConflict(
                            "previous Job Claim attempt is not released"
                        )
                    if any(
                        str(row["workflow_task_uuid"]) != task_uuid
                        for row in job_claims
                    ):
                        raise MaterialConflict(
                            "Job Claim attempts belong to different WorkflowTasks"
                        )

                blocked_member = None
                for resource_kind, resource_uuid, _version in members:
                    blocked_member = conn.execute(
                        """
                        SELECT claim_uuid FROM material_claim_member
                        WHERE resource_kind = ? AND resource_uuid = ?
                          AND released_at IS NULL
                        """,
                        (resource_kind, resource_uuid),
                    ).fetchone()
                    if blocked_member is not None:
                        break
                if blocked_member is not None:
                    return JobClaimResult(
                        schema_version=1,
                        command_uuid=command_uuid,
                        status="blocked",
                        claim=None,
                        diagnostics=(
                            {
                                "code": "claim_blocked",
                                "blocking_claim_uuid": str(
                                    blocked_member["claim_uuid"]
                                ),
                            },
                        ),
                        outbox_sequence=None,
                    )

                claim_uuid = str(
                    uuid.uuid5(
                        UUID(job_uuid),
                        f"material-claim:{attempt}:{set_fingerprint}",
                    )
                )
                sequence_row = conn.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence
                    FROM material_claim_fence_sequence
                    """
                ).fetchone()
                fencing_token = int(sequence_row["next_sequence"])
                conn.execute(
                    """
                    INSERT INTO material_claim(
                        uuid, workflow_task_uuid, workflow_node_job_uuid,
                        attempt, set_fingerprint, fencing_token, state,
                        uncertainty_reason, acquired_at, create_time, running_at,
                        release_proof_kind, release_proof_fingerprint,
                        release_reason, terminal_changeset_uuid,
                        workflow_terminal_fingerprint, release_command_uuid,
                        released_at, update_time
                    ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', NULL, ?, ?, NULL,
                              NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?)
                    """,
                    (
                        claim_uuid,
                        task_uuid,
                        job_uuid,
                        attempt,
                        set_fingerprint,
                        fencing_token,
                        now_iso,
                        now_iso,
                        now_iso,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO material_claim_fence_sequence(sequence, claim_uuid)
                    VALUES (?, ?)
                    """,
                    (fencing_token, claim_uuid),
                )
                for resource_kind, resource_uuid, version in members:
                    conn.execute(
                        """
                        INSERT INTO material_claim_member(
                            claim_uuid, resource_kind, resource_uuid,
                            acquired_version, expected_version, released_at
                        ) VALUES (?, ?, ?, ?, ?, NULL)
                        """,
                        (claim_uuid, resource_kind, resource_uuid, version, version),
                    )
                    conn.execute(
                        """
                        INSERT INTO material_resource_fence(
                            resource_kind, resource_uuid, fencing_token,
                            claim_uuid, update_time
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(resource_kind, resource_uuid) DO UPDATE SET
                            fencing_token = excluded.fencing_token,
                            claim_uuid = excluded.claim_uuid,
                            update_time = excluded.update_time
                        WHERE excluded.fencing_token
                              > material_resource_fence.fencing_token
                        """,
                        (
                            resource_kind,
                            resource_uuid,
                            fencing_token,
                            claim_uuid,
                            now_iso,
                        ),
                    )
                outbox_sequence = self._emit(
                    conn,
                    now_ms,
                    "material_claim",
                    claim_uuid,
                    fencing_token,
                    "material_claim.acquired",
                    {
                        "workflow_task_uuid": task_uuid,
                        "workflow_node_job_uuid": job_uuid,
                        "attempt": attempt,
                        "fencing_token": fencing_token,
                        "set_fingerprint": set_fingerprint,
                        "members": [
                            {
                                "resource_kind": kind,
                                "resource_uuid": resource_uuid,
                            }
                            for kind, resource_uuid, _version in members
                        ],
                    },
                    causation_id=command_uuid,
                )
                claim = self._read_job_claim(conn, claim_uuid=claim_uuid)
                if claim is None:
                    raise MaterialAuthorityUnavailable(
                        "committed Job Claim is not readable"
                    )
                result = JobClaimResult(
                    schema_version=1,
                    command_uuid=command_uuid,
                    status="acquired",
                    claim=claim,
                    diagnostics=(),
                    outbox_sequence=outbox_sequence,
                )
                self._insert_processed(
                    conn,
                    command_uuid=command_uuid,
                    idempotency_key=idempotency_key,
                    command_type="material.claim.acquire",
                    payload_hash=payload_hash,
                    result=_claim_result_payload(result),
                    status="completed",
                    now_ms=now_ms,
                )
        except sqlite3.IntegrityError as exc:
            raise MaterialConflict("Job Claim conflicts") from exc
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable("failed to acquire Job Claim") from None
        return result

    def get_job_claim(self, job_uuid: str, attempt: int) -> JobClaimRecord:
        """按 formal Job attempt identity 读取一个 durable Claim。"""

        canonical_job_uuid = _canonical_uuid(job_uuid, "workflow_node_job_uuid")
        canonical_attempt = self._validate_attempt(attempt)
        try:
            with self._tx() as conn:
                claim = self._read_job_claim(
                    conn,
                    job_uuid=canonical_job_uuid,
                    attempt=canonical_attempt,
                )
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable("failed to read Job Claim") from None
        if claim is None:
            raise MaterialNotFound("Job Claim not found")
        return claim

    def list_unsettled_claims(
        self,
        *,
        workflow_task_uuid: str | None = None,
    ) -> tuple[JobClaimRecord, ...]:
        """按 deterministic recovery order 读取全部 live fences。"""

        task_uuid = (
            _canonical_uuid(workflow_task_uuid, "workflow_task_uuid")
            if workflow_task_uuid is not None
            else None
        )
        try:
            with self._tx() as conn:
                if task_uuid is None:
                    rows = conn.execute(
                        """
                        SELECT uuid FROM material_claim
                        WHERE state IN ('reserved', 'running', 'uncertain')
                        ORDER BY create_time, uuid
                        """
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT uuid FROM material_claim
                        WHERE workflow_task_uuid = ?
                          AND state IN ('reserved', 'running', 'uncertain')
                        ORDER BY create_time, uuid
                        """,
                        (task_uuid,),
                    ).fetchall()
                claims = tuple(
                    self._read_job_claim(conn, claim_uuid=str(row["uuid"]))
                    for row in rows
                )
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to list unsettled Job Claims"
            ) from None
        if any(claim is None for claim in claims):
            raise MaterialAuthorityUnavailable("Job Claim authority is incomplete")
        return tuple(claim for claim in claims if claim is not None)

    def audit_job_claim_authority(self) -> tuple[JobClaimRecord, ...]:
        """dispatch 前验证 durable Claim/member/fence/receipt facts。"""

        try:
            with self._tx() as conn:
                claim_rows = conn.execute(
                    "SELECT uuid FROM material_claim ORDER BY fencing_token, uuid"
                ).fetchall()
                claims: list[JobClaimRecord] = []
                for claim_row in claim_rows:
                    claim = self._read_job_claim(
                        conn,
                        claim_uuid=str(claim_row["uuid"]),
                    )
                    if claim is None:
                        raise MaterialClaimCorrupt("Job Claim header disappeared")
                    self._audit_job_claim(conn, claim)
                    claims.append(claim)

                orphan_fences = conn.execute(
                    """
                    SELECT f.resource_kind, f.resource_uuid, f.claim_uuid
                    FROM material_resource_fence AS f
                    LEFT JOIN material_claim_member AS m
                      ON m.claim_uuid = f.claim_uuid
                     AND m.resource_kind = f.resource_kind
                     AND m.resource_uuid = f.resource_uuid
                    WHERE m.claim_uuid IS NULL
                    ORDER BY f.resource_kind, f.resource_uuid
                    """
                ).fetchall()
                if orphan_fences:
                    raise MaterialClaimCorrupt(
                        "resource fence does not belong to its recorded Claim set"
                    )
        except MaterialClaimCorrupt:
            raise
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to audit Job Claim authority"
            ) from None
        return tuple(claims)

    def _audit_job_claim(
        self,
        conn: sqlite3.Connection,
        claim: JobClaimRecord,
    ) -> None:
        if not claim.members:
            raise MaterialClaimCorrupt("Job Claim has no members")
        if (
            sum(member.resource_kind == "device_material" for member in claim.members)
            != 1
        ):
            raise MaterialClaimCorrupt(
                "Job Claim must contain exactly one device Material"
            )
        expected_set_fingerprint = _canonical_payload_hash(
            {
                "members": [
                    [
                        member.resource_kind,
                        member.resource_uuid,
                        member.acquired_version,
                    ]
                    for member in claim.members
                ]
            }
        )
        if claim.set_fingerprint != expected_set_fingerprint:
            raise MaterialClaimCorrupt("Job Claim set fingerprint is corrupt")

        sequence = conn.execute(
            """
            SELECT sequence FROM material_claim_fence_sequence
            WHERE claim_uuid = ?
            """,
            (claim.uuid,),
        ).fetchone()
        if sequence is None or int(sequence["sequence"]) != claim.fencing_token:
            raise MaterialClaimCorrupt("Job Claim fencing sequence is corrupt")

        is_live = claim.state in {"reserved", "running", "uncertain"}
        for member in claim.members:
            if member.expected_version < member.acquired_version:
                raise MaterialClaimCorrupt("Job Claim member version moved backward")
            if is_live != (member.released_at is None):
                raise MaterialClaimCorrupt(
                    "Job Claim member release state differs from its header"
                )
            fence = conn.execute(
                """
                SELECT fencing_token, claim_uuid FROM material_resource_fence
                WHERE resource_kind = ? AND resource_uuid = ?
                """,
                (member.resource_kind, member.resource_uuid),
            ).fetchone()
            if fence is None or int(fence["fencing_token"]) < claim.fencing_token:
                raise MaterialClaimCorrupt(
                    "Job Claim resource fence is missing or stale"
                )
            if is_live and (
                int(fence["fencing_token"]) != claim.fencing_token
                or str(fence["claim_uuid"]) != claim.uuid
            ):
                raise MaterialClaimCorrupt("live Job Claim does not own its fence")
            if (
                int(fence["fencing_token"]) == claim.fencing_token
                and str(fence["claim_uuid"]) != claim.uuid
            ):
                raise MaterialClaimCorrupt("Job Claim fencing token owner is corrupt")
            if is_live:
                table = "site" if member.resource_kind == "site" else "material"
                row = conn.execute(
                    f'SELECT version, material_kind FROM "{table}" WHERE uuid = ?'
                    if table == "material"
                    else f'SELECT version FROM "{table}" WHERE uuid = ?',
                    (member.resource_uuid,),
                ).fetchone()
                if row is None or int(row["version"]) != member.expected_version:
                    raise MaterialClaimCorrupt(
                        "live Job Claim member version differs from durable reality"
                    )
                if table == "material":
                    expected_kind = (
                        "device"
                        if member.resource_kind == "device_material"
                        else "business"
                    )
                    if str(row["material_kind"]) != expected_kind:
                        raise MaterialClaimCorrupt(
                            "Job Claim member kind differs from durable reality"
                        )

        changesets = conn.execute(
            """
            SELECT * FROM material_changeset
            WHERE claim_uuid = ? ORDER BY create_time, uuid
            """,
            (claim.uuid,),
        ).fetchall()
        if len(changesets) > 1:
            raise MaterialClaimCorrupt("Job Claim has multiple terminal ChangeSets")
        if changesets:
            self._audit_material_changeset(conn, claim, changesets[0])
        if claim.terminal_changeset_uuid is not None and (
            not changesets
            or str(changesets[0]["uuid"]) != claim.terminal_changeset_uuid
        ):
            raise MaterialClaimCorrupt("released Claim terminal receipt is missing")
        if (
            claim.release_proof_kind
            in {
                "terminal_settled",
                "reconciled_terminal",
            }
            and not changesets
        ):
            raise MaterialClaimCorrupt("terminal Claim release has no ChangeSet")
        if claim.release_proof_kind == "not_submitted" and changesets:
            raise MaterialClaimCorrupt("not-submitted Claim has a terminal ChangeSet")

    def _audit_material_changeset(
        self,
        conn: sqlite3.Connection,
        claim: JobClaimRecord,
        row: sqlite3.Row,
    ) -> None:
        if (
            str(row["workflow_task_uuid"]) != claim.workflow_task_uuid
            or str(row["workflow_node_job_uuid"]) != claim.workflow_node_job_uuid
            or int(row["attempt"]) != claim.attempt
            or int(row["fencing_token"]) != claim.fencing_token
        ):
            raise MaterialClaimCorrupt("terminal ChangeSet owner or fence is corrupt")
        effects = conn.execute(
            """
            SELECT * FROM material_changeset_effect
            WHERE changeset_uuid = ? ORDER BY effect_key
            """,
            (row["uuid"],),
        ).fetchall()
        member_keys = {
            (member.resource_kind, member.resource_uuid) for member in claim.members
        }
        normalized_effects: list[dict[str, Any]] = []
        for effect in effects:
            key = (str(effect["resource_kind"]), str(effect["resource_uuid"]))
            if effect["operation"] != "create" and key not in member_keys:
                raise MaterialClaimCorrupt(
                    "terminal ChangeSet affects an undeclared Claim member"
                )
            normalized_effects.append(
                {
                    "effect_key": str(effect["effect_key"]),
                    "resource_kind": key[0],
                    "resource_uuid": key[1],
                    "operation": str(effect["operation"]),
                    "expected_version": effect["expected_version"],
                    "before": _stored_json_object(effect["before_json"]),
                    "after": _stored_json_object(effect["after_json"]),
                }
            )
        fingerprint = _canonical_payload_hash(
            {
                "workflow_task_uuid": claim.workflow_task_uuid,
                "workflow_node_job_uuid": claim.workflow_node_job_uuid,
                "attempt": claim.attempt,
                "claim_uuid": claim.uuid,
                "fencing_token": claim.fencing_token,
                "effect_identity": str(row["effect_identity"]),
                "outcome": str(row["outcome"]),
                "result": _stored_json_object(row["result_json"]),
                "effects": normalized_effects,
            }
        )
        if fingerprint != str(row["deterministic_fingerprint"]):
            raise MaterialClaimCorrupt("terminal ChangeSet fingerprint is corrupt")
        outbox = conn.execute(
            """
            SELECT aggregate_type, aggregate_id, event_type
            FROM sync_outbox WHERE sequence = ?
            """,
            (row["outbox_sequence"],),
        ).fetchone()
        if outbox is None or (
            str(outbox["aggregate_type"]) != "material_changeset"
            or str(outbox["aggregate_id"]) != str(row["uuid"])
            or str(outbox["event_type"]) != "material_changeset.committed"
        ):
            raise MaterialClaimCorrupt("terminal ChangeSet outbox receipt is corrupt")

    def get_terminal_material_changeset(
        self,
        job_uuid: str,
        attempt: int,
    ) -> MaterialChangeSetReceipt | None:
        """读取 startup saga recovery 使用的唯一 M1EF terminal receipt。"""

        canonical_job_uuid = _canonical_uuid(job_uuid, "workflow_node_job_uuid")
        canonical_attempt = self._validate_attempt(attempt)
        try:
            with self._tx() as conn:
                row = conn.execute(
                    """
                    SELECT * FROM material_changeset
                    WHERE workflow_node_job_uuid = ? AND attempt = ?
                      AND effect_identity = 'terminal'
                    """,
                    (canonical_job_uuid, canonical_attempt),
                ).fetchone()
                if row is None:
                    return None
                effect_rows = conn.execute(
                    """
                    SELECT * FROM material_changeset_effect
                    WHERE changeset_uuid = ? ORDER BY effect_key
                    """,
                    (row["uuid"],),
                ).fetchall()
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to read terminal Material ChangeSet"
            ) from None
        return MaterialChangeSetReceipt(
            schema_version=1,
            command_uuid=str(
                uuid.uuid5(
                    UUID(canonical_job_uuid),
                    f"m1ef:{canonical_attempt}:terminal-changeset",
                )
            ),
            uuid=str(row["uuid"]),
            workflow_task_uuid=str(row["workflow_task_uuid"]),
            workflow_node_job_uuid=str(row["workflow_node_job_uuid"]),
            attempt=int(row["attempt"]),
            claim_uuid=str(row["claim_uuid"]),
            fencing_token=int(row["fencing_token"]),
            effect_identity=str(row["effect_identity"]),
            deterministic_fingerprint=str(row["deterministic_fingerprint"]),
            outcome=str(row["outcome"]),
            result=_stored_json_object(row["result_json"]),
            effects=tuple(
                MaterialChangeSetEffect(
                    effect_key=str(effect["effect_key"]),
                    resource_kind=str(effect["resource_kind"]),
                    resource_uuid=str(effect["resource_uuid"]),
                    operation=str(effect["operation"]),
                    expected_version=(
                        int(effect["expected_version"])
                        if effect["expected_version"] is not None
                        else None
                    ),
                    before=_stored_json_object(effect["before_json"]),
                    after=_stored_json_object(effect["after_json"]),
                )
                for effect in effect_rows
            ),
            create_time=str(row["create_time"]),
            outbox_sequence=int(row["outbox_sequence"]),
        )

    def mark_job_claim_running(
        self,
        command: JobClaimStateCommand,
    ) -> JobClaimResult:
        """用 accepted evidence 把 reserved/uncertain Claim 推进到 running。"""

        if not isinstance(command, JobClaimStateCommand):
            raise MaterialInvalidInput("command must be a JobClaimStateCommand")
        return self._transition_job_claim(
            command=command,
            target_state="running",
            uncertainty_reason=None,
        )

    def mark_job_claim_uncertain(
        self,
        command: JobClaimUncertainCommand,
    ) -> JobClaimResult:
        """fence 住不明确 physical reality，并把 business members 标为 reconciling。"""

        if not isinstance(command, JobClaimUncertainCommand):
            raise MaterialInvalidInput("command must be a JobClaimUncertainCommand")
        return self._transition_job_claim(
            command=command,
            target_state="uncertain",
            uncertainty_reason=self._validate_nonblank(
                command.uncertainty_reason,
                "uncertainty_reason",
            ),
        )

    def _transition_job_claim(
        self,
        *,
        command: JobClaimStateCommand | JobClaimUncertainCommand,
        target_state: str,
        uncertainty_reason: str | None,
    ) -> JobClaimResult:
        if command.schema_version != 1:
            raise MaterialInvalidInput("unsupported Job Claim schema_version")
        command_uuid = _canonical_uuid(command.command_uuid, "command_uuid")
        job_uuid = _canonical_uuid(
            command.workflow_node_job_uuid,
            "workflow_node_job_uuid",
        )
        claim_uuid = _canonical_uuid(command.claim_uuid, "claim_uuid")
        attempt = self._validate_attempt(command.attempt)
        token = self._validate_fencing_token(command.fencing_token)
        idempotency_key = self._validate_nonblank(
            command.idempotency_key,
            "idempotency_key",
        )
        evidence_kind = self._validate_nonblank(
            getattr(command, "evidence_kind", "uncertain"),
            "evidence_kind",
        )
        evidence_fingerprint = self._validate_fingerprint(
            command.evidence_fingerprint,
            "evidence_fingerprint",
        )
        expected_state = getattr(command, "expected_state", None)
        if expected_state is not None:
            expected_state = self._validate_nonblank(
                expected_state,
                "expected_state",
            )
            if expected_state not in {"reserved", "running", "uncertain"}:
                raise MaterialInvalidInput("expected_state is invalid")
        normalized_payload = {
            "schema_version": 1,
            "command_uuid": command_uuid,
            "idempotency_key": idempotency_key,
            "workflow_node_job_uuid": job_uuid,
            "attempt": attempt,
            "claim_uuid": claim_uuid,
            "fencing_token": token,
            "target_state": target_state,
            "uncertainty_reason": uncertainty_reason,
            "evidence_kind": evidence_kind,
            "evidence_fingerprint": evidence_fingerprint,
            "expected_state": expected_state,
        }
        payload_hash = _canonical_payload_hash(normalized_payload)
        now_iso = self._now_iso()
        now_ms = self._now_ms()
        try:
            with self._tx() as conn:
                replay = self._processed_payload(conn, command_uuid, payload_hash)
                if replay is not None:
                    return _claim_result_from_payload(replay)
                claim = self._read_job_claim(conn, claim_uuid=claim_uuid)
                if claim is None:
                    raise MaterialNotFound("Job Claim not found")
                if (
                    claim.workflow_node_job_uuid != job_uuid
                    or claim.attempt != attempt
                    or claim.fencing_token != token
                ):
                    raise MaterialConflict("stale Job Claim owner or fencing token")
                if expected_state is not None and claim.state != expected_state:
                    raise MaterialConflict("Job Claim expected_state is stale")
                allowed = {
                    "running": {"reserved", "running", "uncertain"},
                    "uncertain": {"reserved", "running", "uncertain"},
                }[target_state]
                if claim.state not in allowed:
                    raise MaterialConflict("Job Claim state transition is not allowed")
                if target_state == "uncertain":
                    business_rows = conn.execute(
                        """
                        SELECT m.uuid, m.version, m.disposition
                        FROM material AS m
                        JOIN material_claim_member AS cm
                          ON cm.resource_uuid = m.uuid
                         AND cm.resource_kind = 'business_material'
                        WHERE cm.claim_uuid = ? AND cm.released_at IS NULL
                          AND m.deleted_at IS NULL
                        ORDER BY m.uuid
                        """,
                        (claim_uuid,),
                    ).fetchall()
                    for material in business_rows:
                        expected_version = int(material["version"])
                        if material["disposition"] == "active":
                            expected_version += 1
                            conn.execute(
                                """
                                UPDATE material
                                SET disposition = 'reconciling', version = ?,
                                    update_time = ?
                                WHERE uuid = ?
                                """,
                                (expected_version, now_iso, material["uuid"]),
                            )
                        conn.execute(
                            """
                            UPDATE material_claim_member
                            SET expected_version = ?
                            WHERE claim_uuid = ?
                              AND resource_kind = 'business_material'
                              AND resource_uuid = ?
                            """,
                            (expected_version, claim_uuid, material["uuid"]),
                        )
                conn.execute(
                    """
                    UPDATE material_claim
                    SET state = ?,
                        uncertainty_reason = ?,
                        running_at = CASE WHEN ? = 'running'
                                          THEN COALESCE(running_at, ?)
                                          ELSE running_at END,
                        update_time = ?
                    WHERE uuid = ?
                    """,
                    (
                        target_state,
                        uncertainty_reason,
                        target_state,
                        now_iso,
                        now_iso,
                        claim_uuid,
                    ),
                )
                outbox_sequence = self._emit(
                    conn,
                    now_ms,
                    "material_claim",
                    claim_uuid,
                    token,
                    f"material_claim.{target_state}",
                    {
                        "workflow_node_job_uuid": job_uuid,
                        "attempt": attempt,
                        "fencing_token": token,
                        "evidence_kind": evidence_kind,
                        "evidence_fingerprint": evidence_fingerprint,
                        "uncertainty_reason": uncertainty_reason,
                    },
                    causation_id=command_uuid,
                    reason=uncertainty_reason or evidence_kind,
                )
                updated = self._read_job_claim(conn, claim_uuid=claim_uuid)
                if updated is None:
                    raise MaterialAuthorityUnavailable("Job Claim disappeared")
                result = JobClaimResult(
                    schema_version=1,
                    command_uuid=command_uuid,
                    status=target_state,
                    claim=updated,
                    diagnostics=(),
                    outbox_sequence=outbox_sequence,
                )
                self._insert_processed(
                    conn,
                    command_uuid=command_uuid,
                    idempotency_key=idempotency_key,
                    command_type=f"material.claim.{target_state}",
                    payload_hash=payload_hash,
                    result=_claim_result_payload(result),
                    status="completed",
                    now_ms=now_ms,
                )
        except sqlite3.IntegrityError as exc:
            raise MaterialConflict("Job Claim state command conflicts") from exc
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                f"failed to mark Job Claim {target_state}"
            ) from None
        return result

    @staticmethod
    def _normalized_changeset_effects(
        effects: tuple[MaterialChangeSetEffect, ...],
    ) -> tuple[MaterialChangeSetEffect, ...]:
        if type(effects) is not tuple:
            raise MaterialInvalidInput("effects must be a tuple")
        normalized: list[MaterialChangeSetEffect] = []
        seen: set[str] = set()
        for effect in effects:
            if not isinstance(effect, MaterialChangeSetEffect):
                raise MaterialInvalidInput(
                    "effects must contain MaterialChangeSetEffect values"
                )
            effect_key = InventoryService._validate_nonblank(
                effect.effect_key,
                "effect_key",
            )
            if effect_key in seen:
                raise MaterialInvalidInput("effect_key must be unique")
            seen.add(effect_key)
            if effect.resource_kind not in {"business_material", "site"}:
                raise MaterialInvalidInput("effect resource_kind is invalid")
            resource_uuid = _canonical_uuid(effect.resource_uuid, "resource_uuid")
            if effect.operation not in {
                "create",
                "update",
                "reparent",
                "soft_delete",
                "set_occupancy",
            }:
                raise MaterialInvalidInput("effect operation is invalid")
            expected_version = effect.expected_version
            if expected_version is not None:
                if (
                    isinstance(expected_version, bool)
                    or not isinstance(expected_version, int)
                    or expected_version <= 0
                ):
                    raise MaterialInvalidInput(
                        "effect expected_version must be positive or null"
                    )
            normalized.append(
                MaterialChangeSetEffect(
                    effect_key=effect_key,
                    resource_kind=effect.resource_kind,
                    resource_uuid=resource_uuid,
                    operation=effect.operation,
                    expected_version=expected_version,
                    before=_json_object(effect.before, "effect.before"),
                    after=_json_object(effect.after, "effect.after"),
                )
            )
        return tuple(sorted(normalized, key=lambda effect: effect.effect_key))

    def commit_material_changeset(
        self,
        command: MaterialChangeSetCommand,
    ) -> MaterialChangeSetReceipt:
        """在精确 fence 下提交一个 terminal physical-reality receipt。"""

        if not isinstance(command, MaterialChangeSetCommand):
            raise MaterialInvalidInput("command must be a MaterialChangeSetCommand")
        if command.schema_version != 1:
            raise MaterialInvalidInput("unsupported Material ChangeSet schema_version")
        command_uuid = _canonical_uuid(command.command_uuid, "command_uuid")
        task_uuid = _canonical_uuid(command.workflow_task_uuid, "workflow_task_uuid")
        job_uuid = _canonical_uuid(
            command.workflow_node_job_uuid,
            "workflow_node_job_uuid",
        )
        claim_uuid = _canonical_uuid(command.claim_uuid, "claim_uuid")
        attempt = self._validate_attempt(command.attempt)
        token = self._validate_fencing_token(command.fencing_token)
        idempotency_key = self._validate_nonblank(
            command.idempotency_key,
            "idempotency_key",
        )
        effect_identity = self._validate_nonblank(
            command.effect_identity,
            "effect_identity",
        )
        if effect_identity != "terminal":
            raise MaterialInvalidInput("M1EF v1 only accepts terminal effect identity")
        if command.outcome not in {"succeeded", "failed", "canceled", "timeout"}:
            raise MaterialInvalidInput("Material ChangeSet outcome is invalid")
        result_json = _json_object(command.result, "result")
        effects = self._normalized_changeset_effects(command.effects)
        expected_claim_state = command.expected_claim_state
        if expected_claim_state is not None:
            expected_claim_state = self._validate_nonblank(
                expected_claim_state,
                "expected_claim_state",
            )
            if expected_claim_state not in {"running", "uncertain"}:
                raise MaterialInvalidInput("expected_claim_state is invalid")
        fingerprint_payload = {
            "workflow_task_uuid": task_uuid,
            "workflow_node_job_uuid": job_uuid,
            "attempt": attempt,
            "claim_uuid": claim_uuid,
            "fencing_token": token,
            "effect_identity": effect_identity,
            "outcome": command.outcome,
            "result": result_json,
            "effects": [_effect_payload(effect) for effect in effects],
        }
        deterministic_fingerprint = _canonical_payload_hash(fingerprint_payload)
        normalized_payload = {
            "schema_version": 1,
            "command_uuid": command_uuid,
            "idempotency_key": idempotency_key,
            **fingerprint_payload,
            "expected_claim_state": expected_claim_state,
        }
        payload_hash = _canonical_payload_hash(normalized_payload)
        changeset_uuid = str(
            uuid.uuid5(
                UUID(job_uuid),
                f"material-changeset:{attempt}:{effect_identity}",
            )
        )
        now_iso = self._now_iso()
        now_ms = self._now_ms()
        try:
            with self._tx() as conn:
                replay = self._processed_payload(conn, command_uuid, payload_hash)
                if replay is not None:
                    return _changeset_receipt_from_payload(replay)
                existing = conn.execute(
                    """
                    SELECT * FROM material_changeset
                    WHERE workflow_node_job_uuid = ? AND attempt = ?
                      AND effect_identity = ?
                    """,
                    (job_uuid, attempt, effect_identity),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["deterministic_fingerprint"]
                        != deterministic_fingerprint
                    ):
                        raise MaterialConflict(
                            "terminal Material ChangeSet fingerprint conflicts"
                        )
                    stored_effects = conn.execute(
                        """
                        SELECT * FROM material_changeset_effect
                        WHERE changeset_uuid = ? ORDER BY effect_key
                        """,
                        (existing["uuid"],),
                    ).fetchall()
                    receipt = MaterialChangeSetReceipt(
                        schema_version=1,
                        command_uuid=command_uuid,
                        uuid=str(existing["uuid"]),
                        workflow_task_uuid=str(existing["workflow_task_uuid"]),
                        workflow_node_job_uuid=str(existing["workflow_node_job_uuid"]),
                        attempt=int(existing["attempt"]),
                        claim_uuid=str(existing["claim_uuid"]),
                        fencing_token=int(existing["fencing_token"]),
                        effect_identity=str(existing["effect_identity"]),
                        deterministic_fingerprint=str(
                            existing["deterministic_fingerprint"]
                        ),
                        outcome=str(existing["outcome"]),
                        result=_stored_json_object(existing["result_json"]),
                        effects=tuple(
                            MaterialChangeSetEffect(
                                effect_key=str(row["effect_key"]),
                                resource_kind=str(row["resource_kind"]),
                                resource_uuid=str(row["resource_uuid"]),
                                operation=str(row["operation"]),
                                expected_version=(
                                    int(row["expected_version"])
                                    if row["expected_version"] is not None
                                    else None
                                ),
                                before=_stored_json_object(row["before_json"]),
                                after=_stored_json_object(row["after_json"]),
                            )
                            for row in stored_effects
                        ),
                        create_time=str(existing["create_time"]),
                        outbox_sequence=int(existing["outbox_sequence"]),
                    )
                    self._insert_processed(
                        conn,
                        command_uuid=command_uuid,
                        idempotency_key=idempotency_key,
                        command_type="material.changeset.commit",
                        payload_hash=payload_hash,
                        result=_changeset_receipt_payload(receipt),
                        status="completed",
                        now_ms=now_ms,
                    )
                    return receipt
                claim = self._read_job_claim(conn, claim_uuid=claim_uuid)
                if claim is None:
                    raise MaterialNotFound("Job Claim not found")
                if (
                    claim.workflow_task_uuid != task_uuid
                    or claim.workflow_node_job_uuid != job_uuid
                    or claim.attempt != attempt
                    or claim.fencing_token != token
                ):
                    raise MaterialConflict("stale Job Claim owner or fencing token")
                if claim.state not in {"running", "uncertain"}:
                    raise MaterialConflict(
                        "Material ChangeSet requires running or uncertain Claim"
                    )
                if (
                    expected_claim_state is not None
                    and claim.state != expected_claim_state
                ):
                    raise MaterialConflict("Job Claim expected_state is stale")
                self._audit_job_claim(conn, claim)
                member_index = {
                    (member.resource_kind, member.resource_uuid): member
                    for member in claim.members
                    if member.released_at is None
                }
                for effect in effects:
                    member = member_index.get(
                        (effect.resource_kind, effect.resource_uuid)
                    )
                    if effect.operation != "create" and member is None:
                        raise MaterialConflict(
                            "Material ChangeSet affects an undeclared Claim member"
                        )
                    table = (
                        "material"
                        if effect.resource_kind == "business_material"
                        else "site"
                    )
                    current = conn.execute(
                        f'SELECT * FROM "{table}" WHERE uuid = ?',
                        (effect.resource_uuid,),
                    ).fetchone()
                    if effect.operation == "create":
                        if current is not None:
                            raise MaterialConflict("ChangeSet create target exists")
                        if effect.expected_version is not None or effect.before:
                            raise MaterialInvalidInput(
                                "ChangeSet create requires null version and "
                                "empty before"
                            )
                        if table == "material":
                            self._apply_create_material_effect(
                                conn,
                                claim_members=member_index,
                                effect=effect,
                                now_iso=now_iso,
                            )
                        else:
                            self._apply_create_site_effect(
                                conn,
                                claim_members=member_index,
                                effect=effect,
                                now_iso=now_iso,
                            )
                        changed_row = conn.execute(
                            f'SELECT version FROM "{table}" WHERE uuid = ?',
                            (effect.resource_uuid,),
                        ).fetchone()
                        InventoryStore.tx_insert_ledger(
                            conn,
                            now_ms,
                            "material_changeset.effect",
                            effect.resource_kind,
                            effect.resource_uuid,
                            _effect_payload(effect),
                            causation_id=command_uuid,
                        )
                        continue
                    if current is None or current["deleted_at"] is not None:
                        raise MaterialNotFound("ChangeSet target not found")
                    if (
                        effect.expected_version is not None
                        and effect.expected_version != member.expected_version
                    ):
                        raise MaterialConflict(
                            "ChangeSet expected_version differs from Claim baseline"
                        )
                    if int(current["version"]) != member.expected_version:
                        raise MaterialConflict("ChangeSet expected_version is stale")
                    if table == "material":
                        before_projection = _material_record(dict(current)).to_dict()
                    else:
                        site_projection = _read_site(conn, effect.resource_uuid)
                        if site_projection is None:
                            raise MaterialNotFound("ChangeSet Site not found")
                        before_projection = site_projection.to_dict()
                    for key, value in effect.before.items():
                        if before_projection.get(key) != value:
                            raise MaterialConflict("ChangeSet before image is stale")
                    previous_version = int(current["version"])
                    if table == "material":
                        self._apply_material_effect(
                            conn,
                            claim_members=member_index,
                            current=dict(current),
                            effect=effect,
                            now_iso=now_iso,
                        )
                    else:
                        target_occupant = effect.after.get("occupied_material_uuid")
                        if target_occupant is not None:
                            target_occupant = _canonical_uuid(
                                target_occupant,
                                "occupied_material_uuid",
                            )
                            if (
                                "business_material",
                                target_occupant,
                            ) not in member_index:
                                raise MaterialConflict(
                                    "target Site occupant is not a Claim member"
                                )
                        self._apply_site_effect(
                            conn,
                            claim_members=member_index,
                            current=dict(current),
                            effect=effect,
                            now_iso=now_iso,
                        )
                    changed_row = conn.execute(
                        f'SELECT version FROM "{table}" WHERE uuid = ?',
                        (effect.resource_uuid,),
                    ).fetchone()
                    changed_version = int(changed_row["version"])
                    if changed_version > previous_version:
                        InventoryStore.tx_insert_ledger(
                            conn,
                            now_ms,
                            "material_changeset.effect",
                            effect.resource_kind,
                            effect.resource_uuid,
                            _effect_payload(effect),
                            causation_id=command_uuid,
                        )
                    conn.execute(
                        """
                        UPDATE material_claim_member SET expected_version = ?
                        WHERE claim_uuid = ? AND resource_kind = ?
                          AND resource_uuid = ?
                        """,
                        (
                            int(changed_row["version"]),
                            claim_uuid,
                            effect.resource_kind,
                            effect.resource_uuid,
                        ),
                    )

                outbox_sequence = InventoryStore.tx_insert_outbox(
                    conn,
                    new_event_id(now_ms),
                    self.edge_id,
                    self.lab_id,
                    "material_changeset",
                    changeset_uuid,
                    1,
                    "material_changeset.committed",
                    now_ms,
                    command_uuid,
                    {
                        "workflow_task_uuid": task_uuid,
                        "workflow_node_job_uuid": job_uuid,
                        "attempt": attempt,
                        "claim_uuid": claim_uuid,
                        "fencing_token": token,
                        "effect_identity": effect_identity,
                        "deterministic_fingerprint": deterministic_fingerprint,
                        "outcome": command.outcome,
                        "effect_count": len(effects),
                    },
                )
                conn.execute(
                    """
                    INSERT INTO material_changeset(
                        uuid, workflow_task_uuid, workflow_node_job_uuid,
                        attempt, claim_uuid, fencing_token, effect_identity,
                        deterministic_fingerprint, outcome, result_json,
                        outbox_sequence, create_time
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        changeset_uuid,
                        task_uuid,
                        job_uuid,
                        attempt,
                        claim_uuid,
                        token,
                        effect_identity,
                        deterministic_fingerprint,
                        command.outcome,
                        json.dumps(result_json, ensure_ascii=False, sort_keys=True),
                        outbox_sequence,
                        now_iso,
                    ),
                )
                for effect in effects:
                    conn.execute(
                        """
                        INSERT INTO material_changeset_effect(
                            changeset_uuid, effect_key, resource_kind,
                            resource_uuid, operation, expected_version,
                            before_json, after_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            changeset_uuid,
                            effect.effect_key,
                            effect.resource_kind,
                            effect.resource_uuid,
                            effect.operation,
                            effect.expected_version,
                            json.dumps(
                                effect.before, ensure_ascii=False, sort_keys=True
                            ),
                            json.dumps(
                                effect.after, ensure_ascii=False, sort_keys=True
                            ),
                        ),
                    )
                receipt = MaterialChangeSetReceipt(
                    schema_version=1,
                    command_uuid=command_uuid,
                    uuid=changeset_uuid,
                    workflow_task_uuid=task_uuid,
                    workflow_node_job_uuid=job_uuid,
                    attempt=attempt,
                    claim_uuid=claim_uuid,
                    fencing_token=token,
                    effect_identity=effect_identity,
                    deterministic_fingerprint=deterministic_fingerprint,
                    outcome=command.outcome,
                    result=result_json,
                    effects=effects,
                    create_time=now_iso,
                    outbox_sequence=outbox_sequence,
                )
                self._insert_processed(
                    conn,
                    command_uuid=command_uuid,
                    idempotency_key=idempotency_key,
                    command_type="material.changeset.commit",
                    payload_hash=payload_hash,
                    result=_changeset_receipt_payload(receipt),
                    status="completed",
                    now_ms=now_ms,
                )
        except sqlite3.IntegrityError as exc:
            raise MaterialConflict("Material ChangeSet conflicts") from exc
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to commit Material ChangeSet"
            ) from None
        return receipt

    def _apply_create_material_effect(
        self,
        conn: sqlite3.Connection,
        *,
        claim_members: Mapping[tuple[str, str], JobClaimMemberRecord],
        effect: MaterialChangeSetEffect,
        now_iso: str,
    ) -> None:
        allowed = {
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
        }
        if not set(effect.after).issubset(allowed):
            raise MaterialInvalidInput("Material create contains unknown fields")
        template_uuid = _canonical_uuid(
            effect.after.get("resource_template_uuid"),
            "resource_template_uuid",
        )
        template = self._resource_templates.get(template_uuid)
        if template is None:
            raise MaterialInvalidInput("resource_template_uuid is not registered")
        klass = str(effect.after.get("class") or template.material_class).strip()
        if klass != template.material_class:
            raise MaterialInvalidInput("Material class does not match template")
        name = str(effect.after.get("name") or "").strip()
        if not name:
            raise MaterialInvalidInput("Material name must not be blank")
        parent_value = effect.after.get("parent_uuid")
        parent_uuid = (
            _canonical_uuid(parent_value, "parent_uuid")
            if parent_value is not None
            else None
        )
        if parent_uuid is not None and parent_uuid == effect.resource_uuid:
            raise MaterialConflict("created Material parent would create a cycle")
        if parent_uuid is not None:
            self._require_live_claimed_material(
                conn,
                claim_members=claim_members,
                resource_kind="business_material",
                material_uuid=parent_uuid,
                require_active=True,
            )
            if self._relationship_path_exists(
                conn,
                source_uuid=effect.resource_uuid,
                target_uuid=parent_uuid,
            ):
                raise MaterialConflict("created Material parent would create a cycle")
        disposition = str(effect.after.get("disposition") or "active")
        if disposition not in {
            "active",
            "consumed",
            "discarded",
            "quarantined",
            "reconciling",
        }:
            raise MaterialInvalidInput("Material disposition is invalid")
        description = effect.after.get("description")
        if description is not None and not isinstance(description, str):
            raise MaterialInvalidInput("Material description must be string or null")
        conn.execute(
            """
            INSERT INTO material(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, resource_template_uuid, parent_uuid, class,
                barcode, name, config, data, disposition, material_kind, version
            ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'business', 1)
            """,
            (
                effect.resource_uuid,
                now_iso,
                now_iso,
                description,
                json.dumps(
                    _json_object(effect.after.get("meta_data"), "after.meta_data"),
                    ensure_ascii=False,
                ),
                template_uuid,
                parent_uuid,
                klass,
                str(effect.after.get("barcode") or ""),
                name,
                json.dumps(
                    _json_object(effect.after.get("config"), "after.config"),
                    ensure_ascii=False,
                ),
                json.dumps(
                    _json_object(effect.after.get("data"), "after.data"),
                    ensure_ascii=False,
                ),
                disposition,
            ),
        )

    def _apply_create_site_effect(
        self,
        conn: sqlite3.Connection,
        *,
        claim_members: Mapping[tuple[str, str], JobClaimMemberRecord],
        effect: MaterialChangeSetEffect,
        now_iso: str,
    ) -> None:
        allowed = {
            "description",
            "meta_data",
            "material_uuid",
            "name",
            "sort_order",
            "allowed_resource_template_uuids",
            "occupied_material_uuid",
            "position_x",
            "position_y",
            "position_z",
            "depth",
            "length",
            "width",
        }
        if not set(effect.after).issubset(allowed):
            raise MaterialInvalidInput("Site create contains unknown fields")
        owner_uuid = _canonical_uuid(effect.after.get("material_uuid"), "material_uuid")
        if not any(
            (kind, owner_uuid) in claim_members
            for kind in ("device_material", "business_material")
        ):
            raise MaterialConflict("created Site owner is not a Claim member")
        owner_kind = (
            "device_material"
            if ("device_material", owner_uuid) in claim_members
            else "business_material"
        )
        self._require_live_claimed_material(
            conn,
            claim_members=claim_members,
            resource_kind=owner_kind,
            material_uuid=owner_uuid,
            require_active=owner_kind == "business_material",
        )
        name = str(effect.after.get("name") or "").strip()
        if not name:
            raise MaterialInvalidInput("Site name must not be blank")
        sort_order = effect.after.get("sort_order", 0)
        if (
            isinstance(sort_order, bool)
            or not isinstance(sort_order, int)
            or sort_order < 0
        ):
            raise MaterialInvalidInput("Site sort_order is invalid")
        raw_allowlist = effect.after.get("allowed_resource_template_uuids", [])
        if not isinstance(raw_allowlist, list):
            raise MaterialInvalidInput("Site allowlist must be an array")
        allowlist = tuple(
            sorted(
                {
                    _canonical_uuid(value, "allowed_resource_template_uuid")
                    for value in raw_allowlist
                }
            )
        )
        if any(value not in self._resource_templates for value in allowlist):
            raise MaterialInvalidInput("Site allowlist template is not registered")
        occupant_value = effect.after.get("occupied_material_uuid")
        occupant_uuid = (
            _canonical_uuid(occupant_value, "occupied_material_uuid")
            if occupant_value is not None
            else None
        )
        self._validate_site_placement(
            conn,
            claim_members=claim_members,
            site_uuid=None,
            owner_uuid=owner_uuid,
            occupant_uuid=occupant_uuid,
            allowed_template_uuids=allowlist,
        )
        geometry = {
            name: _finite_number(effect.after.get(name, 0), name)
            for name in (
                "position_x",
                "position_y",
                "position_z",
                "depth",
                "length",
                "width",
            )
        }
        if any(geometry[name] < 0 for name in ("depth", "length", "width")):
            raise MaterialInvalidInput("Site dimensions must not be negative")
        description = effect.after.get("description")
        if description is not None and not isinstance(description, str):
            raise MaterialInvalidInput("Site description must be string or null")
        conn.execute(
            """
            INSERT INTO site(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, material_uuid, name, sort_order,
                occupied_material_uuid, position_x, position_y, position_z,
                depth, length, width, version
            ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                effect.resource_uuid,
                now_iso,
                now_iso,
                description,
                json.dumps(
                    _json_object(effect.after.get("meta_data"), "after.meta_data"),
                    ensure_ascii=False,
                ),
                owner_uuid,
                name,
                sort_order,
                occupant_uuid,
                geometry["position_x"],
                geometry["position_y"],
                geometry["position_z"],
                geometry["depth"],
                geometry["length"],
                geometry["width"],
            ),
        )
        for template_uuid in allowlist:
            conn.execute(
                """
                INSERT INTO site_allowed_resource_template(
                    site_uuid, resource_template_uuid
                ) VALUES (?, ?)
                """,
                (effect.resource_uuid, template_uuid),
            )

    def _require_live_claimed_material(
        self,
        conn: sqlite3.Connection,
        *,
        claim_members: Mapping[tuple[str, str], JobClaimMemberRecord],
        resource_kind: str,
        material_uuid: str,
        require_active: bool,
    ) -> sqlite3.Row:
        member = claim_members.get((resource_kind, material_uuid))
        if member is None:
            raise MaterialConflict("referenced Material is not a live Claim member")
        expected_kind = "device" if resource_kind == "device_material" else "business"
        row = conn.execute(
            """
            SELECT uuid, resource_template_uuid, disposition,
                   material_kind, version
            FROM material
            WHERE uuid = ? AND deleted_at IS NULL
            """,
            (material_uuid,),
        ).fetchone()
        if row is None or str(row["material_kind"]) != expected_kind:
            raise MaterialConflict("referenced Claim Material is not live")
        if int(row["version"]) != member.expected_version:
            raise MaterialConflict("referenced Claim Material version is stale")
        if require_active and row["disposition"] != "active":
            raise MaterialConflict("referenced business Material is not active")
        return row

    def _validate_site_placement(
        self,
        conn: sqlite3.Connection,
        *,
        claim_members: Mapping[tuple[str, str], JobClaimMemberRecord],
        site_uuid: str | None,
        owner_uuid: str,
        occupant_uuid: str | None,
        allowed_template_uuids: tuple[str, ...],
    ) -> None:
        if occupant_uuid is None:
            return
        if occupant_uuid == owner_uuid:
            raise MaterialConflict("Site placement would create a cycle")
        occupant = self._require_live_claimed_material(
            conn,
            claim_members=claim_members,
            resource_kind="business_material",
            material_uuid=occupant_uuid,
            require_active=True,
        )
        if (
            allowed_template_uuids
            and str(occupant["resource_template_uuid"]) not in allowed_template_uuids
        ):
            raise MaterialConflict("Site occupant template is not allowed")
        duplicate = conn.execute(
            """
            SELECT uuid FROM site
            WHERE deleted_at IS NULL AND occupied_material_uuid = ?
              AND (? IS NULL OR uuid <> ?)
            LIMIT 1
            """,
            (occupant_uuid, site_uuid, site_uuid),
        ).fetchone()
        if duplicate is not None:
            raise MaterialConflict("Material already occupies another Site")
        if self._relationship_path_exists(
            conn,
            source_uuid=occupant_uuid,
            target_uuid=owner_uuid,
            excluded_site_uuid=site_uuid,
        ):
            raise MaterialConflict("Site placement would create a cycle")

    @staticmethod
    def _relationship_path_exists(
        conn: sqlite3.Connection,
        *,
        source_uuid: str,
        target_uuid: str,
        excluded_site_uuid: str | None = None,
    ) -> bool:
        """在 Material composition 与 Site occupancy 组合图中检查可达性。"""

        return (
            conn.execute(
                """
            WITH RECURSIVE
            edges(source_uuid, target_uuid) AS (
                SELECT parent_uuid, uuid FROM material
                WHERE parent_uuid IS NOT NULL AND deleted_at IS NULL
                UNION ALL
                SELECT material_uuid, occupied_material_uuid FROM site
                WHERE occupied_material_uuid IS NOT NULL
                  AND deleted_at IS NULL
                  AND (? IS NULL OR uuid <> ?)
            ),
            reachable(uuid) AS (
                SELECT ?
                UNION
                SELECT edges.target_uuid FROM edges
                JOIN reachable ON edges.source_uuid = reachable.uuid
            )
            SELECT 1 FROM reachable WHERE uuid = ? LIMIT 1
            """,
                (
                    excluded_site_uuid,
                    excluded_site_uuid,
                    source_uuid,
                    target_uuid,
                ),
            ).fetchone()
            is not None
        )

    def _apply_material_effect(
        self,
        conn: sqlite3.Connection,
        *,
        claim_members: Mapping[tuple[str, str], JobClaimMemberRecord],
        current: dict[str, Any],
        effect: MaterialChangeSetEffect,
        now_iso: str,
    ) -> None:
        if effect.operation not in {"update", "reparent", "soft_delete"}:
            raise MaterialInvalidInput("operation is invalid for business Material")
        allowed = {
            "description",
            "meta_data",
            "parent_uuid",
            "barcode",
            "name",
            "config",
            "data",
            "disposition",
        }
        if not set(effect.after).issubset(allowed):
            raise MaterialInvalidInput("Material ChangeSet contains unknown fields")
        normalized_after = dict(effect.after)
        if effect.operation != "reparent" and "parent_uuid" in normalized_after:
            raise MaterialInvalidInput(
                "parent_uuid can only be changed by a reparent effect"
            )
        if effect.operation == "reparent":
            parent_uuid = normalized_after.get("parent_uuid")
            canonical_parent = (
                _canonical_uuid(parent_uuid, "parent_uuid")
                if parent_uuid is not None
                else None
            )
            if canonical_parent == effect.resource_uuid:
                raise MaterialConflict("Material reparent would create a cycle")
            if canonical_parent is not None:
                self._require_live_claimed_material(
                    conn,
                    claim_members=claim_members,
                    resource_kind="business_material",
                    material_uuid=canonical_parent,
                    require_active=True,
                )
                if self._relationship_path_exists(
                    conn,
                    source_uuid=effect.resource_uuid,
                    target_uuid=canonical_parent,
                ):
                    raise MaterialConflict("Material reparent would create a cycle")
            normalized_after["parent_uuid"] = canonical_parent
        if effect.operation == "soft_delete":
            active_occupancy = conn.execute(
                """
                SELECT 1 FROM site
                WHERE deleted_at IS NULL AND occupied_material_uuid = ? LIMIT 1
                """,
                (effect.resource_uuid,),
            ).fetchone()
            if active_occupancy is not None:
                raise MaterialConflict("material_in_use")
            conn.execute(
                """
                UPDATE material SET deleted_at = ?, update_time = ?,
                    version = version + 1
                WHERE uuid = ?
                """,
                (now_iso, now_iso, effect.resource_uuid),
            )
            return
        current_projection = _material_record(current).to_dict()
        if all(
            current_projection.get(key) == value
            for key, value in normalized_after.items()
        ):
            return
        updated = dict(current)
        updated.update(normalized_after)
        if "meta_data" in normalized_after:
            updated["meta_data"] = json.dumps(
                _json_object(normalized_after["meta_data"], "after.meta_data"),
                ensure_ascii=False,
            )
        if "config" in normalized_after:
            updated["config"] = json.dumps(
                _json_object(normalized_after["config"], "after.config"),
                ensure_ascii=False,
            )
        if "data" in normalized_after:
            updated["data"] = json.dumps(
                _json_object(normalized_after["data"], "after.data"),
                ensure_ascii=False,
            )
        if updated["disposition"] not in {
            "active",
            "consumed",
            "discarded",
            "quarantined",
            "reconciling",
        }:
            raise MaterialInvalidInput("Material disposition is invalid")
        if not isinstance(updated["name"], str) or not updated["name"].strip():
            raise MaterialInvalidInput("Material name must not be blank")
        conn.execute(
            """
            UPDATE material SET description = ?, meta_data = ?, parent_uuid = ?,
                barcode = ?, name = ?, config = ?, data = ?, disposition = ?,
                version = version + 1, update_time = ?
            WHERE uuid = ?
            """,
            (
                updated["description"],
                updated["meta_data"],
                updated["parent_uuid"],
                updated["barcode"],
                updated["name"].strip(),
                updated["config"],
                updated["data"],
                updated["disposition"],
                now_iso,
                effect.resource_uuid,
            ),
        )

    def _apply_site_effect(
        self,
        conn: sqlite3.Connection,
        *,
        claim_members: Mapping[tuple[str, str], JobClaimMemberRecord],
        current: dict[str, Any],
        effect: MaterialChangeSetEffect,
        now_iso: str,
    ) -> None:
        if effect.operation not in {"update", "set_occupancy", "soft_delete"}:
            raise MaterialInvalidInput("operation is invalid for Site")
        if effect.operation == "soft_delete":
            if current["occupied_material_uuid"] is not None:
                raise MaterialConflict("material_in_use")
            conn.execute(
                """
                UPDATE site SET deleted_at = ?, update_time = ?, version = version + 1
                WHERE uuid = ?
                """,
                (now_iso, now_iso, effect.resource_uuid),
            )
            return
        allowed = {
            "description",
            "meta_data",
            "name",
            "sort_order",
            "occupied_material_uuid",
            "position_x",
            "position_y",
            "position_z",
            "depth",
            "length",
            "width",
        }
        if not set(effect.after).issubset(allowed):
            raise MaterialInvalidInput("Site ChangeSet contains unknown fields")
        current_record = _read_site(conn, effect.resource_uuid)
        if current_record is None:
            raise MaterialNotFound("ChangeSet Site not found")
        current_projection = current_record.to_dict()
        if all(
            current_projection.get(key) == value for key, value in effect.after.items()
        ):
            return
        updated = dict(current)
        updated.update(effect.after)
        occupant_uuid = updated["occupied_material_uuid"]
        if occupant_uuid is not None:
            occupant_uuid = _canonical_uuid(
                occupant_uuid,
                "occupied_material_uuid",
            )
        allowlist = tuple(
            str(row["resource_template_uuid"])
            for row in conn.execute(
                """
                SELECT resource_template_uuid
                FROM site_allowed_resource_template
                WHERE site_uuid = ?
                ORDER BY resource_template_uuid
                """,
                (effect.resource_uuid,),
            ).fetchall()
        )
        self._validate_site_placement(
            conn,
            claim_members=claim_members,
            site_uuid=effect.resource_uuid,
            owner_uuid=str(current["material_uuid"]),
            occupant_uuid=occupant_uuid,
            allowed_template_uuids=allowlist,
        )
        if "meta_data" in effect.after:
            updated["meta_data"] = json.dumps(
                _json_object(effect.after["meta_data"], "after.meta_data"),
                ensure_ascii=False,
            )
        conn.execute(
            """
            UPDATE site SET description = ?, meta_data = ?, name = ?, sort_order = ?,
                occupied_material_uuid = ?, position_x = ?, position_y = ?,
                position_z = ?, depth = ?, length = ?, width = ?,
                version = version + 1, update_time = ?
            WHERE uuid = ?
            """,
            (
                updated["description"],
                updated["meta_data"],
                updated["name"],
                updated["sort_order"],
                occupant_uuid,
                updated["position_x"],
                updated["position_y"],
                updated["position_z"],
                updated["depth"],
                updated["length"],
                updated["width"],
                now_iso,
                effect.resource_uuid,
            ),
        )

    def release_job_claim(
        self,
        command: JobClaimReleaseCommand,
    ) -> JobClaimResult:
        """只有 exact no-send 或 terminal-settled proof 才能释放 Claim。"""

        if not isinstance(command, JobClaimReleaseCommand):
            raise MaterialInvalidInput("command must be a JobClaimReleaseCommand")
        if command.schema_version != 1:
            raise MaterialInvalidInput("unsupported Job Claim schema_version")
        command_uuid = _canonical_uuid(command.command_uuid, "command_uuid")
        job_uuid = _canonical_uuid(
            command.workflow_node_job_uuid,
            "workflow_node_job_uuid",
        )
        claim_uuid = _canonical_uuid(command.claim_uuid, "claim_uuid")
        attempt = self._validate_attempt(command.attempt)
        token = self._validate_fencing_token(command.fencing_token)
        idempotency_key = self._validate_nonblank(
            command.idempotency_key,
            "idempotency_key",
        )
        proof_kind = self._validate_nonblank(
            command.release_proof_kind,
            "release_proof_kind",
        )
        if proof_kind not in {
            "not_submitted",
            "terminal_settled",
            "reconciled_terminal",
        }:
            raise MaterialInvalidInput("release_proof_kind is invalid")
        terminal_fingerprint = (
            self._validate_fingerprint(
                command.workflow_terminal_fingerprint,
                "workflow_terminal_fingerprint",
            )
            if command.workflow_terminal_fingerprint is not None
            else None
        )
        no_send_fingerprint = (
            self._validate_fingerprint(
                command.no_send_proof_fingerprint,
                "no_send_proof_fingerprint",
            )
            if command.no_send_proof_fingerprint is not None
            else None
        )
        reason = self._validate_nonblank(command.reason, "reason")
        expected_state = command.expected_state
        if expected_state is not None:
            expected_state = self._validate_nonblank(
                expected_state,
                "expected_state",
            )
            if expected_state not in {"reserved", "running", "uncertain"}:
                raise MaterialInvalidInput("expected_state is invalid")
        changeset_uuid = (
            _canonical_uuid(command.material_changeset_uuid, "material_changeset_uuid")
            if command.material_changeset_uuid is not None
            else None
        )
        changeset_fingerprint = (
            self._validate_fingerprint(
                command.material_changeset_fingerprint,
                "material_changeset_fingerprint",
            )
            if command.material_changeset_fingerprint is not None
            else None
        )
        if proof_kind == "not_submitted":
            if changeset_uuid is not None or changeset_fingerprint is not None:
                raise MaterialInvalidInput(
                    "not_submitted proof must not carry a Material ChangeSet"
                )
            if terminal_fingerprint is not None:
                raise MaterialInvalidInput(
                    "not_submitted proof must not carry workflow terminal proof"
                )
            if no_send_fingerprint is None:
                raise MaterialInvalidInput(
                    "not_submitted release requires durable no-send proof"
                )
        elif changeset_uuid is None or changeset_fingerprint is None:
            raise MaterialInvalidInput(
                "terminal release proof requires Material ChangeSet identity"
            )
        elif terminal_fingerprint is None:
            raise MaterialInvalidInput(
                "terminal release proof requires workflow terminal proof"
            )
        elif no_send_fingerprint is not None:
            raise MaterialInvalidInput(
                "terminal release proof must not carry no-send proof"
            )
        proof_payload = {
            "release_proof_kind": proof_kind,
            "material_changeset_uuid": changeset_uuid,
            "material_changeset_fingerprint": changeset_fingerprint,
            "workflow_terminal_fingerprint": terminal_fingerprint,
            "no_send_proof_fingerprint": no_send_fingerprint,
        }
        proof_fingerprint = (
            no_send_fingerprint
            if proof_kind == "not_submitted"
            else _canonical_payload_hash(proof_payload)
        )
        normalized_payload = {
            "schema_version": 1,
            "command_uuid": command_uuid,
            "idempotency_key": idempotency_key,
            "workflow_node_job_uuid": job_uuid,
            "attempt": attempt,
            "claim_uuid": claim_uuid,
            "fencing_token": token,
            **proof_payload,
            "reason": reason,
            "expected_state": expected_state,
        }
        payload_hash = _canonical_payload_hash(normalized_payload)
        now_iso = self._now_iso()
        now_ms = self._now_ms()
        try:
            with self._tx() as conn:
                replay = self._processed_payload(conn, command_uuid, payload_hash)
                if replay is not None:
                    return _claim_result_from_payload(replay)
                claim = self._read_job_claim(conn, claim_uuid=claim_uuid)
                if claim is None:
                    raise MaterialNotFound("Job Claim not found")
                if (
                    claim.workflow_node_job_uuid != job_uuid
                    or claim.attempt != attempt
                    or claim.fencing_token != token
                ):
                    raise MaterialConflict("stale Job Claim owner or fencing token")
                if expected_state is not None and claim.state != expected_state:
                    raise MaterialConflict("Job Claim expected_state is stale")
                if claim.state == "released":
                    if (
                        claim.release_proof_fingerprint != proof_fingerprint
                        or claim.release_command_uuid != command_uuid
                    ):
                        raise MaterialConflict("Job Claim release proof conflicts")
                    result = JobClaimResult(
                        schema_version=1,
                        command_uuid=command_uuid,
                        status="released",
                        claim=claim,
                        diagnostics=(),
                        outbox_sequence=None,
                    )
                    self._insert_processed(
                        conn,
                        command_uuid=command_uuid,
                        idempotency_key=idempotency_key,
                        command_type="material.claim.release",
                        payload_hash=payload_hash,
                        result=_claim_result_payload(result),
                        status="completed",
                        now_ms=now_ms,
                    )
                    return result
                if proof_kind == "not_submitted" and claim.state != "reserved":
                    raise MaterialConflict(
                        "not_submitted proof only releases a reserved Claim"
                    )
                if proof_kind != "not_submitted":
                    receipt = conn.execute(
                        "SELECT * FROM material_changeset WHERE uuid = ?",
                        (changeset_uuid,),
                    ).fetchone()
                    if (
                        receipt is None
                        or receipt["claim_uuid"] != claim_uuid
                        or int(receipt["fencing_token"]) != token
                        or receipt["deterministic_fingerprint"] != changeset_fingerprint
                    ):
                        raise MaterialConflict(
                            "terminal Material ChangeSet proof does not match Claim"
                        )
                conn.execute(
                    """
                    UPDATE material_claim
                    SET state = 'released', release_proof_kind = ?,
                        release_proof_fingerprint = ?, release_reason = ?,
                        terminal_changeset_uuid = ?,
                        workflow_terminal_fingerprint = ?,
                        release_command_uuid = ?, released_at = ?, update_time = ?
                    WHERE uuid = ?
                    """,
                    (
                        proof_kind,
                        proof_fingerprint,
                        reason,
                        changeset_uuid,
                        terminal_fingerprint,
                        command_uuid,
                        now_iso,
                        now_iso,
                        claim_uuid,
                    ),
                )
                conn.execute(
                    """
                    UPDATE material_claim_member SET released_at = ?
                    WHERE claim_uuid = ? AND released_at IS NULL
                    """,
                    (now_iso, claim_uuid),
                )
                outbox_sequence = self._emit(
                    conn,
                    now_ms,
                    "material_claim",
                    claim_uuid,
                    token,
                    "material_claim.released",
                    {
                        "workflow_node_job_uuid": job_uuid,
                        "attempt": attempt,
                        "fencing_token": token,
                        **proof_payload,
                        "reason": reason,
                    },
                    causation_id=command_uuid,
                    reason=reason,
                )
                released = self._read_job_claim(conn, claim_uuid=claim_uuid)
                if released is None:
                    raise MaterialAuthorityUnavailable("released Claim disappeared")
                result = JobClaimResult(
                    schema_version=1,
                    command_uuid=command_uuid,
                    status="released",
                    claim=released,
                    diagnostics=(),
                    outbox_sequence=outbox_sequence,
                )
                self._insert_processed(
                    conn,
                    command_uuid=command_uuid,
                    idempotency_key=idempotency_key,
                    command_type="material.claim.release",
                    payload_hash=payload_hash,
                    result=_claim_result_payload(result),
                    status="completed",
                    now_ms=now_ms,
                )
        except sqlite3.IntegrityError as exc:
            raise MaterialConflict("Job Claim release conflicts") from exc
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable("failed to release Job Claim") from None
        return result

    def resolve_job_claim(
        self,
        command: JobClaimResolutionCommand,
    ) -> JobClaimResult:
        """持久化一个封闭、有 evidence 的 reconciliation decision。

        Terminal resolutions commit physical reality only.  The Scheduler then
        projects the Workflow terminal fact and releases the Claim (C4-C6).
        """

        if not isinstance(command, JobClaimResolutionCommand):
            raise MaterialInvalidInput("command must be a JobClaimResolutionCommand")
        if command.schema_version != 1:
            raise MaterialInvalidInput("unsupported Job Claim schema_version")
        command_uuid = _canonical_uuid(command.command_uuid, "command_uuid")
        job_uuid = _canonical_uuid(
            command.workflow_node_job_uuid,
            "workflow_node_job_uuid",
        )
        claim_uuid = _canonical_uuid(command.claim_uuid, "claim_uuid")
        attempt = self._validate_attempt(command.attempt)
        token = self._validate_fencing_token(command.fencing_token)
        idempotency_key = self._validate_nonblank(
            command.idempotency_key,
            "idempotency_key",
        )
        expected_state = self._validate_nonblank(
            command.expected_state,
            "expected_state",
        )
        if expected_state not in {"reserved", "running", "uncertain"}:
            raise MaterialInvalidInput("expected_state is invalid")
        resolution = self._validate_nonblank(command.resolution, "resolution")
        if resolution not in {
            "confirmed_running",
            "confirmed_not_dispatched",
            "confirmed_terminal",
            "quarantine_and_fail",
            "unresolved",
        }:
            raise MaterialInvalidInput("resolution is invalid")
        evidence_kind = self._validate_nonblank(
            command.evidence_kind,
            "evidence_kind",
        )
        evidence_fingerprint = self._validate_fingerprint(
            command.evidence_fingerprint,
            "evidence_fingerprint",
        )
        actor_identity = self._validate_nonblank(
            command.actor_identity,
            "actor_identity",
        )
        reason = self._validate_nonblank(command.reason, "reason")
        observed_at = self._validate_observed_at(command.observed_at)
        no_send_proof = (
            self._validate_fingerprint(
                command.no_send_proof_fingerprint,
                "no_send_proof_fingerprint",
            )
            if command.no_send_proof_fingerprint is not None
            else None
        )
        workflow_terminal_fingerprint = (
            self._validate_fingerprint(
                command.workflow_terminal_fingerprint,
                "workflow_terminal_fingerprint",
            )
            if command.workflow_terminal_fingerprint is not None
            else None
        )
        terminal_payload = (
            _changeset_command_payload(command.terminal_changeset)
            if command.terminal_changeset is not None
            else None
        )
        normalized_payload = {
            "schema_version": 1,
            "command_uuid": command_uuid,
            "idempotency_key": idempotency_key,
            "workflow_node_job_uuid": job_uuid,
            "attempt": attempt,
            "claim_uuid": claim_uuid,
            "fencing_token": token,
            "expected_state": expected_state,
            "resolution": resolution,
            "evidence_kind": evidence_kind,
            "evidence_fingerprint": evidence_fingerprint,
            "observed_at": observed_at,
            "actor_identity": actor_identity,
            "reason": reason,
            "no_send_proof_fingerprint": no_send_proof,
            "terminal_changeset": terminal_payload,
            "workflow_terminal_fingerprint": workflow_terminal_fingerprint,
        }
        payload_hash = _canonical_payload_hash(normalized_payload)
        try:
            with self._tx() as conn:
                replay = self._processed_payload(conn, command_uuid, payload_hash)
                if replay is not None:
                    return _claim_result_from_payload(replay)
                claim = self._read_job_claim(conn, claim_uuid=claim_uuid)
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to read Job Claim resolution state"
            ) from None
        if claim is None:
            raise MaterialNotFound("Job Claim not found")
        if (
            claim.workflow_node_job_uuid != job_uuid
            or claim.attempt != attempt
            or claim.fencing_token != token
        ):
            raise MaterialConflict("stale Job Claim owner or fencing token")

        child_uuid = lambda phase: str(  # noqa: E731 - deterministic command seam
            uuid.uuid5(UUID(command_uuid), phase)
        )
        nested_result: JobClaimResult
        receipt: MaterialChangeSetReceipt | None = None
        if resolution == "confirmed_running":
            if expected_state != "uncertain":
                raise MaterialConflict("confirmed_running requires uncertain Claim")
            self._reject_resolution_terminal_fields(
                no_send_proof,
                command.terminal_changeset,
                workflow_terminal_fingerprint,
            )
            nested_result = self.mark_job_claim_running(
                JobClaimStateCommand(
                    schema_version=1,
                    command_uuid=child_uuid("confirmed-running"),
                    idempotency_key=f"{idempotency_key}:confirmed-running",
                    workflow_node_job_uuid=job_uuid,
                    attempt=attempt,
                    claim_uuid=claim_uuid,
                    fencing_token=token,
                    evidence_kind=evidence_kind,
                    evidence_fingerprint=evidence_fingerprint,
                    expected_state=expected_state,
                )
            )
        elif resolution == "unresolved":
            self._reject_resolution_terminal_fields(
                no_send_proof,
                command.terminal_changeset,
                workflow_terminal_fingerprint,
            )
            nested_result = self.mark_job_claim_uncertain(
                JobClaimUncertainCommand(
                    schema_version=1,
                    command_uuid=child_uuid("unresolved"),
                    idempotency_key=f"{idempotency_key}:unresolved",
                    workflow_node_job_uuid=job_uuid,
                    attempt=attempt,
                    claim_uuid=claim_uuid,
                    fencing_token=token,
                    uncertainty_reason=reason,
                    evidence_fingerprint=evidence_fingerprint,
                    expected_state=expected_state,
                )
            )
        elif resolution == "confirmed_not_dispatched":
            if expected_state != "reserved":
                raise MaterialConflict(
                    "confirmed_not_dispatched requires reserved Claim"
                )
            if evidence_kind != "coordinator_no_send" or no_send_proof is None:
                raise MaterialInvalidInput(
                    "confirmed_not_dispatched requires durable coordinator "
                    "no-send proof"
                )
            if command.terminal_changeset is not None:
                raise MaterialInvalidInput(
                    "confirmed_not_dispatched must not carry a terminal ChangeSet"
                )
            if workflow_terminal_fingerprint is not None:
                raise MaterialInvalidInput(
                    "confirmed_not_dispatched must not carry terminal workflow proof"
                )
            nested_result = self.release_job_claim(
                JobClaimReleaseCommand(
                    schema_version=1,
                    command_uuid=child_uuid("confirmed-not-dispatched"),
                    idempotency_key=(f"{idempotency_key}:confirmed-not-dispatched"),
                    workflow_node_job_uuid=job_uuid,
                    attempt=attempt,
                    claim_uuid=claim_uuid,
                    fencing_token=token,
                    release_proof_kind="not_submitted",
                    material_changeset_uuid=None,
                    material_changeset_fingerprint=None,
                    workflow_terminal_fingerprint=None,
                    reason=reason,
                    no_send_proof_fingerprint=no_send_proof,
                    expected_state=expected_state,
                )
            )
        else:
            if expected_state not in {"running", "uncertain"}:
                raise MaterialConflict(
                    "terminal resolution requires running or uncertain Claim"
                )
            if no_send_proof is not None:
                raise MaterialInvalidInput(
                    "terminal resolution must not carry no-send proof"
                )
            if command.terminal_changeset is None:
                raise MaterialInvalidInput(
                    "terminal resolution requires a Material ChangeSet"
                )
            if workflow_terminal_fingerprint is None:
                raise MaterialInvalidInput(
                    "terminal resolution requires workflow terminal fingerprint"
                )
            terminal = command.terminal_changeset
            if (
                terminal.command_uuid == command_uuid
                or terminal.workflow_task_uuid != claim.workflow_task_uuid
                or terminal.workflow_node_job_uuid != job_uuid
                or terminal.attempt != attempt
                or terminal.claim_uuid != claim_uuid
                or terminal.fencing_token != token
            ):
                raise MaterialConflict(
                    "terminal resolution ChangeSet owner or command identity conflicts"
                )
            if resolution == "quarantine_and_fail":
                if terminal.outcome != "failed":
                    raise MaterialInvalidInput(
                        "quarantine_and_fail requires failed outcome"
                    )
                quarantined = {
                    effect.resource_uuid
                    for effect in terminal.effects
                    if effect.resource_kind == "business_material"
                    and effect.operation == "update"
                    and effect.after.get("disposition") == "quarantined"
                }
                business_members = {
                    member.resource_uuid
                    for member in claim.members
                    if member.resource_kind == "business_material"
                    and member.released_at is None
                }
                if not business_members.issubset(quarantined):
                    raise MaterialInvalidInput(
                        "quarantine_and_fail must quarantine every business member"
                    )
            receipt = self.commit_material_changeset(
                MaterialChangeSetCommand(
                    schema_version=terminal.schema_version,
                    command_uuid=terminal.command_uuid,
                    idempotency_key=terminal.idempotency_key,
                    workflow_task_uuid=terminal.workflow_task_uuid,
                    workflow_node_job_uuid=terminal.workflow_node_job_uuid,
                    attempt=terminal.attempt,
                    claim_uuid=terminal.claim_uuid,
                    fencing_token=terminal.fencing_token,
                    effect_identity=terminal.effect_identity,
                    outcome=terminal.outcome,
                    result=terminal.result,
                    effects=terminal.effects,
                    expected_claim_state=expected_state,
                )
            )
            durable_claim = self.get_job_claim(job_uuid, attempt)
            nested_result = JobClaimResult(
                schema_version=1,
                command_uuid=command_uuid,
                status="terminal_evidence_committed",
                claim=durable_claim,
                diagnostics=(),
                outbox_sequence=receipt.outbox_sequence,
            )

        return self._record_job_claim_resolution(
            command_uuid=command_uuid,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            claim=nested_result.claim,
            status=nested_result.status,
            resolution=resolution,
            evidence_kind=evidence_kind,
            evidence_fingerprint=evidence_fingerprint,
            observed_at=observed_at,
            actor_identity=actor_identity,
            reason=reason,
            workflow_terminal_fingerprint=workflow_terminal_fingerprint,
            receipt=receipt,
        )

    @staticmethod
    def _validate_observed_at(value: str) -> str:
        observed_at = InventoryService._validate_nonblank(value, "observed_at")
        try:
            parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError:
            raise MaterialInvalidInput("observed_at must be RFC3339") from None
        if parsed.tzinfo is None:
            raise MaterialInvalidInput("observed_at must include a timezone")
        return observed_at

    @staticmethod
    def _reject_resolution_terminal_fields(
        no_send_proof: str | None,
        terminal_changeset: MaterialChangeSetCommand | None,
        workflow_terminal_fingerprint: str | None,
    ) -> None:
        if (
            no_send_proof is not None
            or terminal_changeset is not None
            or workflow_terminal_fingerprint is not None
        ):
            raise MaterialInvalidInput(
                "resolution carries evidence fields that do not apply"
            )

    def _record_job_claim_resolution(
        self,
        *,
        command_uuid: str,
        idempotency_key: str,
        payload_hash: str,
        claim: JobClaimRecord | None,
        status: str,
        resolution: str,
        evidence_kind: str,
        evidence_fingerprint: str,
        observed_at: str,
        actor_identity: str,
        reason: str,
        workflow_terminal_fingerprint: str | None,
        receipt: MaterialChangeSetReceipt | None,
    ) -> JobClaimResult:
        if claim is None:
            raise MaterialAuthorityUnavailable("resolved Job Claim disappeared")
        now_ms = self._now_ms()
        details = {
            "resolution": resolution,
            "evidence_kind": evidence_kind,
            "evidence_fingerprint": evidence_fingerprint,
            "observed_at": observed_at,
            "actor_identity": actor_identity,
            "reason": reason,
            "workflow_terminal_fingerprint": workflow_terminal_fingerprint,
            "material_changeset_uuid": receipt.uuid if receipt else None,
            "material_changeset_fingerprint": (
                receipt.deterministic_fingerprint if receipt else None
            ),
        }
        try:
            with self._tx() as conn:
                replay = self._processed_payload(conn, command_uuid, payload_hash)
                if replay is not None:
                    return _claim_result_from_payload(replay)
                outbox_sequence = self._emit(
                    conn,
                    now_ms,
                    "material_claim",
                    claim.uuid,
                    claim.fencing_token,
                    "material_claim.resolved",
                    details,
                    causation_id=command_uuid,
                    actor=actor_identity,
                    reason=reason,
                )
                result = JobClaimResult(
                    schema_version=1,
                    command_uuid=command_uuid,
                    status=status,
                    claim=claim,
                    diagnostics=(details,),
                    outbox_sequence=outbox_sequence,
                )
                self._insert_processed(
                    conn,
                    command_uuid=command_uuid,
                    idempotency_key=idempotency_key,
                    command_type="material.claim.resolve",
                    payload_hash=payload_hash,
                    result=_claim_result_payload(result),
                    status="completed",
                    now_ms=now_ms,
                )
        except sqlite3.IntegrityError:
            try:
                with self._tx() as conn:
                    replay = self._processed_payload(
                        conn,
                        command_uuid,
                        payload_hash,
                    )
            except sqlite3.Error:
                raise MaterialAuthorityUnavailable(
                    "failed to replay Job Claim resolution"
                ) from None
            if replay is None:
                raise MaterialConflict("Job Claim resolution conflicts") from None
            return _claim_result_from_payload(replay)
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to record Job Claim resolution"
            ) from None
        return result

    def get_command_result(
        self,
        command_uuid: str,
    ) -> (
        TaskMaterialAdmissionResult
        | TaskMaterialReleaseResult
        | JobClaimResult
        | MaterialChangeSetReceipt
    ):
        """按 command UUID 读取一个 durable Material command result。"""

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
            if str(row.get("command_type") or "").startswith("material.claim."):
                return _claim_result_from_payload(payload)
            if row.get("command_type") == "material.changeset.commit":
                return _changeset_receipt_from_payload(payload)
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
        """读取一个 exclusive sequence cursor 之后的 durable Inventory events。"""

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
        """返回一个 consumer 独立的 durable acknowledgement watermark。"""

        if consumer not in _CURSOR_NAMES:
            raise MaterialInvalidInput("consumer must be scheduler or cloud")

        try:
            return self._store.get_cursor(consumer)
        except sqlite3.Error:
            raise MaterialAuthorityUnavailable(
                "failed to read acknowledgement"
            ) from None

    def acknowledge(self, sequence: int, *, consumer: str = "scheduler") -> None:
        """单调推进一个 consumer watermark，且不跨 consumer。"""

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

    def _reconcile_resource_graph_devices(
        self,
        conn: sqlite3.Connection,
        *,
        source_id: str,
        materials: list[Any],
        now_iso: str,
    ) -> tuple[str, ...]:
        """Add missing executor Materials from the same ResourceTreeSet source.

        Existing Inventory rows are durable truth and are never rewritten here.  A
        ResourceTreeSet projection may regenerate its runtime UUID while retaining
        the same graph node, template and class; in that case the durable Material
        UUID wins.  Other identity conflicts fail closed.
        """

        existing_by_source_node: dict[str, sqlite3.Row] = {}
        for row in conn.execute(
            """
            SELECT * FROM material
            WHERE material_kind = 'device'
              AND json_extract(meta_data, '$.source') = 'resource-tree-set'
              AND json_extract(meta_data, '$.source_graph') = ?
            ORDER BY uuid
            """,
            (source_id,),
        ).fetchall():
            meta_data = _stored_json_object(row["meta_data"])
            source_node_id = str(meta_data.get("source_node_id") or "").strip()
            if not source_node_id:
                raise MaterialConflict(
                    "stored ResourceTreeSet device identity is incomplete"
                )
            if source_node_id in existing_by_source_node:
                raise MaterialConflict(
                    "stored ResourceTreeSet device identity is ambiguous"
                )
            existing_by_source_node[source_node_id] = row

        projected_by_source_node: dict[str, dict[str, Any]] = {}
        projected_uuid_to_source_node: dict[str, str] = {}
        for raw in materials:
            if not isinstance(raw, Mapping):
                raise MaterialInvalidInput("bootstrap material must be an object")
            if str(raw.get("material_kind") or "") != "device":
                continue
            material_uuid = _canonical_uuid(raw.get("uuid"), "material.uuid")
            template_uuid = _canonical_uuid(
                raw.get("resource_template_uuid"),
                "material.resource_template_uuid",
            )
            if template_uuid not in self._resource_templates:
                raise MaterialInvalidInput(
                    "bootstrap resource_template_uuid is not registered"
                )
            meta_data = _json_object(raw.get("meta_data"), "meta_data")
            source_node_id = str(meta_data.get("source_node_id") or "").strip()
            if (
                meta_data.get("source") != "resource-tree-set"
                or meta_data.get("source_graph") != source_id
                or not source_node_id
            ):
                raise MaterialInvalidInput(
                    "bootstrap device source identity is invalid"
                )
            if source_node_id in projected_by_source_node:
                raise MaterialConflict("bootstrap device source_node_id must be unique")
            previous_source_node = projected_uuid_to_source_node.get(material_uuid)
            if previous_source_node is not None:
                raise MaterialConflict("bootstrap Material UUID must be unique")
            projected_uuid_to_source_node[material_uuid] = source_node_id
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
            parent_value = raw.get("parent_uuid")
            projected_by_source_node[source_node_id] = {
                "uuid": material_uuid,
                "resource_template_uuid": template_uuid,
                "parent_uuid": (
                    _canonical_uuid(parent_value, "material.parent_uuid")
                    if parent_value is not None
                    else None
                ),
                "class": klass,
                "barcode": str(raw.get("barcode") or ""),
                "name": name,
                "description": description,
                "meta_data": meta_data,
                "config": _json_object(raw.get("config"), "config"),
                "data": _json_object(raw.get("data"), "data"),
            }

        added: list[str] = []
        for source_node_id, projected in projected_by_source_node.items():
            existing = existing_by_source_node.get(source_node_id)
            if existing is not None:
                if existing["uuid"] != projected["uuid"]:
                    existing_meta = _stored_json_object(existing["meta_data"])
                    existing_runtime_uuid = str(
                        existing_meta.get("source_runtime_uuid") or ""
                    ).strip()
                    projected_runtime_uuid = str(
                        projected["meta_data"].get("source_runtime_uuid") or ""
                    ).strip()
                    runtime_projection_regenerated = bool(
                        existing_runtime_uuid
                        and projected_runtime_uuid
                        and existing_runtime_uuid != projected_runtime_uuid
                    )
                    same_device_contract = bool(
                        existing["resource_template_uuid"]
                        == projected["resource_template_uuid"]
                        and existing["class"] == projected["class"]
                    )
                    projected_uuid_owner = conn.execute(
                        "SELECT uuid FROM material WHERE uuid = ?",
                        (projected["uuid"],),
                    ).fetchone()
                    if (
                        not runtime_projection_regenerated
                        or not same_device_contract
                        or projected_uuid_owner is not None
                    ):
                        raise MaterialConflict(
                            "ResourceTreeSet device identity changed Material UUID"
                        )
                continue
            uuid_owner = conn.execute(
                "SELECT meta_data FROM material WHERE uuid = ?",
                (projected["uuid"],),
            ).fetchone()
            if uuid_owner is not None:
                raise MaterialConflict(
                    "ResourceTreeSet device Material UUID is already in use"
                )
            parent_uuid = projected["parent_uuid"]
            if parent_uuid is not None:
                parent = conn.execute(
                    "SELECT uuid FROM material WHERE uuid = ? AND deleted_at IS NULL",
                    (parent_uuid,),
                ).fetchone()
                if parent is None or parent_uuid == projected["uuid"]:
                    raise MaterialInvalidInput(
                        "bootstrap device parent Material is missing"
                    )
            conn.execute(
                """
                INSERT INTO material(
                    uuid, create_time, update_time, deleted_at,
                    description, meta_data, resource_template_uuid,
                    parent_uuid, class, barcode, name, config, data,
                    disposition, material_kind, version
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'device', 1)
                """,
                (
                    projected["uuid"],
                    now_iso,
                    now_iso,
                    projected["description"],
                    json.dumps(projected["meta_data"]),
                    projected["resource_template_uuid"],
                    parent_uuid,
                    projected["class"],
                    projected["barcode"],
                    projected["name"],
                    json.dumps(projected["config"]),
                    json.dumps(projected["data"]),
                ),
            )
            added.append(projected["uuid"])
        return tuple(added)

    def bootstrap_resource_graph(self, command: Mapping[str, Any]) -> dict[str, Any]:
        """首次导入 ResourceTreeSet，并幂等补齐同源新增的 device Material。"""

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
                stored_source_row = conn.execute(
                    "SELECT meta_value FROM lab_meta WHERE meta_key = ?",
                    ("resource_graph_bootstrap_source",),
                ).fetchone()
                stored = str(stored_row["meta_value"]) if stored_row else ""
                stored_source = (
                    str(stored_source_row["meta_value"]) if stored_source_row else ""
                )
                if existing:
                    if stored_source != source_id:
                        return {
                            "status": "preserved",
                            "source_id": source_id,
                            "fingerprint": stored or None,
                            "material_count": existing,
                        }
                    if stored == fingerprint:
                        return {
                            "status": "unchanged",
                            "source_id": source_id,
                            "fingerprint": stored,
                            "material_count": existing,
                        }
                    added_device_uuids = self._reconcile_resource_graph_devices(
                        conn,
                        source_id=source_id,
                        materials=materials,
                        now_iso=now_iso,
                    )
                    conn.execute(
                        """
                        UPDATE lab_meta SET meta_value = ?
                        WHERE meta_key = 'resource_graph_bootstrap_fingerprint'
                        """,
                        (fingerprint,),
                    )
                    material_count = existing + len(added_device_uuids)
                    if added_device_uuids:
                        self._emit(
                            conn,
                            now_ms,
                            "material_graph",
                            source_id,
                            1,
                            "material_graph.devices_reconciled",
                            {
                                "source_id": source_id,
                                "fingerprint": fingerprint,
                                "added_device_uuids": list(added_device_uuids),
                                "material_count": material_count,
                            },
                        )
                    return {
                        "status": "reconciled",
                        "source_id": source_id,
                        "fingerprint": fingerprint,
                        "material_count": material_count,
                        "added_device_count": len(added_device_uuids),
                    }

                material_ids: set[str] = set()
                parent_by_material: dict[str, str | None] = {}
                template_by_material: dict[str, str] = {}
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
                    template_by_material[material_uuid] = template_uuid
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

                site_occupants: list[tuple[str, str, tuple[str, ...]]] = []
                occupied_material_ids: set[str] = set()
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
                    occupied_value = raw.get("occupied_material_uuid")
                    if occupied_value is not None:
                        occupied_material_uuid = _canonical_uuid(
                            occupied_value,
                            "site.occupied_material_uuid",
                        )
                        if occupied_material_uuid not in material_ids:
                            raise MaterialInvalidInput(
                                "bootstrap Site occupant is missing"
                            )
                        if occupied_material_uuid == owner_uuid:
                            raise MaterialInvalidInput(
                                "bootstrap Site cannot contain its owner"
                            )
                        if occupied_material_uuid in occupied_material_ids:
                            raise MaterialConflict(
                                "bootstrap Material cannot occupy multiple Sites"
                            )
                        occupant_template_uuid = template_by_material[
                            occupied_material_uuid
                        ]
                        if (
                            allowed_uuids
                            and occupant_template_uuid not in allowed_uuids
                        ):
                            raise MaterialConflict(
                                "bootstrap Site occupant template is not allowed"
                            )
                        occupied_material_ids.add(occupied_material_uuid)
                        site_occupants.append(
                            (site_uuid, occupied_material_uuid, allowed_uuids)
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

                for site_uuid, occupied_material_uuid, _allowed in site_occupants:
                    conn.execute(
                        "UPDATE site SET occupied_material_uuid = ? WHERE uuid = ?",
                        (occupied_material_uuid, site_uuid),
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

    def read_material_model_asset(
        self, public_path: str
    ) -> tuple[MaterialModelAsset, bytes]:
        """Read one exact Catalog asset without exposing its package source path."""

        asset = self._material_model_assets.get(public_path)
        if asset is None:
            raise MaterialNotFound(f"material model asset not found: {public_path}")
        content = asset.read_bytes()
        if len(content) != asset.size:
            raise MaterialAuthorityUnavailable("material model asset size mismatch")
        return asset, content

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
