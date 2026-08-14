"""设备执行未知在本地调度链路中的保真与失败关闭合同。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.workflow.test_f05_task_scheduler_bridge import (
    JOB_UUID,
    TASK_UUID,
    _bridge,
    _seed_task,
)
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.store import StoreConflict, WorkflowStore


def test_execution_unknown_is_persisted_without_finishing_or_unlocking(
    tmp_path: Path,
) -> None:
    """设备未知结果必须保留在途作业、设备互斥和人工处理事实。

    参数：``tmp_path`` 是隔离工作流 SQLite 目录。返回：无；通过公开调度完成
    回调断言 ``execution_unknown`` 未被折叠为 ``failed``，任务仍在运行且清理
    需要人工处理，重复投递保持幂等。异常：任何投影或锁释放错误使测试失败。
    """

    store = WorkflowStore(tmp_path / "workflow_history.db")
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = _bridge(store, scheduler)
    try:
        bridge.submit(_seed_task(store, with_material=False))
        unknown_result = {
            "success": False,
            "state": "execution_unknown",
            "message": "机械臂与导轨的物理结果无法确认",
        }

        first = scheduler.on_job_finished(
            JOB_UUID,
            False,
            unknown_result,
        )
        replay = scheduler.on_job_finished(
            JOB_UUID,
            False,
            unknown_result,
        )
        with pytest.raises(StoreConflict, match="尚未派发"):
            scheduler.on_job_finished(
                JOB_UUID,
                True,
                {"success": True, "message": "迟到成功"},
            )

        task = store.get_task(TASK_UUID)
        job = store.get_job(JOB_UUID)
        runtime_events = store.list_task_runtime_events(
            TASK_UUID,
            after_sequence=0,
            limit=20,
        )["items"]

        assert first["execution_state"] == "execution_unknown"
        assert replay["execution_state"] == "execution_unknown"
        assert task["status"] == "running"
        assert task["cleanup_status"] == "requires_attention"
        assert task["attention_reason"] == f"job_execution_unknown:{JOB_UUID}"
        assert job["status"] == "execution_unknown"
        assert job["uncertainty_reason"] == "机械臂与导轨的物理结果无法确认"
        assert job["return_info"] == unknown_result
        assert JOB_UUID in scheduler.snapshot()["inflight_jobs"]
        assert [event["kind"] for event in runtime_events].count(
            "uncertainty_opened"
        ) == 1
    finally:
        bridge.close()
        store.close()
