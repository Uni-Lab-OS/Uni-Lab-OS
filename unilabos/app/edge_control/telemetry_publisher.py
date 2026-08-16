"""Edge Runtime 设备遥测 HTTP→短通知持久交接深模块。"""

from __future__ import annotations

import asyncio
import copy
import math
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from unilabos.app.edge_control.device_telemetry import (
    DEVICE_PROPERTIES,
    JOINT_STATE,
)
from unilabos.app.edge_control.http import EdgeDataPlane
from unilabos.app.edge_control.store import EdgeControlStore, StoredTelemetry
from unilabos.utils.log import get_comm_logger

logger = get_comm_logger()


class DeviceTelemetryPublisher:
    """把高频设备事实收敛为持久、可重试的 HTTP latest 与 WS 引用。"""

    def __init__(
        self,
        store: EdgeControlStore,
        data_plane: EdgeDataPlane,
        *,
        enabled: bool,
        retry_interval: float,
    ) -> None:
        """建立一个 Edge Runtime 运行代际的发布器。

        参数：协议存储、HTTP 数据面、本地能力门禁与失败重试间隔。返回：无。
        异常：无；``enabled=False`` 时所有提交均为关闭的空操作。
        """

        self._store = store
        self._data_plane = data_plane
        self._enabled = bool(enabled)
        self._retry_interval = max(float(retry_interval), 0.1)
        self._boot_id = str(uuid.uuid4())
        self._lock = threading.RLock()
        self._drain_lock = asyncio.Lock()
        self._bindings: dict[str, str] = {}
        self._sequences: dict[tuple[str, str], int] = {}
        self._retry_after = 0.0

    @property
    def enabled(self) -> bool:
        """返回本次进程是否允许使用尚未进入正式后端的遥测合同。"""

        return self._enabled

    def bind_devices(self, devices: Sequence[Mapping[str, Any]]) -> None:
        """替换最近注册确认的设备绑定（EdgeDeviceBinding）。

        参数：注册请求中的设备数组。返回：无。异常：非法 UUID 关闭式失败。
        正式后端门禁关闭时不保留绑定。
        """

        if not self._enabled:
            return
        bindings = {
            str(device["local_id"]).strip(): str(
                uuid.UUID(str(device["material_uuid"]))
            )
            for device in devices
            if str(device.get("local_id") or "").strip()
            and str(device.get("material_uuid") or "").strip()
        }
        with self._lock:
            self._bindings = bindings

    def submit_properties(
        self,
        local_device_id: str,
        properties: Mapping[str, Any],
        property_observed_epoch_s: Mapping[str, Any],
    ) -> bool:
        """持久合并一个通用设备属性完整快照。

        参数：本地设备身份、完整属性表和每属性 Unix 观测时间。返回：是否已获得
        绑定并写入待提交表。异常：非标量或非有限值抛出 ``ValueError``。
        """

        if not self._enabled:
            return False
        local_id = str(local_device_id or "").strip()
        with self._lock:
            material_uuid = self._bindings.get(local_id, "")
        if not material_uuid or not properties:
            return False
        normalized: dict[str, Any] = {}
        observed: dict[str, str] = {}
        epochs: list[float] = []
        now = time.time()
        for raw_name, value in properties.items():
            name = str(raw_name or "").strip()
            if not name or not isinstance(value, (bool, int, float, str)):
                raise ValueError("device properties must be named JSON scalars")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("device property numbers must be finite")
            epoch = _finite_epoch(property_observed_epoch_s.get(raw_name), now)
            normalized[name] = copy.deepcopy(value)
            observed[name] = _rfc3339(epoch)
            epochs.append(epoch)
        sequence = self._next_sequence(local_id, DEVICE_PROPERTIES)
        payload = {
            "local_device_id": local_id,
            "boot_id": self._boot_id,
            "samples": [
                {
                    "sequence": sequence,
                    "observed_at": _rfc3339(max(epochs)),
                    "properties": normalized,
                    "property_observed_at": observed,
                }
            ],
        }
        self._store.save_telemetry(
            material_uuid=material_uuid,
            local_device_id=local_id,
            telemetry_type=DEVICE_PROPERTIES,
            boot_id=self._boot_id,
            sequence=sequence,
            payload=payload,
        )
        return True

    def submit_joint_states(
        self,
        local_device_id: str,
        joint_states: Mapping[str, Any],
        *,
        boot_id: str,
        sequence: int,
        observed_epoch_s: float,
        topology_digest: str,
        stale_after_s: float = 2.0,
    ) -> bool:
        """持久合并一个机械臂关节状态完整快照。

        参数：设备、关节位置、投影器运行代际与序列、观测时间、拓扑摘要和
        TTL。返回：是否写入待提交表。异常：身份、数值或拓扑非法时抛出
        ``ValueError``。本层不重编序，以免 HTTP 与前端看到不同的帧身份。
        """

        if not self._enabled:
            return False
        local_id = str(local_device_id or "").strip()
        with self._lock:
            material_uuid = self._bindings.get(local_id, "")
        if not material_uuid or not joint_states:
            return False
        normalized = {
            str(name): _finite_number(position)
            for name, position in joint_states.items()
            if str(name).strip()
        }
        if not normalized:
            raise ValueError("joint_states must not be empty")
        normalized_boot_id = str(uuid.UUID(str(boot_id)))
        normalized_sequence = int(sequence)
        if isinstance(sequence, bool) or normalized_sequence < 1:
            raise ValueError("sequence must be a positive integer")
        digest = str(topology_digest or "").strip()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("topology_digest must be lowercase SHA-256")
        ttl = float(stale_after_s)
        if not math.isfinite(ttl) or not 0.5 <= ttl <= 5.0:
            raise ValueError("stale_after_s must be between 0.5 and 5.0")
        payload = {
            "local_device_id": local_id,
            "boot_id": normalized_boot_id,
            "samples": [
                {
                    "sequence": normalized_sequence,
                    "observed_at": _rfc3339(_finite_epoch(observed_epoch_s, time.time())),
                    "stale_after_s": ttl,
                    "topology_digest": digest,
                    "joint_states": normalized,
                }
            ],
        }
        self._store.save_telemetry(
            material_uuid=material_uuid,
            local_device_id=local_id,
            telemetry_type=JOINT_STATE,
            boot_id=normalized_boot_id,
            sequence=normalized_sequence,
            payload=payload,
        )
        return True

    async def drain(self) -> int:
        """提交当前持久 latest，并把成功回执原子转成 WS outbox 事件。

        参数：无。返回：本轮完成数量。异常：单个 HTTP 失败被记录并按间隔重试，
        不阻断其他设备。
        """

        if not self._enabled or time.time() < self._retry_after:
            return 0
        completed = 0
        failed = False
        async with self._drain_lock:
            for telemetry in self._store.pending_telemetry():
                try:
                    response = await asyncio.to_thread(self._commit, telemetry)
                    notification = _notification(telemetry, response)
                    self._store.complete_telemetry(telemetry, notification)
                    completed += 1
                except Exception as error:  # noqa: BLE001 - 持久项必须留待重试
                    failed = True
                    logger.warning(
                        "[EdgeControl] 提交设备遥测投影失败，稍后重试: "
                        f"{telemetry.local_device_id}/{telemetry.telemetry_type}: {error}"
                    )
            self._retry_after = (
                time.time() + self._retry_interval if failed else 0.0
            )
        return completed

    def _commit(self, telemetry: StoredTelemetry) -> Mapping[str, Any]:
        if telemetry.telemetry_type == DEVICE_PROPERTIES:
            return self._data_plane.commit_device_properties(
                telemetry.material_uuid,
                telemetry.payload,
            )
        if telemetry.telemetry_type == JOINT_STATE:
            return self._data_plane.commit_joint_states(
                telemetry.material_uuid,
                telemetry.payload,
            )
        raise ValueError("unsupported telemetry_type in persistent outbox")

    def _next_sequence(self, local_device_id: str, telemetry_type: str) -> int:
        key = (local_device_id, telemetry_type)
        with self._lock:
            sequence = self._sequences.get(key, 0) + 1
            self._sequences[key] = sequence
            return sequence


