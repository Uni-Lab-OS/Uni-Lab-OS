"""F05.3-B 任务运行投影（TaskRuntimeProjection）的行为合同测试。"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.store import StoreConflict, WorkflowStore

WORKFLOW_UUID = "10000000-0000-4000-8000-000000000001"
TASK_UUID = "20000000-0000-4000-8000-000000000001"
NODE_UUIDS = (
    "30000000-0000-4000-8000-000000000001",
    "30000000-0000-4000-8000-000000000002",
)
JOB_UUIDS = (
    "40000000-0000-4000-8000-000000000001",
    "40000000-0000-4000-8000-000000000002",
)
_CREATED_AT = "2026-08-05T00:00:00Z"


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[WorkflowStore]:
    """创建隔离的标准工作流存储（WorkflowStore）。

    参数：``tmp_path`` 是 pytest 为单项测试提供的临时目录。
    产生：可写的本地工作流任务（WorkflowTask）权威；测试结束后关闭连接。
    """

    # ``opened_store`` 是本测试唯一的工作流任务（WorkflowTask）写权威。
    opened_store = WorkflowStore(tmp_path / "workflow_history.db")
    try:
        yield opened_store
    finally:
        opened_store.close()


def _projection(store: WorkflowStore) -> Any:
    """延迟加载待实现的任务运行投影（TaskRuntimeProjection）。

    参数：``store`` 是标准工作流存储（WorkflowStore）。
    返回：绑定该存储的任务运行投影实例。
    异常：RED 阶段模块不存在时抛出 ``ModuleNotFoundError``。
    """

    # ``projection_module`` 是 F05.3-B 约定的唯一生产模块接缝。
    projection_module = importlib.import_module(
        "unilabos.workflow.task_runtime_projection"
    )
    return projection_module.TaskRuntimeProjection(store)


def _seed_task(store: WorkflowStore, *, job_count: int = 2) -> tuple[str, ...]:
    """写入一个标准工作流任务（WorkflowTask）及其待处理作业。

    参数：``store`` 是唯一写权威；``job_count`` 是要创建的一或两个工作流节点作业
    （WorkflowNodeJob）数量。
    返回：按拓扑顺序排列的工作流节点作业 UUID。
    异常：数量不在测试支持范围内时抛出 ``ValueError``；数据库约束异常原样传播。
    """

    if job_count not in {1, 2}:
        raise ValueError("测试任务只支持一或两个工作流节点作业")
    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="F05.3-B 运行投影",
        tags=[],
        description=None,
        meta_data={},
    )
    # ``selected_job_uuids`` 是本次任务真正拥有的稳定作业身份集合。
    selected_job_uuids = JOB_UUIDS[:job_count]
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workflow_task(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_uuid, status, workflow_snapshot,
                execution_plan, run_mode, target_node_uuid, control_status,
                cleanup_status, trace_context, input, output, error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, 'pending', '{}', '{}',
                      'normal', NULL, 'active', 'none', '{}', '{}', '{}', '[]')
            """,
            (TASK_UUID, _CREATED_AT, _CREATED_AT, WORKFLOW_UUID),
        )
        for topological_index, (node_uuid, job_uuid) in enumerate(
            zip(NODE_UUIDS, selected_job_uuids, strict=False)
        ):
            # ``topological_index`` 冻结兄弟作业的标准查询顺序，不代表执行尝试号。
            connection.execute(
                """
                INSERT INTO workflow_node_job(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_task_uuid, workflow_node_uuid,
                    feedback_sequence, topological_index, executor_kind,
                    execution_policy, execution_timeout_seconds, status,
                    attempt, param, feedback_data, return_info, control_data,
                    error_info
                ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, 0, ?,
                          'device_action', '{}', 0, 'pending', 1, '{}', '{}',
                          '{}', '{}', '[]')
                """,
                (
                    job_uuid,
                    _CREATED_AT,
                    _CREATED_AT,
                    TASK_UUID,
                    node_uuid,
                    topological_index,
                ),
            )
    return selected_job_uuids


def _aggregate(store: WorkflowStore) -> dict[str, Any]:
    """读取标准工作流任务（WorkflowTask）/作业聚合。

    参数：``store`` 是本地工作流权威。
    返回：包含一个任务投影和按拓扑顺序作业投影的字典。
    """

    return {
        "task": store.get_task(TASK_UUID),
        "jobs": store.list_jobs(TASK_UUID),
    }


