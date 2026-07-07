"""框架级异常定义

包含:
1. DeviceClassInvalid: 设备类校验失败异常 (registry / initialize_device 使用)
2. DeviceException 及子类: 设备驱动运行时异常，配合 @action(exception_handling=True)
   触发前端弹窗 + 用户决策链路

设计原则:
1. 所有设备异常继承自 DeviceException
2. 子类通过类属性声明 category/severity (无需 __init__ 设置)
3. suggested_actions 通过 _default_actions() 提供,可被实例覆盖
4. device_snapshot 在抛出点采集,框架层不负责
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, List, Optional
import traceback


class DeviceClassInvalid(Exception):
    """设备类校验失败: registry 加载或 initialize_device 时使用"""
    pass


class DeviceExceptionCategory(str, Enum):
    NETWORK = "network"          # 通信类: Modbus/OPC-UA/串口
    HARDWARE = "hardware"        # 硬件类: 急停/传感器/电机
    TIMEOUT = "timeout"          # 超时类: 步序卡死/动作超时
    PARAMETER = "parameter"      # 参数类: 用户输入/范围越界
    RESOURCE = "resource"        # 资源类: 位置占用/耗材不足
    UNKNOWN = "unknown"


class DeviceExceptionSeverity(str, Enum):
    WARNING = "warning"          # 可继续,需注意
    ERROR = "error"              # 当前任务失败,需处理
    CRITICAL = "critical"        # 影响设备安全,需立即处理


@dataclass
class UserAction:
    """前端给用户展示的可选操作

    通用 action(retry/skip/abort/manual_fix)由框架直接处理。
    设备特定 action(如 use_next_tip)需提供 handler 回调,框架调用 handler
    后再根据 then 字段决定后续行为(默认 retry)。
    """
    action: str                  # 通用: retry | skip | abort | manual_fix
                                 # 自定义: 任意字符串,如 use_next_tip
    label: str                   # 按钮文案,如"使用下一个枪头"
    description: str = ""        # hover 提示
    # 驱动层提供的恢复回调,框架在 retry 前调用。仅在 Edge 侧使用,不序列化
    handler: Optional[Callable[..., Awaitable[Any]]] = field(
        default=None, repr=False, compare=False
    )
    # handler 执行后的下一步: retry(重新执行原函数) / skip(跳过) / continue(直接返回 handler 结果)
    then: str = "retry"


class DeviceException(Exception):
    """设备异常基类

    驱动开发者通过继承此类定义设备特有异常。
    框架层只需要 catch DeviceException 即可统一处理。
    """
    # 子类通过类属性覆盖
    category: DeviceExceptionCategory = DeviceExceptionCategory.UNKNOWN
    severity: DeviceExceptionSeverity = DeviceExceptionSeverity.ERROR

    def __init__(
        self,
        message: str,
        suggested_actions: Optional[List[UserAction]] = None,
        device_snapshot: Optional[dict] = None,
        cause: Optional[BaseException] = None,
    ):
        super().__init__(message)
        self.message = message
        self.suggested_actions = suggested_actions or self._default_actions()
        self.device_snapshot = device_snapshot or {}
        self.cause = cause
        self.traceback_str = traceback.format_exc()

    def _default_actions(self) -> List[UserAction]:
        return [
            UserAction("retry", "重试", "重新执行当前操作"),
            UserAction("skip", "跳过", "跳过当前操作继续执行"),
            UserAction("abort", "终止任务", "停止当前任务"),
        ]

    def to_alarm_dict(
        self, device_id: str, device_uuid: str,
        action_name: str, task_id: str, job_id: str,
    ) -> dict:
        return {
            "device_id": device_id,
            "device_uuid": device_uuid,
            "action_name": action_name,
            "task_id": task_id,
            "job_id": job_id,
            "exception_type": type(self).__name__,
            "category": self.category.value,
            "severity": self.severity.value,
            "error_message": self.message,
            "suggested_actions": [
                {"action": a.action, "label": a.label, "description": a.description}
                for a in self.suggested_actions
            ],
            "device_snapshot": self.device_snapshot,
            "traceback": self.traceback_str,
            "require_confirmation": True,
        }


# ==================== 通用异常 ====================

class TimeoutException(DeviceException):
    """通用超时异常,由装饰器从 asyncio.TimeoutError 转换而来"""
    category = DeviceExceptionCategory.TIMEOUT
    severity = DeviceExceptionSeverity.ERROR

    def _default_actions(self):
        return [
            UserAction("retry", "重试", "延长超时时间再次尝试"),
            UserAction("skip", "跳过", "跳过当前操作继续执行"),
            UserAction("manual_fix", "手动干预", "现场检查后继续"),
            UserAction("abort", "终止任务", "停止当前任务"),
        ]


class ParameterError(DeviceException):
    category = DeviceExceptionCategory.PARAMETER
    severity = DeviceExceptionSeverity.WARNING

    def _default_actions(self):
        return [
            UserAction("abort", "终止任务", "参数错误需修改后重新提交"),
        ]


# ==================== 通信类异常 ====================

class ModbusConnectionError(DeviceException):
    category = DeviceExceptionCategory.NETWORK
    severity = DeviceExceptionSeverity.ERROR

    def _default_actions(self):
        return [
            UserAction("retry", "重试连接", "重新建立 Modbus 连接"),
            UserAction("skip", "跳过", "跳过当前操作继续执行"),
            UserAction("manual_fix", "手动检查", "检查网络和电源后继续"),
            UserAction("abort", "终止任务", "停止当前任务"),
        ]


class OPCUAConnectionError(DeviceException):
    category = DeviceExceptionCategory.NETWORK
    severity = DeviceExceptionSeverity.ERROR


# ==================== 硬件类异常 ====================

class EmergencyStopError(DeviceException):
    """急停异常 - 默认 critical,弹窗不自动关闭"""
    category = DeviceExceptionCategory.HARDWARE
    severity = DeviceExceptionSeverity.CRITICAL

    def _default_actions(self):
        return [
            UserAction("manual_fix", "解除急停", "现场解除急停按钮后继续"),
            UserAction("abort", "终止任务", "停止任务"),
        ]


class PLCStepTimeout(DeviceException):
    """PLC 步序超时 - 步序号卡住"""
    category = DeviceExceptionCategory.TIMEOUT
    severity = DeviceExceptionSeverity.ERROR

    def __init__(self, message: str, current_step: int = -1,
                 expected_step: int = -1, **kwargs):
        super().__init__(message, **kwargs)
        self.current_step = current_step
        self.expected_step = expected_step

    def to_alarm_dict(self, **kwargs) -> dict:
        d = super().to_alarm_dict(**kwargs)
        d["current_step"] = self.current_step
        d["expected_step"] = self.expected_step
        return d


class SensorError(DeviceException):
    """传感器异常: 液位/温度/压力等读数异常"""
    category = DeviceExceptionCategory.HARDWARE
    severity = DeviceExceptionSeverity.ERROR


# ==================== 资源类异常 ====================

class ResourceConflictError(DeviceException):
    """资源冲突: 位置占用 / 耗材不足"""
    category = DeviceExceptionCategory.RESOURCE
    severity = DeviceExceptionSeverity.WARNING

    def _default_actions(self):
        return [
            UserAction("retry", "等待重试", "等待资源释放后重试"),
            UserAction("skip", "跳过该步", "跳过当前动作继续下一步"),
            UserAction("abort", "终止任务", "停止任务"),
        ]


class TipPickupError(DeviceException):
    """枪头拾取失败"""
    category = DeviceExceptionCategory.HARDWARE
    severity = DeviceExceptionSeverity.ERROR

    def __init__(self, message: str, tip_position: str = "",
                 remaining_tips: int = 0, **kwargs):
        super().__init__(message, **kwargs)
        self.tip_position = tip_position
        self.remaining_tips = remaining_tips

    def to_alarm_dict(self, **kwargs) -> dict:
        d = super().to_alarm_dict(**kwargs)
        d["tip_position"] = self.tip_position
        d["remaining_tips"] = self.remaining_tips
        return d
