"""MoveIt 规划失败重试策略；次数由全局 MoveItConfig 拥有。"""

from __future__ import annotations

DEFAULT_PLAN_RETRY_ATTEMPTS = 10

# moveit_msgs/MoveItErrorCodes 中与规划相关、可安全重试的取值。
_PLANNING_FAILED = -1
_INVALID_MOTION_PLAN = -2
_MOTION_PLAN_INVALIDATED_BY_ENVIRONMENT_CHANGE = -3
_CONTROL_FAILED = -4
_TIMED_OUT = -6
_PREEMPTED = -7
_SUCCESS = 1

_RETRYABLE_PLAN_ERROR_VALS = frozenset(
    {
        _PLANNING_FAILED,
        _INVALID_MOTION_PLAN,
        _MOTION_PLAN_INVALIDATED_BY_ENVIRONMENT_CHANGE,
        _TIMED_OUT,
    }
)


def plan_retry_attempts() -> int:
    """读取失败后的重试次数。0 = 不重试；负值按 0；无法解析时回退为 10。"""

    try:
        from unilabos.config.config import MoveItConfig

        raw = getattr(
            MoveItConfig, "plan_retry_attempts", DEFAULT_PLAN_RETRY_ATTEMPTS
        )
        value = int(raw)
    except (TypeError, ValueError, ImportError):
        return DEFAULT_PLAN_RETRY_ATTEMPTS
    return value if value >= 0 else 0


def plan_attempt_limit() -> int:
    """含首次在内的总尝试次数：1 + 失败后重试次数。"""

    return 1 + plan_retry_attempts()


def is_retryable_plan_failure(*, succeeded: bool, error_code: object | None) -> bool:
    """仅对规划失败重试；控制失败或抢占后不得自动再派发。"""

    if succeeded:
        return False
    if error_code is None:
        return True
    raw = getattr(error_code, "val", error_code)
    try:
        code = int(raw)
    except (TypeError, ValueError):
        return True
    if code in {_SUCCESS, _CONTROL_FAILED, _PREEMPTED}:
        return False
    return code in _RETRYABLE_PLAN_ERROR_VALS
