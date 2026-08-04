"""设备单动作运行（DeviceActionRun）到旧调度器（EdgeScheduler）的桥接测试。"""

from __future__ import annotations

from typing import Any

import pytest

from unilabos.app.scheduler.dispatch import CallbackDispatcher, RecordingDispatcher
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.device_action_run_bridge import DeviceActionRunWorkflowSpecBridge
from unilabos.workflow.device_action_run_store import DeviceActionRunStore
from unilabos.workflow.store import WorkflowStore


DEVICE_A_UUID = "10000000-0000-4000-8000-000000000001"
DEVICE_B_UUID = "10000000-0000-4000-8000-000000000002"
PLATE_UUID = "10000000-0000-4000-8000-000000000003"
TASK_A_UUID = "20000000-0000-4000-8000-000000000001"
TASK_B_UUID = "20000000-0000-4000-8000-000000000002"
JOB_A_UUID = "30000000-0000-4000-8000-000000000001"
JOB_B_UUID = "30000000-0000-4000-8000-000000000002"
NODE_A_UUID = "40000000-0000-4000-8000-000000000001"
NODE_B_UUID = "40000000-0000-4000-8000-000000000002"


def test_bridge_reuses_standard_job_identity_and_writes_terminal_state(
    tmp_path: Any,
) -> None:
    """桥接派发必须沿用标准 Job UUID，并把终态结果写回标准 Task/Job。"""

    store = WorkflowStore(tmp_path / "workflow_history.db")
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(
        dispatcher=dispatcher,
        material_lock_resolver=_material_lock_resolver,
    )
    bridge = DeviceActionRunWorkflowSpecBridge(
        store,
        scheduler=scheduler,
        material_resolver=_material_resolver,
    )
    try:
        # ``aggregate`` 是设备单动作创建事务已经提交的标准 Task/Job 聚合。
        aggregate = _insert_run(
            store,
            task_uuid=TASK_A_UUID,
            job_uuid=JOB_A_UUID,
            node_uuid=NODE_A_UUID,
            device_material_uuid=DEVICE_A_UUID,
        )

        bridge.submit(aggregate)

        assert len(dispatcher.dispatched) == 1
        assert dispatcher.dispatched[0]["job_id"] == JOB_A_UUID
        assert dispatcher.dispatched[0]["task_id"] == TASK_A_UUID
        assert dispatcher.dispatched[0]["device_id"] == "device-a"
        assert store.get_task(TASK_A_UUID)["status"] == "running"
        assert store.get_job(JOB_A_UUID)["status"] == "dispatched"

        scheduler.on_job_finished(
            JOB_A_UUID,
            True,
            {"completed": True},
        )

        task = store.get_task(TASK_A_UUID)
        job = store.get_job(JOB_A_UUID)
        assert task["status"] == "succeeded"
        assert task["cleanup_status"] == "settled"
        assert job["status"] == "succeeded"
        assert job["return_info"] == {"completed": True}
    finally:
        bridge.close()
        store.close()


def test_bridge_commits_standard_job_before_physical_dispatch(tmp_path: Any) -> None:
    """兼容桥必须先提交标准 Job 派发状态，再调用物理执行适配器。"""

    store = WorkflowStore(tmp_path / "workflow_history.db")
    # ``observed_states`` 记录执行适配器被调用当刻的持久事实，用于守住
    # “先提交派发意图、后产生物理效果”的最小崩溃窗口不变量。
    observed_states: list[tuple[str, str]] = []

    def observe_dispatch(_payload: dict[str, Any]) -> None:
        """在物理派发边界读取状态；参数是旧 job_start 载荷，返回无。"""

        observed_states.append(
            (
                store.get_task(TASK_A_UUID)["status"],
                store.get_job(JOB_A_UUID)["status"],
            )
        )

    scheduler = EdgeScheduler(
        dispatcher=CallbackDispatcher(observe_dispatch),
        material_lock_resolver=_material_lock_resolver,
    )
    bridge = DeviceActionRunWorkflowSpecBridge(
        store,
        scheduler=scheduler,
        material_resolver=_material_resolver,
    )
    try:
        aggregate = _insert_run(
            store,
            task_uuid=TASK_A_UUID,
            job_uuid=JOB_A_UUID,
            node_uuid=NODE_A_UUID,
            device_material_uuid=DEVICE_A_UUID,
        )

        bridge.submit(aggregate)

        assert observed_states == [("running", "dispatched")]
    finally:
        bridge.close()
        store.close()


def test_bridge_commit_failure_cannot_leave_a_dispatchable_scheduler_run(
    tmp_path: Any,
) -> None:
    """派发意图提交失败后，旧调度运行不得在后续重排中偷偷执行。"""

    store = WorkflowStore(tmp_path / "workflow_history.db")
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(
        dispatcher=dispatcher,
        material_lock_resolver=_material_lock_resolver,
    )
    bridge = DeviceActionRunWorkflowSpecBridge(
        store,
        scheduler=scheduler,
        material_resolver=_material_resolver,
    )

    def fail_dispatch_commit(*, task_uuid: str, job_uuid: str) -> None:
        """模拟标准状态事务失败；两个参数是待提交的 Task/Job 身份，返回无。"""

        del task_uuid, job_uuid
        raise RuntimeError("workflow database unavailable")

    # ``mark_dispatched`` 故障发生在执行适配器之前，用来验证失败清理不依赖重启。
    bridge._run_store.mark_dispatched = fail_dispatch_commit  # type: ignore[method-assign]
    try:
        aggregate = _insert_run(
            store,
            task_uuid=TASK_A_UUID,
            job_uuid=JOB_A_UUID,
            node_uuid=NODE_A_UUID,
            device_material_uuid=DEVICE_A_UUID,
        )

        with pytest.raises(RuntimeError, match="database unavailable"):
            bridge.submit(aggregate)

        assert dispatcher.dispatched == []
        assert scheduler.reschedule() == []
        assert scheduler.workflow_snapshot(TASK_A_UUID)["state"] == "canceled"
        assert store.get_job(JOB_A_UUID)["status"] == "pending"
    finally:
        bridge.close()
        store.close()


