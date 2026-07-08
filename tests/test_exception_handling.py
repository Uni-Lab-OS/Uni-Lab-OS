"""框架层异常处理 + 用户决策回环 端到端测试

不依赖 ROS2 环境:FakeNode 内嵌等价的异常处理逻辑(与 BaseROS2DeviceNode 一致)。
在真实环境中通过 _run_action_with_decision_loop 方法验证;此处用 FakeNode 模拟
即可覆盖核心分支(retry/skip/abort/manual_fix/自定义 action/超时/死循环防护)。
"""
import asyncio
import logging
import traceback
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from unilabos.utils.exception import (
    DeviceException,
    ModbusConnectionError,
    TipPickupError,
    UserAction,
)


class FakeNode:
    """最小化 device_node,方法逻辑与 BaseROS2DeviceNode._run_action_with_decision_loop 等价"""

    device_id = "fake_device"
    uuid = "fake-uuid-1"

    def __init__(self, decision_timeout: float = 5.0):
        self._pending_decisions: Dict[str, asyncio.Future] = {}
        self._user_decision_timeout = decision_timeout
        self.ws_client = MagicMock()
        self.ws_client.publish_job_error_decision_required = MagicMock()

    def get_logger(self):
        return logging.getLogger("FakeNode")

    def _get_ws_client(self):
        return self.ws_client

    async def _run_action_with_decision_loop(
        self, action_func, action_name, task_id, job_id, action_kwargs,
        max_iterations: int = 10,
        error_policy=None,
        option_handlers=None,
    ) -> Any:
        policy = error_policy or {}
        opt_handlers = option_handlers or {}
        max_iter = int(policy.get("max_retries", max_iterations))
        decision_timeout = float(policy.get("decision_timeout_seconds", self._user_decision_timeout))
        default_on_timeout = str(policy.get("default_on_decision_timeout", "abort"))
        allow_retry = bool(policy.get("allow_retry", True))
        allow_skip = bool(policy.get("allow_skip", True))
        policy_options = policy.get("options") or []

        def _build_fallback():
            items = []
            if allow_retry:
                items.append(UserAction("retry", "重试", "重新执行"))
            if allow_skip:
                items.append(UserAction("skip", "跳过", "跳过当前操作继续执行"))
            for opt in policy_options:
                if not isinstance(opt, dict):
                    continue
                act_key = opt.get("action")
                if not isinstance(act_key, str) or act_key in ("retry", "skip", "abort"):
                    continue
                items.append(UserAction(
                    action=act_key,
                    label=opt.get("label", act_key),
                    description=opt.get("description", ""),
                    handler=opt_handlers.get(act_key),
                    then=opt.get("then", "retry"),
                ))
            items.append(UserAction("abort", "终止", "终止任务"))
            return items

        pending_handler = None

        for _ in range(1, max_iter + 1):
            try:
                if pending_handler is not None:
                    hfunc, prev_exc, decision, then = pending_handler
                    pending_handler = None
                    handler_result = await hfunc(exception=prev_exc, decision=decision)
                    if then == "continue":
                        return handler_result
                    if then == "skip":
                        return {"status": "skipped", "reason": f"after_{decision.get('action')}"}
                return await action_func(**action_kwargs)
            except Exception as exc:
                if isinstance(exc, DeviceException) and exc.suggested_actions:
                    suggested = exc.suggested_actions
                else:
                    suggested = _build_fallback()

                if isinstance(exc, DeviceException):
                    alarm = exc.to_alarm_dict(
                        device_id=self.device_id, device_uuid=self.uuid,
                        action_name=action_name, task_id=task_id, job_id=job_id,
                    )
                else:
                    alarm = {
                        "device_id": self.device_id,
                        "device_uuid": self.uuid,
                        "action_name": action_name,
                        "task_id": task_id,
                        "job_id": job_id,
                        "exception_type": type(exc).__name__,
                        "category": policy.get("category", "unknown"),
                        "severity": policy.get("severity", "error"),
                        "error_message": f"{type(exc).__name__}: {exc}",
                        "suggested_actions": [
                            {"action": a.action, "label": a.label, "description": a.description}
                            for a in suggested
                        ],
                        "device_snapshot": {},
                        "traceback": traceback.format_exc(),
                        "require_confirmation": True,
                    }
                self.ws_client.publish_job_error_decision_required(alarm)

                loop = asyncio.get_event_loop()
                fut: asyncio.Future = loop.create_future()
                key = f"{task_id}:{str(exc)[:32]}"
                self._pending_decisions[key] = fut
                try:
                    decision = await asyncio.wait_for(fut, timeout=decision_timeout)
                except asyncio.TimeoutError:
                    decision = {"action": default_on_timeout, "reason": "user_decision_timeout"}
                finally:
                    self._pending_decisions.pop(key, None)

                action = decision.get("action", "abort")
                if action in ("retry", "manual_fix"):
                    continue
                if action == "skip":
                    return {"status": "skipped", "reason": decision.get("reason", "user_skip")}
                if action == "abort":
                    raise

                matched = next((ua for ua in suggested if ua.action == action), None)
                if matched is None or matched.handler is None:
                    raise
                pending_handler = (
                    matched.handler, exc, decision, getattr(matched, "then", "retry") or "retry",
                )
                continue

        raise DeviceException(
            f"动作 {action_name} 异常处理超过 {max_iter} 轮,强制终止"
        )

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
    result = await node._run_action_with_decision_loop(
        driver, "driver", "t1", "j1", {"v": 42},
    )
    assert result == {"ok": True, "v": 42}
    node.ws_client.publish_job_error_decision_required.assert_not_called()


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
    result = await node._run_action_with_decision_loop(driver, "driver", "t2", "j2", {})
    assert result == {"ok": True}
    assert calls["n"] == 2
    node.ws_client.publish_job_error_decision_required.assert_called_once()


