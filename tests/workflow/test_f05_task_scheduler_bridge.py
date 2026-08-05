"""F05.3-C 工作流任务调度桥（TaskSchedulerBridge）的纵向行为合同。"""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.inventory.domain import InsufficientStock
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11000000-0000-4000-8000-000000000001"
TASK_UUID = "21000000-0000-4000-8000-000000000001"
NODE_UUID = "31000000-0000-4000-8000-000000000001"
JOB_UUID = "41000000-0000-4000-8000-000000000001"
MATERIAL_UUID = "51000000-0000-4000-8000-000000000001"
_CREATED_AT = "2026-08-05T00:00:00Z"


class _ToggleInventory:
    """模拟可由补料改变结果的本地库存权威（Inventory Authority）。"""

    def __init__(self, *, available: bool) -> None:
        """设置物料可用性。

        参数：``available`` 表示整任务物料是否可以一次预留。返回无；不访问真实
        SQLite。``reserve_calls`` 记录准入重试（AdmissionRetry）的同一稳定身份。
        """

        self.available = available
        self.reserve_calls: list[tuple[str, dict[str, Any]]] = []

    def reserve_workflow(
        self,
        workflow_uuid: str,
        requirements: dict[str, Any],
    ) -> None:
        """模拟遗留整图物料预留。

        参数：``workflow_uuid`` 是工作流任务稳定身份，``requirements`` 是按节点
        汇总的物料需求。返回无；不可用时抛 ``InsufficientStock``。
        """

        self.reserve_calls.append((workflow_uuid, requirements))
        if not self.available:
            raise InsufficientStock("测试物料不足")

    def consume_reservation(self, workflow_uuid: str, node_uuid: str) -> None:
        """模拟派发前消费预留；参数是任务与节点身份，返回无。"""

    def quarantine_reservation(self, workflow_uuid: str, node_uuid: str) -> None:
        """模拟失败隔离；参数是任务与节点身份，返回无。"""

    def release_workflow(self, workflow_uuid: str, *, reason: str) -> None:
        """模拟终态释放；参数是任务身份和释放原因，返回无。"""


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[WorkflowStore]:
    """创建隔离工作流存储（WorkflowStore）。

    参数：``tmp_path`` 是 pytest 临时目录。产生：本测试唯一任务写权威；结束时
    关闭数据库连接。
    """

    # ``opened_store`` 是任务与作业标准事实的唯一持久化位置。
    opened_store = WorkflowStore(tmp_path / "workflow_history.db")
    try:
        yield opened_store
    finally:
        opened_store.close()


