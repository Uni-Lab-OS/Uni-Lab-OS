"""Backend-shaped Workflow Interface and local soft-delete coverage."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.api import create_scheduler_router
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge


class EmptyTemplateSnapshotProvider:
    """提供一个已成功发布的空模板投影，供组合路由测试使用。"""

    def __init__(self) -> None:
        """创建稳定的空不可变模板快照。"""

        self._snapshot = AuthoringCatalogSnapshot.from_entities([], [])

    def snapshot(self) -> AuthoringCatalogSnapshot:
        """返回已发布空快照，不访问设备注册表或数据库。"""

        return self._snapshot


def _client(tmp_path):
    store = WorkflowStore(tmp_path / "workflow_history.db")
    service = WorkflowService(store)
    return TestClient(create_workflow_app(service)), store


def test_workflow_definition_task_snapshot_and_soft_delete_match_backend(tmp_path):
    """公共定义、任务快照和软删除须保持后端（Backend）合同。

    参数：``tmp_path`` 隔离工作流数据库。返回：无。异常：HTTP、任务输入公开
    投影、快照或软删除回归由断言暴露。
    """

    client, store = _client(tmp_path)

    created = client.post(
        "/api/v1/workflows",
        json={"name": "local workflow", "tags": [], "meta_data": {}},
    )
    assert created.status_code == 201
    assert created.json()["code"] == 0
    workflow = created.json()["data"]
    workflow_uuid = workflow["uuid"]
    assert "deleted_at" not in workflow
    assert workflow["revision"] == 1

    graph = client.put(
        f"/api/v1/workflows/{workflow_uuid}/graph",
        json={
            "revision": 1,
            "nodes": [
                {
                    "uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "name": "approval",
                    "type": "manual_confirm",
                    "pose": {},
                    "param": {},
                    "execution_policy": {},
                    "disabled": False,
                    "minimized": False,
                    "meta_data": {},
                }
            ],
            "edges": [],
        },
    )
    assert graph.status_code == 200
    assert graph.json()["code"] == 0
    public_node = graph.json()["data"]["nodes"][0]
    assert "status" not in public_node

    task_response = client.post(
        "/api/v1/workflow-tasks",
        json={
            "workflow_uuid": workflow_uuid,
            "run_mode": "normal",
            "meta_data": {},
        },
    )
    assert task_response.status_code == 201
    task = task_response.json()["data"]
    assert task["status"] == "pending"
    assert task["input"] == {}
    assert "output" not in task
    assert "status" not in task["workflow_snapshot"]["nodes"][0]

    deleted = client.delete(f"/api/v1/workflows/{workflow_uuid}")
    assert deleted.status_code == 200
    assert deleted.json() == {"code": 0}
    row = store._conn.execute(
        "SELECT deleted_at FROM workflow WHERE uuid=?", (workflow_uuid,)
    ).fetchone()
    assert row["deleted_at"] is not None
    store.close()


def test_workflow_task_command_route_persists_step_command(tmp_path):
    """单步命令必须进入公共 WorkflowTask 命令合同而不是返回路由 404。"""

    client, store = _client(tmp_path)
    workflow = client.post(
        "/api/v1/workflows",
        json={"name": "step command", "tags": [], "meta_data": {}},
    ).json()["data"]
    task = client.post(
        "/api/v1/workflow-tasks",
        json={
            "workflow_uuid": workflow["uuid"],
            "run_mode": "step",
            "meta_data": {},
        },
    ).json()["data"]

    response = client.post(
        f"/api/v1/workflow-tasks/{task['uuid']}/commands",
        json={"type": "step", "idempotency_key": "toolbar-step-1"},
    )

    assert response.status_code == 201, response.text
    command = response.json()["data"]
    assert command["workflow_task_uuid"] == task["uuid"]
    assert command["type"] == "step"
    assert command["idempotency_key"] == "toolbar-step-1"
    assert command["status"] == "pending"
    store.close()


def test_workflow_task_step_command_applies_once_through_scheduler_bridge(tmp_path):
    """公共命令路由必须让单个就绪节点越过门控，幂等重放不得再发一次。"""

    store = WorkflowStore(tmp_path / "workflow_history.db")
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    service = WorkflowService(store, task_scheduler_bridge=bridge)
    client = TestClient(create_workflow_app(service))
    try:
        workflow_uuid = "11000000-0000-4000-8000-000000000001"
        task_uuid = "21000000-0000-4000-8000-000000000001"
        node_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        job_uuid = "41000000-0000-4000-8000-000000000001"
        created_at = "2026-08-07T00:00:00Z"
        plan = {
            "version": 1,
            "run_mode": "step",
            "nodes": [
                {
                    "uuid": node_uuid,
                    "kind": "device_action",
                    "device_id": "reactor-a",
                    "action_name": "distribute",
                    "action_type": "UniLabJsonCommand",
                    "param": {},
                    "param_schema": {
                        "type": "object",
                        "properties": {"goal": {"type": "object"}},
                    },
                }
            ],
            "handles": [],
            "edges": [],
        }
        store.create_workflow(
            workflow_uuid=workflow_uuid,
            name="bridged step",
            tags=[],
            description=None,
            meta_data={},
        )
        with store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO workflow_task(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_uuid, status, workflow_snapshot,
                    execution_plan, run_mode, target_node_uuid, control_status,
                    cleanup_status, trace_context, input, output, error_info
                ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, 'pending', '{}', ?,
                          'step', NULL, 'paused', 'none', '{}', '{}', '{}', '[]')
                """,
                (task_uuid, created_at, created_at, workflow_uuid, json.dumps(plan)),
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
                (job_uuid, created_at, created_at, task_uuid, node_uuid),
            )
        aggregate = bridge.submit(store.get_task(task_uuid))
        assert aggregate["task"]["control_status"] == "paused"
        assert store.list_jobs(task_uuid)[0]["status"] == "pending"
        assert dispatcher.dispatched == []

        body = {"type": "step", "idempotency_key": "toolbar-step-1"}
        first = client.post(
            f"/api/v1/workflow-tasks/{task_uuid}/commands",
            json=body,
        )
        replay = client.post(
            f"/api/v1/workflow-tasks/{task_uuid}/commands",
            json=body,
        )

        assert first.status_code == 201, first.text
        assert replay.status_code == 201, replay.text
        assert first.json()["data"]["status"] == "succeeded"
        assert replay.json()["data"]["uuid"] == first.json()["data"]["uuid"]
        assert store.list_jobs(task_uuid)[0]["status"] == "dispatched"
        assert [payload["job_id"] for payload in dispatcher.dispatched] == [job_uuid]
        assert store.count_rows("workflow_task_command") == 1
    finally:
        service.close()


