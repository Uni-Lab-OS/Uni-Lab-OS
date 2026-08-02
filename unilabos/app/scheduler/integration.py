"""Edge scheduler 与 unilab 主进程 / 云端 ws 链路的装配层（composition root）。

装配关系：

    云端 ──ws──▶ ws_client.MessageProcessor
                    │ workflow_start / workflow_cancel（整图下发，本模块注入 edge_scheduler）
                    ▼
                EdgeScheduler ──dispatch──▶ JobExecutionBackend ──send_goal──▶ HostNode
                    ▲                            │（注册进 HostNode.bridges 收执行回报）
                    └────── on_job_finished ─────┘
                    │
                    └ workflow_status 终态上报 ──▶ ws_client.send_message ──▶ 云端

main.py 在组装 bridges 时调用 ``setup_edge_scheduler``，把返回的 backend 追加进
bridges 列表即可（backend 的 ``publish_job_status`` 是 bridge 形状）。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from unilabos.app.scheduler.backend import JobExecutionBackend, create_edge_stack
from unilabos.app.scheduler.ordering import HttpSchedulerOrderer, StableLocalOrderer
from unilabos.app.scheduler.service import EdgeScheduler

logger = logging.getLogger(__name__)

# 进程内单例（主进程装配一次，ws_client/api 层共享）
_scheduler: EdgeScheduler | None = None
_backend: JobExecutionBackend | None = None
_inventory: Any | None = None
_outbox_worker: Any | None = None
_owns_inventory = False
_workflow_tasks: Any | None = None
_workflow_reconciler_attached = False


def get_edge_scheduler() -> EdgeScheduler | None:
    return _scheduler


def get_edge_backend() -> JobExecutionBackend | None:
    return _backend


def get_inventory_service() -> Any | None:
    return _inventory


def make_http_sync_sender() -> Any:
    """生产 outbox sender：批量 POST 云端 /edge/sync/events，返回 acked_sequence。

    复用 HTTPClient 的 remote_addr + Lab auth 会话；云端未部署该端点时请求会
    失败，OutboxWorker 按指数退避保留事件重试（不丢数据、自愈）。
    """
    from unilabos.app.web.client import http_client

    def send(events: Any) -> int:
        resp = http_client._session.post(
            f"{http_client.remote_addr}/edge/sync/events",
            json={"edge_id": events[0]["edge_id"], "events": events},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data") or body
        return int(data.get("acked_sequence") or 0)

    return send


def setup_edge_scheduler(
    ws_client: Any = None,
    ordering_url: str = "",
    ordering_algorithm: str = "WeightedCriticalPath",
    lab_id: str = "edge-lab",
    host_node_getter: Any = None,
    working_dir: str = "",
    inventory_resource_templates: Any = None,
    inventory_service: Any = None,
    workflow_tasks: Any = None,
    edge_id: str = "edge-default",
    sync_sender: Any = None,
    device_state_db_path: str = "",
    workflow_history_db_path: str = "",
) -> tuple[EdgeScheduler, JobExecutionBackend]:
    """装配 EdgeScheduler + 微后端，并接通云端 ws 链路（幂等）。

    Args:
        ws_client: WebSocketClient 实例。传入时：
            - 注入 message_processor.edge_scheduler（workflow_start/cancel 转发目标）
            - 注入 message_processor.inventory_service（inventory_command 执行目标）
            - 注册工作流终态上报（workflow_status 消息）
        ordering_url: uni-lab-scheduler 地址（空则本地稳定排序）
        working_dir: OS workspace；Inventory 固定使用其下的 ``inventory.db``。
        inventory_resource_templates: Registry 注入的只读 ResourceTemplate identities。
        inventory_service: workspace composition 已打开的唯一 InventoryService。
        workflow_tasks: 同一 composition 的 WorkflowService durable Task authority。
        sync_sender: outbox 上报 callable（events → acked_sequence）；
            传入时启动 OutboxWorker，不传则事件保留在 outbox（云端端点就绪后再挂）
        device_state_db_path: 设备状态 SQLite 路径（独立于仓储/工作流库；
            空则用 ULAB_DEVICE_STATE_DB，默认 ~/.unilabos/device_state.db，
            "off" 关闭落盘）。微后端经 publish_device_status bridge 收
            HostNode 属性更新并串行写入。
        workflow_history_db_path: 工作流执行历史 SQLite 路径（第三个独立库；
            空则用 ULAB_WORKFLOW_HISTORY_DB，默认
            ~/.unilabos/workflow_history.db，"off" 关闭）。启动时把上一
            世代残留的非终态 run 标记 interrupted。
    Returns:
        (scheduler, backend)；backend 需由调用方追加进 HostNode bridges 列表。
    """
    global _scheduler, _backend, _inventory, _outbox_worker, _owns_inventory
    global _workflow_tasks, _workflow_reconciler_attached
    if _scheduler is not None and _backend is not None:
        if workflow_tasks is not None:
            from unilabos.workflow.composition import (
                configure_device_action_runtime,
            )

            _workflow_tasks = workflow_tasks
            configure_device_action_runtime(
                workflow_tasks,
                _scheduler,
                _backend,
            )
        logger.warning(
            "[EdgeSchedulerIntegration] already set up, reusing existing stack"
        )
        return _scheduler, _backend

    # 时长预估器：orderer（排序 duration）与 scheduler（泳道图/历史样本）共享
    from unilabos.app.scheduler.estimation import DurationEstimator

    estimator = DurationEstimator(
        mode=os.environ.get("ULAB_ESTIMATE_MODE", "auto").strip() or "auto",
        default_s=float(os.environ.get("ULAB_ESTIMATE_DEFAULT_S", "60")),
    )

    if ordering_url:
        orderer: Any = HttpSchedulerOrderer(
            base_url=ordering_url,
            lab_id=lab_id,
            algorithm=ordering_algorithm,
            estimator=estimator,
        )
    else:
        orderer = StableLocalOrderer()

    if inventory_service is not None and working_dir:
        raise ValueError("inventory_service and working_dir are mutually exclusive")
    inventory = inventory_service
    if working_dir:
        from unilabos.app.scheduler.inventory.service import InventoryService
        from unilabos.app.scheduler.monitor import monitor_bus as _monitor_bus

        inventory = InventoryService.open(
            working_dir=working_dir,
            resource_templates=inventory_resource_templates or {},
            edge_id=edge_id,
            lab_id=lab_id,
            monitor=_monitor_bus,
        )
        _owns_inventory = True
    if inventory is not None:
        _inventory = inventory
        # 本地优先：仅显式传入 sync_sender 时才启动云端同步；纯本地模式下
        # 领域事件留在 sync_outbox（SQLite），后续接入云端时挂 worker 重放即可。
        if sync_sender is not None:
            from unilabos.app.scheduler.inventory.sync import OutboxWorker

            _outbox_worker = OutboxWorker(inventory, sync_sender)
            _outbox_worker.start()
        else:
            logger.info(
                "[EdgeSchedulerIntegration] cloud sync disabled (local-only mode); "
                "outbox events retained in workspace inventory.db",
            )

    from unilabos.app.scheduler.monitor import monitor_bus

    # 设备状态存储：独立 SQLite（与仓储/工作流库分开），归微后端管
    device_state_store = None
    state_db = device_state_db_path or os.environ.get(
        "ULAB_DEVICE_STATE_DB", "~/.unilabos/device_state.db"
    )
    state_db = state_db.strip()
    if state_db and state_db.lower() != "off":
        from unilabos.app.scheduler.device_state import DeviceStateStore

        state_db = os.path.abspath(os.path.expanduser(state_db))
        os.makedirs(os.path.dirname(state_db) or ".", exist_ok=True)
        device_state_store = DeviceStateStore(state_db)
        logger.info("[EdgeSchedulerIntegration] device state store: %s", state_db)

    # 工作流执行历史：第三个独立 SQLite（低频 append，审计/回放/跨重启）
    history_store = None
    history_db = workflow_history_db_path or os.environ.get(
        "ULAB_WORKFLOW_HISTORY_DB", "~/.unilabos/workflow_history.db"
    )
    history_db = history_db.strip()
    if history_db and history_db.lower() != "off":
        from unilabos.app.scheduler.history import WorkflowHistoryStore

        history_db = os.path.abspath(os.path.expanduser(history_db))
        os.makedirs(os.path.dirname(history_db) or ".", exist_ok=True)
        history_store = WorkflowHistoryStore(history_db)
        interrupted = history_store.mark_interrupted()
        if interrupted:
            logger.info(
                "[EdgeSchedulerIntegration] marked %d stale workflow runs interrupted",
                interrupted,
            )
        logger.info("[EdgeSchedulerIntegration] workflow history store: %s", history_db)

    shared_device_manager = (
        getattr(ws_client, "device_manager", None) if ws_client is not None else None
    )
    scheduler, backend = create_edge_stack(
        orderer=orderer,
        device_manager=shared_device_manager,
        host_node_getter=host_node_getter,
        inventory=inventory,
        estimator=estimator,
        monitor=monitor_bus,
        device_state_store=device_state_store,
        history=history_store,
        workflow_tasks=workflow_tasks,
    )
    _scheduler, _backend = scheduler, backend

    if workflow_tasks is not None:
        from unilabos.workflow.composition import configure_device_action_runtime

        _workflow_tasks = workflow_tasks
        configure_device_action_runtime(workflow_tasks, scheduler, backend)

    if workflow_tasks is not None and inventory is not None:
        from unilabos.workflow.composition import configure_workflow_task_reconciler

        _workflow_tasks = workflow_tasks
        _workflow_reconciler_attached = configure_workflow_task_reconciler(
            workflow_tasks,
            scheduler.reconcile_task_material_state,
            scheduler.can_dispatch_task_materials,
        )
        scheduler.reconcile_workflow_task_materials()

    if ws_client is not None:
        _wire_ws_client(scheduler, ws_client)

    logger.info(
        "[EdgeSchedulerIntegration] edge scheduler ready (ordering=%s)",
        ordering_url or "local-stable",
    )
    return scheduler, backend


def _wire_ws_client(scheduler: EdgeScheduler, ws_client: Any) -> None:
    """把调度器接到 ws 链路：收整图消息 + 回报工作流终态。"""
    message_processor = getattr(ws_client, "message_processor", None)
    if message_processor is not None:
        message_processor.edge_scheduler = scheduler
        if _inventory is not None:
            message_processor.inventory_service = _inventory
    else:
        logger.warning("[EdgeSchedulerIntegration] ws_client has no message_processor")

    def _report_workflow_state(workflow_id: str, state: str) -> None:
        run_snapshot = scheduler.workflow_snapshot(workflow_id) or {}
        message = {
            "action": "workflow_status",
            "data": {
                "workflow_id": workflow_id,
                "task_id": run_snapshot.get("task_id", workflow_id),
                "status": state,
                "timestamp": time.time(),
            },
        }
        try:
            if message_processor is not None:
                message_processor.send_message(message)
        except Exception:
            logger.exception("[EdgeSchedulerIntegration] workflow_status report failed")

    scheduler.set_workflow_state_listener(_report_workflow_state)


def reset_for_test() -> None:
    """测试用：清掉进程内单例。"""
    global _scheduler, _backend, _inventory, _outbox_worker, _owns_inventory
    global _workflow_tasks, _workflow_reconciler_attached
    if _workflow_tasks is not None:
        from unilabos.workflow.composition import configure_device_action_runtime

        configure_device_action_runtime(_workflow_tasks, None, None)
    if _workflow_reconciler_attached and _workflow_tasks is not None:
        from unilabos.workflow.composition import configure_workflow_task_reconciler

        configure_workflow_task_reconciler(_workflow_tasks, None)
    if _backend is not None:
        _backend.stop()
    if _outbox_worker is not None:
        _outbox_worker.stop()
    if _inventory is not None:
        _inventory.set_change_listener(None)
        if _owns_inventory:
            _inventory.close()
    _scheduler = None
    _backend = None
    _inventory = None
    _outbox_worker = None
    _owns_inventory = False
    _workflow_tasks = None
    _workflow_reconciler_attached = False


__all__ = [
    "get_edge_backend",
    "get_edge_scheduler",
    "get_inventory_service",
    "make_http_sync_sender",
    "reset_for_test",
    "setup_edge_scheduler",
]
