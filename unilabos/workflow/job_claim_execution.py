"""工作流运行器接入作业执行占用的窄适配层。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from unilabos.workflow.job_claims import (
    JobClaimAdmission,
    WorkflowJobClaimCoordinator,
    workflow_terminal_fingerprint,
)
from unilabos.workflow.json_codec import encode_json
from unilabos.workflow.runtime_feedback import commit_runtime_job_feedback
from unilabos.workflow.store import StoreConflict


def _fingerprint(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(encode_json(payload, sort_keys=True)).hexdigest()


class WorkflowJobClaimExecution:
    """让 WorkflowRuntimeWorker 只依赖少量 claim 生命周期操作。"""

    def __init__(self, coordinator: Any, inventory: Any) -> None:
        self._coordinator = coordinator
        self._claims = WorkflowJobClaimCoordinator(coordinator._store, inventory)
        self._claims.recover_startup()

    def admit_dispatch(
        self,
        *,
        task: Mapping[str, Any],
        job: Mapping[str, Any],
        planned_node: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> JobClaimAdmission:
        admission = self._claims.acquire(
            task_uuid=str(task["uuid"]),
            job_uuid=str(job["uuid"]),
            attempt=int(job["attempt"]),
            device_id=str(payload["device_id"]),
            param_schema=(
                planned_node.get("param_schema")
                if isinstance(planned_node.get("param_schema"), dict)
                else None
            ),
            param=payload["param"],
        )
        if admission.status == "acquired":
            if admission.claim is None:
                raise StoreConflict("acquired JobExecutionClaim is missing")
            self._claims.prepare_dispatch(
                task_uuid=str(task["uuid"]),
                job_uuid=str(job["uuid"]),
                attempt=int(job["attempt"]),
                claim=admission.claim,
                param=payload["param"],
                coordinator=self._coordinator,
            )
            self._claims.acknowledge(admission.outbox_sequence)
        return admission

    def mark_dispatch_unknown(
        self,
        job: Mapping[str, Any],
        reason: str,
        *,
        phase: str,
        error_type: str | None = None,
    ) -> None:
        claim = self._require_claim(job)
        evidence_payload = {
            "job_uuid": job["uuid"],
            "phase": phase,
            "reason": reason,
        }
        if error_type is not None:
            evidence_payload["error_type"] = error_type
        uncertain = self._claims.mark_unknown(
            claim,
            reason=reason,
            evidence_fingerprint=_fingerprint(evidence_payload),
        )
        self._claims.acknowledge(uncertain.outbox_sequence)

    def on_job_status(
        self,
        job_uuid: str,
        feedback_data: dict[str, Any],
        status: str,
    ) -> None:
        if status != "running":
            return
        job = self._coordinator._execution_job(job_uuid)
        if self._coordinator._is_device_action_task(job["workflow_task_uuid"]):
            return
        if job["status"] not in {"dispatched", "running"}:
            return
        claim = self._require_claim(job)
        if claim.state in {"reserved", "uncertain"}:
            running = self._claims.mark_running(
                claim,
                evidence_fingerprint=_fingerprint(
                    {
                        "job_uuid": job_uuid,
                        "status": status,
                        "feedback": feedback_data,
                    }
                ),
            )
            self._claims.acknowledge(running.outbox_sequence)
        if job["status"] == "dispatched":
            self._coordinator.transition_job(job_uuid, "running")
        if feedback_data:
            commit_runtime_job_feedback(
                self._coordinator,
                source="workflow",
                job_uuid=job_uuid,
                feedback_data=feedback_data,
            )

    def commit_terminal(
        self,
        job: Mapping[str, Any],
        *,
        outcome: str,
        return_info: Mapping[str, Any],
        error_info: list[Any],
    ) -> Any:
        claim = self._require_claim(job)
        if claim.state == "reserved":
            running = self._claims.mark_running(
                claim,
                evidence_fingerprint=_fingerprint(
                    {
                        "job_uuid": job["uuid"],
                        "phase": "terminal_evidence",
                        "outcome": outcome,
                    }
                ),
            )
            self._claims.acknowledge(running.outbox_sequence)
            if running.claim is None:
                raise StoreConflict("terminal JobExecutionClaim evidence was rejected")
            claim = running.claim
        if any(
            member.resource_kind in {"business_material", "site"}
            for member in claim.members
        ):
            # 普通工作流的业务物料（Material）/库位（Site）结果尚没有冻结的
            # 变更集（ChangeSet）映射。不能把设备终态伪装成无业务影响的
            # 物理结算（PhysicalSettlement）；保留不确定栅栏，等待显式对账。
            uncertain = self._claims.mark_unknown(
                claim,
                reason="material_settlement_unavailable",
                evidence_fingerprint=_fingerprint(
                    {
                        "job_uuid": job["uuid"],
                        "phase": "terminal_material_settlement",
                        "outcome": outcome,
                        "return_info": dict(return_info),
                        "error_info": list(error_info),
                    }
                ),
            )
            self._claims.acknowledge(uncertain.outbox_sequence)
            return None
        receipt = self._claims.commit_terminal(
            claim,
            outcome=outcome,
            result={
                "return_info": dict(return_info),
                "error_info": list(error_info),
            },
        )
        self._claims.acknowledge(receipt.outbox_sequence)
        return receipt

    def release_terminal(
        self,
        job: Mapping[str, Any],
        receipt: Any,
        *,
        outcome: str,
        return_info: Mapping[str, Any],
        error_info: list[Any],
    ) -> None:
        if receipt is None:
            return
        claim = self._require_claim(job)
        terminal_fingerprint = workflow_terminal_fingerprint(
            job_uuid=str(job["uuid"]),
            attempt=int(job["attempt"]),
            outcome=outcome,
            receipt=receipt,
            return_info=return_info,
            error_info=error_info,
        )
        released = self._claims.release_terminal(
            claim,
            receipt,
            workflow_terminal_fingerprint=terminal_fingerprint,
        )
        self._claims.acknowledge(released.outbox_sequence)

    def _require_claim(self, job: Mapping[str, Any]) -> Any:
        claim = self._claims.find_claim(str(job["uuid"]), int(job["attempt"]))
        if claim is None:
            raise StoreConflict("dispatched Workflow Job has no JobExecutionClaim")
        return claim


__all__ = ["WorkflowJobClaimExecution"]
