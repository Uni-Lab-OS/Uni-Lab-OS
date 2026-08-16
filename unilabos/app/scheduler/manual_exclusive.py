"""手动独占（Exclusive）的进程内调度准入深模块。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass


class ManualExclusiveBusyError(RuntimeError):
    """设备已有作业占用，不能进入手动独占（Exclusive）。"""


@dataclass(frozen=True)
class ManualExclusiveSnapshot:
    """一个设备当前的手动独占（Exclusive）准入快照。"""

    local_device_id: str
    state: str

    @property
    def exclusive(self) -> bool:
        """返回当前快照是否处于手动独占（Exclusive）。

        参数：无。返回：``state`` 为 ``exclusive`` 时返回 ``True``。异常：无。
        """

        return self.state == "exclusive"


class ManualExclusiveGate:
    """在调度器重排锁内原子裁决设备的手动独占（Exclusive）。

    本模块只保存当前进程 epoch 的设备键集合。它不创建 owner、租约、TTL、
    栅栏（Fence）或动作完成事实，也不提供跨重启恢复。
    """

    def __init__(
        self,
        *,
        lock: threading.RLock,
        runtime_busy_keys: Callable[[], set[str]],
        reschedule_locked: Callable[[], object],
    ) -> None:
        """装配与调度器共享原子区的手动独占（Exclusive）门禁。

        参数：``lock`` 是调度重排锁；``runtime_busy_keys`` 读取不含手动独占的
        当前作业占用键；``reschedule_locked`` 在释放后推进等待作业。返回：无。
        异常：依赖回调错误原样传播，防止准入状态与调度事实静默分叉。
        """

        self._lock = lock
        self._runtime_busy_keys = runtime_busy_keys
        self._reschedule_locked = reschedule_locked
        # 设备级准入身份使用与调度器相同的 ``/devices/{local_device_id}`` 键。
        self._exclusive_device_keys: set[str] = set()

    def snapshot(self, local_device_id: str) -> ManualExclusiveSnapshot:
        """读取一个设备的 ``idle/busy/exclusive`` 当前状态。

        参数：``local_device_id`` 是 Edge 注册的本地设备身份。返回：不可变状态
        快照。异常：空白、含斜杠或过长身份引发 ``ValueError``。
        """

        identity, device_key = _device_identity(local_device_id)
        with self._lock:
            return self._snapshot_locked(identity, device_key)

    def acquire(self, local_device_id: str) -> ManualExclusiveSnapshot:
        """在设备空闲时幂等取得手动独占（Exclusive）。

        参数：``local_device_id`` 是本地设备身份。返回：``exclusive`` 快照。
        异常：设备已有作业占用时抛出 ``ManualExclusiveBusyError``；身份非法时
        抛出 ``ValueError``。检查与置位共用调度锁，不存在先查后派发竞态。
        """

        identity, device_key = _device_identity(local_device_id)
        with self._lock:
            if device_key in self._exclusive_device_keys:
                return ManualExclusiveSnapshot(identity, "exclusive")
            if device_key in self._runtime_busy_keys():
                raise ManualExclusiveBusyError(f"device {identity} is busy")
            self._exclusive_device_keys.add(device_key)
            return ManualExclusiveSnapshot(identity, "exclusive")

    def release(self, local_device_id: str) -> ManualExclusiveSnapshot:
        """幂等释放手动独占（Exclusive）并立即重排等待作业。

        参数：``local_device_id`` 是本地设备身份。返回：重排后的设备状态，可能
        因等待作业立即派发而为 ``busy``。异常：身份非法或重排失败时原样抛出；
        释放本身已在同一原子区完成，不恢复已删除的非持久状态。
        """

        identity, device_key = _device_identity(local_device_id)
        with self._lock:
            existed = device_key in self._exclusive_device_keys
            self._exclusive_device_keys.discard(device_key)
            if existed:
                self._reschedule_locked()
            return self._snapshot_locked(identity, device_key)

    def busy_keys(self) -> set[str]:
        """返回手动独占（Exclusive）对调度器暴露的设备忙碌键副本。

        参数：无。返回：不会与内部集合共享的设备键集合。异常：无。
        """

        with self._lock:
            return set(self._exclusive_device_keys)

    def _snapshot_locked(
        self,
        identity: str,
        device_key: str,
    ) -> ManualExclusiveSnapshot:
        """在已持有调度锁时组合一个设备状态快照。

        参数：规范本地设备身份及其调度键。返回：独占优先、其次作业忙碌、最后
        空闲的状态。异常：忙碌提供者错误原样传播，避免误报空闲。
        """

        if device_key in self._exclusive_device_keys:
            state = "exclusive"
        elif device_key in self._runtime_busy_keys():
            state = "busy"
        else:
            state = "idle"
        return ManualExclusiveSnapshot(identity, state)


def _device_identity(local_device_id: str) -> tuple[str, str]:
    """校验本地设备身份并生成设备级调度键。

    参数：``local_device_id`` 是候选身份。返回：规范身份与
    ``/devices/{identity}`` 键。异常：非法身份抛出 ``ValueError``。
    """

    identity = str(local_device_id or "").strip()
    if not identity or len(identity) > 200 or "/" in identity:
        raise ValueError("local_device_id is invalid")
    return identity, f"/devices/{identity}"


__all__ = [
    "ManualExclusiveBusyError",
    "ManualExclusiveGate",
    "ManualExclusiveSnapshot",
]
