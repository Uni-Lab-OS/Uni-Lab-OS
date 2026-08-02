"""M1EF InventoryService Job Claim/ChangeSet/reopen 纵向合同。

行为只通过 public ``InventoryService`` 和冻结的 command/record types 触发。
SQLite 只用于证明 complete-set acquisition 零 partial write、monotonic fence
与 close/reopen 后的 exact v6 durable facts；测试不调用 Store 或私有 helper。
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import unilabos.app.scheduler.inventory as inventory_api

DEVICE_MATERIAL_UUID = "51000000-0000-4000-8000-000000000301"
MOUNT_MATERIAL_UUID = "51000000-0000-4000-8000-000000000302"
SAMPLE_MATERIAL_UUID = "51000000-0000-4000-8000-000000000303"
DEVICE_TEMPLATE_UUID = "21000000-0000-4000-8000-000000000301"
MOUNT_TEMPLATE_UUID = "21000000-0000-4000-8000-000000000302"
SAMPLE_TEMPLATE_UUID = "21000000-0000-4000-8000-000000000303"
SITE_UUID = "61000000-0000-4000-8000-000000000301"

WORKFLOW_TASK_UUID = "91000000-0000-4000-8000-000000000301"
MATERIAL_SOURCE_NODE_UUID = "a1000000-0000-4000-8000-000000000301"
FIRST_JOB_UUID = "b1000000-0000-4000-8000-000000000301"
SECOND_JOB_UUID = "b1000000-0000-4000-8000-000000000302"

ADMISSION_COMMAND_UUID = "81000000-0000-4000-8000-000000000301"
FIRST_ACQUIRE_COMMAND_UUID = "82000000-0000-4000-8000-000000000301"
SECOND_ACQUIRE_COMMAND_UUID = "82000000-0000-4000-8000-000000000302"
RUNNING_COMMAND_UUID = "83000000-0000-4000-8000-000000000301"
SECOND_RUNNING_COMMAND_UUID = "83000000-0000-4000-8000-000000000302"
CHANGESET_COMMAND_UUID = "84000000-0000-4000-8000-000000000301"
RELEASE_COMMAND_UUID = "85000000-0000-4000-8000-000000000301"
STALE_CHANGESET_COMMAND_UUID = "84000000-0000-4000-8000-000000000302"

WORKFLOW_SNAPSHOT_FINGERPRINT = "3" * 64
DRIVER_ACCEPTED_FINGERPRINT = "4" * 64
WORKFLOW_TERMINAL_FINGERPRINT = "5" * 64


def _resource_templates() -> dict[str, inventory_api.ResourceTemplateIdentity]:
    identities = (
        inventory_api.ResourceTemplateIdentity(
            uuid=DEVICE_TEMPLATE_UUID,
            material_class="Heater",
        ),
        inventory_api.ResourceTemplateIdentity(
            uuid=MOUNT_TEMPLATE_UUID,
            material_class="Deck",
        ),
        inventory_api.ResourceTemplateIdentity(
            uuid=SAMPLE_TEMPLATE_UUID,
            material_class="SampleTube",
        ),
    )
    return {identity.uuid: identity for identity in identities}


def _open_inventory(working_dir: Path) -> inventory_api.InventoryService:
    return inventory_api.InventoryService.open(
        working_dir=working_dir,
        resource_templates=_resource_templates(),
    )


def _seed_device_material(inventory: inventory_api.InventoryService) -> None:
    imported = inventory.bootstrap_resource_graph(
        {
            "source_id": "m1ef-device-graph.json",
            "fingerprint": "sha256:" + "6" * 64,
            "materials": [
                {
                    "uuid": DEVICE_MATERIAL_UUID,
                    "resource_template_uuid": DEVICE_TEMPLATE_UUID,
                    "parent_uuid": None,
                    "class": "Heater",
                    "barcode": "",
                    "name": "heater-301",
                    "description": "M1EF selected executor",
                    "meta_data": {
                        "source": "resource-tree-set",
                        "source_node_id": "heater-301",
                    },
                    "config": {},
                    "data": {},
                    "material_kind": "device",
                }
            ],
            "relative_positions": [],
            "sites": [],
        }
    )
    assert imported["status"] == "imported"


def _seed_business_material_and_site(
    inventory: inventory_api.InventoryService,
) -> None:
    inventory.create_material(
        material_uuid=MOUNT_MATERIAL_UUID,
        resource_template_uuid=MOUNT_TEMPLATE_UUID,
        barcode="M1EF-DECK-301",
        name="M1EF deck",
    )
    inventory.create_material(
        material_uuid=SAMPLE_MATERIAL_UUID,
        resource_template_uuid=SAMPLE_TEMPLATE_UUID,
        barcode="M1EF-SAMPLE-301",
        name="M1EF sample",
        data={"temperature_c": 20.0},
    )
    inventory.create_site(
        site_uuid=SITE_UUID,
        description="M1EF occupancy-changing Site",
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


def _admit_task(
    inventory: inventory_api.InventoryService,
) -> inventory_api.TaskMaterialAdmissionResult:
    source = inventory_api.TaskMaterialAdmissionSource(
        material_source_node_uuid=MATERIAL_SOURCE_NODE_UUID,
        mode="existing",
        resource_template_uuid=SAMPLE_TEMPLATE_UUID,
        mount={"uuid": MOUNT_MATERIAL_UUID},
        material_uuid=SAMPLE_MATERIAL_UUID,
        site_uuid=SITE_UUID,
        candidate_site_uuids=(),
        flow_role="sample",
    )
    admitted = inventory.admit_task(
        inventory_api.TaskMaterialAdmissionCommand(
            schema_version=1,
            command_uuid=ADMISSION_COMMAND_UUID,
            idempotency_key="m1ef-admit-task-301",
            workflow_task_uuid=WORKFLOW_TASK_UUID,
            workflow_snapshot_fingerprint=WORKFLOW_SNAPSHOT_FINGERPRINT,
            sources=(source,),
        )
    )
    assert admitted.status == "admitted"
    assert admitted.reservation_uuid is not None
    assert inventory.has_active_task_reservation(
        WORKFLOW_TASK_UUID,
        admitted.reservation_uuid,
    )
    return admitted


def _claim_command(
    *,
    command_uuid: str,
    job_uuid: str,
) -> Any:
    return inventory_api.JobClaimAcquireCommand(
        schema_version=1,
        command_uuid=command_uuid,
        idempotency_key=f"m1ef-acquire-{job_uuid}",
        workflow_task_uuid=WORKFLOW_TASK_UUID,
        workflow_node_job_uuid=job_uuid,
        attempt=1,
        device_material_uuid=DEVICE_MATERIAL_UUID,
        mutable_material_root_uuids=(SAMPLE_MATERIAL_UUID,),
        occupancy_changing_site_uuids=(SITE_UUID,),
    )


def _member_identities(claim: Any) -> set[tuple[str, str]]:
    return {(member.resource_kind, member.resource_uuid) for member in claim.members}


def _read_claim_table_counts(database: Path) -> dict[str, int]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        tables = (
            "material_claim",
            "material_claim_member",
            "material_claim_fence_sequence",
            "material_resource_fence",
            "material_changeset",
            "material_changeset_effect",
        )
        return {
            table: int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in tables
        }
    finally:
        connection.close()


def test_claim_changeset_release_reopen_and_stale_fence_are_one_durable_flow(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory.db"
    inventory = _open_inventory(tmp_path)
    try:
        _seed_device_material(inventory)
        _seed_business_material_and_site(inventory)
        _admit_task(inventory)

        first_command = _claim_command(
            command_uuid=FIRST_ACQUIRE_COMMAND_UUID,
            job_uuid=FIRST_JOB_UUID,
        )
        acquired = inventory.acquire_job_claim(first_command)

        assert acquired.status == "acquired"
        assert inventory.acquire_job_claim(first_command) == acquired
        first_claim = inventory.get_job_claim(FIRST_JOB_UUID, 1)
        assert first_claim.workflow_task_uuid == WORKFLOW_TASK_UUID
        assert first_claim.workflow_node_job_uuid == FIRST_JOB_UUID
        assert first_claim.attempt == 1
        assert first_claim.state == "reserved"
        assert first_claim.fencing_token > 0
        assert _member_identities(first_claim) == {
            ("device_material", DEVICE_MATERIAL_UUID),
            ("business_material", SAMPLE_MATERIAL_UUID),
            ("site", SITE_UUID),
        }

        second_command = _claim_command(
            command_uuid=SECOND_ACQUIRE_COMMAND_UUID,
            job_uuid=SECOND_JOB_UUID,
        )
        blocked = inventory.acquire_job_claim(second_command)

        assert blocked.status == "blocked"
        assert inventory.acquire_job_claim(second_command) == blocked
        assert _read_claim_table_counts(database) == {
            "material_claim": 1,
            "material_claim_member": 3,
            "material_claim_fence_sequence": 1,
            "material_resource_fence": 3,
            "material_changeset": 0,
            "material_changeset_effect": 0,
        }

        inventory.mark_job_claim_running(
            inventory_api.JobClaimStateCommand(
                schema_version=1,
                command_uuid=RUNNING_COMMAND_UUID,
                idempotency_key="m1ef-running-job-301",
                workflow_node_job_uuid=FIRST_JOB_UUID,
                attempt=1,
                claim_uuid=first_claim.uuid,
                fencing_token=first_claim.fencing_token,
                evidence_kind="driver_accepted",
                evidence_fingerprint=DRIVER_ACCEPTED_FINGERPRINT,
            )
        )
        assert inventory.get_job_claim(FIRST_JOB_UUID, 1).state == "running"

        changeset_command = inventory_api.MaterialChangeSetCommand(
            schema_version=1,
            command_uuid=CHANGESET_COMMAND_UUID,
            idempotency_key="m1ef-terminal-no-op-job-301",
            workflow_task_uuid=WORKFLOW_TASK_UUID,
            workflow_node_job_uuid=FIRST_JOB_UUID,
            attempt=1,
            claim_uuid=first_claim.uuid,
            fencing_token=first_claim.fencing_token,
            effect_identity="terminal",
            outcome="succeeded",
            result={"return_info": {"heated": True}},
            effects=(),
        )
        receipt = inventory.commit_material_changeset(changeset_command)

        assert receipt.workflow_node_job_uuid == FIRST_JOB_UUID
        assert receipt.attempt == 1
        assert receipt.claim_uuid == first_claim.uuid
        assert receipt.fencing_token == first_claim.fencing_token
        assert receipt.effect_identity == "terminal"
        assert receipt.outcome == "succeeded"
        assert receipt.deterministic_fingerprint
        assert inventory.commit_material_changeset(changeset_command) == receipt
        assert inventory.get_material(SAMPLE_MATERIAL_UUID).version == 1
        assert inventory.get_site(SITE_UUID).version == 1

        inventory.release_job_claim(
            inventory_api.JobClaimReleaseCommand(
                schema_version=1,
                command_uuid=RELEASE_COMMAND_UUID,
                idempotency_key="m1ef-release-job-301",
                workflow_node_job_uuid=FIRST_JOB_UUID,
                attempt=1,
                claim_uuid=first_claim.uuid,
                fencing_token=first_claim.fencing_token,
                release_proof_kind="terminal_settled",
                material_changeset_uuid=receipt.uuid,
                material_changeset_fingerprint=receipt.deterministic_fingerprint,
                workflow_terminal_fingerprint=WORKFLOW_TERMINAL_FINGERPRINT,
                reason="workflow_job_terminal_settled",
            )
        )
        released = inventory.get_job_claim(FIRST_JOB_UUID, 1)
        assert released.state == "released"
        assert released.terminal_changeset_uuid == receipt.uuid
        assert released.workflow_terminal_fingerprint == WORKFLOW_TERMINAL_FINGERPRINT
    finally:
        inventory.close()

    reopened = _open_inventory(tmp_path)
    try:
        durable_claim = reopened.get_job_claim(FIRST_JOB_UUID, 1)
        assert durable_claim.state == "released"
        assert durable_claim.fencing_token == first_claim.fencing_token
        assert reopened.commit_material_changeset(changeset_command) == receipt
        replayed_after_release = reopened.acquire_job_claim(first_command)
        assert replayed_after_release.status == "rejected"
        assert replayed_after_release.claim == durable_claim

        acquired_after_release = reopened.acquire_job_claim(second_command)
        assert acquired_after_release.status == "acquired"
        second_claim = reopened.get_job_claim(SECOND_JOB_UUID, 1)
        assert second_claim.state == "reserved"
        assert second_claim.fencing_token > first_claim.fencing_token
        assert _member_identities(second_claim) == _member_identities(first_claim)

        reopened.mark_job_claim_running(
            inventory_api.JobClaimStateCommand(
                schema_version=1,
                command_uuid=SECOND_RUNNING_COMMAND_UUID,
                idempotency_key="m1ef-running-job-302",
                workflow_node_job_uuid=SECOND_JOB_UUID,
                attempt=1,
                claim_uuid=second_claim.uuid,
                fencing_token=second_claim.fencing_token,
                evidence_kind="driver_accepted",
                evidence_fingerprint="7" * 64,
            )
        )
        assert reopened.get_job_claim(SECOND_JOB_UUID, 1).state == "running"

        stale_command = replace(
            changeset_command,
            command_uuid=STALE_CHANGESET_COMMAND_UUID,
            idempotency_key="m1ef-stale-token-job-302",
            workflow_node_job_uuid=SECOND_JOB_UUID,
            claim_uuid=second_claim.uuid,
            fencing_token=first_claim.fencing_token,
            result={"return_info": {"stale": True}},
        )
        before_stale_write = _read_claim_table_counts(database)
        with pytest.raises(inventory_api.MaterialConflict) as stale:
            reopened.commit_material_changeset(stale_command)

        assert stale.value.code == "conflict"
        assert _read_claim_table_counts(database) == before_stale_write
        assert reopened.get_material(SAMPLE_MATERIAL_UUID).version == 1
        assert reopened.get_site(SITE_UUID).version == 1
    finally:
        reopened.close()

    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert _read_claim_table_counts(database) == {
            "material_claim": 2,
            "material_claim_member": 6,
            "material_claim_fence_sequence": 2,
            "material_resource_fence": 3,
            "material_changeset": 1,
            "material_changeset_effect": 0,
        }
        resource_fences = connection.execute(
            """
            SELECT resource_kind, resource_uuid, fencing_token, claim_uuid
            FROM material_resource_fence
            ORDER BY resource_kind, resource_uuid
            """
        ).fetchall()
        assert resource_fences == [
            (
                "business_material",
                SAMPLE_MATERIAL_UUID,
                second_claim.fencing_token,
                second_claim.uuid,
            ),
            (
                "device_material",
                DEVICE_MATERIAL_UUID,
                second_claim.fencing_token,
                second_claim.uuid,
            ),
            (
                "site",
                SITE_UUID,
                second_claim.fencing_token,
                second_claim.uuid,
            ),
        ]
    finally:
        connection.close()
