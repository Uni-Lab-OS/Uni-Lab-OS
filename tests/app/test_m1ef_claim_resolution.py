"""M1EF evidenced resolution commands remain fenced and replayable."""

from __future__ import annotations

from pathlib import Path

import pytest

import tests.app.test_m1ef_inventory_claim_lifecycle as lifecycle
from unilabos.app.scheduler.inventory import (
    JobClaimAcquireCommand,
    JobClaimReleaseCommand,
    JobClaimResolutionCommand,
    JobClaimUncertainCommand,
    MaterialChangeSetCommand,
    MaterialChangeSetEffect,
    MaterialConflict,
)

NO_SEND_JOB_UUID = "b1000000-0000-4000-8000-000000000701"
BUSINESS_JOB_UUID = "b1000000-0000-4000-8000-000000000702"


def _device_only_claim(tmp_path: Path):
    inventory = lifecycle._open_inventory(tmp_path)
    lifecycle._seed_device_material(inventory)
    acquired = inventory.acquire_job_claim(
        JobClaimAcquireCommand(
            schema_version=1,
            command_uuid="82000000-0000-4000-8000-000000000701",
            idempotency_key="m1ef-resolution-device-only",
            workflow_task_uuid=lifecycle.WORKFLOW_TASK_UUID,
            workflow_node_job_uuid=NO_SEND_JOB_UUID,
            attempt=1,
            device_material_uuid=lifecycle.DEVICE_MATERIAL_UUID,
            mutable_material_root_uuids=(),
            occupancy_changing_site_uuids=(),
        )
    )
    assert acquired.claim is not None
    return inventory, acquired.claim


def _uncertain_business_claim(tmp_path: Path):
    inventory = lifecycle._open_inventory(tmp_path)
    lifecycle._seed_device_material(inventory)
    lifecycle._seed_business_material_and_site(inventory)
    lifecycle._admit_task(inventory)
    acquired = inventory.acquire_job_claim(
        JobClaimAcquireCommand(
            schema_version=1,
            command_uuid="82000000-0000-4000-8000-000000000702",
            idempotency_key="m1ef-resolution-business",
            workflow_task_uuid=lifecycle.WORKFLOW_TASK_UUID,
            workflow_node_job_uuid=BUSINESS_JOB_UUID,
            attempt=1,
            device_material_uuid=lifecycle.DEVICE_MATERIAL_UUID,
            mutable_material_root_uuids=(lifecycle.SAMPLE_MATERIAL_UUID,),
            occupancy_changing_site_uuids=(lifecycle.SITE_UUID,),
        )
    )
    assert acquired.claim is not None
    uncertain = inventory.mark_job_claim_uncertain(
        JobClaimUncertainCommand(
            schema_version=1,
            command_uuid="83000000-0000-4000-8000-000000000702",
            idempotency_key="m1ef-resolution-uncertain",
            workflow_node_job_uuid=BUSINESS_JOB_UUID,
            attempt=1,
            claim_uuid=acquired.claim.uuid,
            fencing_token=acquired.claim.fencing_token,
            uncertainty_reason="transport_unknown",
            evidence_fingerprint="6" * 64,
        )
    )
    assert uncertain.claim is not None
    return inventory, uncertain.claim


def test_confirmed_not_dispatched_requires_and_freezes_no_send_proof(
    tmp_path: Path,
) -> None:
    inventory, claim = _device_only_claim(tmp_path)
    command = JobClaimResolutionCommand(
        schema_version=1,
        command_uuid="86000000-0000-4000-8000-000000000701",
        idempotency_key="m1ef-confirmed-not-dispatched",
        workflow_node_job_uuid=NO_SEND_JOB_UUID,
        attempt=1,
        claim_uuid=claim.uuid,
        fencing_token=claim.fencing_token,
        expected_state="reserved",
        resolution="confirmed_not_dispatched",
        evidence_kind="coordinator_no_send",
        evidence_fingerprint="7" * 64,
        observed_at="2026-08-02T12:00:00Z",
        actor_identity="scheduler:edge-default",
        reason="dispatch journal proves zero send",
        no_send_proof_fingerprint="8" * 64,
    )
    try:
        result = inventory.resolve_job_claim(command)
        assert result.status == "released"
        assert result.claim is not None
        assert result.claim.release_proof_kind == "not_submitted"
        assert result.claim.terminal_changeset_uuid is None
        assert inventory.resolve_job_claim(command) == result
        assert inventory.get_command_result(command.command_uuid) == result
    finally:
        inventory.close()


