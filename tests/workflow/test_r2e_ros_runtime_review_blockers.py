"""Reviewer-blocker regressions for the R2E ROS Workflow runtime round."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from unilabos.package_manager import WorkspaceSource, compile_package_source
from unilabos.ros.nodes.presets import host_node as host_node_module
from unilabos.workflow.composition import (
    compose_workflow_runtime,
    reset_workflow_service_for_test,
)
from unilabos.workflow.models import WorkflowNodeWrite
from unilabos.workflow.runtime import WorkflowRuntimeCoordinator, WorkflowRuntimeWorker
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

_FIXTURE_WORKSPACE = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "r2e_szlab_workspace"
)
_LEGACY_IDENTITY_KEYS = {"job_id", "task_id", "node_id", "workflow_id"}
_UUID_IDENTITY_KEYS = {"job_uuid", "task_uuid", "node_uuid", "workflow_uuid"}


class _RecordingDispatcher:
    def __init__(self, *, raise_on_dispatch: bool = False) -> None:
        self.raise_on_dispatch = raise_on_dispatch
        self.payloads: list[dict[str, Any]] = []
        self.listeners: list[Any] = []
        self.removed: list[Any] = []

    def execution_ready(self) -> bool:
        return True

    def dispatch(self, payload: dict[str, Any]) -> None:
        self.payloads.append(dict(payload))
        if self.raise_on_dispatch:
            raise RuntimeError("synchronous transport uncertainty")

    def add_job_finished_listener(self, listener: Any) -> None:
        self.listeners.append(listener)

    def remove_job_finished_listener(self, listener: Any) -> None:
        self.listeners.remove(listener)
        self.removed.append(listener)


class _EmptyMessageDispatcher(_RecordingDispatcher):
    def dispatch(self, payload: dict[str, Any]) -> None:
        self.payloads.append(dict(payload))
        raise RuntimeError()


class _MaterialGateCoordinator:
    def __init__(self) -> None:
        self.starts: list[str] = []
        self.task = {
            "uuid": str(uuid4()),
            "workflow_uuid": str(uuid4()),
            "status": "pending",
            "control_status": "active",
            "workflow_snapshot": {
                "nodes": [{"uuid": str(uuid4()), "type": "material_source"}]
            },
        }

    def _execution_tasks(self) -> list[dict[str, Any]]:
        return [dict(self.task)]

    def start_task(self, task_uuid: str) -> dict[str, Any]:
        self.starts.append(task_uuid)
        return {**self.task, "status": "running"}


def _create_device_task(
    service: WorkflowService,
) -> tuple[dict[str, Any], dict[str, Any]]:
    workflow = service.create_workflow(
        name="ROS dispatch uncertainty",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=str(uuid4()),
    )
    node = WorkflowNodeWrite(
        uuid=str(uuid4()),
        material_uuid=str(uuid4()),
        name="prepare",
        status="idle",
        type="device_action",
        pose={},
        param={"batch": 7},
        action_name="prepare",
        action_type="example_msgs/action/Prepare",
        execution_policy={},
        disabled=False,
        minimized=False,
        meta_data={},
    )
    service.save_graph(
        workflow["uuid"],
        revision=workflow["revision"],
        nodes=[node],
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
    return task, service.list_workflow_node_jobs(task["uuid"])[0]


def _wait_for_job_status(
    service: WorkflowService,
    job_uuid: str,
    expected: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + 2
    observed = service.get_workflow_node_job(job_uuid)
    while observed["status"] != expected and time.monotonic() < deadline:
        time.sleep(0.01)
        observed = service.get_workflow_node_job(job_uuid)
    return observed


def test_synchronous_dispatch_error_is_unknown_and_payload_uses_uuid_identities(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store)
    task, job = _create_device_task(service)
    dispatcher = _RecordingDispatcher(raise_on_dispatch=True)
    worker = WorkflowRuntimeWorker(
        WorkflowRuntimeCoordinator(store),
        dispatcher=dispatcher,
        device_identity_resolver=lambda _identity: "r2e_szlab_mixer",
        poll_interval_seconds=0.01,
    )
    try:
        worker.start()
        observed = _wait_for_job_status(service, job["uuid"], "execution_unknown")
    finally:
        worker.stop()
        worker.join(timeout=1)
        service.close()

    assert observed["status"] == "execution_unknown"
    assert observed["uncertainty_reason"] == "synchronous transport uncertainty"
    assert len(dispatcher.payloads) == 1
    payload = dispatcher.payloads[0]
    assert _LEGACY_IDENTITY_KEYS.isdisjoint(payload)
    assert _UUID_IDENTITY_KEYS.issubset(payload)
    assert payload["job_uuid"] == job["uuid"]
    assert payload["task_uuid"] == task["uuid"]
    assert payload["node_uuid"] == job["workflow_node_uuid"]
    assert payload["workflow_uuid"] == task["workflow_uuid"]


def test_empty_dispatch_error_still_opens_stable_reconciliation_fence(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store)
    task, job = _create_device_task(service)
    dispatcher = _EmptyMessageDispatcher()
    worker = WorkflowRuntimeWorker(
        WorkflowRuntimeCoordinator(store),
        dispatcher=dispatcher,
        device_identity_resolver=lambda _identity: "r2e_szlab_mixer",
        poll_interval_seconds=0.01,
    )
    try:
        worker.start()
        observed_job = _wait_for_job_status(
            service,
            job["uuid"],
            "execution_unknown",
        )
        observed_task = service.get_workflow_task(task["uuid"])
    finally:
        worker.stop()
        worker.join(timeout=1)
        service.close()

    assert observed_job["status"] == "execution_unknown"
    assert observed_job["status"] != "running"
    assert observed_job["uncertainty_reason"] == "dispatch_outcome_unknown"
    assert observed_task["control_status"] == "waiting_reconciliation"
    assert observed_task["cleanup_status"] == "requires_attention"
    assert observed_task["attention_reason"] == "dispatch_outcome_unknown"


def test_worker_stop_unsubscribes_dispatch_completion_listener(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    dispatcher = _RecordingDispatcher()
    worker = WorkflowRuntimeWorker(
        WorkflowRuntimeCoordinator(store),
        dispatcher=dispatcher,
        device_identity_resolver=lambda _identity: "r2e_szlab_mixer",
    )

    assert len(dispatcher.listeners) == 1
    worker.stop()

    assert dispatcher.listeners == []
    assert len(dispatcher.removed) == 1
    store.close()


def test_material_source_task_dispatch_is_fail_closed_until_scheduler_proves_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _MaterialGateCoordinator()
    dispatcher = _RecordingDispatcher()
    worker = WorkflowRuntimeWorker(  # type: ignore[arg-type]
        coordinator,
        dispatcher=dispatcher,
        device_identity_resolver=lambda _identity: "r2e_szlab_mixer",
    )
    advanced: list[str] = []
    monkeypatch.setattr(
        worker,
        "_advance_task",
        lambda task: advanced.append(task["uuid"]),
    )
    admitted = False
    try:
        worker._sweep_execution_tasks()
        worker.set_task_reconciler(
            lambda _task_uuid: None,
            lambda _task_uuid: admitted,
        )
        worker._sweep_execution_tasks()
        admitted = True
        worker._sweep_execution_tasks()
    finally:
        worker.stop()

    assert coordinator.starts == [coordinator.task["uuid"]]
    assert advanced == [coordinator.task["uuid"]]


@pytest.mark.parametrize(
    "changed_capability",
    ["dispatcher", "resolver", "catalog"],
)
def test_composition_rejects_changed_execution_configuration(
    tmp_path: Path,
    changed_capability: str,
) -> None:
    catalog = compile_package_source(WorkspaceSource(_FIXTURE_WORKSPACE))
    dispatcher = _RecordingDispatcher()

    def resolver(_identity: str) -> str:
        return "r2e_szlab_mixer"

    first_options: dict[str, Any] = {
        "workflow_job_dispatcher": dispatcher,
        "device_identity_resolver": resolver,
        "workflow_package_catalogs": (catalog,),
    }
    second_options = dict(first_options)
    if changed_capability == "dispatcher":
        second_options["workflow_job_dispatcher"] = _RecordingDispatcher()
    elif changed_capability == "resolver":
        second_options["device_identity_resolver"] = lambda _identity: (
            "replacement_mixer"
        )
    else:
        second_options["workflow_package_catalogs"] = (
            replace(catalog, catalog_digest="sha256:" + "f" * 64),
        )

    try:
        compose_workflow_runtime(tmp_path / "runtime", **first_options)
        with pytest.raises(RuntimeError, match="configuration|配置"):
            compose_workflow_runtime(tmp_path / "runtime", **second_options)
    finally:
        reset_workflow_service_for_test()


def test_hostnode_action_client_failure_prevents_device_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Logger:
        def __getattr__(self, _name: str) -> Any:
            return lambda *_args, **_kwargs: None

    class _FailingActionClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("invalid action type support")

    ros_node = SimpleNamespace(
        namespace="/devices",
        _action_value_mappings={
            "prepare": {"type": object()},
        },
    )
    device = SimpleNamespace(_ros_node=ros_node)
    host = SimpleNamespace(
        devices_names={},
        device_machine_names={},
        devices_instances={},
        _action_value_mappings={},
        _action_clients={},
        _online_devices=set(),
        lab_logger=lambda: _Logger(),
        _report_action_locks_free=lambda _pairs: None,
    )
    monkeypatch.setattr(
        host_node_module,
        "initialize_device_from_dict",
        lambda _device_id, _device_config: device,
    )
    monkeypatch.setattr(host_node_module, "ActionClient", _FailingActionClient)

    initialized = host_node_module.HostNode.initialize_device(
        host,
        "r2e_szlab_mixer",
        object(),
    )

    assert initialized is False
    assert host._online_devices == set()
    assert host._action_clients == {}


def test_hostnode_test_mode_builds_typed_action_outputs_from_goal_values() -> None:
    resource = {
        "uuid": str(uuid4()),
        "resource_template_uuid": str(uuid4()),
    }
    host = SimpleNamespace(
        _action_value_mappings={
            "robot": {
                "pick": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "goal": {
                                "type": "object",
                                "properties": {
                                    "resource": {"$slot": "ResourceSlot"},
                                    "site": {"type": "string"},
                                },
                            },
                            "result": {
                                "type": "object",
                                "properties": {
                                    "resource": {"$slot": "ResourceSlot"},
                                    "success": {"type": "boolean"},
                                    "state": {
                                        "type": "string",
                                        "enum": ["RUNNING", "SUCCEEDED"],
                                    },
                                    "message": {"type": "string"},
                                },
                            },
                        },
                        "x-unilabos-action-contract": {
                            "output_order": [
                                "resource",
                                "success",
                                "state",
                                "message",
                            ]
                        },
                    }
                }
            }
        }
    )

    result = host_node_module.HostNode._build_test_mode_return(
        host,
        "robot",
        "pick",
        {"resource": resource, "site": "L1B1"},
    )

    assert result == {
        "test_mode": True,
        "action_name": "pick",
        "resource": resource,
        "success": True,
        "state": "SUCCEEDED",
        "message": "",
    }
    assert result["resource"] is not resource


def test_hostnode_test_mode_reads_action_schema_from_registered_local_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = {
        "uuid": str(uuid4()),
        "resource_template_uuid": str(uuid4()),
    }
    action_mapping = {
        "schema": {
            "properties": {
                "result": {
                    "properties": {
                        "resource": {"$slot": "ResourceSlot"},
                        "success": {"type": "boolean"},
                    }
                }
            },
            "x-unilabos-action-contract": {
                "output_order": ["resource", "success"]
            },
        }
    }
    monkeypatch.setattr(
        host_node_module,
        "registered_devices",
        {
            "robot": {
                "base_node_instance": SimpleNamespace(
                    _action_value_mappings={"pick": action_mapping}
                )
            }
        },
        raising=False,
    )
    host = SimpleNamespace(
        _action_value_mappings={"robot": {"pick": {"type": "discovered"}}}
    )

    result = host_node_module.HostNode._build_test_mode_return(
        host,
        "robot",
        "pick",
        {"resource": resource},
    )

    assert result["resource"] == resource
    assert result["success"] is True


def test_hostnode_test_mode_reads_action_schema_from_device_type_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = {
        "uuid": str(uuid4()),
        "resource_template_uuid": str(uuid4()),
    }
    action_mapping = {
        "schema": {
            "properties": {
                "result": {
                    "properties": {
                        "resource": {"$slot": "ResourceSlot"},
                        "success": {"type": "boolean"},
                    }
                }
            },
            "x-unilabos-action-contract": {
                "output_order": ["resource", "success"]
            },
        }
    }
    monkeypatch.setattr(host_node_module, "registered_devices", {})
    monkeypatch.setattr(
        host_node_module,
        "lab_registry",
        SimpleNamespace(
            device_type_registry={
                "robot_type": {
                    "class": {
                        "action_value_mappings": {"pick": action_mapping}
                    }
                }
            }
        ),
    )
    host = SimpleNamespace(
        _action_value_mappings={"robot": {"pick": {"type": "discovered"}}},
        devices_config=SimpleNamespace(
            trees=[
                SimpleNamespace(
                    root_node=SimpleNamespace(
                        res_content=SimpleNamespace(
                            id="robot",
                            klass="robot_type",
                        )
                    )
                )
            ]
        ),
    )

    result = host_node_module.HostNode._build_test_mode_return(
        host,
        "robot",
        "pick",
        {"resource": resource},
    )

    assert result["resource"] == resource
    assert result["success"] is True
