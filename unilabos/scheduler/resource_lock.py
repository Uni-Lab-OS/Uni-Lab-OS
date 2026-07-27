"""Layer-A live resource leases for safe, unscheduled Quick Debug execution."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, replace
from typing import Optional, Tuple


@dataclass(frozen=True)
class ResolvedResourceClaim:
    """A concrete runtime resource request resolved from an action contract."""

    resource_id: str
    resource_kind: str = "device"
    quantity: int = 1
    mode: str = "exclusive"
    scope: str = "action"

    def __post_init__(self) -> None:
        if not self.resource_id:
            raise ValueError("resource_id must not be empty")
        if self.resource_kind not in {"device", "material", "slot"}:
            raise ValueError(
                f"unsupported resource_kind: {self.resource_kind}"
            )
        if self.quantity < 1:
            raise ValueError("claim quantity must be positive")
        if self.mode not in {"exclusive", "shared"}:
            raise ValueError(f"unsupported claim mode: {self.mode}")
        if self.scope not in {
            "action",
            "until_handoff",
            "workflow_block",
            "persistent",
        }:
            raise ValueError(f"unsupported claim scope: {self.scope}")


@dataclass(frozen=True)
class LeaseRequest:
    """An acquire-all request for one node/holder."""

    holder_id: str
    claims: Tuple[ResolvedResourceClaim, ...]


@dataclass(frozen=True)
class ResourceLease:
    """A fenced live lease owned by one runtime epoch."""

    lease_id: str
    holder_id: str
    claims: Tuple[ResolvedResourceClaim, ...]
    runtime_epoch: str
    state: str = "active"
    reason: Optional[str] = None


class ResourceLockManager:
    """Single authority for atomic Layer-A admission in one OS process.

    No future intervals are represented here.  Claims are checked and installed
    under one mutex, so a rejected request leaves no partial ownership behind.
    """

    def __init__(
        self,
        *,
        runtime_epoch: str,
        supported_resource_kinds: frozenset[str] = frozenset({"device"}),
    ) -> None:
        if not runtime_epoch:
            raise ValueError("runtime_epoch must not be empty")
        if not supported_resource_kinds:
            raise ValueError("supported_resource_kinds must not be empty")
        self.runtime_epoch = runtime_epoch
        self.supported_resource_kinds = frozenset(supported_resource_kinds)
        self._guard = asyncio.Lock()
        self._changed = asyncio.Event()
        self._leases: dict[str, ResourceLease] = {}
        self._holders_by_resource: dict[str, set[str]] = {}

    async def acquire_all(self, request: LeaseRequest) -> Optional[ResourceLease]:
        self.validate_request(request)
        claims = tuple(
            sorted(
                request.claims,
                key=lambda item: (
                    item.resource_id,
                    item.mode,
                    item.quantity,
                    item.scope,
                ),
            )
        )
        if len({claim.resource_id for claim in claims}) != len(claims):
            raise ValueError("one lease request cannot repeat a resource_id")
        async with self._guard:
            if any(self._conflicts(claim) for claim in claims):
                return None
            lease = ResourceLease(
                lease_id=str(uuid.uuid4()),
                holder_id=request.holder_id,
                claims=claims,
                runtime_epoch=self.runtime_epoch,
            )
            self._leases[lease.lease_id] = lease
            for claim in claims:
                self._holders_by_resource.setdefault(claim.resource_id, set()).add(
                    lease.lease_id
                )
            return lease

    def validate_request(self, request: LeaseRequest) -> None:
        """Fail closed when the active engine cannot own a claim kind."""

        unsupported = sorted(
            {
                claim.resource_kind
                for claim in request.claims
                if claim.resource_kind not in self.supported_resource_kinds
            }
        )
        if unsupported:
            raise ValueError(
                "unsupported resource kind for this lock manager: "
                f"{unsupported[0]}"
            )

    def _conflicts(self, requested: ResolvedResourceClaim) -> bool:
        lease_ids = self._holders_by_resource.get(requested.resource_id, set())
        for lease_id in lease_ids:
            lease = self._leases[lease_id]
            if lease.state not in {"active", "unknown"}:
                continue
            existing = next(
                claim
                for claim in lease.claims
                if claim.resource_id == requested.resource_id
            )
            if requested.mode == "exclusive" or existing.mode == "exclusive":
                return True
        return False

    async def release(
        self,
        lease_id: str,
        *,
        scope: str | None = None,
        resource_id: str | None = None,
    ) -> bool:
        async with self._guard:
            lease = self._leases.get(lease_id)
            if lease is None or lease.state == "released":
                return False
            if lease.state == "unknown":
                return False
            return self._release_locked(
                lease,
                scope=scope,
                resource_id=resource_id,
            )

    async def release_holder(
        self,
        holder_id: str,
        *,
        scope: str | None = None,
    ) -> int:
        """Release a holder's confirmed-safe claims, optionally by scope.

        Long-lived claims are deliberately not tied to node terminal state.  A
        handoff or workflow controller must call this method explicitly.
        Unknown leases are never released by this path.
        """

        async with self._guard:
            released = 0
            for lease in tuple(self._leases.values()):
                if lease.holder_id != holder_id or lease.state != "active":
                    continue
                if self._release_locked(lease, scope=scope, resource_id=None):
                    released += 1
            return released

    def _release_locked(
        self,
        lease: ResourceLease,
        *,
        scope: str | None,
        resource_id: str | None,
    ) -> bool:
        released_claims = tuple(
            claim
            for claim in lease.claims
            if (scope is None or claim.scope == scope)
            and (resource_id is None or claim.resource_id == resource_id)
        )
        if not released_claims:
            return False

        for claim in released_claims:
            self._remove_claim_ownership(lease.lease_id, claim.resource_id)

        remaining = tuple(
            claim for claim in lease.claims if claim not in released_claims
        )
        if remaining:
            self._leases[lease.lease_id] = replace(lease, claims=remaining)
        else:
            self._leases[lease.lease_id] = replace(
                lease,
                claims=(),
                state="released",
            )
        self._changed.set()
        return True

    async def mark_unknown(self, lease_id: str, reason: str) -> ResourceLease:
        async with self._guard:
            lease = self._leases.get(lease_id)
            if lease is None:
                raise KeyError(lease_id)
            if lease.state != "active":
                raise RuntimeError(
                    f"lease {lease_id} is {lease.state}, cannot become unknown"
                )
            unknown = replace(lease, state="unknown", reason=reason)
            self._leases[lease_id] = unknown
            return unknown

    async def resolve_unknown(self, lease_id: str, *, release: bool) -> ResourceLease:
        """Resolve a reconciled lease explicitly; unknown never expires itself."""

        async with self._guard:
            lease = self._leases.get(lease_id)
            if lease is None:
                raise KeyError(lease_id)
            if lease.state != "unknown":
                raise RuntimeError(f"lease {lease_id} is not unknown")
            state = "released" if release else "active"
            resolved = replace(lease, state=state, reason=None)
            self._leases[lease_id] = resolved
            if release:
                self._remove_ownership(lease)
                self._changed.set()
            return resolved

    def install_unknown(
        self,
        *,
        holder_id: str,
        claims: Tuple[ResolvedResourceClaim, ...],
        reason: str,
        lease_id: str | None = None,
    ) -> ResourceLease:
        """Install a persisted fence during single-threaded startup recovery.

        Recovery happens before the runtime accepts submissions, so this
        synchronous method intentionally does not enter the asyncio mutex.
        """

        self.validate_request(
            LeaseRequest(holder_id=holder_id, claims=claims)
        )
        restored = ResourceLease(
            lease_id=lease_id or str(uuid.uuid4()),
            holder_id=holder_id,
            claims=tuple(
                sorted(
                    claims,
                    key=lambda item: (
                        item.resource_id,
                        item.mode,
                        item.quantity,
                        item.scope,
                    ),
                )
            ),
            runtime_epoch=self.runtime_epoch,
            state="unknown",
            reason=reason,
        )
        existing = self._leases.get(restored.lease_id)
        if existing is not None:
            if (
                existing.holder_id == restored.holder_id
                and existing.claims == restored.claims
            ):
                # Recovery can be requested after the current epoch already
                # installed the same fence.  Never resurrect or duplicate it;
                # the caller still observes its current live state.
                return existing
            raise ValueError(f"duplicate lease_id: {restored.lease_id}")
        if any(self._conflicts(claim) for claim in restored.claims):
            raise RuntimeError(
                f"cannot restore conflicting lease for holder {holder_id}"
            )
        self._leases[restored.lease_id] = restored
        for claim in restored.claims:
            self._holders_by_resource.setdefault(claim.resource_id, set()).add(
                restored.lease_id
            )
        return restored

    async def wait_for_change(self) -> None:
        """Wait until resource availability may have changed."""

        await self._changed.wait()
        self._changed.clear()

    def notify_waiters(self) -> None:
        """Wake blocked admission loops, for example during run cancellation."""

        self._changed.set()

    def _remove_ownership(self, lease: ResourceLease) -> None:
        for claim in lease.claims:
            self._remove_claim_ownership(lease.lease_id, claim.resource_id)

    def _remove_claim_ownership(self, lease_id: str, resource_id: str) -> None:
        holders = self._holders_by_resource.get(resource_id)
        if holders is None:
            return
        holders.discard(lease_id)
        if not holders:
            self._holders_by_resource.pop(resource_id, None)

    def get_lease(self, lease_id: str) -> ResourceLease:
        return self._leases[lease_id]

    def active_leases(self) -> Tuple[ResourceLease, ...]:
        return tuple(
            lease
            for lease in self._leases.values()
            if lease.state in {"active", "unknown"}
        )
