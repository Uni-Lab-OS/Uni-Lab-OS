"""把设备包提供的机械臂调试端口暴露为 OS 本地维护 API。"""

from __future__ import annotations

import math
import threading
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field


class OpenCommissioningSessionRequest(BaseModel):
    """打开独占调试会话所需的操作员身份与可信部署模式。"""

    owner_id: str = Field(min_length=1, max_length=128)
    requested_deployment_mode: Literal["simulation", "maintenance"]


class CommissioningCommandRequest(BaseModel):
    """网页使用的最小调试命令；安全身份由 OS 会话注入。"""

    schema_version: Literal[2] = 2
    command_id: str = Field(min_length=1, max_length=128)
    type: Literal[
        "move_target",
        "move_pose",
        "tcp_jog",
        "joint_jog",
        "controlled_stop",
    ]
    motion_profile_ref: str | None = None
    velocity_scale: float | None = None
    acceleration_scale: float | None = None
    target_ref: str | None = None
    pose_input: dict[str, Any] | None = None
    frame_ref: str | None = None
    axis: str | None = None
    direction: str | None = None
    step_si: float | None = None
    joint_ref: str | None = None
    target_command_id: str | None = None
    reason: str | None = None


@dataclass
class _SessionEntry:
    """OS 持有的单个短生命周期维护会话。"""

    device_id: str
    session_id: str
    source_boot_id: str
    session: Any
    sequence: int = 0
    command_sequences: dict[str, int] | None = None

    def next_sequence(self, command_id: str) -> int:
        """为新命令分配单调序号，重试同一命令保持原序号。"""

        if self.command_sequences is None:
            self.command_sequences = {}
        existing = self.command_sequences.get(command_id)
        if existing is not None:
            return existing
        self.sequence += 1
        self.command_sequences[command_id] = self.sequence
        return self.sequence


