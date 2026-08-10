"""Workflow Debugger 的公开 HTTP 合同与静态禁用语义。"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore


START_NODE_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DISABLED_NODE_UUID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class RecordingTaskSchedulerBridge:
    """只替代真实调度器边界，保留公开服务和 SQLite 写模型。"""

    def __init__(self, store: WorkflowStore) -> None:
        self.store = store
        self.steps: list[tuple[str, str | None]] = []

    def submit(self, task: dict[str, Any]) -> dict[str, Any]:
        return {"task": self.store.get_task(task["uuid"]), "jobs": self.store.list_jobs(task["uuid"])}

    def step(
        self,
        task_uuid: str,
        *,
        target_node_uuid: str | None = None,
    ) -> dict[str, Any]:
        self.steps.append((task_uuid, target_node_uuid))
        return {"workflow_id": task_uuid, "state": "paused", "dispatched": [target_node_uuid]}


def _runtime(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.db")
    bridge = RecordingTaskSchedulerBridge(store)
    service = WorkflowService(store, task_scheduler_bridge=bridge)  # type: ignore[arg-type]
    client = TestClient(create_workflow_app(service))
    workflow = client.post(
        "/api/v1/workflows",
        json={"name": "debug fixture", "tags": [], "meta_data": {}},
    ).json()["data"]
    graph = client.put(
        f"/api/v1/workflows/{workflow['uuid']}/graph",
        json={
            "revision": 1,
            "nodes": [
                {
                    "uuid": START_NODE_UUID,
                    "name": "可运行节点",
                    "type": "manual_confirm",
                    "pose": {"x": 0, "y": 0},
                    "param": {},
                    "execution_policy": {},
                    "disabled": False,
                    "minimized": False,
                    "meta_data": {},
                },
                {
                    "uuid": DISABLED_NODE_UUID,
                    "name": "禁用节点",
                    "type": "manual_confirm",
                    "pose": {"x": 200, "y": 0},
                    "param": {},
                    "execution_policy": {},
                    "disabled": True,
                    "minimized": False,
                    "meta_data": {},
                },
            ],
            "edges": [],
        },
    )
    assert graph.status_code == 200, graph.text
    return client, store, bridge, workflow["uuid"]


def test_debug_launch_freezes_configuration_and_excludes_disabled_node(tmp_path) -> None:
    client, store, _bridge, workflow_uuid = _runtime(tmp_path)

    response = client.post(
        "/api/v1/debug/workflow-tasks",
        json={
            "workflow_uuid": workflow_uuid,
            "start_node_uuids": [START_NODE_UUID],
            "breakpoint_node_uuids": [START_NODE_UUID],
            "input": {},
            "meta_data": {},
        },
    )

    assert response.status_code == 201, response.text
    task = response.json()["data"]
    jobs = client.get(f"/api/v1/workflow-tasks/{task['uuid']}/jobs").json()["data"]
    projection = client.get(
        f"/api/v1/debug/workflow-tasks/{task['uuid']}"
    ).json()["data"]

    assert {node["uuid"] for node in task["workflow_snapshot"]["nodes"]} == {
        START_NODE_UUID,
        DISABLED_NODE_UUID,
    }
    assert [node["uuid"] for node in task["execution_plan"]["nodes"]] == [
        START_NODE_UUID
    ]
    assert [job["workflow_node_uuid"] for job in jobs] == [START_NODE_UUID]
    assert projection["configuration"] == {
        "start_node_uuids": [START_NODE_UUID],
        "breakpoint_node_uuids": [START_NODE_UUID],
    }
    assert projection["active_node_uuids"] == [START_NODE_UUID]
    assert projection["out_of_scope_node_uuids"] == []
    assert projection["disabled_node_uuids"] == [DISABLED_NODE_UUID]
    assert projection["holds"][0]["workflow_node_uuid"] == START_NODE_UUID
    assert projection["holds"][0]["status"] == "open"
    store.close()


def test_debug_step_requires_exact_open_hold_and_is_idempotent(tmp_path) -> None:
    client, store, bridge, workflow_uuid = _runtime(tmp_path)
    task = client.post(
        "/api/v1/debug/workflow-tasks",
        json={
            "workflow_uuid": workflow_uuid,
            "start_node_uuids": [START_NODE_UUID],
            "breakpoint_node_uuids": [],
        },
    ).json()["data"]
    projection = client.get(
        f"/api/v1/debug/workflow-tasks/{task['uuid']}"
    ).json()["data"]
    hold_uuid = projection["holds"][0]["uuid"]
    body = {
        "type": "step",
        "scope": {"type": "hold", "hold_uuid": hold_uuid},
        "idempotency_key": "debug-step-1",
    }

    first = client.post(
        f"/api/v1/debug/workflow-tasks/{task['uuid']}/commands", json=body
    )
    replay = client.post(
        f"/api/v1/debug/workflow-tasks/{task['uuid']}/commands", json=body
    )

    assert first.status_code == 201, first.text
    assert replay.status_code == 201, replay.text
    assert first.json()["data"] == replay.json()["data"]
    assert first.json()["data"]["status"] == "succeeded"
    assert bridge.steps == [(task["uuid"], START_NODE_UUID)]
    released = client.get(
        f"/api/v1/debug/workflow-tasks/{task['uuid']}"
    ).json()["data"]["holds"][0]
    assert released["status"] == "released"
    store.close()
