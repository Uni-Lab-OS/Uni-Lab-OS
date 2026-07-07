"""框架层异常处理 + 用户决策回环 端到端测试

不依赖 ROS2 环境:FakeNode 内嵌等价的异常处理逻辑(与 BaseROS2DeviceNode 一致)。
在真实环境中通过 _execute_with_exception_handling 方法验证;此处用 FakeNode 模拟
即可覆盖核心分支(retry/skip/abort/manual_fix/自定义 action/超时/死循环防护)。
"""
import asyncio
import logging
import traceback
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest

from unilabos.utils.exception import (
    DeviceException,
    ModbusConnectionError,
    TipPickupError,
    UserAction,
)


class FakeNode:
    """最小化 device_node,方法逻辑与 BaseROS2DeviceNode 中等价"""

    device_id = "fake_device"
    uuid = "fake-uuid-1"

    def __init__(self, decision_timeout: float = 5.0):
        self._pending_decisions: Dict[str, asyncio.Future] = {}
        self._user_decision_timeout = decision_timeout
        self.ws_client = MagicMock()
        self.ws_client.publish_device_exception_alarm = MagicMock()

    def get_logger(self):
        return logging.getLogger("FakeNode")

    def _get_ws_client(self):
        return self.ws_client

    async def _execute_with_exception_handling(
        self, action_func, action_name, task_id, job_id, action_kwargs,
        max_user_iterations: int = 10,
    ) -> Any:
        iteration = 0
        exc: Optional[DeviceException] = None
        while True:
            iteration += 1
            if iteration > max_user_iterations:
                raise DeviceException(
                    f"动作 {action_name} 异常处理超过 {max_user_iterations} 轮,强制终止"
                )
            try:
                return await action_func(**action_kwargs)
            except DeviceException as e:
                exc = e
                decision = await self._handle_device_exception(e, action_name, task_id, job_id)
            except Exception as e:
                wrapped = DeviceException(
                    f"未预期异常: {type(e).__name__}: {e}",
                    suggested_actions=[
                        UserAction("retry", "重试"),
                        UserAction("abort", "终止"),
                    ],
                    cause=e,
                )
                wrapped.traceback_str = traceback.format_exc()
                exc = wrapped
                decision = await self._handle_device_exception(wrapped, action_name, task_id, job_id)

            action = decision.get("action", "abort")
            if action == "retry":
                continue
            if action == "skip":
                return {"status": "skipped", "reason": decision.get("reason", "user_skip")}
            if action == "manual_fix":
                continue
            if action == "abort":
                raise exc

            matched: Optional[UserAction] = None
            for ua in (exc.suggested_actions if isinstance(exc, DeviceException) else []):
                if ua.action == action:
                    matched = ua
                    break
            if matched is None or matched.handler is None:
                raise exc

            try:
                handler_result = await matched.handler(exception=exc, decision=decision)
            except DeviceException as he:
                exc = he
                next_decision = await self._handle_device_exception(he, action_name, task_id, job_id)
                act2 = next_decision.get("action", "abort")
                if act2 == "retry" or act2 == "manual_fix":
                    continue
                if act2 == "skip":
                    return {"status": "skipped", "reason": next_decision.get("reason", "user_skip")}
                raise he

            then = matched.then
            if then == "retry":
                continue
            if then == "skip":
                return {"status": "skipped", "reason": f"after_{action}"}
            if then == "continue":
                return handler_result
            continue

    async def _handle_device_exception(self, exc, action_name, task_id, job_id) -> dict:
        alarm = exc.to_alarm_dict(
            device_id=self.device_id, device_uuid=self.uuid,
            action_name=action_name, task_id=task_id, job_id=job_id,
        )
        self.ws_client.publish_device_exception_alarm(alarm)
        try:
            return await self._wait_for_user_decision(task_id, exc)
        except asyncio.TimeoutError:
            return {"action": "abort", "reason": "user_decision_timeout"}

    async def _wait_for_user_decision(self, task_id, exc) -> dict:
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        key = f"{task_id}:{exc.message[:32]}"
        self._pending_decisions[key] = fut
        try:
            return await asyncio.wait_for(fut, timeout=self._user_decision_timeout)
        finally:
            self._pending_decisions.pop(key, None)

    def handle_user_decision(self, task_id, decision):
        for key in list(self._pending_decisions.keys()):
            if key.startswith(f"{task_id}:"):
                fut = self._pending_decisions[key]
                if not fut.done():
                    fut.set_result(decision)
                break


def _post_decision(node, decision, delay=0.01):
    async def _send():
        await asyncio.sleep(delay)
        keys = list(node._pending_decisions.keys())
        if not keys:
            return
        task_id = keys[0].split(":", 1)[0]
        node.handle_user_decision(task_id, decision)
    return asyncio.create_task(_send())