def _notification(
    telemetry: StoredTelemetry,
    response: Mapping[str, Any],
) -> dict[str, Any]:
    expected_fields = {
        "material_uuid",
        "local_device_id",
        "telemetry_type",
        "boot_id",
        "through_sequence",
        "accepted_ref",
        "created",
    }
    if set(response) != expected_fields:
        raise ValueError("telemetry commit response fields changed")
    expected = {
        "material_uuid": telemetry.material_uuid,
        "local_device_id": telemetry.local_device_id,
        "telemetry_type": telemetry.telemetry_type,
        "boot_id": telemetry.boot_id,
        "through_sequence": telemetry.sequence,
    }
    if any(response.get(key) != value for key, value in expected.items()):
        raise ValueError("telemetry commit response identity changed")
    accepted_ref = str(response.get("accepted_ref") or "")
    if not accepted_ref.startswith("sha256:") or len(accepted_ref) != 71:
        raise ValueError("telemetry commit response accepted_ref is invalid")
    return {**expected, "accepted_ref": accepted_ref}


def _rfc3339(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s, timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _finite_epoch(value: Any, fallback: float) -> float:
    try:
        epoch = float(value)
    except (TypeError, ValueError):
        epoch = float(fallback)
    return epoch if math.isfinite(epoch) and epoch > 0 else float(fallback)


def _finite_number(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("joint positions must be numbers")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("joint positions must be finite")
    return number


__all__ = ["DeviceTelemetryPublisher"]
