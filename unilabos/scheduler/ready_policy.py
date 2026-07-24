"""Deterministic admission policy for Quick Debug without a scheduler Plan."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, List

from .resource_lock import ResourceLease, ResourceLockManager


@dataclass(frozen=True)
class AdmittedNode:
    node: Any
    lease: ResourceLease


class DeterministicReadyPolicy:
    """Stable ready ordering plus non-blocking Layer-A admission attempts."""

    def __init__(self, *, lock_manager: ResourceLockManager | None) -> None:
        self.lock_manager = lock_manager

    def order(self, ready: Iterable[Any]) -> List[Any]:
        return sorted(
            ready,
            key=lambda node: (
                node.ready_since_seq,
                node.canonical_index,
                node.node_id,
            ),
        )

    async def admit(self, ready: Iterable[Any]) -> List[AdmittedNode]:
        if self.lock_manager is None:
            raise RuntimeError("lock_manager is required for admission")
        admitted: List[AdmittedNode] = []
        for node in self.order(ready):
            lease = await self.lock_manager.acquire_all(node.lease_request)
            if lease is None:
                node.admission_state = "ready"
                continue
            node.admission_state = "admitted"
            admitted.append(AdmittedNode(node=node, lease=lease))
        return admitted