@pytest.mark.asyncio
async def test_normal_return():
    node = FakeNode()
    async def driver(**kwargs):
        return {"ok": True, "v": kwargs.get("v")}
    result = await node._execute_with_exception_handling(
        driver, "driver", "t1", "j1", {"v": 42},
    )
    assert result == {"ok": True, "v": 42}
    node.ws_client.publish_device_exception_alarm.assert_not_called()


@pytest.mark.asyncio
async def test_retry_then_success():
    node = FakeNode()
    calls = {"n": 0}
    async def driver(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ModbusConnectionError("first fail")
        return {"ok": True}
    _post_decision(node, {"action": "retry"})
    result = await node._execute_with_exception_handling(driver, "driver", "t2", "j2", {})
    assert result == {"ok": True}
    assert calls["n"] == 2
    node.ws_client.publish_device_exception_alarm.assert_called_once()


@pytest.mark.asyncio
async def test_abort_raises():
    node = FakeNode()
    async def driver(**kwargs):
        raise ModbusConnectionError("network down")
    _post_decision(node, {"action": "abort"})
    with pytest.raises(ModbusConnectionError):
        await node._execute_with_exception_handling(driver, "driver", "t3", "j3", {})


@pytest.mark.asyncio
async def test_skip_returns_skipped():
    node = FakeNode()
    async def driver(**kwargs):
        raise ModbusConnectionError("fail")
    _post_decision(node, {"action": "skip", "reason": "user_skip_here"})
    result = await node._execute_with_exception_handling(driver, "driver", "t4", "j4", {})
    assert result["status"] == "skipped"
    assert result["reason"] == "user_skip_here"


@pytest.mark.asyncio
async def test_user_decision_timeout_aborts():
    node = FakeNode(decision_timeout=0.05)
    async def driver(**kwargs):
        raise ModbusConnectionError("net err")
    with pytest.raises(ModbusConnectionError):
        await node._execute_with_exception_handling(driver, "driver", "t5", "j5", {})


@pytest.mark.asyncio
async def test_custom_action_with_handler_then_retry():
    node = FakeNode()
    state = {"tip": 0, "calls": 0}

    async def advance(exception, decision):
        state["tip"] += 1

    async def driver(**kwargs):
        state["calls"] += 1
        if state["tip"] == 0:
            raise TipPickupError(
                "pickup failed",
                suggested_actions=[
                    UserAction("use_next_tip", "下一个", handler=advance, then="retry"),
                    UserAction("abort", "终止"),
                ],
            )
        return {"tip_used": state["tip"]}

    _post_decision(node, {"action": "use_next_tip"})
    result = await node._execute_with_exception_handling(driver, "driver", "t6", "j6", {})
    assert result == {"tip_used": 1}
    assert state["calls"] == 2
    assert state["tip"] == 1


@pytest.mark.asyncio
async def test_handler_raises_device_exception_enters_next_round():
    node = FakeNode()

    async def bad_handler(exception, decision):
        raise ModbusConnectionError("handler also failed")

    async def driver(**kwargs):
        raise TipPickupError(
            "pickup failed",
            suggested_actions=[
                UserAction("use_next_tip", "下一个", handler=bad_handler, then="retry"),
                UserAction("abort", "终止"),
            ],
        )

    async def feeder():
        # 第一轮:回 use_next_tip
        for _ in range(50):
            await asyncio.sleep(0.005)
            keys = list(node._pending_decisions.keys())
            if keys:
                node.handle_user_decision(keys[0].split(":", 1)[0], {"action": "use_next_tip"})
                break
        # 第二轮:handler 抛错后,回 abort
        for _ in range(50):
            await asyncio.sleep(0.005)
            keys = list(node._pending_decisions.keys())
            if keys:
                node.handle_user_decision(keys[0].split(":", 1)[0], {"action": "abort"})
                return

    asyncio.create_task(feeder())
    with pytest.raises(ModbusConnectionError):
        await node._execute_with_exception_handling(driver, "driver", "t7", "j7", {})
    assert node.ws_client.publish_device_exception_alarm.call_count >= 2


@pytest.mark.asyncio
async def test_max_user_iterations():
    node = FakeNode()
    calls = {"n": 0}
    async def driver(**kwargs):
        calls["n"] += 1
        raise ModbusConnectionError(f"fail {calls['n']}")

    async def auto_retry():
        for _ in range(50):
            await asyncio.sleep(0.005)
            keys = list(node._pending_decisions.keys())
            if keys:
                node.handle_user_decision(keys[0].split(":", 1)[0], {"action": "retry"})

    asyncio.create_task(auto_retry())
    with pytest.raises(DeviceException) as ei:
        await node._execute_with_exception_handling(
            driver, "driver", "t8", "j8", {}, max_user_iterations=3,
        )
    assert "强制终止" in str(ei.value)
