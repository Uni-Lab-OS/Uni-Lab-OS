"""设备遥测投影（DeviceTelemetryProjection）的内存 latest 深模块。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

DEVICE_PROPERTIES = "device_properties"
JOINT_STATE = "joint_state"
TELEMETRY_TYPES = frozenset({DEVICE_PROPERTIES, JOINT_STATE})
TELEMETRY_COMMITTED_EVENT = "device.telemetry_committed"

_MAX_BODY_BYTES = 4 << 20
_MAX_SAMPLES = 128
_MAX_PROPERTIES = 512
_MAX_JOINTS = 512
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SCALAR_TYPES = (bool, int, float, str)


class DeviceTelemetryError(ValueError):
    """设备遥测合同拒绝结果。

    参数：``message`` 是诊断文本，``business_code`` 是正式后端（Backend）
    响应码，``http_status`` 是 HTTP 状态。返回：异常实例。异常：无。
    """

    def __init__(
        self,
        message: str,
        *,
        business_code: int = 1000,
        http_status: int = 200,
    ) -> None:
        super().__init__(message)
        self.business_code = business_code
        self.http_status = http_status


@dataclass(frozen=True, slots=True)
class TelemetryCommit:
    """一次 HTTP latest 提交的可通知引用。"""

    material_uuid: str
    local_device_id: str
    telemetry_type: str
    boot_id: str
    through_sequence: int
    accepted_ref: str
    created: int

    def as_dict(self) -> dict[str, Any]:
        """编码正式后端（Backend）形状的提交结果。

        参数：无。返回：可放入 ``data`` 的独立字典。异常：无。
        """

        return {
            "material_uuid": self.material_uuid,
            "local_device_id": self.local_device_id,
            "telemetry_type": self.telemetry_type,
            "boot_id": self.boot_id,
            "through_sequence": self.through_sequence,
            "accepted_ref": self.accepted_ref,
            "created": self.created,
        }

    def notification_payload(self) -> dict[str, Any]:
        """编码 ``device.telemetry_committed`` 的最小短通知。

        参数：无。返回：不含完整遥测数据的独立字典。异常：无。
        """

        result = self.as_dict()
        result.pop("created")
        return result


@dataclass(frozen=True, slots=True)
class _TelemetryFact:
    """已校验且可由 ``accepted_ref`` 唯一引用的 latest 事实。"""

    material_uuid: str
    local_device_id: str
    telemetry_type: str
    boot_id: str
    sequence: int
    observed_at: str
    observed_epoch_s: float
    stale_after_s: float | None
    data: dict[str, Any]
    accepted_ref: str

    @property
    def key(self) -> tuple[str, str]:
        """返回物料身份与遥测类型组成的 latest 键。

        参数：无。返回：``(material_uuid, telemetry_type)``。异常：无。
        """

        return self.material_uuid, self.telemetry_type

    def as_event(self, *, stale: bool) -> dict[str, Any]:
        """编码前端服务器发送事件（SSE）使用的完整事实。

        参数：``stale`` 表示当前新鲜度。返回：脱离内部状态的字典。异常：无。
        """

        return {
            "material_uuid": self.material_uuid,
            "local_device_id": self.local_device_id,
            "telemetry_type": self.telemetry_type,
            "boot_id": self.boot_id,
            "sequence": self.sequence,
            "accepted_ref": self.accepted_ref,
            "observed_at": self.observed_at,
            "stale_after_s": self.stale_after_s,
            "stale": stale,
            "data": _copy_json(self.data),
        }


class TelemetrySubscription:
    """按设备和遥测类型合并的单个前端订阅。"""

    def __init__(
        self,
        *,
        material_uuid: str = "",
        local_device_id: str = "",
        telemetry_type: str = "",
    ) -> None:
        """建立有界合并缓冲。

        参数：三个可选过滤字段限定物料、设备和遥测类型。返回：无。异常：
        ``telemetry_type`` 不在闭集时抛出 ``DeviceTelemetryError``。
        """

        if telemetry_type and telemetry_type not in TELEMETRY_TYPES:
            raise DeviceTelemetryError("telemetry_type is unsupported")
        self.material_uuid = material_uuid
        self.local_device_id = local_device_id
        self.telemetry_type = telemetry_type
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, str], dict[str, Any]] = {}
        self._closed = False

    def accepts(self, event: Mapping[str, Any]) -> bool:
        """判断一个事件是否满足订阅过滤。

        参数：``event`` 是完整遥测事件。返回：是否接收。异常：无。
        """

        return (
            (not self.material_uuid or event.get("material_uuid") == self.material_uuid)
            and (
                not self.local_device_id
                or event.get("local_device_id") == self.local_device_id
            )
            and (
                not self.telemetry_type
                or event.get("telemetry_type") == self.telemetry_type
            )
        )

    def push(self, event: dict[str, Any]) -> None:
        """用同设备同类型的最新事件覆盖等待项。

        参数：``event`` 是完整遥测事件。返回：无。异常：关闭后静默忽略。
        """

        if not self.accepts(event):
            return
        key = (str(event["material_uuid"]), str(event["telemetry_type"]))
        with self._lock:
            if not self._closed:
                self._pending[key] = _copy_json(event)

    def drain(self) -> list[dict[str, Any]]:
        """按稳定键顺序取走当前全部合并事件。

        参数：无。返回：等待事件的脱离副本。异常：无。
        """

        with self._lock:
            events = [self._pending[key] for key in sorted(self._pending)]
            self._pending.clear()
        return events

    def close(self) -> None:
        """关闭订阅并清空等待事件。

        参数：无。返回：无。异常：无；重复关闭幂等。
        """

        with self._lock:
            self._closed = True
            self._pending.clear()


class DeviceTelemetryHub:
    """集中完成接收、通知、快照、新鲜度与订阅背压的深模块。"""

    def __init__(
        self,
        binding_resolver: Callable[[str, str], bool] | None = None,
    ) -> None:
        """建立当前运行 epoch 的内存 latest。

        参数：``binding_resolver`` 校验 ``local_device_id`` 与物料 UUID 是否属于
        当前边缘设备绑定（EdgeDeviceBinding）。返回：无。异常：无；本地后端
        （Local Backend）只维护当前进程的 latest，不承担重启恢复。
        """

        self._binding_resolver = binding_resolver or (lambda _local, _material: True)
        self._lock = threading.RLock()
        self._accepted: dict[tuple[str, str], _TelemetryFact] = {}
        self._published: dict[tuple[str, str], _TelemetryFact] = {}
        self._published_stale: dict[tuple[str, str], bool] = {}
        self._subscriptions: set[TelemetrySubscription] = set()

    def ingest_properties(
        self, material_uuid: str, payload: Mapping[str, Any]
    ) -> TelemetryCommit:
        """接受一批通用设备属性完整快照。

        参数：``material_uuid`` 是设备物料身份，``payload`` 是严格 v1 请求体。
        返回：最新样本的可通知提交引用。异常：非法、未绑定或冲突请求抛出
        ``DeviceTelemetryError``，且整批不产生部分更新。
        """

        facts = _properties_facts(material_uuid, payload)
        return self._ingest(facts)

    def ingest_joint_states(
        self, material_uuid: str, payload: Mapping[str, Any]
    ) -> TelemetryCommit:
        """接受一批机械臂关节状态完整快照。

        参数：``material_uuid`` 是设备物料身份，``payload`` 是严格 v1 请求体。
        返回：最新样本的可通知提交引用。异常：非法、未绑定或冲突请求抛出
        ``DeviceTelemetryError``，且整批不产生部分更新。
        """

        facts = _joint_state_facts(material_uuid, payload)
        return self._ingest(facts)

    def _ingest(self, facts: tuple[_TelemetryFact, ...]) -> TelemetryCommit:
        """原子推进一个设备和遥测类型的 accepted latest。

        参数：``facts`` 是已校验、序列递增的一批事实。返回：提交引用。异常：
        设备绑定缺失返回 7000，旧序列或同身份异内容返回 7001。
        """

        latest = facts[-1]
        if not self._binding_resolver(latest.local_device_id, latest.material_uuid):
            raise DeviceTelemetryError(
                "Edge device binding was not found",
                business_code=7000,
            )
        with self._lock:
            current = self._accepted.get(latest.key)
            if current is not None and current.boot_id == latest.boot_id:
                if latest.sequence < current.sequence:
                    raise DeviceTelemetryError(
                        "telemetry sequence moved backwards",
                        business_code=7001,
                        http_status=409,
                    )
                if latest.sequence == current.sequence:
                    if latest.accepted_ref != current.accepted_ref:
                        raise DeviceTelemetryError(
                            "telemetry identity was reused with different content",
                            business_code=7001,
                            http_status=409,
                        )
                    return _commit(current, created=0)
            self._accepted[latest.key] = latest
            return _commit(latest, created=len(facts))

    def notify(self, payload: Mapping[str, Any]) -> bool:
        """消费 WebSocket 已提交通知并发布命中的当前 latest。

        参数：``payload`` 只含设备身份、类型、代际、序列和 ``accepted_ref``。
        返回：是否产生新的 SSE 可见值；旧通知、重复通知或已被新 HTTP 事实覆盖的
        通知返回 ``False``。异常：通知形状非法时抛出 ``DeviceTelemetryError``。
        """

        _exact_fields(
            payload,
            {
                "material_uuid",
                "local_device_id",
                "telemetry_type",
                "boot_id",
                "through_sequence",
                "accepted_ref",
            },
            "telemetry notification",
        )
        material_uuid = _uuid_text(payload.get("material_uuid"), "material_uuid")
        local_device_id = _text(payload.get("local_device_id"), "local_device_id")
        telemetry_type = _text(payload.get("telemetry_type"), "telemetry_type")
        if telemetry_type not in TELEMETRY_TYPES:
            raise DeviceTelemetryError("telemetry_type is unsupported")
        boot_id = _text(payload.get("boot_id"), "boot_id", maximum=128)
        sequence = _positive_int(payload.get("through_sequence"), "through_sequence")
        accepted_ref = _text(payload.get("accepted_ref"), "accepted_ref", maximum=80)
        key = (material_uuid, telemetry_type)
        with self._lock:
            fact = self._accepted.get(key)
            if fact is None or (
                fact.local_device_id != local_device_id
                or fact.boot_id != boot_id
                or fact.sequence != sequence
                or fact.accepted_ref != accepted_ref
            ):
                return False
            current = self._published.get(key)
            if current is not None and current.accepted_ref == accepted_ref:
                return False
            event = fact.as_event(stale=False)
            self._published[key] = fact
            self._published_stale[key] = False
            self._broadcast_locked(event)
            return True

    def subscribe(
        self,
        *,
        material_uuid: str = "",
        local_device_id: str = "",
        telemetry_type: str = "",
    ) -> tuple[TelemetrySubscription, list[dict[str, Any]]]:
        """建立先快照后更新的有界订阅。

        参数：三个可选过滤字段限定物料、设备和遥测类型。返回：订阅句柄与当前
        可见 latest 快照。异常：过滤值非法时抛出 ``DeviceTelemetryError``。
        """

        normalized_material = (
            _uuid_text(material_uuid, "material_uuid") if material_uuid else ""
        )
        subscription = TelemetrySubscription(
            material_uuid=normalized_material,
            local_device_id=local_device_id.strip(),
            telemetry_type=telemetry_type.strip(),
        )
        with self._lock:
            self._subscriptions.add(subscription)
            snapshot = [
                fact.as_event(stale=self._published_stale.get(key, False))
                for key, fact in sorted(self._published.items())
                if subscription.accepts(fact.as_event(stale=False))
            ]
        return subscription, snapshot

    def unsubscribe(self, subscription: TelemetrySubscription) -> None:
        """移除并关闭一个订阅。

        参数：``subscription`` 是先前返回的句柄。返回：无。异常：重复移除幂等。
        """

        with self._lock:
            self._subscriptions.discard(subscription)
        subscription.close()

    def expire(self, *, now_epoch_s: float | None = None) -> int:
        """把超过 TTL 的关节状态投影为一次 ``stale`` 转换。

        参数：``now_epoch_s`` 是测试可注入的当前时间。返回：本次新转换数量。
        异常：无；通用设备属性没有 TTL，不会因值未变化被误判过期。
        """

        current = time.time() if now_epoch_s is None else float(now_epoch_s)
        changed = 0
        with self._lock:
            for key, fact in self._published.items():
                ttl = fact.stale_after_s
                if ttl is None or self._published_stale.get(key, False):
                    continue
                if current - fact.observed_epoch_s <= ttl:
                    continue
                self._published_stale[key] = True
                self._broadcast_locked(fact.as_event(stale=True))
                changed += 1
        return changed

    def _broadcast_locked(self, event: dict[str, Any]) -> None:
        """在持有 Hub 锁时向每个订阅写入合并缓冲。

        参数：``event`` 是完整遥测事件。返回：无。异常：订阅故障被隔离。
        """

        for subscription in tuple(self._subscriptions):
            try:
                subscription.push(event)
            except Exception:
                self._subscriptions.discard(subscription)


def _properties_facts(
    material_uuid: str, payload: Mapping[str, Any]
) -> tuple[_TelemetryFact, ...]:
    """校验属性批次并产生规范事实。

    参数：设备物料 UUID 和请求体。返回：非空事实元组。异常：合同不合法时抛出
    ``DeviceTelemetryError``。
    """

    common = _batch_common(material_uuid, payload)
    facts: list[_TelemetryFact] = []
    for sample in common["samples"]:
        _exact_fields(
            sample,
            {"sequence", "observed_at", "properties", "property_observed_at"},
            "device properties sample",
        )
        properties = sample.get("properties")
        property_times = sample.get("property_observed_at")
        if not isinstance(properties, Mapping) or not 0 < len(properties) <= _MAX_PROPERTIES:
            raise DeviceTelemetryError("properties must be a non-empty object")
        if not isinstance(property_times, Mapping) or set(property_times) != set(properties):
            raise DeviceTelemetryError("property_observed_at must match properties")
        normalized_properties: dict[str, Any] = {}
        normalized_times: dict[str, str] = {}
        for raw_name, value in properties.items():
            name = _text(raw_name, "property name", maximum=255)
            if not isinstance(value, _SCALAR_TYPES) or (
                isinstance(value, float) and not math.isfinite(value)
            ):
                raise DeviceTelemetryError("device property values must be finite scalars")
            normalized_properties[name] = value
            normalized_times[name] = _timestamp(property_times.get(raw_name))[0]
        observed_at, observed_epoch_s = _timestamp(sample.get("observed_at"))
        facts.append(
            _fact(
                common,
                sample,
                DEVICE_PROPERTIES,
                observed_at,
                observed_epoch_s,
                None,
                {
                    "properties": normalized_properties,
                    "property_observed_at": normalized_times,
                },
            )
        )
    return _ordered_facts(facts)


def _joint_state_facts(
    material_uuid: str, payload: Mapping[str, Any]
) -> tuple[_TelemetryFact, ...]:
    """校验关节状态批次并产生规范事实。

    参数：设备物料 UUID 和请求体。返回：非空事实元组。异常：合同不合法时抛出
    ``DeviceTelemetryError``。
    """

    common = _batch_common(material_uuid, payload)
    facts: list[_TelemetryFact] = []
    for sample in common["samples"]:
        _exact_fields(
            sample,
            {
                "sequence",
                "observed_at",
                "stale_after_s",
                "topology_digest",
                "joint_states",
            },
            "joint state sample",
        )
        topology_digest = _text(sample.get("topology_digest"), "topology_digest")
        if _DIGEST.fullmatch(topology_digest) is None:
            raise DeviceTelemetryError("topology_digest must be lowercase SHA-256")
        joints = sample.get("joint_states")
        if not isinstance(joints, Mapping) or not 0 < len(joints) <= _MAX_JOINTS:
            raise DeviceTelemetryError("joint_states must be a non-empty object")
        normalized_joints: dict[str, float] = {}
        for raw_name, raw_position in joints.items():
            name = _text(raw_name, "joint name", maximum=255)
            if isinstance(raw_position, bool) or not isinstance(raw_position, (int, float)):
                raise DeviceTelemetryError("joint positions must be finite numbers")
            position = float(raw_position)
            if not math.isfinite(position):
                raise DeviceTelemetryError("joint positions must be finite numbers")
            normalized_joints[name] = position
        stale_after_s = float(sample.get("stale_after_s", 0.0))
        if not math.isfinite(stale_after_s) or not 0.5 <= stale_after_s <= 5.0:
            raise DeviceTelemetryError("stale_after_s must be between 0.5 and 5.0")
        observed_at, observed_epoch_s = _timestamp(sample.get("observed_at"))
        facts.append(
            _fact(
                common,
                sample,
                JOINT_STATE,
                observed_at,
                observed_epoch_s,
                stale_after_s,
                {
                    "topology_digest": topology_digest,
                    "joint_states": normalized_joints,
                },
            )
        )
    return _ordered_facts(facts)


def _batch_common(material_uuid: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """校验两类批次共享的设备身份和大小。

    参数：设备物料 UUID 与请求体。返回：规范共享字段。异常：非法时抛出
    ``DeviceTelemetryError``。
    """

    if not isinstance(payload, Mapping):
        raise DeviceTelemetryError("telemetry body must be an object")
    _exact_fields(payload, {"local_device_id", "boot_id", "samples"}, "telemetry body")
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    except (TypeError, ValueError) as error:
        raise DeviceTelemetryError("telemetry body must be JSON") from error
    if len(encoded) > _MAX_BODY_BYTES:
        raise DeviceTelemetryError("telemetry body exceeds 4 MiB")
    samples = payload.get("samples")
    if not isinstance(samples, list) or not 0 < len(samples) <= _MAX_SAMPLES:
        raise DeviceTelemetryError("samples must contain between 1 and 128 objects")
    if any(not isinstance(sample, Mapping) for sample in samples):
        raise DeviceTelemetryError("every telemetry sample must be an object")
    return {
        "material_uuid": _uuid_text(material_uuid, "material_uuid"),
        "local_device_id": _text(payload.get("local_device_id"), "local_device_id"),
        "boot_id": _text(payload.get("boot_id"), "boot_id", maximum=128),
        "samples": samples,
    }


def _fact(
    common: Mapping[str, Any],
    sample: Mapping[str, Any],
    telemetry_type: str,
    observed_at: str,
    observed_epoch_s: float,
    stale_after_s: float | None,
    data: dict[str, Any],
) -> _TelemetryFact:
    """构造带内容摘要的不可变 latest 事实。

    参数：共享身份、单个样本、类型、时间、新鲜度和类型数据。返回：事实。
    异常：序列非法时抛出 ``DeviceTelemetryError``。
    """

    sequence = _positive_int(sample.get("sequence"), "sequence")
    canonical = {
        "material_uuid": common["material_uuid"],
        "local_device_id": common["local_device_id"],
        "telemetry_type": telemetry_type,
        "boot_id": common["boot_id"],
        "sequence": sequence,
        "observed_at": observed_at,
        "stale_after_s": stale_after_s,
        "data": data,
    }
    digest = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return _TelemetryFact(
        material_uuid=str(common["material_uuid"]),
        local_device_id=str(common["local_device_id"]),
        telemetry_type=telemetry_type,
        boot_id=str(common["boot_id"]),
        sequence=sequence,
        observed_at=observed_at,
        observed_epoch_s=observed_epoch_s,
        stale_after_s=stale_after_s,
        data=data,
        accepted_ref=f"sha256:{digest}",
    )


def _ordered_facts(facts: list[_TelemetryFact]) -> tuple[_TelemetryFact, ...]:
    """要求一个批次内序列严格递增。

    参数：同设备同类型事实列表。返回：不可变元组。异常：乱序或重复时抛出
    ``DeviceTelemetryError``。
    """

    sequences = [fact.sequence for fact in facts]
    if sequences != sorted(set(sequences)):
        raise DeviceTelemetryError("sample sequences must be strictly increasing")
    return tuple(facts)


def _commit(fact: _TelemetryFact, *, created: int) -> TelemetryCommit:
    """从 latest 事实建立提交引用。

    参数：``fact`` 是当前事实，``created`` 是新接受样本数。返回：提交引用。
    异常：无。
    """

    return TelemetryCommit(
        material_uuid=fact.material_uuid,
        local_device_id=fact.local_device_id,
        telemetry_type=fact.telemetry_type,
        boot_id=fact.boot_id,
        through_sequence=fact.sequence,
        accepted_ref=fact.accepted_ref,
        created=created,
    )


def _exact_fields(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    """拒绝缺失和未知字段。

    参数：对象、允许字段和诊断标签。返回：无。异常：字段集合不同时抛出
    ``DeviceTelemetryError``。
    """

    actual = set(value)
    if actual != fields:
        raise DeviceTelemetryError(
            f"{label} fields must be exactly {sorted(fields)}"
        )


def _text(value: Any, field: str, *, maximum: int = 255) -> str:
    """规范化必填短文本。

    参数：原值、字段名和最大长度。返回：去空白文本。异常：非法时抛出
    ``DeviceTelemetryError``。
    """

    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise DeviceTelemetryError(f"{field} must be non-empty text up to {maximum} chars")
    return value.strip()


def _uuid_text(value: Any, field: str) -> str:
    """规范化稳定 UUID 身份。

    参数：原值与字段名。返回：小写 UUID 文本。异常：非法时抛出
    ``DeviceTelemetryError``。
    """

    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as error:
        raise DeviceTelemetryError(f"{field} must be a UUID") from error


def _positive_int(value: Any, field: str) -> int:
    """规范化正整数序列。

    参数：原值与字段名。返回：正整数。异常：布尔值、零或非整数被拒绝。
    """

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DeviceTelemetryError(f"{field} must be a positive integer")
    return value


def _timestamp(value: Any) -> tuple[str, float]:
    """规范化带时区 RFC3339 时间。

    参数：时间文本。返回：UTC 微秒文本与 Unix 秒。异常：无时区或非法时间被
    ``DeviceTelemetryError`` 拒绝。
    """

    if not isinstance(value, str) or not value.strip():
        raise DeviceTelemetryError("observed_at must be RFC3339 text")
    encoded = value.strip()
    try:
        parsed = datetime.fromisoformat(encoded.replace("Z", "+00:00"))
    except ValueError as error:
        raise DeviceTelemetryError("observed_at must be RFC3339 text") from error
    if parsed.tzinfo is None:
        raise DeviceTelemetryError("observed_at must include a timezone")
    utc = parsed.astimezone(timezone.utc)
    return utc.isoformat(timespec="microseconds").replace("+00:00", "Z"), utc.timestamp()


def _copy_json(value: Any) -> Any:
    """建立只含 JSON 类型的深副本。

    参数：已校验 JSON 值。返回：独立值。异常：调用方破坏内部不变量时原样抛出。
    """

    return json.loads(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


__all__ = [
    "DEVICE_PROPERTIES",
    "JOINT_STATE",
    "TELEMETRY_COMMITTED_EVENT",
    "TELEMETRY_TYPES",
    "DeviceTelemetryError",
    "DeviceTelemetryHub",
    "TelemetryCommit",
    "TelemetrySubscription",
]
