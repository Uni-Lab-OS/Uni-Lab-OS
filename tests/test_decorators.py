"""@action 装饰器超时/异常处理扩展单测

覆盖：
- 默认参数下的属性值
- 协程超时被转换为 TimeoutException
- 协程未超时正常返回
- exception_handling=False 时属性正确
- get_action_timeout / is_exception_handling_enabled 辅助函数
- 同步函数装饰后元数据齐全
"""
import asyncio

import pytest

from unilabos.devices.exceptions import TimeoutException
from unilabos.registry.decorators import (
    action,
    get_action_meta,
    get_action_timeout,
    is_exception_handling_enabled,
)


class DummyDevice:
    """测试用宿主类，wrapper 沿用 *args, **kwargs，self 通过 args[0] 传入"""

    def _get_device_snapshot(self):
        return {"step": 1}


# ---------- a) 默认参数 ----------

def test_default_attrs_async():
    @action()
    async def foo(self):
        return 1

    assert foo._action_timeout is None
    assert foo._exception_handling is True


def test_default_attrs_sync():
    @action()
    def bar(self):
        return 2

    assert bar._action_timeout is None
    assert bar._exception_handling is True


# ---------- b) 协程超时 -> TimeoutException ----------

def test_async_timeout_raises_timeout_exception():
    @action(timeout=0.1)
    async def slow(self):
        await asyncio.sleep(1)

    dev = DummyDevice()
    with pytest.raises(TimeoutException) as ei:
        asyncio.run(slow(dev))
    # 快照应被正确采集
    assert ei.value.device_snapshot == {"step": 1}
    assert "slow" in ei.value.message


def test_async_timeout_snapshot_failure_does_not_break():
    """_get_device_snapshot 抛错也应正常抛 TimeoutException"""

    class BadDevice:
        def _get_device_snapshot(self):
            raise RuntimeError("snapshot broken")

    @action(timeout=0.05)
    async def slow(self):
        await asyncio.sleep(1)

    with pytest.raises(TimeoutException) as ei:
        asyncio.run(slow(BadDevice()))
    assert ei.value.device_snapshot == {}


# ---------- c) 未超时正常返回 ----------

def test_async_within_timeout_returns_value():
    @action(timeout=10)
    async def quick(self):
        await asyncio.sleep(0)
        return 42

    assert asyncio.run(quick(DummyDevice())) == 42


def test_async_no_timeout_returns_value():
    @action()
    async def quick(self):
        return "ok"

    assert asyncio.run(quick(DummyDevice())) == "ok"


# ---------- d) exception_handling=False ----------

def test_exception_handling_disabled():
    @action(exception_handling=False)
    async def foo(self):
        return None

    assert foo._exception_handling is False
    meta = get_action_meta(foo)
    assert meta["exception_handling"] is False


def test_exception_handling_default_not_in_meta():
    """默认 True 时不写入 meta（沿用 feedback_interval 模式）"""

    @action()
    async def foo(self):
        return None

    meta = get_action_meta(foo)
    assert "exception_handling" not in meta


# ---------- e) 辅助函数 ----------

def test_get_action_timeout():
    @action(timeout=5.0)
    async def foo(self):
        return None

    assert get_action_timeout(foo) == 5.0


def test_get_action_timeout_none():
    @action()
    async def foo(self):
        return None

    assert get_action_timeout(foo) is None


def test_is_exception_handling_enabled_true():
    @action()
    async def foo(self):
        return None

    assert is_exception_handling_enabled(foo) is True


def test_is_exception_handling_enabled_false():
    @action(exception_handling=False)
    async def foo(self):
        return None

    assert is_exception_handling_enabled(foo) is False


# ---------- f) 同步函数元数据齐全 ----------

def test_sync_function_metadata():
    @action(timeout=3.0, exception_handling=False, default_on_user_timeout="retry")
    def bar(self, x):
        return x * 2

    assert bar(DummyDevice(), 5) == 10
    assert bar._action_timeout == 3.0
    assert bar._exception_handling is False

    meta = get_action_meta(bar)
    assert meta is not None
    assert meta["timeout"] == 3.0
    assert meta["exception_handling"] is False
    assert meta["default_on_user_timeout"] == "retry"


def test_default_on_user_timeout_default_not_in_meta():
    @action(timeout=1.0)
    async def foo(self):
        return None

    meta = get_action_meta(foo)
    assert meta["timeout"] == 1.0
    assert "default_on_user_timeout" not in meta
