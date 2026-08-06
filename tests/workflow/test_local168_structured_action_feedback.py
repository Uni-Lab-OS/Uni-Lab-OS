"""LOCAL-168 作业运行迁移与结构化反馈回归。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import unilabos.ros.action_feedback as action_feedback_module
from unilabos.app.ws_client import QueueItem
from unilabos.ros.action_feedback import (
    attach_action_feedback,
    decode_action_feedback,
    publish_action_feedback,
)
from unilabos.ros.nodes.presets.host_node import HostNode
from unilabos.workflow.device_action_task import DeviceActionTaskRuntimeBridge
from unilabos.workflow.models import WorkflowNodeWrite
from unilabos.workflow.runtime import WorkflowRuntimeCoordinator
from unilabos.workflow.runtime_feedback import commit_runtime_job_feedback
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import StoreConflict, WorkflowStore


def _create_running_job(
    store: WorkflowStore,
    *,
    running: bool = True,
) -> tuple[str, str]:
    service = WorkflowService(store)
    workflow = service.create_workflow(
        name="LOCAL-168",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=str(uuid4()),
    )
    service.save_graph(
        workflow["uuid"],
        revision=1,
        nodes=[
            WorkflowNodeWrite(
                uuid=str(uuid4()),
                name="stir",
                status="idle",
                type="compute",
                pose={},
                param={},
                execution_policy={},
                disabled=False,
                minimized=False,
                meta_data={},
            )
        ],
        edges=[],
    )
    task = service.create_workflow_task(
        workflow_uuid=workflow["uuid"],
        run_mode="normal",
        target_node_uuid=None,
        input_value={},
        description=None,
        meta_data={},
    )
    job = service.list_workflow_node_jobs(task["uuid"])[0]
    coordinator = WorkflowRuntimeCoordinator(store)
    coordinator.start_task(task["uuid"])
    coordinator.transition_job(job["uuid"], "dispatched")
    if running:
        coordinator.transition_job(job["uuid"], "running")
    return task["uuid"], job["uuid"]


def test_feedback_context_throttles_ticks_and_preserves_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, object]] = []
    diagnostic_logs: list[str] = []
    monkeypatch.setattr(
        action_feedback_module.logger,
        "info",
        lambda message: diagnostic_logs.append(str(message)),
    )
    with attach_action_feedback(
        lambda payload: published.append(payload) is None,
        job_uuid="job-1",
        task_uuid="task-1",
        device_id="stirrer",
        action_name="run_stirring",
    ):
        assert publish_action_feedback(
            "waiting_precondition",
            {
                "diagnostic_event": "precondition_check_started",
                "sensor": "传感器状态_上位机[2].NO[10]",
                "position": 1,
                "expected_value": True,
                "actual_value": False,
                "elapsed_s": 0.0,
            },
        )
        assert not publish_action_feedback(
            "waiting_precondition",
            {
                "diagnostic_event": "precondition_check_started",
                "sensor": "传感器状态_上位机[2].NO[10]",
                "position": 1,
                "expected_value": True,
                "actual_value": False,
                "elapsed_s": 0.1,
            },
        )
        assert publish_action_feedback(
            "waiting_precondition",
            {
                "diagnostic_event": "satisfied",
                "sensor": "传感器状态_上位机[2].NO[10]",
                "position": 1,
                "expected_value": True,
                "actual_value": True,
                "elapsed_s": 0.2,
            },
        )

    assert [item["feedback_sequence"] for item in published] == [1, 2]
    assert published[-1]["task_uuid"] == "task-1"
    assert published[-1]["goal"] == {
        "device_id": "stirrer",
        "action_name": "run_stirring",
    }
    assert published[0]["effect"] == {
        "identity": "job-1:1",
        "phase": "waiting_precondition",
    }
    assert len(diagnostic_logs) == 2
    first_log = json.loads(diagnostic_logs[0].split("] ", 1)[1])
    assert first_log["diagnostic_event"] == "precondition_check_started"
    assert first_log["task_uuid"] == "task-1"
    assert first_log["job_uuid"] == "job-1"
    assert first_log["effect"]["identity"] == "job-1:1"
    assert first_log["sensor"] == "传感器状态_上位机[2].NO[10]"
    assert decode_action_feedback(
        {"feedback": '{"phase":"processing","position":2}'}
    ) == {"phase": "processing", "position": 2}


def test_feedback_survives_reopen_and_replay_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "workflow.db"
    store = WorkflowStore(path)
    task_uuid, job_uuid = _create_running_job(store)
    payload = {
        "phase": "waiting_precondition",
        "feedback_event_id": f"{job_uuid}:1",
        "observed_at": "2026-08-06T04:00:00Z",
        "position": 1,
        "sensor": "S04.material_present.1",
        "expected_value": True,
        "actual_value": False,
        "elapsed_s": 5.0,
        "timeout_s": 300.0,
        "remaining_s": 295.0,
        "task_uuid": task_uuid,
        "job_uuid": job_uuid,
    }
    first = commit_runtime_job_feedback(
        WorkflowRuntimeCoordinator(store),
        source="d1a",
        job_uuid=job_uuid,
        feedback_data=payload,
    )
    store.close()

    reopened = WorkflowStore(path)
    try:
        coordinator = WorkflowRuntimeCoordinator(reopened)
        replay = commit_runtime_job_feedback(
            coordinator,
            source="d1a",
            job_uuid=job_uuid,
            feedback_data=payload,
        )
        job = WorkflowService(reopened).get_workflow_node_job(job_uuid)
        history = WorkflowService(reopened).list_workflow_node_job_feedback(
            job_uuid, after_sequence=0, limit=100
        )
        assert first == {"through_sequence": 1, "created": 1}
        assert replay == {"through_sequence": 1, "created": 0}
        assert job["feedback_sequence"] == 1
        assert history["items"][0]["data"] == payload

        with pytest.raises(StoreConflict, match="other content"):
            commit_runtime_job_feedback(
                coordinator,
                source="d1a",
                job_uuid=job_uuid,
                feedback_data={**payload, "actual_value": True},
            )
    finally:
        reopened.close()


def test_empty_running_signal_transitions_dispatched_job_and_sets_started_at(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        _task_uuid, job_uuid = _create_running_job(store, running=False)
        coordinator = WorkflowRuntimeCoordinator(store)
        bridge = DeviceActionTaskRuntimeBridge.__new__(DeviceActionTaskRuntimeBridge)
        bridge._started = True
        bridge._store = store
        bridge._coordinator = coordinator
        bridge._scheduler = SimpleNamespace(
            inventory_job_claim=lambda _job_uuid, _attempt: SimpleNamespace(
                state="running"
            )
        )
        bridge._is_d1a_job = lambda _job_uuid: True

        bridge._on_job_status(job_uuid, {}, "running")

        job = WorkflowService(store).get_workflow_node_job(job_uuid)
        assert job["status"] == "running"
        assert job["started_at"] is not None
        assert job["feedback_sequence"] == 0
    finally:
        store.close()


class _Logger:
    def info(self, _message: str) -> None: ...

    def warning(self, _message: str) -> None: ...


class _ResultFuture:
    def add_done_callback(self, callback) -> None:
        self.callback = callback

    def result(self) -> None:
        return None


class _GoalHandle:
    accepted = True

    def __init__(self) -> None:
        self.result_future = _ResultFuture()

    def get_result_async(self) -> _ResultFuture:
        return self.result_future


def test_goal_acceptance_publishes_running_before_first_feedback() -> None:
    calls: list[tuple[dict[str, object], QueueItem, str]] = []
    bridge = SimpleNamespace(
        publish_job_status=lambda data, item, status: calls.append((data, item, status))
    )
    host = SimpleNamespace(
        bridges=[bridge],
        lab_logger=lambda: _Logger(),
        _goals={},
        _goal_trace_contexts={},
        _pending_goal_requests=set(),
        _pending_goal_cancellations=set(),
    )
    item = QueueItem(
        task_type="job_call_back_status",
        device_id="stirrer",
        action_name="run_stirring",
        task_id=str(uuid4()),
        job_id=str(uuid4()),
        notebook_id="",
        device_action_key="/devices/stirrer/run_stirring",
    )
    goal = _GoalHandle()

    HostNode.goal_response_callback(
        host,
        item,
        item.device_action_key,
        SimpleNamespace(result=lambda: goal),
    )

    assert calls == [({}, item, "running")]
    assert host._goals[item.job_id] is goal


def test_structured_feedback_topic_routes_the_persistable_payload() -> None:
    calls: list[tuple[dict[str, object], object, str]] = []
    bridge = SimpleNamespace(
        publish_job_status=lambda data, item, status: calls.append((data, item, status))
    )
    host = SimpleNamespace(bridges=[bridge])
    payload = {
        "phase": "waiting_precondition",
        "job_uuid": str(uuid4()),
        "task_uuid": str(uuid4()),
        "goal": {"device_id": "stirrer", "action_name": "run_stirring"},
        "position": 2,
        "actual_value": False,
    }

    HostNode.structured_action_feedback_callback(
        host,
        SimpleNamespace(data=json.dumps(payload)),
    )

    assert len(calls) == 1
    data, item, status = calls[0]
    assert data == payload
    assert item.job_id == payload["job_uuid"]
    assert item.task_id == payload["task_uuid"]
    assert status == "running"
