"""本地调试用嵌入式 Scheduler 微后端运行模块。"""

from __future__ import annotations

import os
from pathlib import Path

from unilabos.app.control_plane import (
    ControlPlaneRuntimeContext,
    ControlPlaneRuntimeHandle,
)
from unilabos.utils.banner_print import print_status


def start_embedded_scheduler_runtime(
    context: ControlPlaneRuntimeContext,
) -> ControlPlaneRuntimeHandle:
    """启动本地 Inventory、DAG Scheduler、历史存储和 HostLink。"""

    from unilabos.app.communication import (
        CommunicationClientFactory,
        get_communication_client,
    )
    from unilabos.app.runtime_storage import prepare_runtime_storage_session
    from unilabos.app.scheduler.host_network import setup_host_network_service
    from unilabos.app.scheduler.integration import (
        setup_edge_inventory,
        setup_edge_scheduler,
        shutdown_edge_services,
    )
    from unilabos.config.config import HostLinkConfig
    from unilabos.config.config import BasicConfig, EdgeControlConfig
    from unilabos.registry.template_snapshot import RegistryTemplateSnapshot

    arguments = context.arguments
    paths = prepare_runtime_storage_session(
        arguments,
        working_dir=context.working_dir,
    )
    communication_clients = []
    bridges = []
    legacy_client = None
    if "websocket" in arguments.get("app_bridges", ()):
        CommunicationClientFactory.reset_client()
        legacy_client = get_communication_client("websocket")
        legacy_client.start()
        communication_clients.append(legacy_client)
        bridges.append(legacy_client)

    inventory_db = os.path.abspath(os.path.expanduser(paths.inventory_db))
    setup_edge_inventory(
        inventory_db,
        ws_client=legacy_client,
        resource_tree_set=context.resource_tree_set,
        registry_snapshot=RegistryTemplateSnapshot.from_registry(context.registry),
        resource_graph_source_id=context.graph_source_id,
        material_shapes=context.material_shapes,
        material_model_catalog=context.material_model_catalog,
    )
    print_status(
        f"本地调试物料服务已启用 (SQLite WAL: {inventory_db})",
        "info",
    )

    execution_backend = None
    if BasicConfig.process_role == "workspace_backend":
        from unilabos.app.edge_control.local_authority import (
            LocalEdgeAuthorityStore,
            LocalEdgeControlAuthority,
        )

        execution_backend = LocalEdgeControlAuthority(
            LocalEdgeAuthorityStore(
                Path(paths.workflow_history_db).with_name("edge_authority.db")
            ),
            api_key=str(EdgeControlConfig.api_key or "").strip(),
        )

    _scheduler, execution_backend = setup_edge_scheduler(
        ws_client=legacy_client,
        inventory_db_path=inventory_db,
        device_state_db_path=paths.device_state_db,
        workflow_history_db_path=paths.workflow_history_db,
        execution_backend=execution_backend,
    )
    # The combined process still needs the in-process bridge attached to its
    # HostNode.  Workspace Backend dispatches over the durable loopback Edge
    # protocol, so attaching that authority as a ROS bridge would recreate the
    # lifecycle coupling this split removes.
    if BasicConfig.process_role != "workspace_backend":
        bridges.append(execution_backend)
    print_status(
        "本地调试 Scheduler 已启用 (DAG 调度 + 设备状态 + 工作流历史)",
        "info",
    )

    host_network = (
        setup_host_network_service()
        if BasicConfig.process_role != "workspace_backend"
        else None
    )
    if host_network is not None:
        print_status(
            f"本地调试微后端已监听 Slave 连接: "
            f"{HostLinkConfig.bind}:{host_network.server.port}",
            "info",
        )
    return ControlPlaneRuntimeHandle(
        bridges=tuple(bridges),
        communication_clients=tuple(communication_clients),
        shutdown_services=shutdown_edge_services,
    )


__all__ = ["start_embedded_scheduler_runtime"]
