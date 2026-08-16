"""验证 MoveIt 规划失败重试的全局预算和错误分类。"""

from types import SimpleNamespace

from unilabos.config.config import MoveItConfig


def test_plan_retry_budget_counts_only_retries(monkeypatch):
    """重试配置不包含首次规划，总尝试数必须额外加一。

    参数：pytest ``monkeypatch``。返回：无。异常：无。安全：负预算不得导致
    无限重试，畸形配置必须回退为供体定义的固定默认值。
    """

    from unilabos.devices.ros_dev.moveit_plan_retry import (
        DEFAULT_PLAN_RETRY_ATTEMPTS,
        plan_attempt_limit,
        plan_retry_attempts,
    )

    monkeypatch.setattr(MoveItConfig, "plan_retry_attempts", 2, raising=False)
    assert plan_retry_attempts() == 2
    assert plan_attempt_limit() == 3

    monkeypatch.setattr(MoveItConfig, "plan_retry_attempts", -1, raising=False)
    assert plan_retry_attempts() == 0

    monkeypatch.setattr(MoveItConfig, "plan_retry_attempts", "invalid", raising=False)
    assert plan_retry_attempts() == DEFAULT_PLAN_RETRY_ATTEMPTS


def test_plan_retry_rejects_control_and_preemption_failures():
    """只重试规划失败，不得把控制失败或抢占后的动作再次派发。

    参数：无。返回：无。异常：无。安全：控制器已接管后的失败不能自动重放。
    """

    from unilabos.devices.ros_dev.moveit_plan_retry import is_retryable_plan_failure

    assert is_retryable_plan_failure(succeeded=False, error_code=-1)
    assert is_retryable_plan_failure(
        succeeded=False,
        error_code=SimpleNamespace(val=-6),
    )
    assert not is_retryable_plan_failure(succeeded=True, error_code=-1)
    assert not is_retryable_plan_failure(succeeded=False, error_code=-4)
    assert not is_retryable_plan_failure(succeeded=False, error_code=-7)
    assert not is_retryable_plan_failure(succeeded=False, error_code=1)


def test_moveit2_plan_retries_planning_failure_then_returns_trajectory(
    monkeypatch,
):
    """同步 ``plan`` 必须在首轮无轨迹后重新规划并返回第二轮结果。

    参数：pytest ``monkeypatch``。返回：无。异常：无。安全：只模拟规划服务，
    不创建 ROS 节点、不派发控制动作。
    """

    from unilabos.devices.ros_dev.moveit2 import MoveIt2

    monkeypatch.setattr(MoveItConfig, "plan_retry_attempts", 1, raising=False)
    futures = [SimpleNamespace(done=lambda: True) for _ in range(2)]
    trajectories = [None, "trajectory"]
    observed: list[dict[str, object]] = []
    fake = SimpleNamespace(
        _node=SimpleNamespace(
            create_rate=lambda _hz: SimpleNamespace(sleep=lambda: None),
            get_logger=lambda: SimpleNamespace(
                info=lambda _message: None,
                warn=lambda _message: None,
            ),
        ),
        plan_async=lambda **kwargs: observed.append(kwargs) or futures.pop(0),
        get_trajectory=lambda *_args, **_kwargs: trajectories.pop(0),
        _log_plan_retry=lambda *_args: None,
    )

    assert MoveIt2.plan(fake, joint_positions=[0.1]) == "trajectory"
    assert len(observed) == 2
    assert observed[0]["joint_positions"] == [0.1]


def test_move_group_action_retries_planning_failure_with_fresh_joint_state(
    monkeypatch,
):
    """异步动作只重试规划失败，并为下一轮读取最新关节观测。"""

    from action_msgs.msg import GoalStatus

    from unilabos.devices.ros_dev import moveit_plan_retry

    class ImmediateFuture:
        def __init__(self, value):
            self._value = value

        def result(self):
            return self._value

        def add_done_callback(self, callback):
            callback(self)

    results = [
        SimpleNamespace(
            status=GoalStatus.STATUS_ABORTED,
            result=SimpleNamespace(error_code=SimpleNamespace(val=-1)),
        ),
        SimpleNamespace(
            status=GoalStatus.STATUS_SUCCEEDED,
            result=SimpleNamespace(error_code=SimpleNamespace(val=1)),
        ),
    ]

    class GoalHandle:
        accepted = True

        def get_result_async(self):
            return ImmediateFuture(results.pop(0))

    sent_goals = []

    class ActionClient:
        _action_name = "move_action"

        @staticmethod
        def server_is_ready():
            return True

        @staticmethod
        def send_goal_async(*, goal, feedback_callback):
            del feedback_callback
            sent_goals.append(goal)
            return ImmediateFuture(GoalHandle())

    joint_states = iter(["joint-state-1", "joint-state-2"])
    updates = []
    node = SimpleNamespace(
        get_clock=lambda: SimpleNamespace(
            now=lambda: SimpleNamespace(to_msg=lambda: "stamp")
        ),
        get_logger=lambda: SimpleNamespace(warn=lambda _message: None),
    )
    goal = SimpleNamespace(
        request=SimpleNamespace(
            start_state=SimpleNamespace(joint_state=None),
            workspace_parameters=SimpleNamespace(
                header=SimpleNamespace(stamp=None)
            ),
        )
    )
    monkeypatch.setattr(moveit_plan_retry, "plan_retry_attempts", lambda: 1)

    retry = moveit_plan_retry.MoveGroupActionRetry(
        node=node,
        action_client=ActionClient(),
        current_joint_state=lambda: next(joint_states),
        publish_state=updates.append,
    )
    retry.start(goal)

    assert [
        sent.request.start_state.joint_state for sent in sent_goals
    ] == ["joint-state-1", "joint-state-2"]
    assert updates[-1].succeeded is True
    assert goal.request.start_state.joint_state is None
