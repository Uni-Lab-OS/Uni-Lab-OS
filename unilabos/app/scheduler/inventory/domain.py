"""仓储领域模型：状态机、不变量、领域错误.

本模块零外部依赖（纯 stdlib），不 import HTTP/ROS/sqlite。
Edge 是仓储/物料实例/物理层级/内容物/预留的唯一事实源（云端只做投影）。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# 领域错误
# ---------------------------------------------------------------------------


class InventoryError(Exception):
    """仓储领域错误基类."""

    code = "inventory_error"


class InsufficientStock(InventoryError):
    """可用数量不足（预留/消费时）."""

    code = "insufficient_stock"


class VersionConflict(InventoryError):
    """expected_version 与当前 aggregate version 不一致（禁止 Last-Write-Wins）."""

    code = "version_conflict"


class InvariantViolation(InventoryError):
    """数量不变量被破坏（非负 / available+reserved<=total）."""

    code = "invariant_violation"


class DuplicateBarcode(InventoryError):
    """barcode 在 active 实例中必须唯一."""

    code = "duplicate_barcode"


class NotFound(InventoryError):
    """目标聚合不存在."""

    code = "not_found"


class CommandRejected(InventoryError):
    """云端 command 被拒绝（版本过期/参数非法/状态机不允许）."""

    code = "command_rejected"


class MaterialError(InventoryError):
    """Material public error 基类。"""

    code = "material_error"


class MaterialInvalidInput(MaterialError):
    """调用者提交了无效的 Material identity 或字段。"""

    code = "invalid_input"


class MaterialNotFound(MaterialError):
    """未找到可见的 Material。"""

    code = "not_found"


class MaterialConflict(MaterialError):
    """Material identity 或持久约束冲突。"""

    code = "conflict"


class MaterialAuthorityUnavailable(MaterialError):
    """Material durable store 无法完成请求。"""

    code = "material_authority_unavailable"


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------


class LotState(str, Enum):
    AVAILABLE = "available"
    RESERVED = "reserved"  # 全部数量都被预留
    DEPLETED = "depleted"
    QUARANTINED = "quarantined"


class InstanceState(str, Enum):
    WAREHOUSE = "warehouse"
    RESERVED = "reserved"
    BENCH = "bench"  # 已上台（deploy）
    IN_USE = "in_use"
    CONSUMED = "consumed"
    DISCARDED = "discarded"
    QUARANTINED = "quarantined"


class ReservationState(str, Enum):
    ACTIVE = "active"
    CONSUMED = "consumed"  # 预留已转为实际消费
    RELEASED = "released"
    QUARANTINED = "quarantined"  # 节点失败但物理已使用，转人工复核


#: instance 状态机允许的迁移（from -> {to}）
INSTANCE_TRANSITIONS: dict[InstanceState, set] = {
    InstanceState.WAREHOUSE: {
        InstanceState.RESERVED,
        InstanceState.BENCH,
        InstanceState.DISCARDED,
        InstanceState.QUARANTINED,
    },
    InstanceState.RESERVED: {
        InstanceState.WAREHOUSE,
        InstanceState.BENCH,
        InstanceState.QUARANTINED,
    },
    InstanceState.BENCH: {
        InstanceState.IN_USE,
        InstanceState.WAREHOUSE,
        InstanceState.CONSUMED,
        InstanceState.DISCARDED,
        InstanceState.QUARANTINED,
    },
    InstanceState.IN_USE: {
        InstanceState.BENCH,
        InstanceState.CONSUMED,
        InstanceState.DISCARDED,
        InstanceState.QUARANTINED,
    },
    InstanceState.CONSUMED: set(),
    InstanceState.DISCARDED: set(),
    InstanceState.QUARANTINED: {
        InstanceState.WAREHOUSE,
        InstanceState.DISCARDED,
    },  # 人工复核后放行/报废
}

#: active（占用 barcode / 占用库存）的实例状态
ACTIVE_INSTANCE_STATES = {
    InstanceState.WAREHOUSE,
    InstanceState.RESERVED,
    InstanceState.BENCH,
    InstanceState.IN_USE,
    InstanceState.QUARANTINED,
}


def check_instance_transition(current: InstanceState, target: InstanceState) -> None:
    if target not in INSTANCE_TRANSITIONS[current]:
        raise CommandRejected(
            f"instance transition {current.value} -> {target.value} not allowed"
        )


def lot_state_for(
    total: float, available: float, reserved: float, quarantined: bool
) -> LotState:
    """根据数量推导 lot 状态（状态是数量的函数，不单独维护）."""
    if quarantined:
        return LotState.QUARANTINED
    if total <= 0:
        return LotState.DEPLETED
    if available <= 0 and reserved > 0:
        return LotState.RESERVED
    return LotState.AVAILABLE


def check_lot_invariants(total: float, available: float, reserved: float) -> None:
    """数量非负，available + reserved <= total."""
    if total < 0 or available < 0 or reserved < 0:
        raise InvariantViolation(
            f"negative quantity: total={total} available={available} reserved={reserved}"
        )
    # 浮点容差
    if available + reserved > total + 1e-9:
        raise InvariantViolation(
            f"available({available}) + reserved({reserved}) > total({total})"
        )


# ---------------------------------------------------------------------------
# 值对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResourceTemplateIdentity:
    """Registry/PackageCatalog 提供给 InventoryService 的最小 identity。"""

    uuid: str
    material_class: str


@dataclass(frozen=True, slots=True)
class MaterialModelAsset:
    """Catalog-audited model asset exposed through an opaque read callback."""

    public_path: str
    media_type: str
    digest: str
    size: int
    read_bytes: Callable[[], bytes] = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ResourceSlotResolution:
    """InventoryService 对 concrete ResourceSlot 的 canonical resolution。"""

    uuid: str
    resource_template_uuid: str


@dataclass(frozen=True, slots=True)
class TaskMaterialAdmissionSource:
    """One closed MaterialSource entry in a Task admission command。"""

    material_source_node_uuid: str
    mode: str
    resource_template_uuid: str
    mount: dict[str, Any]
    material_uuid: str | None
    site_uuid: str | None
    candidate_site_uuids: tuple[str, ...]
    flow_role: str


@dataclass(frozen=True, slots=True)
class TaskMaterialAdmissionCommand:
    """Versioned Task-wide Material admission command。"""

    schema_version: int
    command_uuid: str
    idempotency_key: str
    workflow_task_uuid: str
    workflow_snapshot_fingerprint: str
    sources: tuple[TaskMaterialAdmissionSource, ...]


@dataclass(frozen=True, slots=True)
class TaskMaterialBinding:
    """Canonical Material binding for one MaterialSource node。"""

    material_source_node_uuid: str
    resource_slot: dict[str, Any]
    site_uuid: str | None


@dataclass(frozen=True, slots=True)
class TaskMaterialAdmissionResult:
    """Closed durable result of one Task-wide admission command。"""

    schema_version: int
    command_uuid: str
    workflow_task_uuid: str
    status: str
    reservation_uuid: str | None
    bindings: tuple[TaskMaterialBinding, ...]
    diagnostics: tuple[dict[str, Any], ...]
    outbox_sequence: int


@dataclass(frozen=True, slots=True)
class TaskMaterialReleaseCommand:
    """Versioned terminal release command for one WorkflowTask。"""

    schema_version: int
    command_uuid: str
    idempotency_key: str
    workflow_task_uuid: str
    reason: str


@dataclass(frozen=True, slots=True)
class TaskMaterialReleaseResult:
    """Closed durable result of one Task Reservation release。"""

    schema_version: int
    command_uuid: str
    workflow_task_uuid: str
    status: str
    reservation_uuid: str | None
    outbox_sequence: int


@dataclass(frozen=True, slots=True)
class InventoryEvent:
    """Public immutable projection of one durable Inventory outbox event。"""

    sequence: int
    event_id: str
    edge_id: str
    lab_id: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    event_type: str
    occurred_at: int
    causation_id: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MaterialRecord:
    """Backend-field-aligned durable Material projection。"""

    uuid: str
    create_time: str
    update_time: str
    deleted_at: str | None
    description: str | None
    meta_data: dict[str, Any]
    resource_template_uuid: str
    parent_uuid: str | None
    klass: str
    barcode: str
    name: str
    config: dict[str, Any]
    data: dict[str, Any]
    disposition: str | None
    material_kind: str
    version: int

    def to_dict(self) -> dict[str, Any]:
        """投影为 Backend exact-baseline 的结构化 Material 字段。"""

        return {
            "uuid": self.uuid,
            "create_time": self.create_time,
            "update_time": self.update_time,
            "deleted_at": self.deleted_at,
            "description": self.description,
            "meta_data": dict(self.meta_data),
            "resource_template_uuid": self.resource_template_uuid,
            "parent_uuid": self.parent_uuid,
            "class": self.klass,
            "barcode": self.barcode,
            "name": self.name,
            "config": dict(self.config),
            "data": dict(self.data),
            "disposition": self.disposition,
            "material_kind": self.material_kind,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class SiteRecord:
    """Backend-field-aligned durable Site projection。"""

    uuid: str
    create_time: str
    update_time: str
    deleted_at: str | None
    description: str | None
    meta_data: dict[str, Any]
    material_uuid: str
    name: str
    sort_order: int
    allowed_resource_template_uuids: tuple[str, ...]
    occupied_material_uuid: str | None
    position_x: float
    position_y: float
    position_z: float
    depth: float
    length: float
    width: float
    version: int

    def to_dict(self) -> dict[str, Any]:
        """Project one active Site to the Backend-shaped public record."""

        projection: dict[str, Any] = {
            "uuid": self.uuid,
            "create_time": self.create_time,
            "update_time": self.update_time,
            "meta_data": dict(self.meta_data),
            "material_uuid": self.material_uuid,
            "name": self.name,
            "sort_order": self.sort_order,
            "allowed_resource_template_uuids": list(
                self.allowed_resource_template_uuids
            ),
            "position_x": self.position_x,
            "position_y": self.position_y,
            "position_z": self.position_z,
            "depth": self.depth,
            "length": self.length,
            "width": self.width,
            "version": self.version,
        }
        if self.description is not None:
            projection["description"] = self.description
        if self.occupied_material_uuid is not None:
            projection["occupied_material_uuid"] = self.occupied_material_uuid
        return projection


@dataclass
class MaterialRequirement:
    """一个节点对物料的需求（挂在 WorkflowNode 上，可选字段）.

    - lot 需求：template_id/lot_id + quantity（数量型，FIFO 扣 lot）
    - instance 需求：instance_uuid 或 barcode（实体型，deploy 具体实例）
    """

    template_id: str = ""
    lot_id: str = ""
    quantity: float = 0.0
    unit: str = ""
    instance_uuid: str = ""
    barcode: str = ""

    def is_instance_requirement(self) -> bool:
        return bool(self.instance_uuid or self.barcode)

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "lot_id": self.lot_id,
            "quantity": self.quantity,
            "unit": self.unit,
            "instance_uuid": self.instance_uuid,
            "barcode": self.barcode,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MaterialRequirement:
        return cls(
            template_id=str(data.get("template_id") or data.get("templateId") or ""),
            lot_id=str(data.get("lot_id") or data.get("lotId") or ""),
            quantity=float(data.get("quantity") or 0.0),
            unit=str(data.get("unit") or ""),
            instance_uuid=str(
                data.get("instance_uuid") or data.get("instanceUuid") or ""
            ),
            barcode=str(data.get("barcode") or ""),
        )


@dataclass
class OutboxEvent:
    """同步事件 envelope（sequence 由 store 落库时分配）."""

    event_id: str
    edge_id: str
    lab_id: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    event_type: str
    occurred_at: int  # 毫秒
    causation_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    sequence: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "edge_id": self.edge_id,
            "lab_id": self.lab_id,
            "sequence": self.sequence,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "aggregate_version": self.aggregate_version,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "causation_id": self.causation_id,
            "payload": self.payload,
        }


def new_event_id(occurred_at_ms: int) -> str:
    """可排序 event id：毫秒时间戳(12hex) + uuid4 尾部（UUIDv7 风格）."""
    ts_hex = format(occurred_at_ms & 0xFFFFFFFFFFFF, "012x")
    tail = uuid.uuid4().hex[12:]
    return f"{ts_hex[:8]}-{ts_hex[8:12]}-7{tail[:3]}-{tail[3:7]}-{tail[7:19]}"


def idempotency_key(workflow_id: str, node_id: str, attempt: int) -> str:
    return f"{workflow_id}:{node_id}:{attempt}"


# 需求聚合 --------------------------------------------------------------------


def aggregate_lot_requirements(
    requirements: list[MaterialRequirement],
) -> dict[str, float]:
    """按 (lot_id 或 template:xxx) 汇总数量型需求，用于整 DAG 预留."""
    totals: dict[str, float] = {}
    for req in requirements:
        if req.is_instance_requirement() or req.quantity <= 0:
            continue
        key = req.lot_id if req.lot_id else f"template:{req.template_id}"
        totals[key] = totals.get(key, 0.0) + req.quantity
    return totals