class RobotCommissioningService:
    """按 Graph ``device_id`` 管理运行时绑定和独占维护会话。"""

    def __init__(self) -> None:
        """创建空注册表；没有设备包注册时 API 返回空目录。"""

        self._bindings: dict[str, Any] = {}
        self._sessions: dict[str, _SessionEntry] = {}
        self._session_by_device: dict[str, str] = {}
        self._source_boot_id = f"robot-commissioning:{uuid.uuid4()}"
        self._lock = threading.RLock()

    def register(self, device_id: str, binding: Any) -> None:
        """注册设备包创建的正式 ``RuntimeBinding``。"""

        normalized = str(device_id).strip()
        if not normalized or not callable(
            getattr(binding, "open_maintenance_session", None)
        ):
            raise ValueError("机械臂调试注册必须包含 device_id 与 RuntimeBinding")
        port = getattr(binding, "commissioning_port", None)
        if port is None:
            raise ValueError("RuntimeBinding 未提供 RobotCommissioningPort")
        _required_context(port)
        with self._lock:
            if normalized in self._session_by_device:
                raise RuntimeError("设备仍有活动维护会话，禁止替换调试运行时")
            previous = self._bindings.get(normalized)
            if previous is not None and previous is not binding:
                raise RuntimeError(f"机械臂调试设备重复注册: {normalized}")
            self._bindings[normalized] = binding

    def unregister(self, device_id: str) -> None:
        """仅在没有活动会话时移除设备运行时。"""

        normalized = str(device_id).strip()
        with self._lock:
            if normalized in self._session_by_device:
                raise RuntimeError("设备仍有活动维护会话，禁止注销")
            binding = self._bindings.pop(normalized, None)
        if binding is not None:
            binding.close()

    def list_devices(self) -> list[dict[str, Any]]:
        """返回真实注册设备及 Adapter 声明的调试能力。"""

        with self._lock:
            items = [
                self._device_context(device_id, binding)
                for device_id, binding in sorted(self._bindings.items())
            ]
        return items

    def open_session(
        self,
        device_id: str,
        owner_id: str,
        requested_deployment_mode: str,
    ) -> dict[str, Any]:
        """取得设备端点独占权并返回网页调用上下文。"""

        normalized = str(device_id).strip()
        with self._lock:
            binding = self._bindings.get(normalized)
            if binding is None:
                raise KeyError(f"机械臂调试设备不存在: {normalized}")
            actual_mode = _deployment_mode(binding)
            requested_mode = str(requested_deployment_mode).strip()
            if requested_mode != actual_mode:
                raise RuntimeError(
                    "机械臂部署模式不匹配: "
                    f"请求 {requested_mode}，当前为 {actual_mode}"
                )
            if normalized in self._session_by_device:
                raise RuntimeError("机械臂已被另一个维护会话占用")
            session = binding.open_maintenance_session(str(owner_id).strip())
            session_id = str(uuid.uuid4())
            entry = _SessionEntry(
                normalized,
                session_id,
                self._source_boot_id,
                session,
            )
            self._sessions[session_id] = entry
            self._session_by_device[normalized] = session_id
            return self._session_payload(entry, binding)

    def session_context(self, device_id: str, session_id: str) -> dict[str, Any]:
        """返回活动会话上下文，不读取或改变机械臂状态。"""

        with self._lock:
            entry, binding = self._entry(device_id, session_id)
            return self._session_payload(entry, binding)

    def snapshot(self, device_id: str, session_id: str) -> dict[str, Any]:
        """读取当前关节/TCP 及在线、空闲、Fence 状态。"""

        with self._lock:
            entry, _binding = self._entry(device_id, session_id)
            session = entry.session
        return _json_value(session.snapshot())

    def execute(
        self,
        device_id: str,
        session_id: str,
        request: CommissioningCommandRequest,
    ) -> dict[str, Any]:
        """把网页最小命令补全为统一合同并同步等待明确结果。"""

        with self._lock:
            entry, binding = self._entry(device_id, session_id)
            sequence = entry.next_sequence(request.command_id)
            command = _build_command(
                request,
                source_boot_id=entry.source_boot_id,
                monotonic_sequence=sequence,
                binding=binding,
                session=entry.session,
            )
            session = entry.session
        result = session.execute(command)
        return {
            "command": command.canonical_payload(),
            "result": _json_value(result),
        }

    def close_session(self, device_id: str, session_id: str) -> None:
        """在没有未知执行 Fence 时释放维护端点。"""

        with self._lock:
            entry, _binding = self._entry(device_id, session_id)
            entry.session.close()
            self._sessions.pop(entry.session_id, None)
            self._session_by_device.pop(entry.device_id, None)

    def _entry(self, device_id: str, session_id: str) -> tuple[_SessionEntry, Any]:
        """校验会话存在且属于 URL 指定的 Device。"""

        entry = self._sessions.get(str(session_id))
        if entry is None or entry.device_id != str(device_id):
            raise KeyError("机械臂维护会话不存在或不属于该设备")
        binding = self._bindings.get(entry.device_id)
        if binding is None:
            raise RuntimeError("机械臂调试运行时已离线")
        return entry, binding

    def _device_context(self, device_id: str, binding: Any) -> dict[str, Any]:
        """从 Adapter 读取当前部署上下文和能力，不由 OS 猜测。"""

        port = binding.commissioning_port
        hardware_digest, tool_digest = _required_context(port)
        commissioning_limits = _required_commissioning_limits(port)
        return {
            "device_id": device_id,
            "schema_version": 2,
            "deployment_mode": _deployment_mode(binding),
            "capabilities": _json_value(port.commissioning_capabilities),
            "target_revision": port.commissioning_target_revision,
            "motion_profile_ref": (
                str(getattr(port, "commissioning_motion_profile_ref", "")).strip()
                or None
            ),
            "hardware_profile_digest": hardware_digest,
            "tool_context_digest": tool_digest,
            "commissioning_limits": commissioning_limits,
            "interaction_modes": {
                "step": True,
                "continuous_hold": False,
            },
            "session_active": device_id in self._session_by_device,
        }

    def _session_payload(self, entry: _SessionEntry, binding: Any) -> dict[str, Any]:
        """生成网页构造请求和显示状态所需的完整会话上下文。"""

        return {
            **self._device_context(entry.device_id, binding),
            "session_id": entry.session_id,
            "owner_id": entry.session.owner_id,
            "source_boot_id": entry.source_boot_id,
            "next_monotonic_sequence": entry.sequence + 1,
        }


def _required_context(port: Any) -> tuple[str, str]:
    """要求 Adapter 明确给出活动 HardwareProfile 与 ToolContext 摘要。"""

    hardware = str(getattr(port, "hardware_profile_digest", "")).strip()
    tool = str(getattr(port, "tool_context_digest", "")).strip()
    if not hardware or not tool:
        raise ValueError("RobotCommissioningPort 缺少活动部署或工具摘要")
    return hardware, tool


def _required_commissioning_limits(port: Any) -> dict[str, float]:
    """投影活动 HardwareProfile 的维护限速，缺失或非法时失败关闭。"""

    limits: dict[str, float] = {}
    for wire_name, attribute_name in (
        ("velocity_scale_max", "commissioning_velocity_limit"),
        ("acceleration_scale_max", "commissioning_acceleration_limit"),
    ):
        raw_value = getattr(port, attribute_name, None)
        if isinstance(raw_value, bool):
            raise ValueError(f"RobotCommissioningPort.{attribute_name} 必须为有限数")
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"RobotCommissioningPort.{attribute_name} 必须为有限数"
            ) from error
        if not math.isfinite(value) or not 0.0 < value <= 0.30:
            raise ValueError(
                f"RobotCommissioningPort.{attribute_name} 必须位于 (0, 0.30]"
            )
        limits[wire_name] = value
    return limits