def test_unresolved_keeps_fence_and_rejects_stale_expected_state(
    tmp_path: Path,
) -> None:
    inventory, claim = _uncertain_business_claim(tmp_path)
    command = JobClaimResolutionCommand(
        schema_version=1,
        command_uuid="86000000-0000-4000-8000-000000000702",
        idempotency_key="m1ef-resolution-unresolved",
        workflow_node_job_uuid=BUSINESS_JOB_UUID,
        attempt=1,
        claim_uuid=claim.uuid,
        fencing_token=claim.fencing_token,
        expected_state="uncertain",
        resolution="unresolved",
        evidence_kind="operator_observation",
        evidence_fingerprint="9" * 64,
        observed_at="2026-08-02T12:01:00+08:00",
        actor_identity="operator:alice",
        reason="physical reality remains unknown",
    )
    try:
        result = inventory.resolve_job_claim(command)
        assert result.status == "uncertain"
        assert result.claim is not None and result.claim.state == "uncertain"
        assert inventory.resolve_job_claim(command) == result
        assert inventory.list_unsettled_claims() == (result.claim,)

        stale = JobClaimResolutionCommand(
            schema_version=1,
            command_uuid="86000000-0000-4000-8000-000000000703",
            idempotency_key="m1ef-resolution-stale",
            workflow_node_job_uuid=BUSINESS_JOB_UUID,
            attempt=1,
            claim_uuid=claim.uuid,
            fencing_token=claim.fencing_token,
            expected_state="running",
            resolution="unresolved",
            evidence_kind="operator_observation",
            evidence_fingerprint="a" * 64,
            observed_at="2026-08-02T12:02:00Z",
            actor_identity="operator:bob",
            reason="stale resolution must fail",
        )
        with pytest.raises(MaterialConflict, match="expected_state"):
            inventory.resolve_job_claim(stale)
    finally:
        inventory.close()


def test_quarantine_and_fail_commits_reality_before_reconciled_release(
    tmp_path: Path,
) -> None:
    inventory, claim = _uncertain_business_claim(tmp_path)
    sample = inventory.get_material(lifecycle.SAMPLE_MATERIAL_UUID)
    terminal = MaterialChangeSetCommand(
        schema_version=1,
        command_uuid="84000000-0000-4000-8000-000000000702",
        idempotency_key="m1ef-resolution-quarantine-changeset",
        workflow_task_uuid=lifecycle.WORKFLOW_TASK_UUID,
        workflow_node_job_uuid=BUSINESS_JOB_UUID,
        attempt=1,
        claim_uuid=claim.uuid,
        fencing_token=claim.fencing_token,
        effect_identity="terminal",
        outcome="failed",
        result={"error_info": [{"code": "reality_quarantined"}]},
        effects=(
            MaterialChangeSetEffect(
                effect_key="quarantine-sample",
                resource_kind="business_material",
                resource_uuid=lifecycle.SAMPLE_MATERIAL_UUID,
                operation="update",
                expected_version=sample.version,
                before={"disposition": "reconciling"},
                after={"disposition": "quarantined"},
            ),
        ),
    )
    command = JobClaimResolutionCommand(
        schema_version=1,
        command_uuid="86000000-0000-4000-8000-000000000704",
        idempotency_key="m1ef-resolution-quarantine",
        workflow_node_job_uuid=BUSINESS_JOB_UUID,
        attempt=1,
        claim_uuid=claim.uuid,
        fencing_token=claim.fencing_token,
        expected_state="uncertain",
        resolution="quarantine_and_fail",
        evidence_kind="operator_reconciliation",
        evidence_fingerprint="b" * 64,
        observed_at="2026-08-02T12:03:00Z",
        actor_identity="operator:carol",
        reason="physical state cannot be established safely",
        terminal_changeset=terminal,
        workflow_terminal_fingerprint="c" * 64,
    )
    try:
        resolved = inventory.resolve_job_claim(command)
        assert resolved.status == "terminal_evidence_committed"
        assert resolved.claim is not None and resolved.claim.state == "uncertain"
        assert inventory.get_material(lifecycle.SAMPLE_MATERIAL_UUID).disposition == (
            "quarantined"
        )
        receipt = inventory.get_terminal_material_changeset(BUSINESS_JOB_UUID, 1)
        assert receipt is not None and receipt.outcome == "failed"
        assert inventory.resolve_job_claim(command) == resolved

        released = inventory.release_job_claim(
            JobClaimReleaseCommand(
                schema_version=1,
                command_uuid="85000000-0000-4000-8000-000000000704",
                idempotency_key="m1ef-resolution-reconciled-release",
                workflow_node_job_uuid=BUSINESS_JOB_UUID,
                attempt=1,
                claim_uuid=claim.uuid,
                fencing_token=claim.fencing_token,
                release_proof_kind="reconciled_terminal",
                material_changeset_uuid=receipt.uuid,
                material_changeset_fingerprint=receipt.deterministic_fingerprint,
                workflow_terminal_fingerprint="c" * 64,
                reason="workflow terminal projection settled",
                expected_state="uncertain",
            )
        )
        assert released.claim is not None and released.claim.state == "released"
        inventory.audit_job_claim_authority()
    finally:
        inventory.close()