def _seed_task(store: WorkflowStore, *, with_material: bool) -> dict[str, Any]:
    """持久化一个带冻结执行计划（ExecutionPlan）的待处理任务。

    参数：``store`` 是工作流写权威；``with_material`` 决定计划是否包含遗留短期
    物料需求。返回：标准工作流任务投影。异常：数据库写入错误原样传播。
    """

    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="F05.3-C 调度桥",
        tags=[],
        description=None,
        meta_data={},
    )
    # ``material_requirements`` 是短期交给高靖库存预留路径的冻结需求，不创建第二
    # 库存权威（Inventory Authority）。
    material_requirements = (
        [{"instance_uuid": MATERIAL_UUID}] if with_material else []
    )
    execution_plan = {
        "version": 1,
        "run_mode": "normal",
        "target_node_uuid": None,
        "nodes": [
            {
                "uuid": NODE_UUID,
                "kind": "device_action",
                "device_id": "reactor-a",
                "action_name": "distribute",
                "action_type": "UniLabJsonCommand",
                "param": {},
                "param_schema": {"type": "object", "properties": {}},
                "material_requirements": material_requirements,
            }
        ],
        "handles": [],
        "edges": [],
    }
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workflow_task(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_uuid, status, workflow_snapshot,
                execution_plan, run_mode, target_node_uuid, control_status,
                cleanup_status, trace_context, input, output, error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, 'pending', '{}', ?,
                      'normal', NULL, 'active', 'none', '{}', '{}', '{}', '[]')
            """,
            (
                TASK_UUID,
                _CREATED_AT,
                _CREATED_AT,
                WORKFLOW_UUID,
                json.dumps(execution_plan),
            ),
        )
        connection.execute(
            """
            INSERT INTO workflow_node_job(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_task_uuid, workflow_node_uuid,
                feedback_sequence, topological_index, executor_kind,
                execution_policy, execution_timeout_seconds, status, attempt,
                param, feedback_data, return_info, control_data, error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, 0, 0,
                      'device_action', '{}', 0, 'pending', 1, '{}', '{}',
                      '{}', '{}', '[]')
            """,
            (JOB_UUID, _CREATED_AT, _CREATED_AT, TASK_UUID, NODE_UUID),
        )
    return store.get_task(TASK_UUID)


def _bridge(store: WorkflowStore, scheduler: EdgeScheduler) -> Any:
    """构造待测工作流任务调度桥（TaskSchedulerBridge）。

    参数：``store`` 是标准任务写权威，``scheduler`` 是既有本地调度器。返回：
    只绑定这两个权威的桥实例。异常：RED 阶段模块不存在时保留导入错误。
    """

    # ``bridge_module`` 是本轮新增的唯一生产模块接缝。
    bridge_module = importlib.import_module("unilabos.workflow.task_scheduler_bridge")
    return bridge_module.TaskSchedulerBridge(store, scheduler=scheduler)


def test_persisted_task_compiles_and_dispatches_with_stable_identities(
    store: WorkflowStore,
) -> None:
    """已持久化任务必须复用稳定任务/作业身份并先投影再物理派发。

    参数：``store`` 是隔离任务权威。返回无；断言设备命令沿用既有 UUID，且命令
    被记录时标准任务已为 ``running``、作业已为 ``dispatched``。
    """

    task = _seed_task(store, with_material=False)
    observed_states: list[tuple[str, str]] = []

    class _ObservingDispatcher(RecordingDispatcher):
        """在执行适配器边界观察标准任务/作业状态。"""

        def dispatch(self, payload: Any) -> None:
            """记录派发瞬间状态；参数是设备命令，返回无。"""

            observed_states.append(
                (store.get_task(TASK_UUID)["status"], store.get_job(JOB_UUID)["status"])
            )
            super().dispatch(payload)

    dispatcher = _ObservingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = _bridge(store, scheduler)
    try:
        aggregate = bridge.submit(task)
    finally:
        bridge.close()

    assert observed_states == [("running", "dispatched")]
    assert dispatcher.dispatched[0]["task_id"] == TASK_UUID
    assert dispatcher.dispatched[0]["job_id"] == JOB_UUID
    assert aggregate["task"]["status"] == "running"


def test_material_task_without_scheduler_inventory_fails_closed(
    store: WorkflowStore,
) -> None:
    """带物料需求但没有库存服务时必须在物理派发前关闭失败。

    参数：``store`` 是隔离任务权威。返回无；断言无设备命令且标准任务/作业仍为
    ``pending``。异常：桥必须抛稳定错误而不是让旧调度器无预留继续执行。
    """

    task = _seed_task(store, with_material=True)
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = _bridge(store, scheduler)
    try:
        with pytest.raises(RuntimeError, match="库存"):
            bridge.submit(task)
    finally:
        bridge.close()

    assert dispatcher.dispatched == []
    assert store.get_task(TASK_UUID)["status"] == "pending"
    assert store.get_job(JOB_UUID)["status"] == "pending"


def test_insufficient_stock_stays_pending_and_reuses_scheduler_inventory(
    store: WorkflowStore,
) -> None:
    """物料不足只形成内部等料，外部标准事实保持待处理。

    参数：``store`` 是隔离任务权威。返回无；断言桥实际调用调度器持有的同一库存
    服务且不派发。
    """

    task = _seed_task(store, with_material=True)
    inventory = _ToggleInventory(available=False)
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)
    bridge = _bridge(store, scheduler)
    try:
        aggregate = bridge.submit(task)
    finally:
        bridge.close()

    assert [call[0] for call in inventory.reserve_calls] == [TASK_UUID, TASK_UUID]
    assert dispatcher.dispatched == []
    assert aggregate["task"]["status"] == "pending"
    assert {job["status"] for job in aggregate["jobs"]} == {"pending"}


def test_admission_retry_keeps_task_and_job_identities(store: WorkflowStore) -> None:
    """补料后的准入重试（AdmissionRetry）必须复用既有任务与作业。

    参数：``store`` 是隔离任务权威。返回无；断言通过调度器重排恢复派发，数据库
    没有新增身份。
    """

    task = _seed_task(store, with_material=True)
    inventory = _ToggleInventory(available=False)
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)
    bridge = _bridge(store, scheduler)
    try:
        bridge.submit(task)
        inventory.available = True
        aggregate = bridge.retry_admission(TASK_UUID)
    finally:
        bridge.close()

    assert aggregate["task"]["uuid"] == TASK_UUID
    assert [job["uuid"] for job in aggregate["jobs"]] == [JOB_UUID]
    assert dispatcher.dispatched[0]["job_id"] == JOB_UUID
    assert len(store.list_jobs(TASK_UUID)) == 1


def test_predispatch_projection_conflict_blocks_dispatcher(store: WorkflowStore) -> None:
    """派发前投影冲突不得越过执行适配器边界。

    参数：``store`` 是隔离任务权威。返回无；先制造作业终态冲突，再断言桥抛错且
    设备派发器没有收到命令。
    """

    task = _seed_task(store, with_material=False)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_node_job SET status = 'failed' WHERE uuid = ?",
            (JOB_UUID,),
        )
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = _bridge(store, scheduler)
    try:
        with pytest.raises(RuntimeError):
            bridge.submit(task)
    finally:
        bridge.close()

    assert dispatcher.dispatched == []


def test_success_callback_projects_standard_succeeded_state(
    store: WorkflowStore,
) -> None:
    """明确成功回调必须聚合为标准 ``succeeded`` 终态。

    参数：``store`` 是隔离任务权威。返回无；断言旧调度器 ``success`` 被适配为
    工作流任务（WorkflowTask）与作业的规范成功状态和结果对象。
    """

    task = _seed_task(store, with_material=False)
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    bridge = _bridge(store, scheduler)
    try:
        bridge.submit(task)
        scheduler.on_job_finished(JOB_UUID, True, {"volume": 2.0})
    finally:
        bridge.close()

    assert store.get_task(TASK_UUID)["status"] == "succeeded"
    assert store.get_job(JOB_UUID)["status"] == "succeeded"
    assert store.get_job(JOB_UUID)["return_info"] == {"volume": 2.0}


def test_failure_callback_projects_standard_error_details(
    store: WorkflowStore,
) -> None:
    """明确失败回调必须写入标准 ``failed`` 与稳定错误详情。

    参数：``store`` 是隔离任务权威。返回无；断言失败不会被包装成成功，也不会
    丢失旧调度器的人工处理分类。
    """

    task = _seed_task(store, with_material=False)
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    bridge = _bridge(store, scheduler)
    try:
        bridge.submit(task)
        scheduler.on_job_finished(
            JOB_UUID,
            False,
            {"reason": "pump_error"},
            "operator_intervention",
        )
    finally:
        bridge.close()

    job = store.get_job(JOB_UUID)
    assert store.get_task(TASK_UUID)["status"] == "failed"
    assert job["status"] == "failed"
    assert job["return_info"] == {"reason": "pump_error"}
    assert job["error_info"] == [
        {
            "code": "legacy_edge_scheduler_action_failed",
            "message": "设备动作执行失败",
            "suc_type": "operator_intervention",
        }
    ]


def test_close_is_idempotent_and_unregisters_scheduler_listeners(
    store: WorkflowStore,
) -> None:
    """关闭桥必须幂等注销全部调度生命周期监听器。

    参数：``store`` 是隔离任务权威。返回无；断言重复关闭后不残留派发前或完成
    回调，避免下一轮组合重复投影。
    """

    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    bridge = _bridge(store, scheduler)

    assert len(scheduler._job_pre_dispatch_listeners) == 1
    assert len(scheduler._job_finished_listeners) == 1

    bridge.close()
    bridge.close()

    assert scheduler._job_pre_dispatch_listeners == []
    assert scheduler._job_finished_listeners == []