def test_workflow_invalid_uuid_uses_backend_business_error(tmp_path):
    client, store = _client(tmp_path)
    response = client.get("/api/v1/workflows/not-a-uuid")
    assert response.status_code == 200
    assert response.json()["code"] == 1000
    assert response.json()["error"]["msg"]
    store.close()


def test_shared_workflow_routes_replace_execution_shaped_workflow_alias(tmp_path):
    router = create_scheduler_router(
        lambda: None,
        include_execution_shaped_workflow_routes=False,
    )
    assert not any(route.path == "/api/v1/workflows" for route in router.routes)

    client, store = _client(tmp_path)
    openapi = client.get("/openapi.json").json()
    assert "/api/v1/workflows" in openapi["paths"]
    edge_schema = openapi["components"]["schemas"]["WorkflowEdgeWrite"]
    assert "source_handle_uuid" in edge_schema["properties"]
    assert "target_handle_uuid" in edge_schema["properties"]
    assert "source_handle_key" not in edge_schema["properties"]
    store.close()


def test_local_workflow_app_mounts_template_query_from_same_runtime(tmp_path) -> None:
    """本地工作流应用传入模板投影时必须同时公开 Backend 模板查询路由。

    参数说明：``tmp_path`` 隔离工作流存储；空投影仍是一次成功发布，列表应返回
    标准空游标页而不是 404 或独立错误外壳。
    """

    store = WorkflowStore(tmp_path / "workflow_history.db")
    service = WorkflowService(store)
    client = TestClient(
        create_workflow_app(
            service,
            template_snapshot_provider=EmptyTemplateSnapshotProvider(),
        )
    )

    # ``response`` 是空目录仍携带权威身份和目录指纹的成功游标页。
    response = client.get("/api/v1/workflow-node-templates")
    assert response.status_code == 200
    # ``data`` 是模板目录响应的数据主体。
    data = response.json()["data"]
    assert data["authority"] == {"authority_id": "local", "kind": "local"}
    assert data["catalog_fingerprint"].startswith("sha256:")
    assert {
        "code": response.json()["code"],
        "data": {
            key: value
            for key, value in data.items()
            if key not in {"authority", "catalog_fingerprint"}
        },
    } == {
        "code": 0,
        "data": {"items": [], "has_more": False, "next_cursor_uuid": None},
    }
    store.close()
