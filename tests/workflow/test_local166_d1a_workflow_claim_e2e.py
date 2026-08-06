"""LOCAL-166 真实 D1A API 与普通工作流双向竞争回归。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from fastapi.testclient import TestClient

import tests.app.test_d1a_device_action_task_contract as contract
from tests.workflow.test_d1a_device_action_runtime_bridge import (
    FeedbackHost,
    _create_generic_device_workflow_task,
    _create_m1ef_edge_stack,
)
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.catalog import TemplateCatalog
from unilabos.workflow.device_action_task import (
    DeviceActionTaskRuntimeBridge,
    DeviceActionTaskService,
)
from unilabos.workflow.runtime import WorkflowRuntimeCoordinator, WorkflowRuntimeWorker
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

DEVICE_MATERIAL_UUID = str(uuid5(NAMESPACE_URL, "d1a:robot"))


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.listeners: list[Any] = []

    def execution_ready(self) -> bool:
        return True

    def dispatch(self, payload: dict[str, Any]) -> None:
        self.payloads.append(dict(payload))

    def add_job_finished_listener(self, listener: Any) -> None:
        self.listeners.append(listener)

    def remove_job_finished_listener(self, listener: Any) -> None:
        self.listeners.remove(listener)

    def request_cancel(self, _job_uuid: str) -> bool:
        return True


def _wait(predicate: Any, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("timed out waiting for D1A/Workflow claim convergence")
        time.sleep(0.01)


def _d1a_runtime(tmp_path: Path) -> dict[str, Any]:
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
    scheduler, backend, inventory = _create_m1ef_edge_stack(
        tmp_path,
        host_node_getter=lambda: host,
    )
    host.backend = backend
    coordinator = WorkflowRuntimeCoordinator(store)
    bridge = DeviceActionTaskRuntimeBridge(
        store=store,
        coordinator=coordinator,
        scheduler=scheduler,
        backend=backend,
    )
    bridge.start()
    live = contract.MutableLiveCatalog()
    d1a = DeviceActionTaskService(
        store=store,
        template_catalog=catalog,
        authority=contract.AUTHORITY,
        live_catalog=live,
        admission=bridge,
    )
    workflow_service = WorkflowService(store)
    client = TestClient(create_workflow_app(workflow_service, device_action_tasks=d1a))
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
    return {
        "store": store,
        "service": workflow_service,
        "d1a": d1a,
        "client": client,
        "harness": harness,
        "host": host,
        "bridge": bridge,
        "backend": backend,
        "inventory": inventory,
    }


def _close(runtime: dict[str, Any], worker: WorkflowRuntimeWorker | None) -> None:
    if worker is not None:
        worker.stop()
        worker.join(timeout=1)
    runtime["client"].close()
    runtime["bridge"].stop()
    runtime["backend"].stop()
    runtime["inventory"].close()
    runtime["store"].close()


def test_running_d1a_keeps_workflow_pending_with_same_attempt(tmp_path: Path) -> None:
    runtime = _d1a_runtime(tmp_path)
    worker = None
    try:
        response = runtime["client"].post(
            "/api/v1/device-action-tasks",
            json=contract._request(runtime["harness"]),
        )
        assert response.status_code == 201
        d1a = response.json()["data"]
        _wait(lambda: len(runtime["host"].sent) == 1)

        _task, job = _create_generic_device_workflow_task(
            runtime["service"],
            device_material_uuid=DEVICE_MATERIAL_UUID,
        )
        dispatcher = _RecordingDispatcher()
        worker = WorkflowRuntimeWorker(
            WorkflowRuntimeCoordinator(runtime["store"]),
            dispatcher=dispatcher,
            device_identity_resolver=lambda _identity: "robot",
            inventory=runtime["inventory"],
            poll_interval_seconds=0.01,
        )
        worker.start()
        _wait(
            lambda: (
                runtime["service"].get_workflow_node_job(job["uuid"])["claim_status"]
                == "waiting_for_claim"
            )
        )

        observed = runtime["service"].get_workflow_node_job(job["uuid"])
        d1a_view = runtime["d1a"].get(d1a["task_uuid"])
        assert dispatcher.payloads == []
        assert len(runtime["host"].sent) == 1
        assert observed["status"] == "pending"
        assert observed["attempt"] == 1
        assert observed["blocking_claim_uuid"] == d1a_view["execution_claim"]["uuid"]
    finally:
        _close(runtime, worker)


def test_running_workflow_keeps_d1a_queued_without_second_goal(tmp_path: Path) -> None:
    runtime = _d1a_runtime(tmp_path)
    dispatcher = _RecordingDispatcher()
    worker = WorkflowRuntimeWorker(
        WorkflowRuntimeCoordinator(runtime["store"]),
        dispatcher=dispatcher,
        device_identity_resolver=lambda _identity: "robot",
        inventory=runtime["inventory"],
        poll_interval_seconds=0.01,
    )
    try:
        task, job = _create_generic_device_workflow_task(
            runtime["service"],
            device_material_uuid=DEVICE_MATERIAL_UUID,
        )
        worker.start()
        _wait(lambda: len(dispatcher.payloads) == 1)
        workflow_job = runtime["service"].get_workflow_node_job(job["uuid"])

        response = runtime["client"].post(
            "/api/v1/device-action-tasks",
            json=contract._request(runtime["harness"]),
        )
        assert response.status_code == 201
        d1a = response.json()["data"]
        _wait(
            lambda: (
                runtime["d1a"].get(d1a["task_uuid"])["execution_claim"]["status"]
                == "waiting_for_claim"
            )
        )

        queued = runtime["d1a"].get(d1a["task_uuid"])
        assert len(dispatcher.payloads) == 1
        assert runtime["host"].sent == []
        assert queued["status"] == "pending"
        assert queued["job_status"] == "pending"
        assert (
            queued["execution_claim"]["blocking_claim_uuid"]
            == workflow_job["claim_uuid"]
        )
        assert runtime["service"].get_workflow_task(task["uuid"])["status"] == (
            "running"
        )
    finally:
        _close(runtime, worker)
