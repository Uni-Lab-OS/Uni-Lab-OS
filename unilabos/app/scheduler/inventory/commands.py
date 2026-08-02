"""Closed Task Material command adapter for InventoryService."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from unilabos.app.scheduler.inventory.domain import (
    InventoryError,
    JobClaimAcquireCommand,
    JobClaimReleaseCommand,
    JobClaimResolutionCommand,
    JobClaimStateCommand,
    JobClaimUncertainCommand,
    MaterialChangeSetCommand,
    MaterialChangeSetEffect,
    TaskMaterialAdmissionCommand,
    TaskMaterialAdmissionSource,
    TaskMaterialReleaseCommand,
)
from unilabos.app.scheduler.inventory.service import InventoryService


def _admission_command(payload: dict[str, Any]) -> TaskMaterialAdmissionCommand:
    raw_sources = payload["sources"]
    if not isinstance(raw_sources, list):
        raise TypeError("sources must be an array")
    sources = tuple(
        TaskMaterialAdmissionSource(
            material_source_node_uuid=item["material_source_node_uuid"],
            mode=item["mode"],
            resource_template_uuid=item["resource_template_uuid"],
            mount=dict(item.get("mount") or {}),
            material_uuid=item.get("material_uuid"),
            site_uuid=item.get("site_uuid"),
            candidate_site_uuids=tuple(item.get("candidate_site_uuids") or ()),
            flow_role=item["flow_role"],
        )
        for item in raw_sources
    )
    return TaskMaterialAdmissionCommand(
        schema_version=payload["schema_version"],
        command_uuid=payload["command_uuid"],
        idempotency_key=payload["idempotency_key"],
        workflow_task_uuid=payload["workflow_task_uuid"],
        workflow_snapshot_fingerprint=payload["workflow_snapshot_fingerprint"],
        sources=sources,
    )


def _release_command(payload: dict[str, Any]) -> TaskMaterialReleaseCommand:
    return TaskMaterialReleaseCommand(
        schema_version=payload["schema_version"],
        command_uuid=payload["command_uuid"],
        idempotency_key=payload["idempotency_key"],
        workflow_task_uuid=payload["workflow_task_uuid"],
        reason=payload["reason"],
    )


def _claim_acquire_command(payload: dict[str, Any]) -> JobClaimAcquireCommand:
    return JobClaimAcquireCommand(
        schema_version=payload["schema_version"],
        command_uuid=payload["command_uuid"],
        idempotency_key=payload["idempotency_key"],
        workflow_task_uuid=payload["workflow_task_uuid"],
        workflow_node_job_uuid=payload["workflow_node_job_uuid"],
        attempt=payload["attempt"],
        device_material_uuid=payload["device_material_uuid"],
        mutable_material_root_uuids=tuple(
            payload.get("mutable_material_root_uuids") or ()
        ),
        occupancy_changing_site_uuids=tuple(
            payload.get("occupancy_changing_site_uuids") or ()
        ),
    )


def _claim_state_command(payload: dict[str, Any]) -> JobClaimStateCommand:
    return JobClaimStateCommand(
        schema_version=payload["schema_version"],
        command_uuid=payload["command_uuid"],
        idempotency_key=payload["idempotency_key"],
        workflow_node_job_uuid=payload["workflow_node_job_uuid"],
        attempt=payload["attempt"],
        claim_uuid=payload["claim_uuid"],
        fencing_token=payload["fencing_token"],
        evidence_kind=payload["evidence_kind"],
        evidence_fingerprint=payload["evidence_fingerprint"],
        expected_state=payload.get("expected_state"),
    )


def _claim_uncertain_command(payload: dict[str, Any]) -> JobClaimUncertainCommand:
    return JobClaimUncertainCommand(
        schema_version=payload["schema_version"],
        command_uuid=payload["command_uuid"],
        idempotency_key=payload["idempotency_key"],
        workflow_node_job_uuid=payload["workflow_node_job_uuid"],
        attempt=payload["attempt"],
        claim_uuid=payload["claim_uuid"],
        fencing_token=payload["fencing_token"],
        uncertainty_reason=payload["uncertainty_reason"],
        evidence_fingerprint=payload["evidence_fingerprint"],
        expected_state=payload.get("expected_state"),
    )


def _changeset_command(payload: dict[str, Any]) -> MaterialChangeSetCommand:
    raw_effects = payload.get("effects") or []
    if not isinstance(raw_effects, list):
        raise TypeError("effects must be an array")
    effects = tuple(
        MaterialChangeSetEffect(
            effect_key=item["effect_key"],
            resource_kind=item["resource_kind"],
            resource_uuid=item["resource_uuid"],
            operation=item["operation"],
            expected_version=item.get("expected_version"),
            before=dict(item.get("before") or {}),
            after=dict(item.get("after") or {}),
        )
        for item in raw_effects
    )
    return MaterialChangeSetCommand(
        schema_version=payload["schema_version"],
        command_uuid=payload["command_uuid"],
        idempotency_key=payload["idempotency_key"],
        workflow_task_uuid=payload["workflow_task_uuid"],
        workflow_node_job_uuid=payload["workflow_node_job_uuid"],
        attempt=payload["attempt"],
        claim_uuid=payload["claim_uuid"],
        fencing_token=payload["fencing_token"],
        effect_identity=payload["effect_identity"],
        outcome=payload["outcome"],
        result=dict(payload.get("result") or {}),
        effects=effects,
        expected_claim_state=payload.get("expected_claim_state"),
    )


def _claim_release_command(payload: dict[str, Any]) -> JobClaimReleaseCommand:
    return JobClaimReleaseCommand(
        schema_version=payload["schema_version"],
        command_uuid=payload["command_uuid"],
        idempotency_key=payload["idempotency_key"],
        workflow_node_job_uuid=payload["workflow_node_job_uuid"],
        attempt=payload["attempt"],
        claim_uuid=payload["claim_uuid"],
        fencing_token=payload["fencing_token"],
        release_proof_kind=payload["release_proof_kind"],
        material_changeset_uuid=payload.get("material_changeset_uuid"),
        material_changeset_fingerprint=payload.get("material_changeset_fingerprint"),
        workflow_terminal_fingerprint=payload["workflow_terminal_fingerprint"],
        reason=payload["reason"],
        expected_state=payload.get("expected_state"),
    )


def _claim_resolution_command(payload: dict[str, Any]) -> JobClaimResolutionCommand:
    raw_terminal = payload.get("terminal_changeset")
    if raw_terminal is not None and not isinstance(raw_terminal, dict):
        raise TypeError("terminal_changeset must be an object or null")
    return JobClaimResolutionCommand(
        schema_version=payload["schema_version"],
        command_uuid=payload["command_uuid"],
        idempotency_key=payload["idempotency_key"],
        workflow_node_job_uuid=payload["workflow_node_job_uuid"],
        attempt=payload["attempt"],
        claim_uuid=payload["claim_uuid"],
        fencing_token=payload["fencing_token"],
        expected_state=payload["expected_state"],
        resolution=payload["resolution"],
        evidence_kind=payload["evidence_kind"],
        evidence_fingerprint=payload["evidence_fingerprint"],
        observed_at=payload["observed_at"],
        actor_identity=payload["actor_identity"],
        reason=payload["reason"],
        no_send_proof_fingerprint=payload.get("no_send_proof_fingerprint"),
        terminal_changeset=(
            _changeset_command(raw_terminal) if raw_terminal is not None else None
        ),
        workflow_terminal_fingerprint=payload.get("workflow_terminal_fingerprint"),
    )


def execute_command(
    service: InventoryService,
    command: dict[str, Any],
) -> dict[str, Any]:
    """Execute only closed, versioned Material authority commands."""

    command_type = str(command.get("type") or "")
    payload = command.get("payload")
    command_id = str(command.get("command_id") or "")
    if not isinstance(payload, dict):
        return {
            "command_id": command_id,
            "status": "rejected",
            "error": "payload must be an object",
        }
    if command_id and payload.get("command_uuid") != command_id:
        return {
            "command_id": command_id,
            "status": "rejected",
            "error": "command_id must match payload.command_uuid",
        }
    try:
        if command_type == "material.admit":
            result = service.admit_task(_admission_command(payload))
        elif command_type == "material.release":
            result = service.release_task(_release_command(payload))
        elif command_type == "material.claim.acquire":
            result = service.acquire_job_claim(_claim_acquire_command(payload))
        elif command_type == "material.claim.running":
            result = service.mark_job_claim_running(_claim_state_command(payload))
        elif command_type == "material.claim.uncertain":
            result = service.mark_job_claim_uncertain(_claim_uncertain_command(payload))
        elif command_type == "material.changeset.commit":
            result = service.commit_material_changeset(_changeset_command(payload))
        elif command_type == "material.claim.release":
            result = service.release_job_claim(_claim_release_command(payload))
        elif command_type == "material.claim.resolve":
            result = service.resolve_job_claim(_claim_resolution_command(payload))
        else:
            return {
                "command_id": command_id,
                "status": "rejected",
                "error": f"unknown command type: {command_type}",
            }
    except InventoryError as exc:
        return {
            "command_id": command_id,
            "status": "rejected",
            "error": str(exc),
            "error_code": exc.code,
        }
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "command_id": command_id,
            "status": "rejected",
            "error": f"bad payload: {exc}",
        }
    return {
        "command_id": result.command_uuid,
        "status": "completed",
        "result": asdict(result),
    }
