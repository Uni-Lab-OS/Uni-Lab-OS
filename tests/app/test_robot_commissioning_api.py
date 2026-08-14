"""机械臂调试本地 API 的独占会话和命令接线测试。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import unilabos.app.robot_commissioning as commissioning_api


@dataclass(frozen=True)
class _Capabilities:
    """测试 Adapter 的能力集合。"""

    move_target: bool = True
    move_pose: bool = True
    tcp_jog: bool = True
    joint_jog: bool = True
    controlled_stop: bool = True

    def supports(self, _kind: object) -> bool:
        """测试端口支持全部封闭命令。"""

        return True


@dataclass(frozen=True)
class _Snapshot:
    """可 JSON 投影的新鲜仿真快照。"""

    state: str = "known"
    observed_at: float = 0.0
    max_age_s: float = 1.0
    source: str = "test:moveit"
    online: bool = True
    idle: bool = True
    active_command_id: str | None = None
    execution_fenced: bool = False


@dataclass(frozen=True)
class _Result:
    """测试命令的完成结果。"""

    command_id: str
    state: str = "succeeded"
    message: str = "done"
    output: dict[str, Any] | None = None


class _Command:
    """模拟已经由 OS 补全的共享调试命令。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def canonical_payload(self) -> dict[str, Any]:
        """返回命令 wire 值。"""

        return dict(self.payload)


class _Port:
    """提供部署摘要和能力的测试端口。"""

    hardware_profile_digest = "a" * 64
    tool_context_digest = "b" * 64
    commissioning_capabilities = _Capabilities()
    commissioning_target_revision = "ptlc-points@3.0.0"
    commissioning_motion_profile_ref = "ptlc-cr5-manual-step@1.0.0"


class _Session:
    """记录 API 交付命令的维护会话。"""

    def __init__(self, owner_id: str) -> None:
        self.owner_id = owner_id
        self.capabilities = _Capabilities()
        self.target_revision = "ptlc-points@3.0.0"
        self.closed = False
        self.commands: list[_Command] = []

    def snapshot(self) -> _Snapshot:
        """返回新鲜快照。"""

        return _Snapshot(observed_at=time.time())

    def execute(self, command: _Command) -> _Result:
        """记录命令并返回完成。"""

        self.commands.append(command)
        return _Result(command.payload["command_id"], output={})

    def close(self) -> None:
        """释放测试会话。"""

        self.closed = True


class _Binding:
    """模拟 Robotics RuntimeBinding 的最小公共面。"""

    def __init__(self) -> None:
        self.commissioning_port = _Port()
        self.session: _Session | None = None

    def open_maintenance_session(self, owner_id: str) -> _Session:
        """创建单个测试维护会话。"""

        self.session = _Session(owner_id)
        return self.session

    def close(self) -> None:
        """测试中无需释放外部资源。"""


def _client(service: commissioning_api.RobotCommissioningService) -> TestClient:
    """创建只挂载机械臂维护 API 的测试应用。"""

    app = FastAPI()
    app.include_router(commissioning_api.create_robot_commissioning_router(service))
    return TestClient(app)


def test_robot_commissioning_api_opens_executes_and_closes(
    monkeypatch,
) -> None:
    """网页可按 device_id 完成独占会话、一步命令和释放。"""

    service = commissioning_api.RobotCommissioningService()
    binding = _Binding()
    service.register("robot", binding)

    def build(request, **context):
        return _Command(
            {
                "schema_version": 2,
                "type": request.type,
                "command_id": request.command_id,
                "source_boot_id": context["source_boot_id"],
                "monotonic_sequence": context["monotonic_sequence"],
                "hardware_profile_digest": "a" * 64,
            }
        )

    monkeypatch.setattr(commissioning_api, "_build_command", build)
    client = _client(service)

    catalog = client.get("/api/v1/robot-commissioning").json()
    assert catalog["devices"][0]["device_id"] == "robot"
    assert catalog["devices"][0]["interaction_modes"] == {
        "step": True,
        "continuous_hold": False,
    }
    assert (
        catalog["devices"][0]["motion_profile_ref"]
        == "ptlc-cr5-manual-step@1.0.0"
    )

    opened = client.post(
        "/api/v1/robot-commissioning/robot/sessions",
        json={"owner_id": "workbench:operator-a"},
    )
    assert opened.status_code == 201
    session_id = opened.json()["session_id"]
    snapshot = client.get(
        f"/api/v1/robot-commissioning/robot/sessions/{session_id}/snapshot"
    )
    assert snapshot.json()["online"] is True

    command = client.post(
        f"/api/v1/robot-commissioning/robot/sessions/{session_id}/commands",
        json={
            "schema_version": 2,
            "command_id": "joint-jog-1",
            "type": "joint_jog",
            "motion_profile_ref": "ptlc-cr5-manual-step@1.0.0",
            "velocity_scale": 0.2,
            "acceleration_scale": 0.2,
            "joint_ref": "cr5_joint_1",
            "direction": "positive",
            "step_si": 0.017453292519943295,
        },
    )
    assert command.status_code == 200
    assert command.json()["command"]["monotonic_sequence"] == 1
    assert command.json()["result"]["state"] == "succeeded"

    closed = client.delete(
        f"/api/v1/robot-commissioning/robot/sessions/{session_id}"
    )
    assert closed.status_code == 204
    assert binding.session is not None and binding.session.closed is True


def test_robot_commissioning_api_rejects_duplicate_session() -> None:
    """同一机械臂只允许一个维护页面持有端点。"""

    service = commissioning_api.RobotCommissioningService()
    service.register("robot", _Binding())
    client = _client(service)
    first = client.post(
        "/api/v1/robot-commissioning/robot/sessions",
        json={"owner_id": "operator-a"},
    )
    assert first.status_code == 201
    second = client.post(
        "/api/v1/robot-commissioning/robot/sessions",
        json={"owner_id": "operator-b"},
    )
    assert second.status_code == 409
    assert "占用" in second.json()["detail"]


def test_device_locked_motion_profile_cannot_be_replaced_by_web() -> None:
    """设备包拥有维护 Profile；网页可省略，但不能替换其身份。"""

    port = _Port()
    assert (
        commissioning_api._motion_profile_ref(port, None, "joint_jog")
        == "ptlc-cr5-manual-step@1.0.0"
    )
    with pytest.raises(ValueError, match="活动维护 Profile 不一致"):
        commissioning_api._motion_profile_ref(port, "web-defined", "joint_jog")
