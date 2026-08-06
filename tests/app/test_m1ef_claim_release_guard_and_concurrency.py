"""M1EF retryable Task release guard and independent-client contention."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

import unilabos.app.scheduler.inventory as inventory_api
from tests.app.test_m1ef_inventory_claim_lifecycle import (
    DEVICE_MATERIAL_UUID,
    FIRST_JOB_UUID,
    WORKFLOW_TASK_UUID,
    _admit_task,
    _claim_command,
    _open_inventory,
    _resource_templates,
    _seed_business_material_and_site,
    _seed_device_material,
)

RUNNING_COMMAND_UUID = "83000000-0000-4000-8000-000000000411"
CHANGESET_COMMAND_UUID = "84000000-0000-4000-8000-000000000411"
CLAIM_RELEASE_COMMAND_UUID = "85000000-0000-4000-8000-000000000411"
TASK_RELEASE_COMMAND_UUID = "86000000-0000-4000-8000-000000000411"
SECOND_JOB_UUID = "b1000000-0000-4000-8000-000000000412"
SECOND_ACQUIRE_COMMAND_UUID = "82000000-0000-4000-8000-000000000412"
SECOND_DEVICE_UUID = "51000000-0000-4000-8000-000000000412"


def _seed(tmp_path: Path) -> inventory_api.InventoryService:
    inventory = _open_inventory(tmp_path)
    _seed_device_material(inventory)
    _seed_business_material_and_site(inventory)
    _admit_task(inventory)
    return inventory


def test_task_release_same_command_advances_blocked_to_released(
    tmp_path: Path,
) -> None:
    inventory = _seed(tmp_path)
    task_release = inventory_api.TaskMaterialReleaseCommand(
        schema_version=1,
        command_uuid=TASK_RELEASE_COMMAND_UUID,
        idempotency_key="m1ef-task-release-guard-411",
        workflow_task_uuid=WORKFLOW_TASK_UUID,
        reason="workflow_task_terminal",
    )
    try:
        acquired = inventory.acquire_job_claim(
            _claim_command(
                command_uuid="82000000-0000-4000-8000-000000000411",
                job_uuid=FIRST_JOB_UUID,
            )
        )
        assert acquired.claim is not None
        claim = acquired.claim

        blocked = inventory.release_task(task_release)
        assert blocked.status == "blocked"
        assert inventory.release_task(task_release) == blocked
        assert inventory.has_active_task_reservation(
            WORKFLOW_TASK_UUID,
            blocked.reservation_uuid or "",
        )

        running = inventory.mark_job_claim_running(
            inventory_api.JobClaimStateCommand(
                schema_version=1,
                command_uuid=RUNNING_COMMAND_UUID,
                idempotency_key="m1ef-running-release-guard-411",
                workflow_node_job_uuid=FIRST_JOB_UUID,
                attempt=1,
                claim_uuid=claim.uuid,
                fencing_token=claim.fencing_token,
                evidence_kind="driver_accepted",
                evidence_fingerprint="a" * 64,
            )
        )
        assert running.claim is not None
        receipt = inventory.commit_material_changeset(
            inventory_api.MaterialChangeSetCommand(
                schema_version=1,
                command_uuid=CHANGESET_COMMAND_UUID,
                idempotency_key="m1ef-noop-release-guard-411",
                workflow_task_uuid=WORKFLOW_TASK_UUID,
                workflow_node_job_uuid=FIRST_JOB_UUID,
                attempt=1,
                claim_uuid=claim.uuid,
                fencing_token=claim.fencing_token,
                effect_identity="terminal",
                outcome="succeeded",
                result={},
                effects=(),
            )
        )
        inventory.release_job_claim(
            inventory_api.JobClaimReleaseCommand(
                schema_version=1,
                command_uuid=CLAIM_RELEASE_COMMAND_UUID,
                idempotency_key="m1ef-claim-release-guard-411",
                workflow_node_job_uuid=FIRST_JOB_UUID,
                attempt=1,
                claim_uuid=claim.uuid,
                fencing_token=claim.fencing_token,
                release_proof_kind="terminal_settled",
                material_changeset_uuid=receipt.uuid,
                material_changeset_fingerprint=receipt.deterministic_fingerprint,
                workflow_terminal_fingerprint="b" * 64,
                reason="workflow_job_terminal_settled",
            )
        )

        released = inventory.release_task(task_release)
        assert released.status == "released"
        assert released.command_uuid == blocked.command_uuid
        assert released.outbox_sequence > blocked.outbox_sequence
        assert inventory.get_command_result(TASK_RELEASE_COMMAND_UUID) == released
        assert not inventory.has_active_task_reservation(
            WORKFLOW_TASK_UUID,
            released.reservation_uuid or "",
        )
    finally:
        inventory.close()


def test_two_inventory_clients_acquire_complete_set_all_or_none(
    tmp_path: Path,
) -> None:
    first = _seed(tmp_path)
    second = inventory_api.InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    barrier = Barrier(2)

    def acquire(
        service: inventory_api.InventoryService,
        command_uuid: str,
        job_uuid: str,
    ) -> inventory_api.JobClaimResult:
        barrier.wait(timeout=5)
        return service.acquire_job_claim(
            _claim_command(command_uuid=command_uuid, job_uuid=job_uuid)
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(
                future.result(timeout=10)
                for future in (
                    pool.submit(
                        acquire,
                        first,
                        "82000000-0000-4000-8000-000000000413",
                        FIRST_JOB_UUID,
                    ),
                    pool.submit(
                        acquire,
                        second,
                        SECOND_ACQUIRE_COMMAND_UUID,
                        SECOND_JOB_UUID,
                    ),
                )
            )
        assert sorted(result.status for result in results) == ["acquired", "blocked"]
        unsettled = first.list_unsettled_claims()
        assert len(unsettled) == 1
        assert {
            (member.resource_kind, member.resource_uuid)
            for member in unsettled[0].members
        } >= {("device_material", DEVICE_MATERIAL_UUID)}
    finally:
        second.close()
        first.close()


def test_same_job_cannot_acquire_new_attempt_until_previous_claim_released(
    tmp_path: Path,
) -> None:
    inventory = _open_inventory(tmp_path)
    imported = inventory.bootstrap_resource_graph(
        {
            "source_id": "m1ef-two-device-graph.json",
            "fingerprint": "sha256:" + "d" * 64,
            "materials": [
                {
                    "uuid": material_uuid,
                    "resource_template_uuid": ("21000000-0000-4000-8000-000000000301"),
                    "parent_uuid": None,
                    "class": "Heater",
                    "barcode": "",
                    "name": name,
                    "description": None,
                    "meta_data": {"source_node_id": name},
                    "config": {},
                    "data": {},
                    "material_kind": "device",
                }
                for material_uuid, name in (
                    (DEVICE_MATERIAL_UUID, "heater-attempt-1"),
                    (SECOND_DEVICE_UUID, "heater-attempt-2"),
                )
            ],
            "relative_positions": [],
            "sites": [],
        }
    )
    assert imported["status"] == "imported"
    first = inventory.acquire_job_claim(
        inventory_api.JobClaimAcquireCommand(
            schema_version=1,
            command_uuid="82000000-0000-4000-8000-000000000414",
            idempotency_key="m1ef-same-job-attempt-1",
            workflow_task_uuid=WORKFLOW_TASK_UUID,
            workflow_node_job_uuid=FIRST_JOB_UUID,
            attempt=1,
            device_material_uuid=DEVICE_MATERIAL_UUID,
            mutable_material_root_uuids=(),
            occupancy_changing_site_uuids=(),
        )
    )
    assert first.status == "acquired"
    assert first.claim is not None
    try:
        with pytest.raises(inventory_api.MaterialConflict, match="not released"):
            inventory.acquire_job_claim(
                inventory_api.JobClaimAcquireCommand(
                    schema_version=1,
                    command_uuid="82000000-0000-4000-8000-000000000415",
                    idempotency_key="m1ef-same-job-attempt-2",
                    workflow_task_uuid=WORKFLOW_TASK_UUID,
                    workflow_node_job_uuid=FIRST_JOB_UUID,
                    attempt=2,
                    device_material_uuid=SECOND_DEVICE_UUID,
                    mutable_material_root_uuids=(),
                    occupancy_changing_site_uuids=(),
                )
            )
        assert inventory.list_unsettled_claims() == (first.claim,)
    finally:
        inventory.close()
