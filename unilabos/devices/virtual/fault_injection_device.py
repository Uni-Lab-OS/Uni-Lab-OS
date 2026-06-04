"""
故障注入设备 - 用于异常处理 e2e 联调

通过 @action 触发不同 DeviceException 子类，验证：
- timeout 自动转 TimeoutException
- 自定义异常上抛 → Edge 上报 alarm → 前端弹窗 → 用户决策 → 恢复
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from typing_extensions import TypedDict

from unilabos.devices.exceptions import (
    EmergencyStopError,
    ModbusConnectionError,
    PLCStepTimeout,
    SensorError,
    TipPickupError,
    UserAction,
)
from unilabos.registry.decorators import action, device, not_action
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode


class SimpleResult(TypedDict):
    success: bool
    message: str


@device(
    id="fault_injection_device",
    display_name="故障注入设备",
    category=["virtual_device"],
    description="按 fault_type 主动抛出各类 DeviceException 用于 e2e 测试",
)
class FaultInjectionDevice:
    """故障注入设备，依据传入参数 fault_type 抛出不同异常。"""

    _ros_node: BaseROS2DeviceNode

    def __init__(
        self,
        device_id: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        **kwargs,
    ):
        if device_id is None and "id" in kwargs:
            device_id = kwargs.pop("id")
        if config is None and "config" in kwargs:
            config = kwargs.pop("config")

        self.device_id = device_id or "fault_injection_device"
        self.config = config or {}
        self.logger = logging.getLogger(f"FaultInjectionDevice.{self.device_id}")
        self.data: Dict[str, Any] = {}

        # 用于 use_next_tip 自定义恢复 handler 计数
        self._tip_index = 0

    @not_action
    def post_init(self, ros_node: BaseROS2DeviceNode):
        self._ros_node = ros_node

    # ----------- 自定义恢复 handler -----------

    @not_action
    async def _use_next_tip(self, **kwargs) -> Dict[str, Any]:
        """用户选择"使用下一个 tip"时调用，跳过当前编号继续。"""
        self._tip_index += 1
        self.logger.info(f"[fault_injection] 切换到下一个 tip: index={self._tip_index}")
        return {"success": True, "tip_index": self._tip_index}

    # ----------- @action: 普通正常调用 -----------

    @action(description="正常调用，立即返回成功")
    async def run_ok(self) -> SimpleResult:
        self.logger.info("[fault_injection] run_ok 正常执行")
        await asyncio.sleep(0.2)
        return {"success": True, "message": "ok"}

    # ----------- @action: 超时（timeout 触发） -----------

    @action(description="模拟长耗时操作，触发 timeout", timeout=2.0)
    async def run_long(self, duration: float = 5.0) -> SimpleResult:
        """sleep duration 秒。当 duration > timeout(2s) 时触发 TimeoutException。"""
        self.logger.info(f"[fault_injection] run_long sleep {duration}s")
        await asyncio.sleep(duration)
        return {"success": True, "message": f"slept {duration}s"}

    # ----------- @action: Modbus 连接异常 -----------

    @action(description="抛出 ModbusConnectionError")
    async def raise_modbus_error(self) -> SimpleResult:
        self.logger.warning("[fault_injection] 抛出 ModbusConnectionError")
        raise ModbusConnectionError(
            message="Modbus 端口连接失败 (模拟)",
            device_snapshot={"port": "/dev/ttyUSB0", "baudrate": 9600},
        )

    # ----------- @action: 急停异常（critical） -----------

    @action(description="抛出 EmergencyStopError，critical 不可关闭")
    async def raise_emergency_stop(self) -> SimpleResult:
        self.logger.warning("[fault_injection] 抛出 EmergencyStopError")
        raise EmergencyStopError(
            message="急停按钮已触发 (模拟)",
        )

    # ----------- @action: PLC 步序超时 -----------

    @action(description="抛出 PLCStepTimeout")
    async def raise_plc_step_timeout(self) -> SimpleResult:
        self.logger.warning("[fault_injection] 抛出 PLCStepTimeout")
        raise PLCStepTimeout(
            message="PLC 步序 step_5 长时间未变化 (模拟)",
            device_snapshot={"current_step": 5, "elapsed_seconds": 120},
        )

    # ----------- @action: 传感器异常 -----------

    @action(description="抛出 SensorError")
    async def raise_sensor_error(self) -> SimpleResult:
        self.logger.warning("[fault_injection] 抛出 SensorError")
        raise SensorError(
            message="温度传感器读数异常 (模拟)",
            device_snapshot={"sensor": "temp_1", "value": -999.0},
        )

    # ----------- @action: 取头失败（带自定义 handler） -----------

    @action(description="抛出 TipPickupError，提供 use_next_tip 自定义恢复")
    async def raise_tip_pickup_error(self) -> SimpleResult:
        self.logger.warning("[fault_injection] 抛出 TipPickupError")
        raise TipPickupError(
            message=f"tip {self._tip_index} 取头失败 (模拟)",
            device_snapshot={"tip_index": self._tip_index},
            suggested_actions=[
                UserAction(action="retry", label="重试当前 tip"),
                UserAction(
                    action="use_next_tip",
                    label="使用下一个 tip",
                    handler=self._use_next_tip,
                ),
                UserAction(action="skip", label="跳过此步骤"),
                UserAction(action="abort", label="中止任务"),
            ],
        )

    # =========== 同步版本驱动函数（用于 simple backend 测试） ===========

    # =========== 同步版本驱动函数（注意：同步函数不支持异常处理回环）===========
    #
    # 重要说明：框架的 DeviceException 异常处理机制（上报 alarm → 前端弹窗 → 用户决策）
    # 只对异步函数生效。同步函数抛出异常后只会被记录到日志，不会触发前端弹窗。
    #
    # 如果需要测试异常处理，请使用上面的异步版本（run_ok、raise_modbus_error 等）。
    # 以下同步版本仅用于演示同步函数的写法。

    @action(description="同步版本：正常调用", exception_handling=True)
    def run_ok_sync(self) -> SimpleResult:
        """同步版本的正常调用，不使用 async/await"""
        self.logger.info("[fault_injection] run_ok_sync 正常执行（同步）")
        import time
        time.sleep(0.2)
        return {"success": True, "message": "ok (sync)"}

    @action(description="同步版本：模拟长耗时触发超时", timeout=2.0, exception_handling=True)
    def run_long_sync(self, duration: float = 5.0) -> SimpleResult:
        """同步版本：sleep duration 秒。当 duration > timeout(2s) 时触发 TimeoutException。"""
        self.logger.info(f"[fault_injection] run_long_sync sleep {duration}s")
        import time
        time.sleep(duration)
        return {"success": True, "message": f"slept {duration}s (sync)"}

    @action(description="同步版本：抛出 ModbusConnectionError", exception_handling=True)
    def raise_modbus_error_sync(self) -> SimpleResult:
        """同步版本的 Modbus 异常（注意：不会触发前端弹窗）"""
        self.logger.warning("[fault_injection] 抛出 ModbusConnectionError（同步）")
        raise ModbusConnectionError(
            message="Modbus 端口连接失败 (模拟-同步)",
            device_snapshot={"port": "/dev/ttyUSB0", "baudrate": 9600},
        )

    @action(description="同步版本：抛出 SensorError", exception_handling=True)
    def raise_sensor_error_sync(self) -> SimpleResult:
        """同步版本的传感器异常（注意：不会触发前端弹窗）"""
        self.logger.warning("[fault_injection] 抛出 SensorError（同步）")
        raise SensorError(
            message="温度传感器读数异常 (模拟-同步)",
            device_snapshot={"sensor": "temp_1", "value": -999.0},
        )

    @action(description="同步版本：抛出 TipPickupError", exception_handling=True)
    def raise_tip_pickup_error_sync(self) -> SimpleResult:
        """同步版本的取头失败异常（注意：不会触发前端弹窗）"""
        self.logger.warning("[fault_injection] 抛出 TipPickupError（同步）")
        raise TipPickupError(
            message=f"tip {self._tip_index} 取头失败 (模拟-同步)",
            device_snapshot={"tip_index": self._tip_index},
            suggested_actions=[
                UserAction(action="retry", label="重试当前 tip"),
                UserAction(
                    action="use_next_tip",
                    label="使用下一个 tip",
                    handler=self._use_next_tip,
                ),
                UserAction(action="skip", label="跳过此步骤"),
                UserAction(action="abort", label="中止任务"),
            ],
        )

    @action(description="同步版本：抛出 EmergencyStopError", exception_handling=True)
    def raise_emergency_stop_sync(self) -> SimpleResult:
        """同步版本的急停异常（注意：不会触发前端弹窗）"""
        self.logger.warning("[fault_injection] 抛出 EmergencyStopError（同步）")
        raise EmergencyStopError(
            message="急停按钮已触发 (模拟-同步)",
        )
