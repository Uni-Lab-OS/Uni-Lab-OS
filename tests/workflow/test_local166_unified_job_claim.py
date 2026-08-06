"""LOCAL-166 单动作与完整工作流共享作业执行占用回归。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi.testclient import TestClient

from unilabos.app.scheduler.inventory import (
    InventoryService,
    JobClaimAcquireCommand,
    ResourceTemplateIdentity,
    TaskMaterialAdmissionCommand,
    TaskMaterialAdmissionSource,
)
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.claim_intent import mutable_material_roots
from unilabos.workflow.job_claim_execution import WorkflowJobClaimExecution
from unilabos.workflow.job_claims import WorkflowJobClaimCoordinator
from unilabos.workflow.models import WorkflowNodeWrite
from unilabos.workflow.runtime import WorkflowRuntimeCoordinator, WorkflowRuntimeWorker
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

DEVICE_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000166"
DEVICE_MATERIAL_UUID = str(uuid5(NAMESPACE_URL, "local166:stirrer"))
SAMPLE_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000166"
SAMPLE_MATERIAL_UUID = str(uuid5(NAMESPACE_URL, "local166:sample"))
MOUNT_TEMPLATE_UUID = "30000000-0000-4000-8000-000000000166"
MOUNT_MATERIAL_UUID = str(uuid5(NAMESPACE_URL, "local166:mount"))
SAMPLE_SITE_UUID = str(uuid5(NAMESPACE_URL, "local166:site"))


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


def _wait(predicate: Any, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("timed out waiting for unified JobExecutionClaim")
        time.sleep(0.01)


def _inventory(working_dir: Path) -> InventoryService:
    inventory = InventoryService.open(
        working_dir=working_dir,
        resource_templates={
            DEVICE_TEMPLATE_UUID: ResourceTemplateIdentity(
                uuid=DEVICE_TEMPLATE_UUID,
                material_class="Stirrer",
            ),
            SAMPLE_TEMPLATE_UUID: ResourceTemplateIdentity(
                uuid=SAMPLE_TEMPLATE_UUID,
                material_class="Sample",
            ),
            MOUNT_TEMPLATE_UUID: ResourceTemplateIdentity(
                uuid=MOUNT_TEMPLATE_UUID,
                material_class="Deck",
            ),
        },
    )
    inventory.bootstrap_resource_graph(
        {
            "source_id": "local166-device-graph.json",
            "fingerprint": "sha256:" + "1" * 64,
            "materials": [
                {
                    "uuid": DEVICE_MATERIAL_UUID,
                    "resource_template_uuid": DEVICE_TEMPLATE_UUID,
                    "parent_uuid": None,
                    "class": "Stirrer",
                    "barcode": "",
                    "name": "stirrer",
                    "description": "LOCAL-166 shared executor",
                    "meta_data": {
                        "source": "resource-tree-set",
                        "source_node_id": "stirrer",
                    },
                    "config": {},
                    "data": {},
                    "material_kind": "device",
                },
                {
                    "uuid": SAMPLE_MATERIAL_UUID,
                    "resource_template_uuid": SAMPLE_TEMPLATE_UUID,
                    "parent_uuid": None,
                    "class": "Sample",
                    "barcode": "",
                    "name": "sample",
                    "description": "LOCAL-166 shared business material",
                    "meta_data": {
                        "source": "resource-tree-set",
                        "source_node_id": "sample",
                    },
                    "config": {},
                    "data": {},
                    "material_kind": "business",
                },
            ],
            "relative_positions": [],
            "sites": [],
        }
    )
    inventory.create_material(
        material_uuid=MOUNT_MATERIAL_UUID,
        resource_template_uuid=MOUNT_TEMPLATE_UUID,
        barcode="LOCAL166-MOUNT",
        name="LOCAL-166 mount",
    )
    inventory.create_site(
        site_uuid=SAMPLE_SITE_UUID,
        description="LOCAL-166 sample Site",
        meta_data={"slot": "A1"},
        material_uuid=MOUNT_MATERIAL_UUID,
        name="A1",
        sort_order=0,
        allowed_resource_template_uuids=[SAMPLE_TEMPLATE_UUID],
        occupied_material_uuid=SAMPLE_MATERIAL_UUID,
        position_x=0.0,
        position_y=0.0,
        position_z=0.0,
        depth=1.0,
        length=1.0,
        width=1.0,
    )
    return inventory


def _workflow_task(
    service: WorkflowService,
) -> tuple[dict[str, Any], dict[str, Any]]:
    workflow = service.create_workflow(
        name="LOCAL-166 stirrer workflow",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=str(uuid4()),
    )
    node = WorkflowNodeWrite(
        uuid=str(uuid4()),
        material_uuid=DEVICE_MATERIAL_UUID,
        name="stir",
        status="idle",
        type="device_action",
        pose={},
        param={"duration_seconds": 30},
        action_name="stir",
        action_type="test.action.Stir",
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


def _acquire_d1a_claim(
    inventory: InventoryService,
    *,
    task_uuid: str,
    job_uuid: str,
) -> Any:
    return inventory.acquire_job_claim(
        JobClaimAcquireCommand(
            schema_version=1,
            command_uuid=str(uuid4()),
            idempotency_key=f"local166:d1a:{job_uuid}",
            workflow_task_uuid=task_uuid,
            workflow_node_job_uuid=job_uuid,
            attempt=1,
            device_material_uuid=DEVICE_MATERIAL_UUID,
            mutable_material_root_uuids=(),
            occupancy_changing_site_uuids=(),
        )
    )


def test_claim_intent_uses_frozen_resource_slot_contract_without_value_guessing() -> (
    None
):
    material_a = str(uuid4())
    material_b = str(uuid4())
    ignored = str(uuid4())
    schema = {
        "properties": {
            "goal": {
                "properties": {
                    "sample": {"$slot": "ResourceSlot"},
                    "optional": {
                        "anyOf": [{"$slot": "ResourceSlot"}, {"type": "null"}]
                    },
                    "batch": {
                        "type": "array",
                        "items": {"$slot": "ResourceSlot"},
                    },
                    "read_only": {
                        "$slot": "ResourceSlot",
                        "x-unilabos-material-lock": False,
                    },
                    "ordinary_object": {"type": "object"},
                }
            }
        }
    }

    assert mutable_material_roots(
        schema,
        {
            "sample": {"uuid": material_b},
            "optional": None,
            "batch": [{"uuid": material_a}, {"uuid": material_b}],
            "read_only": {"uuid": ignored},
            "ordinary_object": {"uuid": ignored},
        },
    ) == tuple(sorted((material_a, material_b)))


def test_workflow_claim_includes_contract_material_member(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store)
    inventory = _inventory(tmp_path)
    task, job = _workflow_task(service)
    claims = WorkflowJobClaimCoordinator(store, inventory)
    try:
        admitted = inventory.admit_task(
            TaskMaterialAdmissionCommand(
                schema_version=1,
                command_uuid=str(uuid4()),
                idempotency_key=f"local166:material-admission:{task['uuid']}",
                workflow_task_uuid=task["uuid"],
                workflow_snapshot_fingerprint="sha256:" + "2" * 64,
                sources=(
                    TaskMaterialAdmissionSource(
                        material_source_node_uuid=str(uuid4()),
                        mode="existing",
                        resource_template_uuid=SAMPLE_TEMPLATE_UUID,
                        mount={"uuid": MOUNT_MATERIAL_UUID},
                        material_uuid=SAMPLE_MATERIAL_UUID,
                        site_uuid=SAMPLE_SITE_UUID,
                        candidate_site_uuids=(),
                        flow_role="primary_sample",
                    ),
                ),
            )
        )
        assert admitted.status == "admitted"
        admission = claims.acquire(
            task_uuid=task["uuid"],
            job_uuid=job["uuid"],
            attempt=1,
            device_id="stirrer",
            param_schema={
                "properties": {
                    "goal": {"properties": {"sample": {"$slot": "ResourceSlot"}}}
                }
            },
            param={
                "sample": {
                    "uuid": SAMPLE_MATERIAL_UUID,
                    "resource_template_uuid": SAMPLE_TEMPLATE_UUID,
                }
            },
        )

        assert admission.claim is not None
        assert {
            (member.resource_kind, member.resource_uuid)
            for member in admission.claim.members
        } == {
            ("device_material", DEVICE_MATERIAL_UUID),
            ("business_material", SAMPLE_MATERIAL_UUID),
        }
        execution = WorkflowJobClaimExecution(
            WorkflowRuntimeCoordinator(store),
            inventory,
        )
        receipt = execution.commit_terminal(
            service.get_workflow_node_job(job["uuid"]),
            outcome="succeeded",
            return_info={"sample": {"uuid": SAMPLE_MATERIAL_UUID}},
            error_info=[],
        )
        assert receipt is None
        assert inventory.get_job_claim(job["uuid"], 1).state == "uncertain"
        assert inventory.get_terminal_material_changeset(job["uuid"], 1) is None
    finally:
        service.close()
        inventory.close()


def test_d1a_claim_blocks_workflow_without_second_dispatch(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store)
    inventory = _inventory(tmp_path)
    task, job = _workflow_task(service)
    dispatcher = _RecordingDispatcher()
    worker = WorkflowRuntimeWorker(
        WorkflowRuntimeCoordinator(store),
        dispatcher=dispatcher,
        device_identity_resolver=lambda _identity: "stirrer",
        inventory=inventory,
        poll_interval_seconds=0.01,
    )
    d1a_task_uuid = str(uuid4())
    d1a_job_uuid = str(uuid4())
    occupied = _acquire_d1a_claim(
        inventory,
        task_uuid=d1a_task_uuid,
        job_uuid=d1a_job_uuid,
    )
    try:
        assert occupied.status == "acquired"
        worker.start()
        _wait(
            lambda: (
                service.get_workflow_node_job(job["uuid"])["claim_status"]
                == "waiting_for_claim"
            )
        )

        observed = service.get_workflow_node_job(job["uuid"])
        assert dispatcher.payloads == []
        assert observed["status"] == "pending"
        assert observed["attempt"] == 1
        assert observed["blocking_claim_uuid"] == occupied.claim.uuid
        assert service.get_workflow_task(task["uuid"])["status"] == "running"
    finally:
        worker.stop()
        worker.join(timeout=1)
        service.close()
        inventory.close()


def test_workflow_claim_blocks_d1a_without_second_attempt(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store)
    inventory = _inventory(tmp_path)
    task, job = _workflow_task(service)
    dispatcher = _RecordingDispatcher()
    worker = WorkflowRuntimeWorker(
        WorkflowRuntimeCoordinator(store),
        dispatcher=dispatcher,
        device_identity_resolver=lambda _identity: "stirrer",
        inventory=inventory,
        poll_interval_seconds=0.01,
    )
    try:
        worker.start()
        _wait(lambda: len(dispatcher.payloads) == 1)
        workflow_claim = inventory.get_job_claim(job["uuid"], 1)

        d1a_job_uuid = str(uuid4())
        blocked = _acquire_d1a_claim(
            inventory,
            task_uuid=str(uuid4()),
            job_uuid=d1a_job_uuid,
        )

        assert blocked.status == "blocked"
        assert blocked.claim is None
        assert blocked.diagnostics == (
            {
                "code": "claim_blocked",
                "blocking_claim_uuid": workflow_claim.uuid,
            },
        )
        assert len(dispatcher.payloads) == 1
        observed = service.get_workflow_node_job(job["uuid"])
        assert observed["attempt"] == 1
        assert observed["claim_uuid"] == workflow_claim.uuid
        assert observed["claim_members"][0]["resource_uuid"] == DEVICE_MATERIAL_UUID
        assert service.get_workflow_task(task["uuid"])["status"] == "running"

        client = TestClient(create_workflow_app(service))
        task_jobs = client.get(f"/api/v1/workflow-tasks/{task['uuid']}/jobs").json()[
            "data"
        ]
        job_view = client.get(f"/api/v1/workflow-node-jobs/{job['uuid']}").json()[
            "data"
        ]
        for projected in (task_jobs[0], job_view):
            assert projected["claim_status"] == "claimed"
            assert projected["claim_uuid"] == workflow_claim.uuid
            assert projected["claim_members"][0]["resource_uuid"] == (
                DEVICE_MATERIAL_UUID
            )
    finally:
        worker.stop()
        worker.join(timeout=1)
        service.close()
        inventory.close()