def test_waiting_for_material_projects_to_pending_without_job_mutation(
    store: WorkflowStore,
) -> None:
    """内部等料不得泄漏为 Backend 之外的工作流任务状态。

    参数：``store`` 是隔离工作流权威。返回无；断言首次观察和重复观察都保持
    工作流任务（WorkflowTask）与全部作业为 ``pending``，且不刷新时间戳。
    """

    _seed_task(store)
    projection = _projection(store)
    before = _aggregate(store)

    projection.project_submission(TASK_UUID, "waiting_for_material")
    first = _aggregate(store)
    projection.project_submission(TASK_UUID, "waiting_for_material")

    assert first == before
    assert _aggregate(store) == before
    assert {job["status"] for job in first["jobs"]} == {"pending"}


def test_pre_dispatch_atomically_marks_exact_job_and_parent_task_running(
    store: WorkflowStore,
) -> None:
    """派发前投影只推进目标作业，并在同一事务启动父任务。

    参数：``store`` 是隔离工作流权威。返回无；断言目标作业为 ``dispatched``、
    兄弟作业仍为 ``pending``，父任务为 ``running`` 且记录开始时间。
    """

    first_job_uuid, second_job_uuid = _seed_task(store)
    projection = _projection(store)

    projection.project_pre_dispatch(task_uuid=TASK_UUID, job_uuid=first_job_uuid)

    aggregate = _aggregate(store)
    assert aggregate["task"]["status"] == "running"
    assert "started_at" in aggregate["task"]
    assert {job["uuid"]: job["status"] for job in aggregate["jobs"]} == {
        first_job_uuid: "dispatched",
        second_job_uuid: "pending",
    }


def test_pre_dispatch_replay_is_zero_write(store: WorkflowStore) -> None:
    """同一派发意图的投递重放（DeliveryReplay）必须零写入。

    参数：``store`` 是隔离工作流权威。返回无；断言重复派发前通知不改变任务、
    作业或其时间戳。
    """

    (job_uuid,) = _seed_task(store, job_count=1)
    projection = _projection(store)
    projection.project_pre_dispatch(task_uuid=TASK_UUID, job_uuid=job_uuid)
    first = _aggregate(store)

    projection.project_pre_dispatch(task_uuid=TASK_UUID, job_uuid=job_uuid)

    assert _aggregate(store) == first


def test_first_legacy_success_maps_job_to_succeeded_but_task_stays_running(
    store: WorkflowStore,
) -> None:
    """首个本地 ``success`` 只完成一个作业，不能提前完成多作业任务。

    参数：``store`` 是隔离工作流权威。返回无；断言本地状态映射为 Backend
    ``succeeded``，父任务仍为 ``running``。
    """

    first_job_uuid, _second_job_uuid = _seed_task(store)
    projection = _projection(store)
    projection.project_pre_dispatch(task_uuid=TASK_UUID, job_uuid=first_job_uuid)

    projection.project_job_finished(
        job_uuid=first_job_uuid,
        scheduler_state="success",
        return_info={"completed": True},
    )

    aggregate = _aggregate(store)
    assert aggregate["jobs"][0]["status"] == "succeeded"
    assert aggregate["jobs"][0]["return_info"] == {"completed": True}
    assert aggregate["task"]["status"] == "running"
    assert "finished_at" not in aggregate["task"]


def test_last_success_aggregates_multi_job_task_to_succeeded(
    store: WorkflowStore,
) -> None:
    """只有最后一个作业成功后，父任务才投影为成功终态。

    参数：``store`` 是隔离工作流权威。返回无；断言两个作业均为 ``succeeded``，
    工作流任务（WorkflowTask）具有唯一成功终态和完成时间。
    """

    first_job_uuid, second_job_uuid = _seed_task(store)
    projection = _projection(store)
    for job_uuid in (first_job_uuid, second_job_uuid):
        projection.project_pre_dispatch(task_uuid=TASK_UUID, job_uuid=job_uuid)
        projection.project_job_finished(
            job_uuid=job_uuid,
            scheduler_state="success",
            return_info={"job_uuid": job_uuid},
        )

    aggregate = _aggregate(store)
    assert [job["status"] for job in aggregate["jobs"]] == [
        "succeeded",
        "succeeded",
    ]
    assert aggregate["task"]["status"] == "succeeded"
    assert "finished_at" in aggregate["task"]