def _deployment_mode(binding: Any) -> str:
    """读取 RuntimeBinding 已验证的部署模式，不从前端或后端类型推断。"""

    mode = getattr(binding, "deployment_mode", None)
    normalized = str(getattr(mode, "value", mode) or "").strip()
    if normalized not in {"simulation", "maintenance"}:
        raise ValueError("Robot RuntimeBinding 缺少 simulation/maintenance 部署模式")
    return normalized


def _build_command(
    request: CommissioningCommandRequest,
    *,
    source_boot_id: str,
    monotonic_sequence: int,
    binding: Any,
    session: Any,
) -> Any:
    """把 REST 请求转换为共享 contracts 中的封闭联合类型。"""

    from unilab_robot_contracts import (
        AngleUnit,
        CommissioningMotionKind,
        CommissioningPoseInput,
        ControlledStopCommand,
        EulerRotationOrder,
        JointJogCommand,
        MotionDirection,
        MovePoseCommand,
        MoveTargetCommand,
        TcpAxis,
        TcpJogCommand,
    )

    port = binding.commissioning_port
    hardware_digest, tool_digest = _required_context(port)
    kind = CommissioningMotionKind(request.type)
    if not session.capabilities.supports(kind):
        raise ValueError(f"当前 Adapter 不支持 {kind.value}")
    common = {
        "command_id": request.command_id,
        "hardware_profile_digest": hardware_digest,
        "source_boot_id": source_boot_id,
        "monotonic_sequence": monotonic_sequence,
    }
    if kind is CommissioningMotionKind.CONTROLLED_STOP:
        return ControlledStopCommand(
            **common,
            target_command_id=_required_text(
                request.target_command_id,
                "controlled_stop.target_command_id",
            ),
            reason=_required_text(request.reason, "controlled_stop.reason"),
        )
    finite = {
        **common,
        "motion_profile_ref": _motion_profile_ref(
            port,
            request.motion_profile_ref,
            kind.value,
        ),
        "velocity_scale": _required_scale(
            request.velocity_scale,
            f"{kind.value}.velocity_scale",
        ),
        "acceleration_scale": _required_scale(
            request.acceleration_scale,
            f"{kind.value}.acceleration_scale",
        ),
    }
    if kind is CommissioningMotionKind.MOVE_TARGET:
        target_revision = session.target_revision
        if not target_revision:
            raise ValueError("当前 Adapter 没有活动 PointSet 版本")
        return MoveTargetCommand(
            **finite,
            target_ref=_required_text(request.target_ref, "move_target.target_ref"),
            target_revision=target_revision,
        )
    if kind is CommissioningMotionKind.MOVE_POSE:
        pose = request.pose_input
        if not isinstance(pose, dict):
            raise ValueError("move_pose.pose_input 必须是对象")
        return MovePoseCommand(
            **finite,
            pose_input=CommissioningPoseInput(
                frame_ref=_required_text(pose.get("frame_ref"), "pose_input.frame_ref"),
                xyz_mm=_triple(pose.get("xyz_mm"), "pose_input.xyz_mm"),
                rotation_xyz=_triple(
                    pose.get("rotation_xyz"),
                    "pose_input.rotation_xyz",
                ),
                angle_unit=AngleUnit(str(pose.get("angle_unit", ""))),
                rotation_order=EulerRotationOrder(
                    str(pose.get("rotation_order", ""))
                ),
            ),
            tool_context_digest=tool_digest,
        )
    if kind is CommissioningMotionKind.TCP_JOG:
        return TcpJogCommand(
            **finite,
            frame_ref=_required_text(request.frame_ref, "tcp_jog.frame_ref"),
            axis=TcpAxis(_required_text(request.axis, "tcp_jog.axis")),
            direction=MotionDirection(
                _required_text(request.direction, "tcp_jog.direction")
            ),
            step_si=_required_positive(request.step_si, "tcp_jog.step_si"),
        )
    return JointJogCommand(
        **finite,
        joint_ref=_required_text(request.joint_ref, "joint_jog.joint_ref"),
        direction=MotionDirection(
            _required_text(request.direction, "joint_jog.direction")
        ),
        step_si=_required_positive(request.step_si, "joint_jog.step_si"),
    )


