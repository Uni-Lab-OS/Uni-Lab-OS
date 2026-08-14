"""ROS JointState 到前端设备实例帧的只读投影器。"""

from __future__ import annotations

import math
import re
import threading
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

_DEVICE_ID = re.compile(r"^[A-Za-z0-9_]+$")
_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class JointStateOwner:
    """Graph Device 对完全限定关节名的冻结归属。"""

    device_id: str
    topology_digest: str
    qualified_joint_names: tuple[str, ...]
    stale_after_s: float = 1.0

    def __post_init__(self) -> None:
        """校验实例身份、拓扑和遥测时效。"""

        if not _DEVICE_ID.fullmatch(self.device_id):
            raise ValueError("JointStateOwner.device_id 必须是可读 Graph node.id")
        if not re.fullmatch(r"[0-9a-f]{64}", self.topology_digest):
            raise ValueError("JointStateOwner.topology_digest 必须是 SHA-256")
        if not self.qualified_joint_names or len(set(self.qualified_joint_names)) != len(
            self.qualified_joint_names
        ):
            raise ValueError("JointStateOwner 必须声明不重复的关节名")
        expected_prefix = f"{self.device_id}_"
        if any(not name.startswith(expected_prefix) for name in self.qualified_joint_names):
            raise ValueError("JointStateOwner 关节名必须以 device_id 完全限定")
        if not math.isfinite(self.stale_after_s) or not 0.5 <= self.stale_after_s <= 5.0:
            raise ValueError("JointStateOwner.stale_after_s 必须位于 [0.5, 5.0]")


class JointStateProjector:
    """以 exact 关节归属将 ROS 总线投影成每设备最新帧。"""

    def __init__(
        self,
        owners: Iterable[JointStateOwner],
        *,
        max_publish_hz: float = 20.0,
        boot_id: str | None = None,
    ) -> None:
        """冻结归属表；任何跨设备重名都在启动前拒绝。"""

        normalized = tuple(owners)
        by_device = {owner.device_id: owner for owner in normalized}
        if len(by_device) != len(normalized):
            raise ValueError("JointStateOwner device_id 不得重复")
        joint_owner: dict[str, str] = {}
        for owner in normalized:
            for joint_name in owner.qualified_joint_names:
                previous = joint_owner.setdefault(joint_name, owner.device_id)
                if previous != owner.device_id:
                    raise ValueError(f"JointState 关节归属冲突: {joint_name}")
        if not math.isfinite(max_publish_hz) or max_publish_hz <= 0:
            raise ValueError("max_publish_hz 必须为正数")
        self._owners = by_device
        self._joint_owner = joint_owner
        self._minimum_interval_s = 1.0 / max_publish_hz
        self._boot_id = boot_id or f"edge-{uuid.uuid4()}"
        self._values: dict[str, dict[str, float]] = {
            device_id: {} for device_id in by_device
        }
        self._observed: dict[str, dict[str, float]] = {
            device_id: {} for device_id in by_device
        }
        self._dirty: set[str] = set()
        self._last_emitted_at: dict[str, float] = {}
        self._sequences: dict[str, int] = {device_id: 0 for device_id in by_device}
        self._lock = threading.RLock()

    @property
    def owners(self) -> tuple[JointStateOwner, ...]:
        """返回按 Device id 排序的冻结归属。"""

        return tuple(self._owners[name] for name in sorted(self._owners))

    def publish_joint_state(
        self,
        names: Sequence[str],
        positions: Sequence[float],
        *,
        observed_at: float | None = None,
    ) -> bool:
        """接收一帧真实关节反馈；未归属关节被忽略而不猜测。"""

        if len(names) != len(positions) or len(set(names)) != len(names):
            return False
        timestamp = time.time() if observed_at is None else float(observed_at)
        if not math.isfinite(timestamp) or timestamp <= 0:
            return False
        accepted = False
        with self._lock:
            for name, raw_position in zip(names, positions, strict=True):
                device_id = self._joint_owner.get(str(name))
                if device_id is None:
                    continue
                position = float(raw_position)
                if not math.isfinite(position):
                    return False
                previous = self._observed[device_id].get(str(name), float("-inf"))
                if timestamp < previous:
                    continue
                self._values[device_id][str(name)] = position
                self._observed[device_id][str(name)] = timestamp
                self._dirty.add(device_id)
                accepted = True
        return accepted

    def drain(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """按每设备最高 20 Hz 产生 latest-value-wins 线上帧。"""

        current = time.time() if now is None else float(now)
        messages: list[dict[str, Any]] = []
        with self._lock:
            for device_id in sorted(self._dirty):
                owner = self._owners[device_id]
                values = self._values[device_id]
                observed = self._observed[device_id]
                if set(values) != set(owner.qualified_joint_names):
                    continue
                oldest = min(observed.values())
                if current - oldest > owner.stale_after_s:
                    continue
                last_emitted = self._last_emitted_at.get(device_id, float("-inf"))
                if current - last_emitted < self._minimum_interval_s:
                    continue
                self._sequences[device_id] += 1
                self._last_emitted_at[device_id] = current
                self._dirty.discard(device_id)
                messages.append(
                    {
                        "type": "push_joint_state",
                        "action": "push_joint_state",
                        "schema_version": _SCHEMA_VERSION,
                        "data": {
                            "device_id": device_id,
                            "topology_digest": owner.topology_digest,
                            "boot_id": self._boot_id,
                            "sequence": self._sequences[device_id],
                            "observed_at": max(observed.values()),
                            "stale_after_s": owner.stale_after_s,
                            "joint_states": {
                                name: values[name]
                                for name in owner.qualified_joint_names
                            },
                        },
                    }
                )
        return messages


_projector = JointStateProjector(())


def configure_joint_state_projection(owners: Iterable[JointStateOwner]) -> None:
    """用本次 Graph 激活的冻结归属替换全局投影器。"""

    global _projector
    _projector = JointStateProjector(tuple(owners))


def publish_joint_state(
    names: Sequence[str],
    positions: Sequence[float],
    *,
    observed_at: float | None = None,
) -> bool:
    """保留原 publish_joint_state 概念的单一本地入口。"""

    return _projector.publish_joint_state(
        names,
        positions,
        observed_at=observed_at,
    )


def drain_joint_state_messages(*, now: float | None = None) -> list[dict[str, Any]]:
    """返回当前可广播帧；不落库、不排队。"""

    return _projector.drain(now=now)


__all__ = [
    "JointStateOwner",
    "JointStateProjector",
    "configure_joint_state_projection",
    "drain_joint_state_messages",
    "publish_joint_state",
]
