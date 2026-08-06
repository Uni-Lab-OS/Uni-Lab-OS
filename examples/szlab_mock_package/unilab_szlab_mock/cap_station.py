"""无 PLC/OPC UA 依赖的 SZLab S08 形态 mock 开关盖工位。"""

from __future__ import annotations

import time
from threading import Lock
from typing import Literal, TypedDict

from unilabos.registry.decorators import action, device


class PingResult(TypedDict):
    """连通性检查结果。"""

    success: bool
    message: str
    station_name: str
    cycle_count: int


class CapProcessResult(TypedDict):
    """一次模拟开关盖工艺结果。"""

    success: bool
    message: str
    operation: str
    sample_id: int
    occupied_slots: int
    cycle_count: int


class StationStatusResult(TypedDict):
    """工位当前可观察状态。"""

    connected: bool
    station_name: str
    occupied_slots: int
    slot_count: int
    cycle_count: int


@device(
    id="mock_s08_cap_station",
    displayname="Mock S08 开关盖工位",
    category=["workstation", "szlab", "mock"],
    description=(
        "用于验证设备广场、心愿单、设备包下载、设备图写入与 Action 执行的"
        "纯内存开关盖工位"
    ),
    version="0.1.0",
    metadata={
        "hardware_required": False,
        "reference_device": "SZLab S08 cap station",
        "validation_scope": "cloud-electron-os",
    },
)
class MockS08CapStation:
    """使用内存状态模拟 S08 开关盖工位，不建立任何外部连接。"""

    def __init__(
        self,
        station_name: str = "Mock S08 Cap Station",
        auto_connect: bool = True,
        cycle_delay_ms: int = 20,
        cap_slot_count: int = 5,
        initial_occupied_slots: int = 0,
        channel_map: dict[str, str] | None = None,
    ) -> None:
        """初始化模拟工位。

        参数 ``station_name`` 是本地显示名称，``auto_connect`` 决定启动后是否可执行
        工艺，``cycle_delay_ms`` 是单次动作的模拟延迟，``cap_slot_count`` 是瓶盖暂存位
        总数，``initial_occupied_slots`` 是初始占用数，``channel_map`` 仅用于验证 object
        配置字段。构造函数不访问网络、串口、PLC 或文件系统；参数越界时直接拒绝启动。
        """

        if not station_name.strip():
            raise ValueError("station_name 不能为空")
        if not 0 <= cycle_delay_ms <= 5_000:
            raise ValueError("cycle_delay_ms 必须在 0-5000 范围内")
        if cap_slot_count <= 0:
            raise ValueError("cap_slot_count 必须大于 0")
        if not 0 <= initial_occupied_slots <= cap_slot_count:
            raise ValueError("initial_occupied_slots 必须在 0-cap_slot_count 范围内")

        self.station_name = station_name.strip()
        self.cycle_delay_ms = cycle_delay_ms
        self.cap_slot_count = cap_slot_count
        self.channel_map = dict(channel_map or {})
        self._connected = auto_connect
        self._occupied_slots = initial_occupied_slots
        self._cycle_count = 0
        # Action 可能由不同执行线程进入，所有模拟工位状态在同一把锁内更新。
        self._state_lock = Lock()

    @property
    def connected(self) -> bool:
        """返回当前模拟连接状态；读取无外部副作用。"""

        return self._connected

    @property
    def cycle_count(self) -> int:
        """返回成功执行的模拟工艺次数；读取无外部副作用。"""

        return self._cycle_count

    @action(description="检查 mock 工位是否可用", displayname="Mock 连通性检查")
    def ping(self, message: str = "hello") -> PingResult:
        """返回调用消息与当前工位状态。

        参数 ``message`` 是调用方用于关联验证的文本。返回稳定的连通性结果；本 Action
        不改变瓶盖暂存状态，也不会访问真实硬件。
        """

        with self._state_lock:
            return {
                "success": self._connected,
                "message": message,
                "station_name": self.station_name,
                "cycle_count": self._cycle_count,
            }

    @action(description="在内存中模拟一次 S08 开盖或关盖", displayname="Mock 开关盖")
    def process_cap(
        self,
        operation: Literal["open", "close"] = "open",
        sample_id: int = 1,
    ) -> CapProcessResult:
        """执行一次可重复的开关盖状态转换。

        参数 ``operation`` 只能为 ``open`` 或 ``close``，``sample_id`` 是正整数样品
        标识。返回工艺结果和最新计数；断开、暂存位满或无盖可取时返回失败结果，不连接
        OPC UA/PLC，也不产生真实设备动作。
        """

        if sample_id <= 0:
            raise ValueError("sample_id 必须大于 0")
        if operation not in {"open", "close"}:
            raise ValueError("operation 必须是 open 或 close")

        with self._state_lock:
            if not self._connected:
                return self._cap_result(
                    success=False,
                    message="mock 工位未连接",
                    operation=operation,
                    sample_id=sample_id,
                )
            if operation == "open" and self._occupied_slots >= self.cap_slot_count:
                return self._cap_result(
                    success=False,
                    message="mock 瓶盖暂存位已满",
                    operation=operation,
                    sample_id=sample_id,
                )
            if operation == "close" and self._occupied_slots <= 0:
                return self._cap_result(
                    success=False,
                    message="mock 瓶盖暂存位为空",
                    operation=operation,
                    sample_id=sample_id,
                )

            if self.cycle_delay_ms:
                time.sleep(self.cycle_delay_ms / 1_000)
            self._occupied_slots += 1 if operation == "open" else -1
            self._cycle_count += 1
            return self._cap_result(
                success=True,
                message=f"mock {operation} 完成",
                operation=operation,
                sample_id=sample_id,
            )

    @action(description="读取 mock 工位状态", displayname="读取 Mock 状态")
    def read_status(self) -> StationStatusResult:
        """返回工位的纯内存状态快照；无参数且不改变运行状态。"""

        with self._state_lock:
            return {
                "connected": self._connected,
                "station_name": self.station_name,
                "occupied_slots": self._occupied_slots,
                "slot_count": self.cap_slot_count,
                "cycle_count": self._cycle_count,
            }

    @action(description="重置 mock 工位计数与连接状态", displayname="重置 Mock 工位")
    def reset(self, reconnect: bool = True) -> StationStatusResult:
        """清空模拟暂存位和工艺计数。

        参数 ``reconnect`` 决定重置后是否恢复为已连接。返回重置后的状态；只修改当前
        进程内存，不会操作设备图、云端数据或真实硬件。
        """

        with self._state_lock:
            self._connected = reconnect
            self._occupied_slots = 0
            self._cycle_count = 0
            return {
                "connected": self._connected,
                "station_name": self.station_name,
                "occupied_slots": self._occupied_slots,
                "slot_count": self.cap_slot_count,
                "cycle_count": self._cycle_count,
            }

    def _cap_result(
        self,
        *,
        success: bool,
        message: str,
        operation: str,
        sample_id: int,
    ) -> CapProcessResult:
        """组装锁内开关盖结果。

        参数描述本次结果及请求身份。返回包含当前占用数和周期数的稳定对象；调用方必须
        已持有 ``_state_lock``，该辅助方法本身不修改状态。
        """

        return {
            "success": success,
            "message": message,
            "operation": operation,
            "sample_id": sample_id,
            "occupied_slots": self._occupied_slots,
            "cycle_count": self._cycle_count,
        }