def _motion_profile_ref(port: Any, requested: object, kind: str) -> str:
    """优先使用设备包锁定的维护 Profile，并拒绝网页替换。"""

    configured = str(
        getattr(port, "commissioning_motion_profile_ref", "") or ""
    ).strip()
    supplied = str(requested or "").strip()
    if configured:
        if supplied and supplied != configured:
            raise ValueError(
                f"{kind}.motion_profile_ref 与设备包活动维护 Profile 不一致"
            )
        return configured
    return _required_text(supplied, f"{kind}.motion_profile_ref")


def _required_text(value: object, name: str) -> str:
    """校验 REST 命令中的必填字符串。"""

    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


def _required_scale(value: object, name: str) -> float:
    """校验低速维护缩放。"""

    normalized = _required_positive(value, name)
    if normalized > 0.30:
        raise ValueError(f"{name} 必须位于 (0, 0.30]")
    return normalized


def _required_positive(value: object, name: str) -> float:
    """校验正的有限数。"""

    try:
        normalized = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} 必须是正的有限数") from error
    if not math.isfinite(normalized) or normalized <= 0.0:
        raise ValueError(f"{name} 必须是正的有限数")
    return normalized


def _triple(value: object, name: str) -> tuple[float, float, float]:
    """校验三元有限向量。"""

    if isinstance(value, (str, bytes)):
        # REST 值形状错误统一投影为 422，而不是领域 API 之外的 TypeError。
        raise ValueError(f"{name} 必须包含三个有限数")  # noqa: TRY004
    try:
        result = tuple(float(item) for item in value)  # type: ignore[union-attr]
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} 必须包含三个有限数") from error
    if len(result) != 3 or not all(math.isfinite(item) for item in result):
        raise ValueError(f"{name} 必须包含三个有限数")
    return result  # type: ignore[return-value]


def _json_value(value: Any) -> Any:
    """把 contracts 数据类和枚举递归转换为 JSON 值。"""

    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def create_robot_commissioning_router(
    service: RobotCommissioningService,
) -> APIRouter:
    """创建不依赖 Backend 的本地机械臂维护路由。"""

    router = APIRouter(prefix="/api/v1/robot-commissioning", tags=["robot-commissioning"])

    @router.get("")
    def list_devices() -> dict[str, Any]:
        """列出本启动代际真实注册的机械臂调试端口。"""

        return {"schema_version": 2, "devices": service.list_devices()}

    @router.post("/{device_id}/sessions", status_code=201)
    def open_session(
        device_id: str,
        request: OpenCommissioningSessionRequest,
    ) -> dict[str, Any]:
        """打开独占维护会话。"""

        return _http_call(
            service.open_session,
            device_id,
            request.owner_id,
            request.requested_deployment_mode,
        )

    @router.get("/{device_id}/sessions/{session_id}")
    def session_context(device_id: str, session_id: str) -> dict[str, Any]:
        """读取会话上下文和下一序号。"""

        return _http_call(service.session_context, device_id, session_id)

    @router.get("/{device_id}/sessions/{session_id}/snapshot")
    def snapshot(device_id: str, session_id: str) -> dict[str, Any]:
        """读取只读机械臂调试快照。"""

        return _http_call(service.snapshot, device_id, session_id)

    @router.post("/{device_id}/sessions/{session_id}/commands")
    def execute(
        device_id: str,
        session_id: str,
        request: CommissioningCommandRequest,
    ) -> dict[str, Any]:
        """同步执行一步封闭调试命令。"""

        return _http_call(service.execute, device_id, session_id, request)

    @router.delete("/{device_id}/sessions/{session_id}", status_code=204)
    def close_session(device_id: str, session_id: str) -> Response:
        """释放维护会话；存在 Fence 时失败关闭。"""

        _http_call(service.close_session, device_id, session_id)
        return Response(status_code=204)

    return router


def _http_call(function: Any, *args: Any) -> Any:
    """把领域异常稳定映射为 HTTP 状态，不把 UNKNOWN 改写为失败。"""

    try:
        return function(*args)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error.args[0])) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


_SERVICE = RobotCommissioningService()


def get_robot_commissioning_service() -> RobotCommissioningService:
    """返回 OS 进程唯一的本地机械臂调试服务。"""

    return _SERVICE


def register_robot_commissioning_runtime(device_id: str, binding: Any) -> None:
    """供领域设备包在 ROS ``post_init`` 阶段注册调试运行时。"""

    _SERVICE.register(device_id, binding)


def unregister_robot_commissioning_runtime(device_id: str) -> None:
    """供领域设备包正常关闭时注销并释放 RuntimeBinding。"""

    _SERVICE.unregister(device_id)


__all__ = [
    "CommissioningCommandRequest",
    "OpenCommissioningSessionRequest",
    "RobotCommissioningService",
    "create_robot_commissioning_router",
    "get_robot_commissioning_service",
    "register_robot_commissioning_runtime",
    "unregister_robot_commissioning_runtime",
]
