"""MoveIt 单动作规划预算与 MoveGroup 重试执行深模块。"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from action_msgs.msg import GoalStatus

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
    """读取首次失败后的重试次数。

    参数：无。返回：非负重试次数；无法解析时返回固定默认值 10。异常：无。
    安全：负值收敛为 0，防止产生无界重试。
    """

    try:
        from unilabos.config.config import MoveItConfig

        raw = getattr(
            MoveItConfig,
            "plan_retry_attempts",
            DEFAULT_PLAN_RETRY_ATTEMPTS,
        )
        value = int(raw)
    except (TypeError, ValueError, ImportError):
        return DEFAULT_PLAN_RETRY_ATTEMPTS
    return value if value >= 0 else 0


def plan_attempt_limit() -> int:
    """返回包含首次规划在内的总尝试次数。

    参数：无。返回：``1 + plan_retry_attempts()``。异常：无。安全：至少尝试一次。
    """

    return 1 + plan_retry_attempts()


def is_retryable_plan_failure(*, succeeded: bool, error_code: object | None) -> bool:
    """判定 MoveIt 终态是否允许重新提交规划。

    参数：``succeeded`` 是动作是否成功；``error_code`` 是整数或带 ``val`` 的
    MoveIt 错误码。返回：仅规划失败、计划失效或超时时为真。异常：无。安全：
    控制失败、抢占和成功终态绝不自动重放动作。
    """

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


def run_plan_attempts(
    *,
    plan_once: Callable[[], object | None],
    resolve_trajectory: Callable[[object], object | None],
    sleep_while_pending: Callable[[], None],
    logger: Any,
) -> object | None:
    """在固定预算内重复规划，但从不重放控制动作。

    参数：单次规划、结果解析、等待节拍和 ROS 日志端口。返回：首个成功轨迹，
    预算耗尽返回 ``None``。异常：规划或解析异常原样上抛。安全：预算包含首次
    请求且总量有界；本函数只取得规划结果，不接触控制器。
    """

    attempts = plan_attempt_limit()
    for attempt in range(1, attempts + 1):
        future = plan_once()
        if future is None:
            _log_attempt(logger, attempt, attempts, "规划请求未发出")
            continue
        while not future.done():
            sleep_while_pending()
        trajectory = resolve_trajectory(future)
        if trajectory is not None:
            if attempt > 1:
                logger.info(f"MoveIt 规划在第 {attempt}/{attempts} 次尝试成功")
            return trajectory
        _log_attempt(logger, attempt, attempts, "规划失败")
    return None


@dataclass(frozen=True, slots=True)
class MoveActionStateUpdate:
    """一次 MoveGroup 动作生命周期状态投影。"""

    requested: bool
    executing: bool
    succeeded: bool
    goal_handle: object | None
    error_code: object | None


class MoveGroupActionRetry:
    """拥有 MoveGroup 目标快照、规划重试预算和异步回调生命周期。"""

    def __init__(
        self,
        *,
        node: Any,
        action_client: Any,
        current_joint_state: Callable[[], object | None],
        publish_state: Callable[[MoveActionStateUpdate], None],
    ) -> None:
        """绑定一个 MoveGroup 动作端口与外层状态投影。

        参数：ROS 节点、动作客户端、当前关节读取器和状态回调。返回：无。
        异常：无。安全：本模块只在明确的规划失败、计划失效或超时时重发目标。
        """

        self._node = node
        self._action_client = action_client
        self._current_joint_state = current_joint_state
        self._publish_state = publish_state
        self._lock = threading.RLock()
        self._saved_goal: object | None = None
        self._retries_remaining = 0

    def start(self, goal: object) -> None:
        """冻结目标并开始首次派发。"""

        with self._lock:
            self._saved_goal = copy.deepcopy(goal)
            self._retries_remaining = plan_retry_attempts()
        self._send_saved_goal()

    def _send_saved_goal(self) -> None:
        """以最新关节观测发送冻结目标。"""

        with self._lock:
            if self._saved_goal is None:
                return
            goal = copy.deepcopy(self._saved_goal)
        current = self._current_joint_state()
        if current is not None:
            goal.request.start_state.joint_state = current
        goal.request.workspace_parameters.header.stamp = (
            self._node.get_clock().now().to_msg()
        )
        if not self._action_client.server_is_ready():
            self._node.get_logger().warn(
                f"Action server '{self._action_client._action_name}' is not yet "
                "available. Better luck next time!"
            )
            self._publish(False, False, False, None, None)
            return
        self._publish(True, False, False, None, None)
        future = self._action_client.send_goal_async(
            goal=goal,
            feedback_callback=None,
        )
        future.add_done_callback(self._on_response)

    def _on_response(self, response: object) -> None:
        goal_handle = response.result()
        if not goal_handle.accepted:
            self._node.get_logger().warn(
                f"Action '{self._action_client._action_name}' was rejected."
            )
            if self._consume_retry("规划请求被拒绝"):
                self._publish(True, False, False, None, None)
                self._send_saved_goal()
                return
            self._publish(False, False, False, None, None)
            return
        self._publish(False, True, False, goal_handle, None)
        future = goal_handle.get_result_async()
        future.add_done_callback(self._on_result)

    def _on_result(self, future: object) -> None:
        result = future.result()
        succeeded = result.status == GoalStatus.STATUS_SUCCEEDED
        error_code = result.result.error_code
        if succeeded:
            self._publish(False, False, True, None, error_code)
            return
        self._node.get_logger().warn(
            f"Action '{self._action_client._action_name}' was unsuccessful: "
            f"{result.status}."
        )
        if is_retryable_plan_failure(
            succeeded=False,
            error_code=error_code,
        ) and self._consume_retry("规划失败"):
            self._publish(True, False, False, None, error_code)
            self._send_saved_goal()
            return
        self._publish(False, False, False, None, error_code)

    def _consume_retry(self, reason: str) -> bool:
        with self._lock:
            if self._retries_remaining <= 0 or self._saved_goal is None:
                return False
            retries = plan_retry_attempts()
            used = retries - self._retries_remaining + 1
            self._retries_remaining -= 1
        self._node.get_logger().warn(f"MoveIt {reason}，重试 {used}/{retries}")
        return True

    def _publish(
        self,
        requested: bool,
        executing: bool,
        succeeded: bool,
        goal_handle: object | None,
        error_code: object | None,
    ) -> None:
        self._publish_state(
            MoveActionStateUpdate(
                requested=requested,
                executing=executing,
                succeeded=succeeded,
                goal_handle=goal_handle,
                error_code=error_code,
            )
        )


def _log_attempt(logger: Any, attempt: int, attempts: int, reason: str) -> None:
    if attempt < attempts:
        logger.warn(f"MoveIt {reason}，重试 {attempt}/{attempts}")
        return
    logger.warn(f"MoveIt {reason}，已达 {attempts} 次上限")


__all__ = [
    "DEFAULT_PLAN_RETRY_ATTEMPTS",
    "MoveActionStateUpdate",
    "MoveGroupActionRetry",
    "is_retryable_plan_failure",
    "plan_attempt_limit",
    "plan_retry_attempts",
    "run_plan_attempts",
]
