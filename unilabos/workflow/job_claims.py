"""让单动作调试（D1A）与普通工作流共用持久作业执行占用。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

from unilabos.app.scheduler.inventory.domain import (
    InventoryError,
    JobClaimAcquireCommand,
    JobClaimRecord,
    JobClaimReleaseCommand,
    JobClaimResult,
    JobClaimStateCommand,
    JobClaimUncertainCommand,
    MaterialChangeSetCommand,
    MaterialChangeSetReceipt,
)
from unilabos.workflow.claim_intent import mutable_material_roots
from unilabos.workflow.json_codec import encode_json
from unilabos.workflow.store import StoreConflict, WorkflowStore, utc_now


@dataclass(frozen=True, slots=True)
class JobClaimAdmission:
    status: str
    claim: JobClaimRecord | None
    blocking_claim_uuid: str | None
    outbox_sequence: int | None


def _command_uuid(job_uuid: str, attempt: int, phase: str) -> str:
    return str(uuid5(UUID(job_uuid), f"job-claim:{attempt}:{phase}"))


def _fingerprint(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(encode_json(payload, sort_keys=True)).hexdigest()


def _claim_members(claim: JobClaimRecord) -> list[dict[str, Any]]:
    return [
        {
            "resource_kind": member.resource_kind,
            "resource_uuid": member.resource_uuid,
            "acquired_version": member.acquired_version,
            "expected_version": member.expected_version,
        }
        for member in claim.members
    ]


def workflow_terminal_fingerprint(
    *,
    job_uuid: str,
    attempt: int,
    outcome: str,
    receipt: MaterialChangeSetReceipt,
    return_info: Mapping[str, Any],
    error_info: list[Any],
) -> str:
    """构造 Workflow commit 与 Claim release 共享的稳定终态证明。"""

    return _fingerprint(
        {
            "job_uuid": job_uuid,
            "attempt": attempt,
            "terminal_job_status": outcome,
            "material_changeset_uuid": receipt.uuid,
            "material_changeset_fingerprint": receipt.deterministic_fingerprint,
            "return_info": dict(return_info),
            "error_info": list(error_info),
        }
    )


class WorkflowJobClaimCoordinator:
    def __init__(self, store: WorkflowStore, inventory: Any) -> None:
        self._store = store
        self._inventory = inventory

    def recover_startup(self) -> None:
        for claim in self._inventory.list_unsettled_claims():
            if self._store.is_device_action_job(claim.workflow_node_job_uuid):
                continue
            try:
                job = self._store.get_job(claim.workflow_node_job_uuid)
            except Exception as error:
                raise StoreConflict("orphan JobExecutionClaim") from error
            if job["workflow_task_uuid"] != claim.workflow_task_uuid:
                raise StoreConflict("JobExecutionClaim owner mismatch")
            if job["status"] in {
                "succeeded",
                "failed",
                "canceled",
                "timeout",
            }:
                receipt = self._inventory.get_terminal_material_changeset(
                    job["uuid"],
                    int(job["attempt"]),
                )
                if receipt is None:
                    uncertain = self.mark_unknown(
                        claim,
                        reason="terminal_without_physical_settlement",
                        evidence_fingerprint=_fingerprint(
                            {
                                "job_uuid": job["uuid"],
                                "attempt": claim.attempt,
                                "status": job["status"],
                            }
                        ),
                    )
                    self.acknowledge(uncertain.outbox_sequence)
                    continue
                result = receipt.result
                return_info = result.get("return_info", {})
                error_info = result.get("error_info", [])
                if not isinstance(return_info, Mapping) or not isinstance(
                    error_info, list
                ):
                    raise StoreConflict("terminal Material ChangeSet is invalid")
                released = self.release_terminal(
                    claim,
                    receipt,
                    workflow_terminal_fingerprint=workflow_terminal_fingerprint(
                        job_uuid=job["uuid"],
                        attempt=int(job["attempt"]),
                        outcome=job["status"],
                        receipt=receipt,
                        return_info=return_info,
                        error_info=error_info,
                    ),
                )
                self.acknowledge(released.outbox_sequence)
                continue
            if job["status"] == "execution_unknown" and claim.state != "uncertain":
                evidence = _fingerprint(
                    {
                        "job_uuid": job["uuid"],
                        "attempt": claim.attempt,
                        "reason": "os_process_restart",
                    }
                )
                result = self.mark_unknown(
                    claim,
                    reason="os_process_restart",
                    evidence_fingerprint=evidence,
                )
                if result.claim is None:
                    raise StoreConflict("JobExecutionClaim recovery failed")
                claim = result.claim
            self._project_claim(
                job["uuid"],
                claim,
                status="unknown" if claim.state == "uncertain" else "claimed",
            )

    def acquire(
        self,
        *,
        task_uuid: str,
        job_uuid: str,
        attempt: int,
        device_id: str,
        param_schema: Mapping[str, Any] | None,
        param: Mapping[str, Any],
    ) -> JobClaimAdmission:
        roots = mutable_material_roots(param_schema, param)
        executor = self._inventory.resolve_executor_material(device_id)
        command_uuid = _command_uuid(job_uuid, attempt, "acquire")
        result = self._inventory.acquire_job_claim(
            JobClaimAcquireCommand(
                schema_version=1,
                command_uuid=command_uuid,
                idempotency_key=f"job-claim:{job_uuid}:{attempt}:acquire",
                workflow_task_uuid=task_uuid,
                workflow_node_job_uuid=job_uuid,
                attempt=attempt,
                device_material_uuid=executor.uuid,
                mutable_material_root_uuids=roots,
                occupancy_changing_site_uuids=(),
            )
        )
        if result.status == "blocked":
            blocker = next(
                (
                    str(item["blocking_claim_uuid"])
                    for item in result.diagnostics
                    if item.get("code") == "claim_blocked"
                    and item.get("blocking_claim_uuid")
                ),
                None,
            )
            self.project_waiting(job_uuid, blocker)
            return JobClaimAdmission("blocked", None, blocker, None)
        if result.status != "acquired" or result.claim is None:
            raise StoreConflict("JobExecutionClaim acquisition rejected")
        return JobClaimAdmission(
            "acquired",
            result.claim,
            None,
            result.outbox_sequence,
        )

    def project_waiting(self, job_uuid: str, blocker: str | None) -> None:
        with self._store.transaction() as connection:
            row = connection.execute(
                "SELECT workflow_task_uuid, status FROM workflow_node_job WHERE uuid = ?",
                (job_uuid,),
            ).fetchone()
            if row is None or row["status"] != "pending":
                return
            now = utc_now()
            changed = connection.execute(
                """
                UPDATE workflow_node_job
                SET claim_status = 'waiting_for_claim', blocking_claim_uuid = ?,
                    update_time = ?
                WHERE uuid = ? AND status = 'pending'
                  AND (claim_status <> 'waiting_for_claim'
                       OR blocking_claim_uuid IS NOT ?)
                """,
                (blocker, now, job_uuid, blocker),
            ).rowcount
            if changed:
                WorkflowStore._append_event(
                    connection,
                    event="workflow.runtime.changed",
                    data={"workflow_task_uuid": row["workflow_task_uuid"]},
                    now=now,
                )

    def prepare_dispatch(
        self,
        *,
        task_uuid: str,
        job_uuid: str,
        attempt: int,
        claim: JobClaimRecord,
        param: Mapping[str, Any],
        coordinator: Any,
    ) -> str:
        command_uuid = _command_uuid(job_uuid, attempt, "dispatch-command")
        with self._store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_node_job WHERE uuid = ?",
                (job_uuid,),
            ).fetchone()
            if row is None:
                raise StoreConflict("WorkflowNodeJob disappeared before dispatch")
            if row["workflow_task_uuid"] != task_uuid or int(row["attempt"]) != attempt:
                raise StoreConflict("WorkflowNodeJob claim owner mismatch")
            if row["status"] != "pending":
                raise StoreConflict("WorkflowNodeJob is no longer dispatchable")
            now = utc_now()
            connection.execute(
                """
                UPDATE workflow_node_job
                SET status = 'dispatched', param = ?, edge_command_uuid = ?,
                    claim_status = 'claimed', inventory_claim_uuid = ?,
                    inventory_fencing_token = ?,
                    inventory_claim_set_fingerprint = ?,
                    inventory_claim_members = ?, blocking_claim_uuid = NULL,
                    update_time = ?
                WHERE uuid = ?
                """,
                (
                    encode_json(dict(param), sort_keys=True).decode("utf-8"),
                    command_uuid,
                    claim.uuid,
                    claim.fencing_token,
                    claim.set_fingerprint,
                    encode_json(_claim_members(claim), sort_keys=True).decode("utf-8"),
                    now,
                    job_uuid,
                ),
            )
            coordinator._append_journal(
                connection,
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                kind="job_transition",
                from_status="pending",
                to_status="dispatched",
                data={
                    "edge_command_uuid": command_uuid,
                    "claim_uuid": claim.uuid,
                    "fencing_token": claim.fencing_token,
                },
                now=now,
            )
            WorkflowStore._append_event(
                connection,
                event="workflow.runtime.changed",
                data={"workflow_task_uuid": task_uuid},
                now=now,
            )
        return command_uuid

    def mark_running(
        self,
        claim: JobClaimRecord,
        *,
        evidence_fingerprint: str,
    ) -> JobClaimResult:
        command_uuid = _command_uuid(
            claim.workflow_node_job_uuid,
            claim.attempt,
            f"running:{evidence_fingerprint}",
        )
        return self._inventory.mark_job_claim_running(
            JobClaimStateCommand(
                schema_version=1,
                command_uuid=command_uuid,
                idempotency_key=(
                    f"job-claim:{claim.workflow_node_job_uuid}:"
                    f"{claim.attempt}:running:{evidence_fingerprint}"
                ),
                workflow_node_job_uuid=claim.workflow_node_job_uuid,
                attempt=claim.attempt,
                claim_uuid=claim.uuid,
                fencing_token=claim.fencing_token,
                evidence_kind="driver_accepted",
                evidence_fingerprint=evidence_fingerprint,
            )
        )

    def mark_unknown(
        self,
        claim: JobClaimRecord,
        *,
        reason: str,
        evidence_fingerprint: str,
    ) -> JobClaimResult:
        command_uuid = _command_uuid(
            claim.workflow_node_job_uuid,
            claim.attempt,
            f"unknown:{reason}:{evidence_fingerprint}",
        )
        result = self._inventory.mark_job_claim_uncertain(
            JobClaimUncertainCommand(
                schema_version=1,
                command_uuid=command_uuid,
                idempotency_key=(
                    f"job-claim:{claim.workflow_node_job_uuid}:"
                    f"{claim.attempt}:unknown:{reason}:{evidence_fingerprint}"
                ),
                workflow_node_job_uuid=claim.workflow_node_job_uuid,
                attempt=claim.attempt,
                claim_uuid=claim.uuid,
                fencing_token=claim.fencing_token,
                uncertainty_reason=reason,
                evidence_fingerprint=evidence_fingerprint,
            )
        )
        if result.claim is not None:
            self._project_claim(
                claim.workflow_node_job_uuid,
                result.claim,
                status="unknown",
            )
        return result

    def commit_terminal(
        self,
        claim: JobClaimRecord,
        *,
        outcome: str,
        result: Mapping[str, Any],
    ) -> MaterialChangeSetReceipt:
        command_uuid = _command_uuid(
            claim.workflow_node_job_uuid,
            claim.attempt,
            "terminal-changeset",
        )
        return self._inventory.commit_material_changeset(
            MaterialChangeSetCommand(
                schema_version=1,
                command_uuid=command_uuid,
                idempotency_key=(
                    f"job-claim:{claim.workflow_node_job_uuid}:"
                    f"{claim.attempt}:terminal-changeset"
                ),
                workflow_task_uuid=claim.workflow_task_uuid,
                workflow_node_job_uuid=claim.workflow_node_job_uuid,
                attempt=claim.attempt,
                claim_uuid=claim.uuid,
                fencing_token=claim.fencing_token,
                effect_identity="terminal",
                outcome=outcome,
                result=dict(result),
                effects=(),
            )
        )

    def release_terminal(
        self,
        claim: JobClaimRecord,
        receipt: MaterialChangeSetReceipt,
        *,
        workflow_terminal_fingerprint: str,
    ) -> JobClaimResult:
        command_uuid = _command_uuid(
            claim.workflow_node_job_uuid,
            claim.attempt,
            "release-terminal",
        )
        result = self._inventory.release_job_claim(
            JobClaimReleaseCommand(
                schema_version=1,
                command_uuid=command_uuid,
                idempotency_key=(
                    f"job-claim:{claim.workflow_node_job_uuid}:"
                    f"{claim.attempt}:release-terminal"
                ),
                workflow_node_job_uuid=claim.workflow_node_job_uuid,
                attempt=claim.attempt,
                claim_uuid=claim.uuid,
                fencing_token=claim.fencing_token,
                release_proof_kind="terminal_settled",
                material_changeset_uuid=receipt.uuid,
                material_changeset_fingerprint=receipt.deterministic_fingerprint,
                workflow_terminal_fingerprint=workflow_terminal_fingerprint,
                reason="workflow_job_terminal_settled",
                no_send_proof_fingerprint=None,
            )
        )
        if result.claim is not None:
            self._project_claim(
                claim.workflow_node_job_uuid,
                result.claim,
                status="released",
            )
        return result

    def find_claim(self, job_uuid: str, attempt: int) -> JobClaimRecord | None:
        try:
            return self._inventory.get_job_claim(job_uuid, attempt)
        except InventoryError as error:
            if error.code == "not_found":
                return None
            raise

    def acknowledge(self, sequence: int | None) -> None:
        if sequence is not None:
            self._inventory.acknowledge(sequence, consumer="scheduler")

    def _project_claim(
        self,
        job_uuid: str,
        claim: JobClaimRecord,
        *,
        status: str,
    ) -> None:
        with self._store.transaction() as connection:
            row = connection.execute(
                "SELECT workflow_task_uuid FROM workflow_node_job WHERE uuid = ?",
                (job_uuid,),
            ).fetchone()
            if row is None:
                raise StoreConflict("JobExecutionClaim projection owner is missing")
            now = utc_now()
            connection.execute(
                """
                UPDATE workflow_node_job
                SET claim_status = ?, inventory_claim_uuid = ?,
                    inventory_fencing_token = ?,
                    inventory_claim_set_fingerprint = ?,
                    inventory_claim_members = ?, blocking_claim_uuid = NULL,
                    update_time = ?
                WHERE uuid = ?
                """,
                (
                    status,
                    claim.uuid,
                    claim.fencing_token,
                    claim.set_fingerprint,
                    encode_json(_claim_members(claim), sort_keys=True).decode("utf-8"),
                    now,
                    job_uuid,
                ),
            )
            WorkflowStore._append_event(
                connection,
                event="workflow.runtime.changed",
                data={"workflow_task_uuid": row["workflow_task_uuid"]},
                now=now,
            )


__all__ = [
    "JobClaimAdmission",
    "WorkflowJobClaimCoordinator",
    "workflow_terminal_fingerprint",
]
