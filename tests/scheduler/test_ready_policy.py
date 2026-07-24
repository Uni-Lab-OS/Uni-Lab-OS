"""Quick Debug Alpha deterministic ready-node admission tests."""

from __future__ import annotations

import asyncio
import importlib
from types import ModuleType, SimpleNamespace

import pytest


def _module(name: str, capability: str) -> ModuleType:
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
        pytest.fail(f"{capability} is missing: add {name}", pytrace=False)


def test_order_is_stable_by_ready_sequence_canonical_index_and_node_id() -> None:
    api = _module("unilabos.scheduler.ready_policy", "DeterministicReadyPolicy")
    policy = api.DeterministicReadyPolicy(lock_manager=None)
    ready = [
        SimpleNamespace(node_id="z", ready_since_seq=2, canonical_index=0),
        SimpleNamespace(node_id="b", ready_since_seq=1, canonical_index=4),
        SimpleNamespace(node_id="a", ready_since_seq=1, canonical_index=4),
        SimpleNamespace(node_id="c", ready_since_seq=1, canonical_index=2),
    ]
    assert [node.node_id for node in policy.order(ready)] == ["c", "a", "b", "z"]


def test_blocked_node_stays_ready_while_later_feasible_node_is_admitted() -> None:
    locks = _module("unilabos.scheduler.resource_lock", "Layer-A ResourceLockManager")
    ready_api = _module("unilabos.scheduler.ready_policy", "DeterministicReadyPolicy")

    def request(holder: str, resource_id: str):
        return locks.LeaseRequest(
            holder_id=holder,
            claims=(
                locks.ResolvedResourceClaim(
                    resource_id=resource_id, quantity=1, mode="exclusive"
                ),
            ),
        )

    async def scenario() -> None:
        manager = locks.ResourceLockManager(runtime_epoch="epoch-1")
        assert await manager.acquire_all(request("running", "tank-1")) is not None
        policy = ready_api.DeterministicReadyPolicy(lock_manager=manager)
        blocked = SimpleNamespace(
            node_id="blocked",
            ready_since_seq=1,
            canonical_index=1,
            lease_request=request("blocked", "tank-1"),
        )
        feasible = SimpleNamespace(
            node_id="feasible",
            ready_since_seq=2,
            canonical_index=2,
            lease_request=request("feasible", "camera-1"),
        )

        admitted = await policy.admit([feasible, blocked])

        assert [item.node.node_id for item in admitted] == ["feasible"]
        assert blocked.admission_state == "ready"
        assert admitted[0].lease.holder_id == "feasible"

    asyncio.run(scenario())
