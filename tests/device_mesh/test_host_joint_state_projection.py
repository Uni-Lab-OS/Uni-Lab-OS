"""宿主节点（HostNode）关节订阅到 Edge 遥测桥的接线测试。"""

from __future__ import annotations

import time

from sensor_msgs.msg import JointState

from unilabos.device_mesh.host_joint_state_projection import (
    HostJointStateProjection,
)
from unilabos.device_mesh.joint_state_projector import JointStateOwner


class _HostNode:
    callback_group = object()

    def __init__(self) -> None:
        self.subscription_callback = None
        self.timer_callback = None

    def create_subscription(self, _type, _topic, callback, _qos, **_kwargs):
        self.subscription_callback = callback
        return object()

    def create_timer(self, _interval, callback, **_kwargs):
        self.timer_callback = callback
        return object()


class _Bridge:
    def __init__(self) -> None:
        self.frames = []

    def publish_joint_state(self, device_id, joint_states, **identity) -> None:
        self.frames.append((device_id, dict(joint_states), identity))


def test_host_owned_subscription_preserves_projector_identity() -> None:
    """ROS 回调、完整帧与 Edge 发布参数必须保持同一帧身份。"""

    host = _HostNode()
    bridge = _Bridge()
    projection = HostJointStateProjection(
        host,
        (
            JointStateOwner(
                device_id="robot",
                topology_digest="a" * 64,
                qualified_joint_names=("robot_joint_1", "robot_joint_2"),
            ),
        ),
        (bridge,),
    )
    message = JointState()
    observed = int(time.time())
    message.header.stamp.sec = observed
    message.name = ["robot_joint_1", "robot_joint_2"]
    message.position = [0.1, 0.2]

    projection._on_joint_state(message)
    projection._drain()

    assert len(bridge.frames) == 1
    device_id, joints, identity = bridge.frames[0]
    assert device_id == "robot"
    assert joints == {"robot_joint_1": 0.1, "robot_joint_2": 0.2}
    assert identity["sequence"] == 1
    assert identity["observed_epoch_s"] == float(observed)
    assert identity["topology_digest"] == "a" * 64
    assert len(identity["boot_id"]) == 36
