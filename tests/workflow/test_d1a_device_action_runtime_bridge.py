"""D1A-S1 正式 Task/Job 到既有 Edge/HostNode 链路的执行 tracer。"""

from __future__ import annotations

import copy
import inspect
import json
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

import tests.app.test_d1a_device_action_task_contract as contract
from unilabos.app.scheduler.backend import JobExecutionBackend, create_edge_stack
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.models import WorkflowNode, WorkflowSpec
from unilabos.app.scheduler.monitor import MonitorBus
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.app.workflow_api import create_workflow_app
from unilabos.utils.type_check import serialize_result_info
from unilabos.workflow.catalog import TemplateCatalog
from unilabos.workflow.device_action_task import (
    DeviceActionTaskRuntimeBridge,
    DeviceActionTaskService,
)
from unilabos.workflow.runtime import WorkflowRuntimeCoordinator, WorkflowRuntimeWorker
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore


def _wait(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _contains_identity(value: Any, identities: set[str]) -> bool:
    if isinstance(value, dict):
        return any(_contains_identity(item, identities) for item in value.values())
    if isinstance(value, list):
        return any(_contains_identity(item, identities) for item in value)
    return isinstance(value, str) and value in identities


class FeedbackHost:
    def __init__(self) -> None:
        self.backend: JobExecutionBackend | None = None
        self.sent: list[Any] = []
        self.action_types: list[str] = []
        self.auto_complete = True
        self.cancel_requests: list[str] = []

    def send_goal(
        self,
        item: Any,
        *,
        action_type: str,
        action_kwargs: dict[str, Any],
        sample_material: dict[str, Any],
        server_info: Any = None,
    ) -> None:
        del action_kwargs, sample_material, server_info
        self.sent.append(item)
        self.action_types.append(action_type)
        assert self.backend is not None
        self.backend.publish_job_status({"progress": 0.5}, item, "running")
        if self.auto_complete:
            self.backend.publish_job_status(
                {"completed": True},
                item,
                "success",
                serialize_result_info("", True, {"completed": True}),
            )

    def cancel_goal_or_defer(self, job_uuid: str) -> bool:
        self.cancel_requests.append(job_uuid)
        return True


class UncertainFeedbackHost(FeedbackHost):
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
        raise RuntimeError("transport acknowledgement was lost")


class GenericRecordingDispatcher:
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


def test_d1a_bridge_only_depends_on_formal_scheduler_identity_port() -> None:
    source = inspect.getsource(DeviceActionTaskRuntimeBridge)

    for forbidden in (
        "WorkflowSpec",
        "WorkflowNode(",
        "submit_workflow(",
        "cancel_workflow(",
        "workflow_snapshot(",
        'payload.get("job_id")',
        'payload.get("task_id")',
    ):
        assert forbidden not in source
    assert "submit_device_action_task(" in source
    assert "cancel_device_action_task(" in source


def test_edge_scheduler_uses_fixed_job_uuid_and_commits_before_dispatch() -> None:
    events: list[tuple[str, str]] = []
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(
        dispatcher=dispatcher,
        pre_dispatch_hook=lambda payload: events.append(("commit", payload["job_id"])),
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


def _submit_formal_device_action(
    scheduler: EdgeScheduler,
) -> tuple[str, str]:
    task_uuid = str(uuid4())
    job_uuid = str(uuid4())
    scheduler.set_device_action_task_hooks(before=lambda **_kwargs: True)
    scheduler.submit_device_action_task(
        task_uuid=task_uuid,
        job_uuid=job_uuid,
        device_id="robot",
        action_name="move",
        action_type="test.action.Move",
        input_value={"duration_seconds": 1},
    )
    return task_uuid, job_uuid


def test_formal_scheduler_identity_mapping_is_released_after_success() -> None:
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    task_uuid, job_uuid = _submit_formal_device_action(scheduler)

    assert scheduler._device_action_tasks_by_job_uuid == {job_uuid: task_uuid}
    scheduler.on_job_finished(job_uuid, success=True, ret_value={"completed": True})

    assert scheduler._device_action_tasks_by_job_uuid == {}


def test_formal_scheduler_identity_mapping_is_released_after_failure() -> None:
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    task_uuid, job_uuid = _submit_formal_device_action(scheduler)

    assert scheduler._device_action_tasks_by_job_uuid == {job_uuid: task_uuid}
    scheduler.on_job_finished(job_uuid, success=False, ret_value={"error": "failed"})

    assert scheduler._device_action_tasks_by_job_uuid == {}


def test_formal_scheduler_identity_mapping_is_released_after_cancel() -> None:
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    task_uuid, job_uuid = _submit_formal_device_action(scheduler)

    assert scheduler._device_action_tasks_by_job_uuid == {job_uuid: task_uuid}
    assert scheduler.cancel_device_action_task(task_uuid) is True

    assert scheduler._device_action_tasks_by_job_uuid == {}


def test_duplicate_formal_task_identity_preserves_the_existing_scheduler_run() -> None:
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    task_uuid, job_uuid = _submit_formal_device_action(scheduler)
    before_snapshot = copy.deepcopy(scheduler.snapshot())
    before_mappings = dict(scheduler._device_action_tasks_by_job_uuid)
    before_resource_locks = copy.deepcopy(scheduler._job_resource_locks)

    try:
        scheduler.submit_device_action_task(
            task_uuid=task_uuid,
            job_uuid=str(uuid4()),
            device_id="other-robot",
            action_name="inspect",
            action_type="test.action.Inspect",
            input_value={},
        )
        raise AssertionError("duplicate formal task identity must be rejected")
    except ValueError:
        pass

    assert scheduler.snapshot() == before_snapshot
    assert scheduler._device_action_tasks_by_job_uuid == before_mappings == {
        job_uuid: task_uuid
    }
    assert scheduler._job_resource_locks == before_resource_locks


def test_generic_workflow_worker_never_dispatches_device_action_tasks(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    store = WorkflowStore(database_path)
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
    admission = contract.RecordingAdmission(database_path)
    live = contract.MutableLiveCatalog()
    service = DeviceActionTaskService(
        store=store,
        template_catalog=catalog,
        authority=contract.AUTHORITY,
        live_catalog=live,
        admission=admission,
    )
    client = TestClient(
        create_workflow_app(WorkflowService(store), device_action_tasks=service)
    )
    harness = contract.Harness(
        database_path,
        store,
        client,
        catalog,
        snapshot.fingerprint,
        str(template["uuid"]),
        "",
        live,
        admission,
    )
    dispatcher = GenericRecordingDispatcher()
    worker = WorkflowRuntimeWorker(
        WorkflowRuntimeCoordinator(store),
        dispatcher=dispatcher,
        device_identity_resolver=lambda _identity: "robot",
        poll_interval_seconds=0.01,
    )
    try:
        created = client.post(
            "/api/v1/device-action-tasks", json=contract._request(harness)
        ).json()["data"]
        worker.start()
        time.sleep(0.1)

        assert dispatcher.payloads == []
        assert service.get(created["task_uuid"])["status"] == "pending"
        assert service.get(created["task_uuid"])["job_status"] == "pending"
    finally:
        worker.stop()
        worker.join(timeout=1)
        client.close()
        store.close()


def test_claim_hook_failure_cannot_leave_a_dispatchable_scheduler_run(
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

    def fail_before_claim(**_kwargs: Any) -> bool:
        raise RuntimeError("claim store unavailable")

    scheduler.set_device_action_task_hooks(before=fail_before_claim, on_error=None)
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
        response = client.post(
            "/api/v1/device-action-tasks", json=contract._request(harness)
        )
        assert response.status_code == 201
        created = response.json()["data"]
        assert service.get(created["task_uuid"])["status"] == "pending"
        assert service.get(created["task_uuid"])["job_status"] == "pending"
        assert scheduler.has_device_action_task(created["task_uuid"]) is False

        assert scheduler.reschedule() == []
        time.sleep(0.1)

        assert host.sent == []
        assert service.get(created["task_uuid"])["status"] == "pending"
        assert service.get(created["task_uuid"])["job_status"] == "pending"
    finally:
        client.close()
        bridge.stop()
        backend.stop()
        store.close()


def test_transport_uncertainty_opens_durable_fence_and_keeps_next_task_pending(
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
    scheduler, backend = create_edge_stack(host_node_getter=lambda: None)
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
        assert _wait(
            lambda: service.get(first["task_uuid"])["job_status"]
            == "execution_unknown"
        )
        second = client.post(
            "/api/v1/device-action-tasks", json=contract._request(harness)
        ).json()["data"]
        time.sleep(0.1)

        assert service.get(first["task_uuid"])["status"] == "running"
        assert service.get(first["task_uuid"])["control_status"] == (
            "waiting_reconciliation"
        )
        assert service.get(second["task_uuid"])["status"] == "pending"
        assert service.get(second["task_uuid"])["job_status"] == "pending"
    finally:
        client.close()
        bridge.stop()
        backend.stop()
        store.close()


def test_transport_unknown_fences_every_action_on_the_selected_device(
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
            ),
            contract._template_import(
                name="inspect",
                display_name="检查",
                resource_template_uuid=contract.RESOURCE_TEMPLATE_UUID,
                schema=contract.SIMPLE_SCHEMA,
            ),
        ],
    )
    templates = {item["name"]: item for item in snapshot.node_templates}
    scheduler, backend = create_edge_stack(host_node_getter=lambda: None)
    bridge = DeviceActionTaskRuntimeBridge(
        store=store,
        coordinator=WorkflowRuntimeCoordinator(store),
        scheduler=scheduler,
        backend=backend,
    )
    bridge.start()
    live = contract.MutableLiveCatalog()
    live.devices["robot"]["actions"]["inspect"] = {
        "type": "action",
        "schema": copy.deepcopy(contract.SIMPLE_SCHEMA),
    }
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
        str(templates["move"]["uuid"]),
        "",
        live,
        bridge,
    )
    try:
        first = client.post(
            "/api/v1/device-action-tasks", json=contract._request(harness)
        ).json()["data"]
        assert _wait(
            lambda: service.get(first["task_uuid"])["job_status"]
            == "execution_unknown"
        )

        second = client.post(
            "/api/v1/device-action-tasks",
            json=contract._request(
                harness,
                template_uuid=str(templates["inspect"]["uuid"]),
            ),
        ).json()["data"]
        time.sleep(0.1)

        assert service.get(second["task_uuid"])["status"] == "pending"
        assert service.get(second["task_uuid"])["job_status"] == "pending"
        assert bridge.busy_device_action_keys() == {"/devices/robot"}
    finally:
        client.close()
        bridge.stop()
        backend.stop()
        store.close()


def test_restart_promotes_inflight_claim_to_unknown_and_fences_new_runtime(
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
    first_host = FeedbackHost()
    first_host.auto_complete = False
    first_scheduler, first_backend = create_edge_stack(
        host_node_getter=lambda: first_host
    )
    first_host.backend = first_backend
    coordinator = WorkflowRuntimeCoordinator(store)
    first_bridge = DeviceActionTaskRuntimeBridge(
        store=store,
        coordinator=coordinator,
        scheduler=first_scheduler,
        backend=first_backend,
    )
    first_bridge.start()
    live = contract.MutableLiveCatalog()
    first_service = DeviceActionTaskService(
        store=store,
        template_catalog=catalog,
        authority=contract.AUTHORITY,
        live_catalog=live,
        admission=first_bridge,
    )
    first_client = TestClient(
        create_workflow_app(
            WorkflowService(store),
            device_action_tasks=first_service,
        )
    )
    harness = contract.Harness(
        tmp_path / "workflow.db",
        store,
        first_client,
        catalog,
        snapshot.fingerprint,
        str(template["uuid"]),
        "",
        live,
        first_bridge,
    )
    second_bridge: DeviceActionTaskRuntimeBridge | None = None
    second_backend: JobExecutionBackend | None = None
    second_client: TestClient | None = None
    try:
        first = first_client.post(
            "/api/v1/device-action-tasks", json=contract._request(harness)
        ).json()["data"]
        assert _wait(lambda: len(first_host.sent) == 1)
        first_bridge.stop()
        first_backend.stop()

        coordinator.recover_startup()

        second_host = FeedbackHost()
        second_host.auto_complete = False
        second_scheduler, second_backend = create_edge_stack(
            host_node_getter=lambda: second_host
        )
        second_host.backend = second_backend
        second_bridge = DeviceActionTaskRuntimeBridge(
            store=store,
            coordinator=coordinator,
            scheduler=second_scheduler,
            backend=second_backend,
        )
        second_bridge.start()
        second_service = DeviceActionTaskService(
            store=store,
            template_catalog=catalog,
            authority=contract.AUTHORITY,
            live_catalog=live,
            admission=second_bridge,
        )
        second_client = TestClient(
            create_workflow_app(
                WorkflowService(store),
                device_action_tasks=second_service,
            )
        )
        second_harness = contract.Harness(
            tmp_path / "workflow.db",
            store,
            second_client,
            catalog,
            snapshot.fingerprint,
            str(template["uuid"]),
            "",
            live,
            second_bridge,
        )
        second = second_client.post(
            "/api/v1/device-action-tasks",
            json=contract._request(second_harness),
        ).json()["data"]
        time.sleep(0.1)

        assert second_service.get(first["task_uuid"])["job_status"] == (
            "execution_unknown"
        )
        assert second_service.get(second["task_uuid"])["status"] == "pending"
        assert second_host.sent == []
        with store.transaction() as connection:
            claim_status = connection.execute(
                """
                SELECT claim_status FROM device_action_task
                WHERE workflow_task_uuid = ?
                """,
                (first["task_uuid"],),
            ).fetchone()[0]
        assert claim_status == "unknown"
    finally:
        first_client.close()
        if second_client is not None:
            second_client.close()
        if second_bridge is not None:
            second_bridge.stop()
        if second_backend is not None:
            second_backend.stop()
        first_bridge.stop()
        first_backend.stop()
        store.close()


def test_durable_completion_precedes_scheduler_release_crash_window(
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

    def crash_after_durable_commit(*_args: Any) -> bool:
        raise RuntimeError("simulated process crash before scheduler release")

    backend.add_job_completion_listener(crash_after_durable_commit)
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
        backend.publish_job_status(
            {"completed": True},
            host.sent[0],
            "success",
            serialize_result_info("", True, {"completed": True}),
        )
        assert _wait(lambda: service.get(first["task_uuid"])["status"] == "succeeded")
        time.sleep(0.1)

        assert service.get(first["task_uuid"])["job_status"] == "succeeded"
        assert service.get(second["task_uuid"])["status"] == "pending"
        assert len(host.sent) == 1
    finally:
        backend.remove_job_completion_listener(crash_after_durable_commit)
        client.close()
        bridge.stop()
        backend.stop()
        store.close()


def test_late_result_closes_transport_uncertainty_and_releases_fence(
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
    host = UncertainFeedbackHost()
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
        created = client.post(
            "/api/v1/device-action-tasks", json=contract._request(harness)
        ).json()["data"]
        assert _wait(
            lambda: service.get(created["task_uuid"])["job_status"]
            == "execution_unknown"
        )

        backend.publish_job_status(
            {"completed": True},
            host.sent[0],
            "success",
            serialize_result_info("", True, {"completed": True}),
        )
        assert _wait(
            lambda: service.get(created["task_uuid"])["status"] == "succeeded"
        )

        view = service.get(created["task_uuid"])
        assert view["job_status"] == "succeeded"
        assert view["control_status"] == "active"
        assert view["cleanup_status"] == "none"
        assert bridge.busy_device_action_keys() == set()
    finally:
        client.close()
        bridge.stop()
        backend.stop()
        store.close()


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
    monitor = MonitorBus()
    scheduler, backend = create_edge_stack(
        host_node_getter=lambda: host,
        monitor=monitor,
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
            lambda: (
                client.get(
                    f"/api/v1/device-action-tasks/{created['task_uuid']}"
                ).json()["data"]["status"]
                == "succeeded"
            )
        )

        view = client.get(f"/api/v1/device-action-tasks/{created['task_uuid']}").json()[
            "data"
        ]
        feedback = client.get(
            f"/api/v1/workflow-node-jobs/{created['job_uuid']}/feedback"
        ).json()["data"]
        assert host.sent[0].job_id == created["job_uuid"]
        assert host.sent[0].task_id == created["task_uuid"]
        assert view["job_status"] == "succeeded"
        assert view["output"] == {"completed": True}
        assert feedback["items"][0]["data"] == {"progress": 0.5}
        with store.transaction() as connection:
            source = dict(
                connection.execute(
                    "SELECT * FROM device_action_system_source"
                ).fetchone()
            )
        internal_identities = {
            source["workflow_uuid"],
            source["workflow_node_uuid"],
        }
        timeline = scheduler.timeline()
        assert timeline["completed"][0]["node_id"] == created["job_uuid"]
        assert not _contains_identity(timeline, internal_identities)
        assert not _contains_identity(
            {
                channel: monitor.recent(channel, 100)
                for channel in ("material", "device", "action", "scheduler")
            },
            internal_identities,
        )
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


def test_running_task_normalizes_result_with_its_frozen_contract_snapshot(
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
        assert _wait(lambda: len(host.sent) == 1)

        revised_schema = copy.deepcopy(contract.SIMPLE_SCHEMA)
        revised_schema["properties"]["result"] = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        }
        revised_schema["x-unilabos-action-contract"]["output_order"] = ["ok"]
        revised = catalog.replace(
            contract.AUTHORITY,
            [
                contract._template_import(
                    name="move",
                    display_name="移动（修订）",
                    resource_template_uuid=contract.RESOURCE_TEMPLATE_UUID,
                    schema=revised_schema,
                )
            ],
        )
        live.devices["robot"]["actions"]["move"] = {
            "type": "action",
            "schema": copy.deepcopy(revised_schema),
        }
        second = client.post(
            "/api/v1/device-action-tasks",
            json=contract._request(harness, fingerprint=revised.fingerprint),
        )
        assert second.status_code == 201, second.text

        backend.publish_job_status(
            {"completed": True},
            host.sent[0],
            "success",
            serialize_result_info("", True, {"completed": True}),
        )

        assert _wait(lambda: service.get(first["task_uuid"])["status"] != "running")
        assert service.get(first["task_uuid"])["status"] == "succeeded"
        assert service.get(first["task_uuid"])["output"] == {"completed": True}
    finally:
        client.close()
        bridge.stop()
        backend.stop()
        store.close()


def test_pending_task_dispatches_with_its_frozen_action_transport_type(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    store = WorkflowStore(database_path)
    catalog = TemplateCatalog(store)
    initial_import = contract._template_import(
        name="move",
        display_name="移动",
        resource_template_uuid=contract.RESOURCE_TEMPLATE_UUID,
        schema=contract.SIMPLE_SCHEMA,
    )
    initial_import.template["type"] = "transport.v1.Move"
    snapshot = catalog.replace(contract.AUTHORITY, [initial_import])
    template = snapshot.node_templates[0]
    live = contract.MutableLiveCatalog()
    live.devices["robot"]["actions"]["move"]["type"] = "transport.v1.Move"
    admission = contract.RecordingAdmission(database_path)
    service = DeviceActionTaskService(
        store=store,
        template_catalog=catalog,
        authority=contract.AUTHORITY,
        live_catalog=live,
        admission=admission,
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
        admission,
    )
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
    try:
        first = client.post(
            "/api/v1/device-action-tasks", json=contract._request(harness)
        ).json()["data"]
        assert service.get(first["task_uuid"])["status"] == "pending"

        revised_import = contract._template_import(
            name="move",
            display_name="移动（新 transport）",
            resource_template_uuid=contract.RESOURCE_TEMPLATE_UUID,
            schema=contract.SIMPLE_SCHEMA,
        )
        revised_import.template["type"] = "transport.v2.Move"
        revised = catalog.replace(contract.AUTHORITY, [revised_import])
        live.devices["robot"]["actions"]["move"]["type"] = "transport.v2.Move"
        second = client.post(
            "/api/v1/device-action-tasks",
            json=contract._request(harness, fingerprint=revised.fingerprint),
        ).json()["data"]

        with store.transaction() as connection:
            source = connection.execute(
                "SELECT source_revision, contract_snapshot "
                "FROM device_action_system_source"
            ).fetchone()
            snapshots = {
                row["uuid"]: json.loads(row["workflow_snapshot"])
                for row in connection.execute(
                    "SELECT uuid, workflow_snapshot FROM workflow_task"
                )
            }
        assert source["source_revision"] == 2
        assert json.loads(source["contract_snapshot"])["action_type"] == (
            "transport.v2.Move"
        )
        assert snapshots[first["task_uuid"]]["nodes"][0]["action_type"] == (
            "transport.v1.Move"
        )
        assert snapshots[second["task_uuid"]]["nodes"][0]["action_type"] == (
            "transport.v2.Move"
        )

        bridge.start()
        assert _wait(lambda: len(host.sent) == 1)
        assert host.action_types[0] == "transport.v1.Move"

        backend.publish_job_status(
            {"completed": True},
            host.sent[0],
            "success",
            serialize_result_info("", True, {"completed": True}),
        )
        assert _wait(lambda: len(host.sent) == 2)
        assert host.action_types[1] == "transport.v2.Move"
        assert service.get(first["task_uuid"])["status"] == "succeeded"
        assert service.get(second["task_uuid"])["status"] == "running"
    finally:
        client.close()
        bridge.stop()
        backend.stop()
        store.close()


def test_durable_cancel_command_requests_host_cancel_and_finishes_canceled(
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
    coordinator = WorkflowRuntimeCoordinator(store)
    bridge = DeviceActionTaskRuntimeBridge(
        store=store,
        coordinator=coordinator,
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
    workflow_service = WorkflowService(store)
    client = TestClient(
        create_workflow_app(workflow_service, device_action_tasks=service)
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
        created = client.post(
            "/api/v1/device-action-tasks", json=contract._request(harness)
        ).json()["data"]
        assert _wait(lambda: len(host.sent) == 1)
        response = client.post(
            f"/api/v1/workflow-tasks/{created['task_uuid']}/commands",
            json={
                "type": "cancel",
                "target_node_uuid": None,
                "idempotency_key": str(uuid4()),
                "description": "operator cancel",
                "meta_data": {},
            },
        )
        assert response.status_code == 201
        coordinator.consume_next_command(created["task_uuid"])
        bridge.sweep_cancellations()
        assert host.cancel_requests == [created["job_uuid"]]

        backend.publish_job_status(
            {},
            host.sent[0],
            "failed",
            serialize_result_info("Job was cancelled", False, {}),
        )
        assert _wait(lambda: service.get(created["task_uuid"])["status"] == "canceled")
        assert service.get(created["task_uuid"])["job_status"] == "canceled"
    finally:
        client.close()
        bridge.stop()
        backend.stop()
        store.close()
