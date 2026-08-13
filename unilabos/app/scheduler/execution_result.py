"""设备执行结果到调度状态的最小适配规则。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

EXECUTION_UNKNOWN_STATE = "execution_unknown"


def execution_state_from_result(result: Any) -> str | None:
    """读取设备结果显式声明的规范执行状态。

    参数：``result`` 是设备动作返回值。返回：对象中非空 ``state`` 的小写值；
    非对象或没有状态时返回 ``None``。异常：无；未知状态只透传给上层分类器，
    本函数不把普通业务失败推断为执行结果不确定。
    """

    if not isinstance(result, Mapping):
        return None
    state = result.get("state")
    if not isinstance(state, str):
        return None
    normalized = state.strip().lower()
    return normalized or None


def is_execution_unknown_result(result: Any) -> bool:
    """判断设备结果是否明确声明物理执行结果不确定。

    参数：``result`` 是设备动作返回值。返回：仅规范化状态精确等于
    ``execution_unknown`` 时为真。异常：无；缺失或歧义输入一律不升级为该状态。
    """

    return execution_state_from_result(result) == EXECUTION_UNKNOWN_STATE


__all__ = [
    "EXECUTION_UNKNOWN_STATE",
    "execution_state_from_result",
    "is_execution_unknown_result",
]
