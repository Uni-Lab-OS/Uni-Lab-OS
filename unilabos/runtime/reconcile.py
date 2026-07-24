"""Execution-side authority for resolving physically unknown resource fences."""

from __future__ import annotations

from dataclasses import dataclass

from unilabos.runtime.event_store import SQLiteEventJournal
from unilabos.scheduler.resource_lock import ResourceLockManager


@dataclass(frozen=True)
class ReconcileResult:
    status: str
    code: str = ""
    node_id: str = ""
    terminal: str = ""


def _resolved_terminal(
    journal: SQLiteEventJournal,
    *,
    run_id: str,
    node_id: str,
) -> str:
    projection = journal.load_node_projection(run_id, node_id)
    if projection is None:
        return ""
    terminal = str(projection.terminal or "")
    return {
        "succeeded": "success",
        "failed": "failed",
        "cancelled": "cancelled",
        "skipped": "skipped",
    }.get(terminal, terminal)


async def reconcile_unknown_fence(
    *,
    journal: SQLiteEventJournal | None,
    lock_manager: ResourceLockManager,
    run_id: str,
    lease_id: str,
    resolution: str,
    actor: str,
    reason: str,
) -> ReconcileResult:
    """Durably resolve, then release, one fence at the execution authority."""

    if resolution != "confirmed_safe" or not actor or not reason:
        return ReconcileResult(status="rejected", code="invalid_decision")
    if journal is None:
        return ReconcileResult(status="rejected", code="journal_unavailable")
    durable_resolution = journal.load_reconcile_resolution(
        run_id=run_id,
        lease_id=lease_id,
    )
    persisted = journal.load_open_lease(run_id=run_id, lease_id=lease_id)
    if persisted is None and durable_resolution is None:
        return ReconcileResult(status="rejected", code="lease_not_found")
    try:
        lease = lock_manager.get_lease(lease_id)
    except KeyError:
        return ReconcileResult(status="rejected", code="lease_not_found")

    if durable_resolution is not None:
        if lease.state == "unknown":
            await lock_manager.resolve_unknown(lease_id, release=True)
        elif lease.state != "released":
            return ReconcileResult(status="rejected", code="lease_not_unknown")
        node_id = str(durable_resolution.node_id or "")
        return ReconcileResult(
            status="reconciled",
            node_id=node_id,
            terminal=_resolved_terminal(
                journal,
                run_id=run_id,
                node_id=node_id,
            ),
        )

    assert persisted is not None
    if lease.state != "unknown" or lease.holder_id != persisted.holder_id:
        return ReconcileResult(status="rejected", code="lease_not_unknown")
    if not lease.holder_id.startswith(f"{run_id}:"):
        return ReconcileResult(status="rejected", code="lease_not_found")

    # The durable decision is the recovery authority.  If SQLite rejects the
    # transaction, the live unknown fence remains installed and conflicting
    # work stays blocked.  A crash after this commit but before the live
    # release is handled by the idempotent retry branch above.
    journal.record_reconcile_resolved(
        run_id=run_id,
        node_id=persisted.node_id,
        lease_id=lease_id,
        resolution=resolution,
        actor=actor,
        reason=reason,
    )
    await lock_manager.resolve_unknown(lease_id, release=True)
    return ReconcileResult(
        status="reconciled",
        node_id=persisted.node_id,
        terminal=_resolved_terminal(
            journal,
            run_id=run_id,
            node_id=persisted.node_id,
        ),
    )