def test_bridge_keeps_second_job_pending_until_shared_material_lock_releases(
    tmp_path: Any,
) -> None:
    """不同设备引用同一物料时，第二个 Job 必须等待第一个执行锁释放。"""

    store = WorkflowStore(tmp_path / "workflow_history.db")
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(
        dispatcher=dispatcher,
        material_lock_resolver=_material_lock_resolver,
    )
    bridge = DeviceActionRunWorkflowSpecBridge(
        store,
        scheduler=scheduler,
        material_resolver=_material_resolver,
    )
    try:
        first = _insert_run(
            store,
            task_uuid=TASK_A_UUID,
            job_uuid=JOB_A_UUID,
            node_uuid=NODE_A_UUID,
            device_material_uuid=DEVICE_A_UUID,
        )
        second = _insert_run(
            store,
            task_uuid=TASK_B_UUID,
            job_uuid=JOB_B_UUID,
            node_uuid=NODE_B_UUID,
            device_material_uuid=DEVICE_B_UUID,
        )

        bridge.submit(first)
        bridge.submit(second)

        assert [item["job_id"] for item in dispatcher.dispatched] == [JOB_A_UUID]
        assert store.get_job(JOB_B_UUID)["status"] == "pending"

        scheduler.on_job_finished(JOB_A_UUID, True, {"completed": True})

        assert [item["job_id"] for item in dispatcher.dispatched] == [
            JOB_A_UUID,
            JOB_B_UUID,
        ]
        assert store.get_job(JOB_B_UUID)["status"] == "dispatched"
    finally:
        bridge.close()
        store.close()


def _insert_run(
    store: WorkflowStore,
    *,
    task_uuid: str,
    job_uuid: str,
    node_uuid: str,
    device_material_uuid: str,
) -> dict[str, Any]:
    """写入一个标准单节点 Task/Job 聚合并返回公共持久投影。

    参数：``store`` 是隔离工作流写模型；三个 UUID 固定执行身份；
    ``device_material_uuid`` 决定实际设备绑定。返回供桥接器消费的创建结果。
    """

    # ``node_snapshot`` 是 Backend-shaped 冻结节点，不携带 Edge 本地设备别名。
    node_snapshot = {
        "uuid": node_uuid,
        "workflow_node_template_uuid": "50000000-0000-4000-8000-000000000001",
        "material_uuid": device_material_uuid,
        "name": "转移物料",
        "type": "ILab",
        "param": {"plate": {"uuid": PLATE_UUID}},
        "action_name": "transfer",
        "action_type": "UniLabJsonCommand",
        "execution_policy": {},
    }
    task = {
        "uuid": task_uuid,
        "description": "桥接测试",
        "meta_data": {},
        "idempotency_key": f"bridge-{task_uuid}",
        "request_fingerprint": f"fingerprint-{task_uuid}",
        "workflow_snapshot": {
            "execution_kind": "ad_hoc_device_action",
            "material_uuid": device_material_uuid,
            "nodes": [node_snapshot],
            "node_templates": [],
        },
        "execution_plan": {
            "run_mode": "single_node",
            "target_node_uuid": node_uuid,
            "nodes": [
                {
                    "uuid": node_uuid,
                    "topological_index": 0,
                    "kind": "device_action",
                    "material_uuid": device_material_uuid,
                    "param": node_snapshot["param"],
                    "execution_policy": {},
                    "inputs": [],
                }
            ],
            "edges": [],
        },
        "target_node_uuid": node_uuid,
    }
    job = {
        "uuid": job_uuid,
        "workflow_node_uuid": node_uuid,
        "material_uuid": device_material_uuid,
        "execution_policy": {},
        "param": node_snapshot["param"],
    }
    return DeviceActionRunStore(store).create_or_reuse(
        task=task,
        job=job,
        idempotency_key=task["idempotency_key"],
        request_fingerprint=task["request_fingerprint"],
    )


def _material_resolver(material_uuid: str) -> dict[str, Any] | None:
    """按物料 UUID 返回设备绑定或业务物料摘要。

    参数：``material_uuid`` 是设备或动作参数中的稳定物料身份。返回设备物料时
    携带明确 ``edge_local_id``；普通业务物料只返回稳定身份。
    """

    if material_uuid == DEVICE_A_UUID:
        return {
            "uuid": material_uuid,
            "resource_template_uuid": "60000000-0000-4000-8000-000000000001",
            "meta_data": {"edge_local_id": "device-a"},
        }
    if material_uuid == DEVICE_B_UUID:
        return {
            "uuid": material_uuid,
            "resource_template_uuid": "60000000-0000-4000-8000-000000000001",
            "meta_data": {"edge_local_id": "device-b"},
        }
    if material_uuid == PLATE_UUID:
        return {
            "uuid": material_uuid,
            "resource_template_uuid": "60000000-0000-4000-8000-000000000002",
        }
    return None


def _material_lock_resolver(
    _device_id: str,
    _action_name: str,
    param: dict[str, Any],
) -> list[str]:
    """从测试最终参数提取需要互斥的孔板物料 UUID。

    参数：两个带下划线字段仅满足旧调度器设备动作解析接口；``param`` 是最终
    动作参数。返回需要建立动作物料锁（ActionMaterialLock）的稳定 UUID 列表。
    """

    return [str(param["plate"]["uuid"])]
