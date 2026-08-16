"""装配运动模型、前端渲染投影与关节归属的单一启动入口。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from unilabos.device_mesh.joint_state_projector import JointStateOwner
from unilabos.device_mesh.package_moveit_model import (
    collect_package_joint_state_owners,
    get_package_render_model,
)


def compile_kinematic_runtime(
    graph_nodes: Mapping[str, Any],
    registry_devices: Mapping[str, Any],
    resource_tree_set: Any,
) -> tuple[JointStateOwner, ...]:
    """一次编译执行、渲染和遥测共同使用的运动学实例身份。

    参数：物理图节点、设备注册表与启动资源树。返回：冻结关节归属。异常：模型
    Provider、拓扑或资源映射不一致时关闭启动。该函数只写渲染投影，不授予动作
    执行权或手动独占（Exclusive）。
    """

    owners = collect_package_joint_state_owners(graph_nodes, registry_devices)
    owner_by_device = {owner.device_id: owner for owner in owners}
    for resource_node in resource_tree_set.all_nodes:
        resource = resource_node.res_content
        owner = owner_by_device.get(str(resource.id))
        parent = getattr(resource, "parent", None)
        parent_model = get_package_render_model(str(getattr(parent, "id", "")))
        if owner is None and (
            parent_model is None or parent_model.mount_link is None
        ):
            continue
        config = dict(resource.config or {})
        raw_rendering = config.get("rendering")
        if raw_rendering is not None and not isinstance(raw_rendering, Mapping):
            raise TypeError("device.config.rendering 必须是 Mapping")
        rendering = dict(raw_rendering or {})
        if owner is not None:
            rendering["model"] = {
                "path": f"/api/v1/kinematic-models/{owner.device_id}.urdf",
                "format": "urdf",
                "position": [0.0, 0.0, 0.0],
                "rotation": [0.0, 0.0, 0.0],
            }
            rendering["kinematics"] = {
                "device_id": owner.device_id,
                "topology_digest": owner.topology_digest,
                "qualified_joint_names": list(owner.qualified_joint_names),
                "stale_after_s": owner.stale_after_s,
            }
        if parent_model is not None and parent_model.mount_link is not None:
            rendering["parent_link"] = parent_model.mount_link
            graph_node = graph_nodes.get(str(resource.id))
            if not isinstance(graph_node, dict):
                raise TypeError("运动学物理图节点必须是可写 dict")
            graph_node["_kinematic_parent_link"] = parent_model.mount_link
        config["rendering"] = rendering
        resource.config = config
    return owners


__all__ = ["compile_kinematic_runtime"]
