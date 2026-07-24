"""Durable-before-live reconcile ordering and retry-idempotency contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from unilabos.runtime.event_store import JournalCommitError, SQLiteEventJournal
from unilabos.runtime.reconcile import reconcile_unknown_fence
from unilabos.scheduler.resource_lock import (
    ResolvedResourceClaim,
    ResourceLockManager,
)


RUN_ID = "reconcile-atomicity"
NODE_ID = "develop-1"
LEASE_ID = "lease-reconcile-atomicity"
HOLDER_ID = f"{RUN_ID}:{NODE_ID}"


def _unknown_fence(
    tmp_path: Path,
) -> tuple[SQLiteEventJournal, ResourceLockManager]:
    journal = SQLiteEventJournal(
        tmp_path / "runtime.sqlite",
        runtime_epoch="epoch-2",
        clock=lambda: 100.0,
    )
    locks = ResourceLockManager(runtime_epoch="epoch-2")
    claims = (
        ResolvedResourceClaim(
            resource_id="developing-chamber-1",
            scope="workflow_block",
        ),
    )
    locks.install_unknown(
        holder_id=HOLDER_ID,
        claims=claims,
        reason="recovered after process exit",
        lease_id=LEASE_ID,
    )
    serialized_claims = [
        {
            "resource_id": claim.resource_id,
            "quantity": claim.quantity,
            "mode": claim.mode,
            "scope": claim.scope,
        }
        for claim in claims
    ]
    journal.record_lock_acquired(
        run_id=RUN_ID,
        node_id=NODE_ID,
        lease_id=LEASE_ID,
        holder_id=HOLDER_ID,
        claims=serialized_claims,
    )
    journal.record_lock_unknown(
        run_id=RUN_ID,
        node_id=NODE_ID,
        lease_id=LEASE_ID,
        holder_id=HOLDER_ID,
        claims=serialized_claims,
        reason="recovered after process exit",
    )
    return journal, locks


def _reconcile(
    journal: SQLiteEventJournal,
    locks: ResourceLockManager,
):
    return asyncio.run(
        reconcile_unknown_fence(
            journal=journal,
            lock_manager=locks,
            run_id=RUN_ID,
            lease_id=LEASE_ID,
            resolution="confirmed_safe",
            actor="operator@example.test",
            reason="device inspected and confirmed idle",
        )
    )


def test_journal_failure_does_not_release_live_unknown_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, locks = _unknown_fence(tmp_path)

    def fail_commit(**_kwargs: Any) -> None:
        raise JournalCommitError("injected reconcile journal failure")

    monkeypatch.setattr(journal, "record_reconcile_resolved", fail_commit)

    with pytest.raises(JournalCommitError, match="injected reconcile"):
        _reconcile(journal, locks)

    assert locks.get_lease(LEASE_ID).state == "unknown"
    assert journal.load_open_lease(run_id=RUN_ID, lease_id=LEASE_ID) is not None


def test_successful_reconcile_retry_is_idempotent(tmp_path: Path) -> None:
    journal, locks = _unknown_fence(tmp_path)

    first = _reconcile(journal, locks)
    second = _reconcile(journal, locks)

    assert first.status == "reconciled"
    assert second.status == "reconciled"
    assert locks.get_lease(LEASE_ID).state == "released"
    assert journal.load_open_lease(run_id=RUN_ID, lease_id=LEASE_ID) is None
    assert [
        event.type
        for event in journal.list_events(RUN_ID)
        if event.type == "reconcile_resolved"
    ] == ["reconcile_resolved"]
