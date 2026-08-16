"""设备网格（Device Mesh）模型编译与 ROS Launch 生命周期深模块。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from unilabos.device_mesh.joint_state_projector import JointStateOwner
from unilabos.resources.graphio import dict_from_graph
from unilabos.utils.banner_print import print_status


@dataclass(slots=True)
class DeviceMeshRuntime:
    """一次 OS 启动所需的运动学投影和可选 ROS Launch 所有者。"""

    joint_state_owners: tuple[JointStateOwner, ...]
    resource_model: dict[str, Any]
    _visualization: Any | None = None

    @property
    def launches_ros(self) -> bool:
        """返回本运行时是否拥有阻塞式 ROS Launch。"""

        return self._visualization is not None

    def start(self) -> None:
        """启动已准备的 ROS Launch；未选择运动/显示时失败关闭调用。"""

        if self._visualization is None:
            raise RuntimeError("设备网格运行时没有待启动的 ROS Launch")
        self._visualization.start()


def prepare_device_mesh_runtime(
    *,
    physical_setup_graph: Any,
    registry_devices: Any,
    resource_tree_set: Any,
    visual: str,
    process_role: str,
) -> DeviceMeshRuntime:
    """编译包模型、关节归属并准备可选 MoveIt/RViz 启动。

    参数：物理图、设备注册表、启动资源树、显示模式和进程角色。返回：统一运行
    时对象。异常：模型合同、MoveIt 必选依赖或 ROS 环境错误失败关闭。安全：工作
    区后端（Workspace Backend）只编译可服务的模型投影，不启动 ROS 或驱动。
    """

    from unilabos.device_mesh.kinematic_runtime import compile_kinematic_runtime
    from unilabos.device_mesh.motion_runtime_plan import plan_motion_runtime

    graph_nodes = dict_from_graph(physical_setup_graph)
    owners = compile_kinematic_runtime(
        graph_nodes,
        registry_devices,
        resource_tree_set,
    )
    plan = plan_motion_runtime(graph_nodes, visual=visual)
    if process_role == "workspace_backend" or not plan.ros_launch_required:
        return DeviceMeshRuntime(owners, {})

    from unilabos.device_mesh.resource_visalization import ResourceVisualization

    visualization = ResourceVisualization(
        graph_nodes,
        [node.res_content for node in resource_tree_set.all_nodes],
        enable_rviz=plan.enable_rviz,
        required_moveit_device_ids=plan.moveit_device_ids,
    )
    try:
        visualization.prepare()
    except OSError as error:
        if plan.motion_runtime_required:
            raise RuntimeError(
                "MoveIt 是物理图明确选择的执行依赖，ROS 环境缺失时必须关闭启动"
            ) from error
        if "AMENT_PREFIX_PATH" not in str(error):
            raise
        print_status(f"ROS 2环境未正确设置，跳过3D可视化: {error}", "warning")
        return DeviceMeshRuntime(owners, {})
    return DeviceMeshRuntime(
        owners,
        dict(visualization.resource_model),
        visualization,
    )


__all__ = ["DeviceMeshRuntime", "prepare_device_mesh_runtime"]