def test_failed_job_dominates_task_without_overwriting_sibling_result(
    store: WorkflowStore,
) -> None:
    """失败业务终态不得阻止兄弟明确结果落盘，也不得被兄弟成功覆盖。

    参数：``store`` 是隔离工作流权威。返回无；断言失败作业使父任务快速失败，
    迟到兄弟成功仍写入自身作业但父任务继续为 ``failed``。
    """

    first_job_uuid, second_job_uuid = _seed_task(store)
    projection = _projection(store)
    for job_uuid in (first_job_uuid, second_job_uuid):
        projection.project_pre_dispatch(task_uuid=TASK_UUID, job_uuid=job_uuid)

    projection.project_job_finished(
        job_uuid=first_job_uuid,
        scheduler_state="failed",
        error_info=[{"code": "device_action_failed"}],
    )
    projection.project_job_finished(
        job_uuid=second_job_uuid,
        scheduler_state="success",
        return_info={"completed": True},
    )

    aggregate = _aggregate(store)
    assert [job["status"] for job in aggregate["jobs"]] == [
        "failed",
        "succeeded",
    ]
    assert aggregate["task"]["status"] == "failed"


def test_terminal_replay_is_idempotent_and_conflicting_terminal_is_zero_write(
    store: WorkflowStore,
) -> None:
    """相同终态重放幂等，状态或结果冲突必须拒绝且零写入。

    参数：``store`` 是隔离工作流权威。返回无；断言终态身份与结果共同构成稳定
    事实，冲突时抛出 ``StoreConflict`` 并回滚完整任务/作业聚合。
    """

    (job_uuid,) = _seed_task(store, job_count=1)
    projection = _projection(store)
    projection.project_pre_dispatch(task_uuid=TASK_UUID, job_uuid=job_uuid)
    projection.project_job_finished(
        job_uuid=job_uuid,
        scheduler_state="success",
        return_info={"completed": True},
    )
    terminal = _aggregate(store)

    projection.project_job_finished(
        job_uuid=job_uuid,
        scheduler_state="success",
        return_info={"completed": True},
    )
    assert _aggregate(store) == terminal

    with pytest.raises(StoreConflict):
        projection.project_job_finished(
            job_uuid=job_uuid,
            scheduler_state="failed",
            error_info=[{"code": "late_conflict"}],
        )
    with pytest.raises(StoreConflict):
        projection.project_job_finished(
            job_uuid=job_uuid,
            scheduler_state="success",
            return_info={"completed": False},
        )
    assert _aggregate(store) == terminal


@pytest.mark.parametrize(
    "unsupported_scheduler_state",
    ["execution_unknown", "interrupted"],
)
def test_projection_never_writes_legacy_history_or_execution_unknown(
    store: WorkflowStore,
    unsupported_scheduler_state: str,
) -> None:
    """短期投影不得写遗留历史，也不得伪造执行未知事实。

    参数：``store`` 是隔离工作流权威；``unsupported_scheduler_state`` 是禁止写入
    标准表的遗留或未来状态。返回无；断言遗留历史哨兵保持不变，非法输入关闭失败。
    """

    (job_uuid,) = _seed_task(store, job_count=1)
    with store.transaction() as connection:
        connection.execute("CREATE TABLE workflow_runs(marker TEXT NOT NULL)")
        connection.execute("CREATE TABLE job_runs(marker TEXT NOT NULL)")
        connection.execute("INSERT INTO workflow_runs(marker) VALUES ('sentinel')")
        connection.execute("INSERT INTO job_runs(marker) VALUES ('sentinel')")
    projection = _projection(store)
    projection.project_submission(TASK_UUID, "waiting_for_material")
    projection.project_pre_dispatch(task_uuid=TASK_UUID, job_uuid=job_uuid)

    before = _aggregate(store)
    with pytest.raises(StoreConflict):
        projection.project_job_finished(
            job_uuid=job_uuid,
            scheduler_state=unsupported_scheduler_state,
        )
    assert _aggregate(store) == before

    projection.project_job_finished(
        job_uuid=job_uuid,
        scheduler_state="success",
        return_info={"completed": True},
    )
    with store.transaction() as connection:
        legacy_workflows = connection.execute(
            "SELECT marker FROM workflow_runs"
        ).fetchall()
        legacy_jobs = connection.execute("SELECT marker FROM job_runs").fetchall()
    assert [row["marker"] for row in legacy_workflows] == ["sentinel"]
    assert [row["marker"] for row in legacy_jobs] == ["sentinel"]
    assert all(
        job["status"] != "execution_unknown" for job in store.list_jobs(TASK_UUID)
    )
