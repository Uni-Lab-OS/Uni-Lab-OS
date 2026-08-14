"""MoveIt2 的订阅回调不得观察到半初始化对象。"""

from __future__ import annotations

from sensor_msgs.msg import JointState

from unilabos.devices.ros_dev import moveit2


class _Logger:
    def warn(self, _message: str) -> None:
        pass


class _Node:
    """在 create_subscription 内同步投递首帧，放大真实并发竞态。"""

    def create_subscription(self, **kwargs):
        message = JointState()
        message.name = ["robot_joint_1"]
        message.position = [0.0]
        kwargs["callback"](message)
        return object()

    def create_client(self, **_kwargs):
        return object()

    def create_publisher(self, *_args, **_kwargs):
        return object()

    def create_rate(self, _frequency: float):
        return object()

    def get_logger(self) -> _Logger:
        return _Logger()


def test_joint_state_fields_exist_before_subscription_can_fire(monkeypatch) -> None:
    """ROS executor 即时投递首帧时，构造过程也不能抛 AttributeError。"""

    monkeypatch.setattr(moveit2, "ActionClient", lambda **_kwargs: object())

    client = moveit2.MoveIt2(
        node=_Node(),
        joint_names=["robot_joint_1"],
        base_link_name="robot_base",
        end_effector_name="robot_tool",
        group_name="robot_arm",
    )

    assert client.joint_state is not None