@pytest.mark.asyncio
async def test_abort_raises():
    node = FakeNode()
    async def driver(**kwargs):
        raise ModbusConnectionError("network down")
    _post_decision(node, {"action": "abort"})
    with pytest.raises(ModbusConnectionError):
        await node._run_action_with_decision_loop(driver, "driver", "t3", "j3", {})


@pytest.mark.asyncio
async def test_skip_returns_skipped():
    node = FakeNode()
    async def driver(**kwargs):
        raise ModbusConnectionError("fail")
    _post_decision(node, {"action": "skip", "reason": "user_skip_here"})
    result = await node._run_action_with_decision_loop(driver, "driver", "t4", "j4", {})
    assert result["status"] == "skipped"
    assert result["reason"] == "user_skip_here"


@pytest.mark.asyncio
async def test_user_decision_timeout_aborts():
    node = FakeNode(decision_timeout=0.05)
    async def driver(**kwargs):
        raise ModbusConnectionError("net err")
    with pytest.raises(ModbusConnectionError):
        await node._run_action_with_decision_loop(driver, "driver", "t5", "j5", {})


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
    result = await node._run_action_with_decision_loop(driver, "driver", "t6", "j6", {})
    assert result == {"tip_used": 1}
    assert state["calls"] == 2
    assert state["tip"] == 1


@pytest.mark.asyncio
async def test_handler_raises_device_exception_enters_next_round():
    """handler 抛错让其自然冒泡到主循环顶端,by-design 触发下一轮决策"""
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
        # 第二轮:handler 抛错冒泡回 for-except 顶端后,回 abort
        for _ in range(50):
            await asyncio.sleep(0.005)
            keys = list(node._pending_decisions.keys())
            if keys:
                node.handle_user_decision(keys[0].split(":", 1)[0], {"action": "abort"})
                return

    asyncio.create_task(feeder())
    with pytest.raises(ModbusConnectionError):
        await node._run_action_with_decision_loop(driver, "driver", "t7", "j7", {})
    assert node.ws_client.publish_job_error_decision_required.call_count >= 2


@pytest.mark.asyncio
async def test_max_iterations():
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
        await node._run_action_with_decision_loop(
            driver, "driver", "t8", "j8", {}, max_iterations=3,
        )
    assert "强制终止" in str(ei.value)


@pytest.mark.asyncio
async def test_error_policy_max_retries_overrides_default():
    """error_policy.max_retries 覆盖 max_iterations"""
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
        await node._run_action_with_decision_loop(
            driver, "driver", "tmax", "jmax", {},
            max_iterations=10,  # 会被 policy 覆盖成 2
            error_policy={"max_retries": 2},
        )
    assert "超过 2 轮" in str(ei.value)


@pytest.mark.asyncio
async def test_error_policy_default_on_timeout_skip():
    """决策超时时使用 policy.default_on_decision_timeout 而不是硬编码 abort"""
    node = FakeNode(decision_timeout=0.05)
    async def driver(**kwargs):
        raise ModbusConnectionError("net err")
    # 超时后走 skip
    result = await node._run_action_with_decision_loop(
        driver, "driver", "tdt", "jdt", {},
        error_policy={"default_on_decision_timeout": "skip"},
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "user_decision_timeout"


@pytest.mark.asyncio
async def test_error_policy_options_provide_handler_via_option_handlers():
    """声明式 options + option_handlers 组装出可运行的 UserAction"""
    node = FakeNode()
    state = {"tip": 0}

    async def swap_tip(exception, decision):
        state["tip"] += 1

    async def driver(**kwargs):
        if state["tip"] == 0:
            # 非 DeviceException,决策 options 只能来自 policy.options
            raise RuntimeError("first fail")
        return {"tip": state["tip"]}

    _post_decision(node, {"action": "swap_tip"})
    result = await node._run_action_with_decision_loop(
        driver, "driver", "topt", "jopt", {},
        error_policy={
            "options": [
                {"action": "swap_tip", "label": "换头", "then": "retry"},
            ],
        },
        option_handlers={"swap_tip": swap_tip},
    )
    assert result == {"tip": 1}
    assert state["tip"] == 1


@pytest.mark.asyncio
async def test_error_policy_allow_skip_false_still_lets_user_skip_if_selected():
    """allow_skip=False 只影响兜底 options,不影响直接收到 skip 的语义"""
    node = FakeNode()
    async def driver(**kwargs):
        raise ModbusConnectionError("fail")

    # 即使 allow_skip=False,当用户回传 skip 时循环仍应视为 skip 分支
    _post_decision(node, {"action": "skip", "reason": "force_skip"})
    result = await node._run_action_with_decision_loop(
        driver, "driver", "tas", "jas", {},
        error_policy={"allow_skip": False},
    )
    assert result["status"] == "skipped"
