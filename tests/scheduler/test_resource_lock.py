"""Quick Debug Alpha Layer-A lease invariants (hermetic, no wall clock)."""

from __future__ import annotations

import asyncio
import importlib
import random
from types import ModuleType

import pytest
from hypothesis import given, strategies as st


def _api() -> ModuleType:
    try:
        return importlib.import_module("unilabos.scheduler.resource_lock")
    except ModuleNotFoundError as exc:
        if exc.name != "unilabos.scheduler.resource_lock":
            raise
        pytest.fail("Layer-A ResourceLockManager capability is missing", pytrace=False)


def _request(api: ModuleType, holder: str, keys: list[str]):
    return api.LeaseRequest(
        holder_id=holder,
        claims=tuple(
            api.ResolvedResourceClaim(resource_id=key, quantity=1, mode="exclusive")
            for key in keys
        ),
    )


def test_acquire_all_is_atomic_and_uses_stable_claim_order() -> None:
    api = _api()

    async def scenario() -> None:
        manager = api.ResourceLockManager(runtime_epoch="epoch-1")
        held = await manager.acquire_all(_request(api, "holder-a", ["tank", "camera"]))
        assert held is not None
        assert [claim.resource_id for claim in held.claims] == ["camera", "tank"]

        rejected = await manager.acquire_all(
            _request(api, "holder-b", ["free-slot", "tank"])
        )
        assert rejected is None

        # If the failed request leaked its first claim, this independent acquire fails.
        free_slot = await manager.acquire_all(_request(api, "holder-c", ["free-slot"]))
        assert free_slot is not None

    asyncio.run(scenario())


def test_unknown_lease_is_not_released_automatically() -> None:
    api = _api()

    async def scenario() -> None:
        manager = api.ResourceLockManager(runtime_epoch="epoch-1")
        lease = await manager.acquire_all(_request(api, "holder-a", ["develop-tank-1"]))
        assert lease is not None
        await manager.mark_unknown(lease.lease_id, "PLC disconnected")
        await manager.release(lease.lease_id)

        blocked = await manager.acquire_all(
            _request(api, "holder-b", ["develop-tank-1"])
        )
        assert blocked is None
        assert manager.get_lease(lease.lease_id).state == "unknown"

    asyncio.run(scenario())


@given(st.lists(st.sampled_from(["device", "material", "slot", "tank"]), unique=True))
def test_claim_input_order_never_changes_lease_order(keys: list[str]) -> None:
    api = _api()

    async def scenario() -> None:
        manager = api.ResourceLockManager(runtime_epoch="epoch-1")
        shuffled = list(keys)
        random.Random(7).shuffle(shuffled)
        lease = await manager.acquire_all(_request(api, "holder", shuffled))
        assert lease is not None
        assert [claim.resource_id for claim in lease.claims] == sorted(keys)

    asyncio.run(scenario())


@given(
    held_key=st.sampled_from(["device", "material", "slot", "tank"]),
    free_key=st.sampled_from(["camera", "robot", "carrier", "line"]),
)
def test_failed_acquire_never_leaks_a_partial_lock(held_key: str, free_key: str) -> None:
    api = _api()

    async def scenario() -> None:
        manager = api.ResourceLockManager(runtime_epoch="epoch-1")
        first = await manager.acquire_all(_request(api, "first", [held_key]))
        assert first is not None
        assert await manager.acquire_all(
            _request(api, "conflict", [free_key, held_key])
        ) is None
        assert await manager.acquire_all(_request(api, "observer", [free_key])) is not None
        # A second holder can never acquire the already-held resource.
        assert await manager.acquire_all(_request(api, "second", [held_key])) is None

    asyncio.run(scenario())
