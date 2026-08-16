"""把 ROS ``JointState`` 精确投影成逐设备完整关节帧。"""

from __future__ import annotations

import math
import re
import threading
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

_DEVICE_ID = re.compile(r"^[A-Za-z0-9_]+$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class JointStateOwner:
    """物理图设备（GraphDevice）对完全限定关节名的冻结归属。"""

    device_id: str
    topology_digest: str
    qualified_joint_names: tuple[str, ...]
    stale_after_s: float = 1.0

    def __post_init__(self) -> None:
        """校验设备身份、拓扑身份和遥测时效合同。"""

        if _DEVICE_ID.fullmatch(self.device_id) is None:
            raise ValueError("JointStateOwner.device_id 必须是可读物理图节点 id")
        if _DIGEST.fullmatch(self.topology_digest) is None:
            raise ValueError("JointStateOwner.topology_digest 必须是 SHA-256")
        if not self.qualified_joint_names or len(set(self.qualified_joint_names)) != len(
            self.qualified_joint_names
        ):
            raise ValueError("JointStateOwner 必须声明不重复的关节名")
        prefix = f"{self.device_id}_"
        if any(not name.startswith(prefix) for name in self.qualified_joint_names):
            raise ValueError("JointStateOwner 关节名必须以 device_id 完全限定")
        if not math.isfinite(self.stale_after_s) or not 0.5 <= self.stale_after_s <= 5.0:
            raise ValueError("JointStateOwner.stale_after_s 必须位于 [0.5, 5.0]")


@dataclass(frozen=True, slots=True)
class ProjectedJointState:
    """关节状态投影器（JointStateProjector）输出的不可变完整设备帧。"""

    device_id: str
    topology_digest: str
    boot_id: str
    sequence: int
    observed_epoch_s: float
    stale_after_s: float
    joint_states: Mapping[str, float]


class JointStateProjector:
    """以 exact 归属把 ROS 总线收敛为逐设备、限频的最新完整帧。"""

    def __init__(
        self,
        owners: Iterable[JointStateOwner],
        *,
        max_publish_hz: float = 20.0,
        boot_id: str | None = None,
    ) -> None:
        """冻结归属表；重复设备或跨设备关节冲突在启动前拒绝。"""

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
        self._boot_id = str(uuid.UUID(boot_id)) if boot_id else str(uuid.uuid4())
        self._values: dict[str, dict[str, float]] = {
            device_id: {} for device_id in by_device
        }
        self._observed: dict[str, dict[str, float]] = {
            device_id: {} for device_id in by_device
        }
        self._dirty: set[str] = set()
        self._last_emitted_at: dict[str, float] = {}
        self._sequences = {device_id: 0 for device_id in by_device}
        self._lock = threading.RLock()

    @property
    def owners(self) -> tuple[JointStateOwner, ...]:
        """返回按设备 id 排序的冻结归属。"""

        return tuple(self._owners[name] for name in sorted(self._owners))

    def ingest(
        self,
        names: Sequence[str],
        positions: Sequence[float],
        *,
        observed_epoch_s: float | None = None,
    ) -> bool:
        """接收真实关节反馈；未归属关节被忽略，不按前缀或单设备猜测。"""

        if len(names) != len(positions) or len(set(names)) != len(names):
            return False
        timestamp = time.time() if observed_epoch_s is None else float(observed_epoch_s)
        if not math.isfinite(timestamp) or timestamp <= 0:
            return False
        accepted = False
        with self._lock:
            for raw_name, raw_position in zip(names, positions, strict=True):
                name = str(raw_name)
                device_id = self._joint_owner.get(name)
                if device_id is None:
                    continue
                try:
                    position = float(raw_position)
                except (TypeError, ValueError):
                    return False
                if not math.isfinite(position):
                    return False
                if timestamp < self._observed[device_id].get(name, float("-inf")):
                    continue
                self._values[device_id][name] = position
                self._observed[device_id][name] = timestamp
                self._dirty.add(device_id)
                accepted = True
        return accepted

    def drain(self, *, now_epoch_s: float | None = None) -> tuple[ProjectedJointState, ...]:
        """按每设备最高发布频率产生 latest-value-wins 完整帧。"""

        current = time.time() if now_epoch_s is None else float(now_epoch_s)
        if not math.isfinite(current) or current <= 0:
            raise ValueError("now_epoch_s 必须是正有限时间")
        frames: list[ProjectedJointState] = []
        with self._lock:
            for device_id in sorted(self._dirty):
                owner = self._owners[device_id]
                values = self._values[device_id]
                observed = self._observed[device_id]
                if set(values) != set(owner.qualified_joint_names):
                    continue
                if current - min(observed.values()) > owner.stale_after_s:
                    continue
                if current - self._last_emitted_at.get(
                    device_id, float("-inf")
                ) < self._minimum_interval_s:
                    continue
                self._sequences[device_id] += 1
                self._last_emitted_at[device_id] = current
                self._dirty.discard(device_id)
                frames.append(
                    ProjectedJointState(
                        device_id=device_id,
                        topology_digest=owner.topology_digest,
                        boot_id=self._boot_id,
                        sequence=self._sequences[device_id],
                        observed_epoch_s=max(observed.values()),
                        stale_after_s=owner.stale_after_s,
                        joint_states={
                            name: values[name]
                            for name in owner.qualified_joint_names
                        },
                    )
                )
        return tuple(frames)


__all__ = ["JointStateOwner", "JointStateProjector", "ProjectedJointState"]
