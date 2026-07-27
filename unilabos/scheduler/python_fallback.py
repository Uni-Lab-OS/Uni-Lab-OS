"""Capability boundary for the reactive Python fallback execution engine.

The Python engine lowers and executes a complete Canonical DAG, but it only
owns live Layer-A device admission.  Material/slot selection, future
reservation, PlannedOccupancy, and solver-backed ordering belong to the Go
main engine and must fail closed here instead of being approximated.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from unilabos.scheduler.dag_model import DagNode, DagValidationError, TaskDag
from unilabos.scheduler.resource_lock import (
    LeaseRequest,
    ResolvedResourceClaim,
    ResourceLockManager,
)

PYTHON_FALLBACK_CAPABILITIES: dict[str, Any] = {
    "schemaVersion": "runtime-capabilities/v1",
    "engine": {
        "id": "python-fallback",
        "displayName": "Uni-Lab OS Python fallback",
        "selection": "deployment",
        "hotFailover": False,
        "admission": "reactive",
        "lowering": "canonical-v2",
        "controlFlow": True,
    },
    "layers": {
        "layerA": {
            "supported": True,
            "resourceKinds": ["device"],
            "atomicAcquireAll": True,
            "unknownFence": True,
        },
        "layerB": {
            "supported": False,
            "plannedOccupancy": False,
            "materialReservation": False,
            "slotReservation": False,
            "inTransitReservation": False,
        },
    },
    "scheduling": {
        "solver": False,
        "optimization": False,
        "decomposition": False,
        "crossStationHardWindowGuarantee": False,
    },
    "materials": {
        "tracking": True,
        "automaticAllocation": False,
        "acceptedAssignmentModes": ["prebound"],
    },
}


class PythonFallbackCapabilityError(DagValidationError):
    """The submitted DAG asks the Python engine to emulate Layer B."""

    code = "PYTHON_FALLBACK_CAPABILITY_UNSUPPORTED"

    def __init__(self, message: str):
        super().__init__(f"{self.code}: {message}")


def python_fallback_capabilities() -> dict[str, Any]:
    """Return a copy safe for an API caller to mutate."""

    return deepcopy(PYTHON_FALLBACK_CAPABILITIES)


def _is_internal_control(node: DagNode) -> bool:
    return (
        node.device_id == "os_control"
        and node.node_type in {"branch", "join", "group"}
        and node.action == node.node_type
    )


def _claim_resource_id(raw: Mapping[str, Any], node: DagNode) -> str:
    resource_id = (
        raw.get("resource_id")
        or raw.get("resource_uuid")
        or raw.get("resource_ref")
    )
    if resource_id:
        return str(resource_id)
    selector = str(raw.get("selector") or "")
    if selector == "bound_device":
        return f"device:{node.device_id}"
    raise PythonFallbackCapabilityError(
        f"node {node.node_id!r} contains an unresolved device selector "
        f"{selector or '-'}; the Python engine does not allocate candidates"
    )


def _resolved_device_claim(
    raw: Mapping[str, Any] | ResolvedResourceClaim,
    node: DagNode,
) -> ResolvedResourceClaim:
    if isinstance(raw, ResolvedResourceClaim):
        claim = raw
    else:
        resource_kind = str(raw.get("resource_kind") or "device")
        claim = ResolvedResourceClaim(
            resource_id=_claim_resource_id(raw, node),
            resource_kind=resource_kind,
            quantity=int(raw.get("quantity", 1) or 1),
            mode=str(raw.get("mode", "exclusive") or "exclusive"),
            scope=str(raw.get("scope", "action") or "action"),
        )
    if claim.resource_kind != "device":
        raise PythonFallbackCapabilityError(
            f"node {node.node_id!r} requests {claim.resource_kind!r} lock "
            f"{claim.resource_id!r}; only live device locks are supported"
        )
    if claim.quantity != 1:
        raise PythonFallbackCapabilityError(
            f"node {node.node_id!r} requests device quantity {claim.quantity}; "
            "the Python engine requires one exact device per claim"
        )
    if claim.mode != "exclusive":
        raise PythonFallbackCapabilityError(
            f"node {node.node_id!r} requests shared device lock "
            f"{claim.resource_id!r}; Layer-A device admission is exclusive"
        )
    return claim


def python_fallback_lease_request(
    node: DagNode,
    *,
    run_id: str | None = None,
) -> LeaseRequest:
    """Resolve one node to exact live device claims.

    The bound execution device is always included unless the action is marked
    ``always_free``.  Action contracts may add other *exact* device ids, but
    may not request material/slot locks or unresolved candidate selectors.
    """

    claims = [
        _resolved_device_claim(raw, node)
        for raw in node.resource_claims
    ]
    bound_device_id = f"device:{node.device_id}"
    if (
        not node.always_free
        and not _is_internal_control(node)
        and all(claim.resource_id != bound_device_id for claim in claims)
    ):
        claims.append(
            ResolvedResourceClaim(
                resource_id=bound_device_id,
                resource_kind="device",
                quantity=1,
                mode="exclusive",
                scope="action",
            )
        )
    claims = sorted(
        claims,
        key=lambda item: (
            item.resource_id,
            item.scope,
            item.mode,
            item.quantity,
        ),
    )
    if len({claim.resource_id for claim in claims}) != len(claims):
        raise PythonFallbackCapabilityError(
            f"node {node.node_id!r} repeats a device claim"
        )
    holder_id = (
        f"{run_id}:{node.node_id}"
        if run_id is not None
        else node.node_id
    )
    return LeaseRequest(holder_id=holder_id, claims=tuple(claims))


def validate_python_fallback_dag(
    dag: TaskDag,
    *,
    lock_manager: ResourceLockManager | None = None,
) -> None:
    """Reject unsupported scheduling semantics before any node dispatch."""

    manager = lock_manager
    for node in dag.nodes.values():
        if _is_internal_control(node):
            continue
        request = python_fallback_lease_request(node, run_id=dag.task_id)
        if manager is not None:
            manager.validate_request(request)
