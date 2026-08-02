"""D1A-S1 正式 Task/Job 到既有 Edge/HostNode 链路的执行 tracer。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

import tests.app.test_d1a_device_action_task_contract as contract
from unilabos.app.scheduler.backend import JobExecutionBackend, create_edge_stack
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.models import WorkflowNode, WorkflowSpec
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.app.workflow_api import create_workflow_app
from unilabos.utils.type_check import serialize_result_info
from unilabos.workflow.catalog import TemplateCatalog
from unilabos.workflow.device_action_task import (
    DeviceActionTaskRuntimeBridge,
    DeviceActionTaskService,
)
from unilabos.workflow.runtime import WorkflowRuntimeCoordinator
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore


def _wait(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class FeedbackHost:
    def __init__(self) -> None:
        self.backend: JobExecutionBackend | None = None
        self.sent: list[Any] = []
        self.auto_complete = True

    def send_goal(
        self,
        item: Any,
        *,
        action_type: str,
        action_kwargs: dict[str, Any],
        sample_material: dict[str, Any],
        server_info: Any = None,
    ) -> None:
        del action_type, action_kwargs, sample_material, server_info
        self.sent.append(item)
        assert self.backend is not None
        self.backend.publish_job_status({"progress": 0.5}, item, "running")
        if self.auto_complete:
            self.backend.publish_job_status(
                {"completed": True},
                item,
                "success",
                serialize_result_info("", True, {"completed": True}),
            )


def test_edge_scheduler_uses_fixed_job_uuid_and_commits_before_dispatch() -> None:
    events: list[tuple[str, str]] = []
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(
        dispatcher=dispatcher,
        pre_dispatch_hook=lambda payload: events.append(
            ("commit", payload["job_id"])
        ),
    )
    job_uuid = str(uuid4())

    result = scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="d1a-fixed-job",
            nodes=[
                WorkflowNode(
                    id="node",
                    job_id=job_uuid,
                    device_id="robot",
                    action_name="move",
                    action_type="test.action.Move",
                    param={"duration_seconds": 5},
                )
            ],
        )
    )
    events.extend(("dispatch", item["job_id"]) for item in dispatcher.dispatched)

    assert result["dispatched"][0]["job_id"] == job_uuid
    assert events == [("commit", job_uuid), ("dispatch", job_uuid)]


def test_device_action_runtime_reuses_formal_job_for_feedback_and_typed_result(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    catalog = TemplateCatalog(store)
    snapshot = catalog.replace(
        contract.AUTHORITY,
        [
            contract._template_import(
                name="move",
                display_name="移动",
                resource_template_uuid=contract.RESOURCE_TEMPLATE_UUID,
                schema=contract.SIMPLE_SCHEMA,
            )
        ],
    )
    template = snapshot.node_templates[0]
    host = FeedbackHost()
    scheduler, backend = create_edge_stack(host_node_getter=lambda: host)
    host.backend = backend
    coordinator = WorkflowRuntimeCoordinator(store)
    bridge = DeviceActionTaskRuntimeBridge(
        store=store,
        coordinator=coordinator,
        scheduler=scheduler,
        backend=backend,
    )
    bridge.start()
    service = DeviceActionTaskService(
        store=store,
        template_catalog=catalog,
        authority=contract.AUTHORITY,
        live_catalog=contract.MutableLiveCatalog(),
        admission=bridge,
    )
    client = TestClient(
        create_workflow_app(
            WorkflowService(store),
            device_action_tasks=service,
        )
    )
    harness = contract.Harness(
        database_path=tmp_path / "workflow.db",
        store=store,
        client=client,
        catalog=catalog,
        fingerprint=snapshot.fingerprint,
        simple_template_uuid=str(template["uuid"]),
        material_template_uuid="",
        live_catalog=contract.MutableLiveCatalog(),
        admission=bridge,
    )
    try:
        created = client.post(
            "/api/v1/device-action-tasks",
            json=contract._request(harness),
        ).json()["data"]
        assert _wait(
            lambda: client.get(
                f"/api/v1/device-action-tasks/{created['task_uuid']}"
            ).json()["data"]["status"]
            == "succeeded"
        )

        view = client.get(
            f"/api/v1/device-action-tasks/{created['task_uuid']}"
        ).json()["data"]
        feedback = client.get(
            f"/api/v1/workflow-node-jobs/{created['job_uuid']}/feedback"
        ).json()["data"]
        assert host.sent[0].job_id == created["job_uuid"]
        assert host.sent[0].task_id == created["task_uuid"]
        assert view["job_status"] == "succeeded"
        assert view["output"] == {"completed": True}
        assert feedback["items"][0]["data"] == {"progress": 0.5}
    finally:
        client.close()
        bridge.stop()
        backend.stop()
        store.close()


def test_busy_second_task_stays_durable_pending_until_first_releases(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    catalog = TemplateCatalog(store)
    snapshot = catalog.replace(
        contract.AUTHORITY,
        [
            contract._template_import(
                name="move",
                display_name="移动",
                resource_template_uuid=contract.RESOURCE_TEMPLATE_UUID,
                schema=contract.SIMPLE_SCHEMA,
            )
        ],
    )
    template = snapshot.node_templates[0]
    host = FeedbackHost()
    host.auto_complete = False
    scheduler, backend = create_edge_stack(host_node_getter=lambda: host)
    host.backend = backend
    bridge = DeviceActionTaskRuntimeBridge(
        store=store,
        coordinator=WorkflowRuntimeCoordinator(store),
        scheduler=scheduler,
        backend=backend,
    )
    bridge.start()
    live = contract.MutableLiveCatalog()
    service = DeviceActionTaskService(
        store=store,
        template_catalog=catalog,
        authority=contract.AUTHORITY,
        live_catalog=live,
        admission=bridge,
    )
    client = TestClient(
        create_workflow_app(WorkflowService(store), device_action_tasks=service)
    )
    harness = contract.Harness(
        tmp_path / "workflow.db",
        store,
        client,
        catalog,
        snapshot.fingerprint,
        str(template["uuid"]),
        "",
        live,
        bridge,
    )
    try:
        first = client.post(
            "/api/v1/device-action-tasks", json=contract._request(harness)
        ).json()["data"]
        second = client.post(
            "/api/v1/device-action-tasks", json=contract._request(harness)
        ).json()["data"]
        assert _wait(lambda: len(host.sent) == 1)
        assert service.get(first["task_uuid"])["status"] == "running"
        assert service.get(second["task_uuid"])["status"] == "pending"

        assert host.backend is not None
        host.backend.publish_job_status(
            {"completed": True},
            host.sent[0],
            "success",
            serialize_result_info("", True, {"completed": True}),
        )
        assert _wait(lambda: len(host.sent) == 2)
        assert service.get(second["task_uuid"])["status"] == "running"
        with store.transaction() as connection:
            holders = connection.execute(
                """
                SELECT COUNT(*) FROM device_action_task
                WHERE claim_status = 'claimed'
                """
            ).fetchone()[0]
        assert holders == 1
    finally:
        client.close()
        bridge.stop()
        backend.stop()
        store.close()
