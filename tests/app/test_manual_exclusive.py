"""手动独占（Exclusive）准入、调度门禁与 HTTP 合同测试。"""

from __future__ import annotations

import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.edge_control.manual_exclusive_api import (
    create_manual_exclusive_router,
)
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.manual_exclusive import (
    ManualExclusiveBusyError,
    ManualExclusiveGate,
)
from unilabos.app.scheduler.models import WorkflowNode, WorkflowSpec
from unilabos.app.scheduler.service import EdgeScheduler


def _workflow(workflow_id: str = "workflow-exclusive") -> WorkflowSpec:
    """构造只含一个机械臂动作的最小工作流（Workflow）。

    参数：稳定工作流身份。返回：以 ``robot-01`` 为执行设备的规范工作流。
    异常：模型构造错误使测试直接失败。
    """

    return WorkflowSpec(
        workflow_id=workflow_id,
        nodes=[
            WorkflowNode(
                id="move",
                device_id="robot-01",
                action_name="move",
                action_type="goal",
                param={},
            )
        ],
        edges=[],
    )


def _standalone_gate(
    busy_keys: set[str] | None = None,
) -> ManualExclusiveGate:
    """构造不依赖完整调度器的 HTTP 合同测试门禁。

    参数：候选作业忙碌键集合。返回：使用独立重入锁和空重排回调的门禁。
    异常：无。
    """

    active_busy_keys = busy_keys if busy_keys is not None else set()
    return ManualExclusiveGate(
        lock=threading.RLock(),
        runtime_busy_keys=lambda: set(active_busy_keys),
        reschedule_locked=lambda: None,
    )


def test_exclusive_blocks_dispatch_until_release() -> None:
    """证明手动独占（Exclusive）阻止派发且释放后立即推进等待作业。

    参数：无。返回：无。异常：任一状态转换或调度断言失败即测试失败。
    """

    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    gate = scheduler.manual_exclusive_gate

    assert gate.acquire("robot-01").state == "exclusive"
    submitted = scheduler.submit_workflow(_workflow())

    assert submitted["dispatched"] == []
    assert dispatcher.dispatched == []
    released = gate.release("robot-01")
    assert released.state == "busy"
    assert [payload["device_id"] for payload in dispatcher.dispatched] == [
        "robot-01"
    ]


def test_busy_device_rejects_exclusive_acquire() -> None:
    """证明已有作业占用时不能取得手动独占（Exclusive）。

    参数：无。返回：无。异常：预期拒绝缺失或状态误报即测试失败。
    """

    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    scheduler.submit_workflow(_workflow())

    assert scheduler.manual_exclusive_gate.snapshot("robot-01").state == "busy"
    with pytest.raises(ManualExclusiveBusyError, match="is busy"):
        scheduler.manual_exclusive_gate.acquire("robot-01")


def test_new_scheduler_epoch_does_not_restore_exclusive() -> None:
    """证明手动独占（Exclusive）不会跨调度器进程 epoch 恢复。

    参数：无。返回：无。异常：新实例仍显示独占即测试失败。
    """

    first = EdgeScheduler()
    first.manual_exclusive_gate.acquire("robot-01")

    second = EdgeScheduler()
    assert second.manual_exclusive_gate.snapshot("robot-01").state == "idle"


def test_manual_exclusive_http_contract_is_exact_and_idempotent() -> None:
    """证明本地 HTTP 取得、读取与释放返回 exact 且幂等的合同形状。

    参数：无。返回：无。异常：路由、业务码或字段集合漂移即测试失败。
    """

    gate = _standalone_gate()
    application = FastAPI()
    application.include_router(
        create_manual_exclusive_router(
            gate,
            lambda local_device_id: local_device_id == "robot-01",
        )
    )
    client = TestClient(application)

    first = client.put("/api/v1/devices/robot-01/exclusive")
    second = client.put("/api/v1/devices/robot-01/exclusive")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {
        "code": 0,
        "data": {
            "local_device_id": "robot-01",
            "state": "exclusive",
            "exclusive": True,
        },
    }
    assert set(first.json()) == {"code", "data"}
    assert set(first.json()["data"]) == {
        "local_device_id",
        "state",
        "exclusive",
    }

    released = client.delete("/api/v1/devices/robot-01/exclusive")
    repeated = client.delete("/api/v1/devices/robot-01/exclusive")
    assert released.json() == repeated.json()
    assert released.json()["data"]["state"] == "idle"


def test_manual_exclusive_http_rejects_busy_and_unknown_device() -> None:
    """证明 HTTP 合同对忙碌设备和未知注册身份关闭式失败。

    参数：无。返回：无。异常：错误状态或业务码漂移即测试失败。
    """

    gate = _standalone_gate({"/devices/robot-01"})
    application = FastAPI()
    application.include_router(
        create_manual_exclusive_router(
            gate,
            lambda local_device_id: local_device_id == "robot-01",
        )
    )
    client = TestClient(application)

    busy = client.put("/api/v1/devices/robot-01/exclusive")
    assert busy.status_code == 409
    assert busy.json()["code"] == 7002

    missing = client.get("/api/v1/devices/missing/exclusive")
    assert missing.status_code == 404
    assert missing.json() == {
        "code": 7000,
        "message": "device binding not found",
    }
