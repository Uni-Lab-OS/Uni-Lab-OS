"""把宿主节点（HostNode）的 ROS 关节反馈接到 Edge 遥测桥。"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from typing import Any

from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import JointState

from unilabos.device_mesh.joint_state_projector import (
    JointStateOwner,
    JointStateProjector,
)


class HostJointStateProjection:
    """宿主节点（HostNode）拥有的 ROS 订阅与无状态网络适配器。"""

    def __init__(
        self,
        host_node: Any,
        owners: Iterable[JointStateOwner],
        bridges: Sequence[Any],
    ) -> None:
        """在既有宿主节点上创建订阅与排水定时器，不创建 ROS 节点或全局缓存。"""

        self._projector = JointStateProjector(tuple(owners))
        self._bridges = tuple(bridges)
        self.subscription = host_node.create_subscription(
            JointState,
            "/joint_states",
            self._on_joint_state,
            qos_profile_sensor_data,
            callback_group=host_node.callback_group,
        )
        self.timer = host_node.create_timer(
            0.025,
            self._drain,
            callback_group=host_node.callback_group,
        )

    @property
    def projector(self) -> JointStateProjector:
        """返回当前宿主节点实例拥有的冻结投影器。"""

        return self._projector

    def _on_joint_state(self, message: JointState) -> None:
        stamp = message.header.stamp
        observed = float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0
        self._projector.ingest(
            message.name,
            message.position,
            observed_epoch_s=observed if observed > 0 else time.time(),
        )

    def _drain(self) -> None:
        for frame in self._projector.drain():
            for bridge in self._bridges:
                publish = getattr(bridge, "publish_joint_state", None)
                if not callable(publish):
                    continue
                publish(
                    frame.device_id,
                    frame.joint_states,
                    boot_id=frame.boot_id,
                    sequence=frame.sequence,
                    observed_epoch_s=frame.observed_epoch_s,
                    topology_digest=frame.topology_digest,
                    stale_after_s=frame.stale_after_s,
                )


def install_host_joint_state_projection(
    host_node: Any,
    owners: Iterable[JointStateOwner],
    bridges: Sequence[Any],
) -> HostJointStateProjection | None:
    """仅在存在 exact 关节归属时给宿主节点安装投影链路。"""

    frozen = tuple(owners)
    return HostJointStateProjection(host_node, frozen, bridges) if frozen else None


__all__ = ["HostJointStateProjection", "install_host_joint_state_projection"]
