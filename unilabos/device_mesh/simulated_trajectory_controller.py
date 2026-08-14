"""macOS MoveIt 仿真使用的最小 FollowJointTrajectory 执行端。"""

from __future__ import annotations

import argparse
import json
import math
import threading
from pathlib import Path
from typing import Any

import rclpy
from control_msgs.action import FollowJointTrajectory
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from sensor_msgs.msg import JointState

try:
    from unilabos.device_mesh.simulated_trajectory_timing import play_trajectory
except ModuleNotFoundError:  # 兼容 launch 以脚本路径直接启动控制器。
    from simulated_trajectory_timing import play_trajectory


def load_controller_specs(path: Path) -> tuple[dict[str, Any], ...]:
    """读取 OS 生成的受限 controller 配置，不接受任意 ROS 参数。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    controllers = payload.get("controllers") if isinstance(payload, dict) else None
    if not isinstance(controllers, list) or not controllers:
        raise ValueError("仿真控制器配置缺少 controllers")
    normalized: list[dict[str, Any]] = []
    owned_joints: set[str] = set()
    for item in controllers:
        if not isinstance(item, dict):
            raise TypeError("仿真 controller 必须是对象")
        name = str(item.get("name") or "").strip()
        action = str(item.get("action") or "").strip()
        joints = tuple(str(value).strip() for value in item.get("joints", ()))
        if (
            not name
            or not action.startswith("/")
            or not joints
            or any(not value for value in joints)
            or owned_joints.intersection(joints)
        ):
            raise ValueError("仿真 controller 身份、Action 或关节归属无效")
        owned_joints.update(joints)
        normalized.append({"name": name, "action": action, "joints": joints})
    return tuple(normalized)


class SimulatedTrajectoryController(Node):
    """执行 MoveIt 规划轨迹并持续发布唯一的仿真关节观测。"""

    def __init__(self, specs: tuple[dict[str, Any], ...]) -> None:
        super().__init__("unilab_simulated_trajectory_controller")
        self._callback_group = ReentrantCallbackGroup()
        self._lock = threading.RLock()
        self._joint_names = tuple(
            joint for spec in specs for joint in spec["joints"]
        )
        self._positions = {joint: 0.0 for joint in self._joint_names}
        self._publisher = self.create_publisher(JointState, "/joint_states", 10)
        self._timer = self.create_timer(
            0.02, self._publish_joint_state, callback_group=self._callback_group
        )
        self._servers = [
            ActionServer(
                self,
                FollowJointTrajectory,
                spec["action"],
                execute_callback=lambda handle, spec=spec: self._execute(spec, handle),
                goal_callback=lambda request, spec=spec: self._accept_goal(spec, request),
                cancel_callback=lambda _handle: CancelResponse.ACCEPT,
                callback_group=self._callback_group,
            )
            for spec in specs
        ]
        self.get_logger().info(
            "Uni-Lab macOS MoveIt simulation controller ready: "
            + ", ".join(str(spec["action"]) for spec in specs)
        )

    def _accept_goal(self, spec: dict[str, Any], request: Any) -> GoalResponse:
        trajectory = request.trajectory
        names = tuple(str(value) for value in trajectory.joint_names)
        if set(names) != set(spec["joints"]) or not trajectory.points:
            return GoalResponse.REJECT
        return (
            GoalResponse.ACCEPT
            if all(
                len(point.positions) == len(names)
                and all(math.isfinite(float(value)) for value in point.positions)
                for point in trajectory.points
            )
            else GoalResponse.REJECT
        )

    def _execute(self, spec: dict[str, Any], goal_handle: Any) -> Any:
        trajectory = goal_handle.request.trajectory
        names = tuple(str(value) for value in trajectory.joint_names)
        with self._lock:
            start_positions = tuple(self._positions[name] for name in names)
        completed = play_trajectory(
            points=(
                (
                    float(point.time_from_start.sec)
                    + float(point.time_from_start.nanosec) / 1_000_000_000.0,
                    tuple(float(value) for value in point.positions),
                )
                for point in trajectory.points
            ),
            initial_positions=start_positions,
            update=lambda values: self._set_positions(names, tuple(values)),
            is_cancel_requested=lambda: bool(goal_handle.is_cancel_requested),
        )
        if not completed:
            goal_handle.canceled()
            return FollowJointTrajectory.Result()
        goal_handle.succeed()
        result = FollowJointTrajectory.Result()
        result.error_code = FollowJointTrajectory.Result.SUCCESSFUL
        return result

    def _set_positions(self, names: tuple[str, ...], values: tuple[float, ...]) -> None:
        with self._lock:
            self._positions.update(zip(names, values, strict=True))

    def _publish_joint_state(self) -> None:
        message = JointState()
        message.header.stamp = self.get_clock().now().to_msg()
        message.name = list(self._joint_names)
        with self._lock:
            message.position = [self._positions[name] for name in self._joint_names]
        message.velocity = [0.0] * len(self._joint_names)
        self._publisher.publish(message)

    def destroy_node(self) -> bool:
        for server in self._servers:
            server.destroy()
        return super().destroy_node()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    arguments = parser.parse_args()
    specs = load_controller_specs(arguments.config.resolve())
    rclpy.init()
    node = SimulatedTrajectoryController(specs)
    executor = MultiThreadedExecutor(num_threads=max(2, len(specs) + 1))
    executor.add_node(node)
    try:
        executor.spin()
    finally:
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
