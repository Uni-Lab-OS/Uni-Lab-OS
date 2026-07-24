"""Compose loaded Profiles into generic runtime driver instances."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from unilabos.runtime.profile_loader import LoadedProfile


ConnectionResolver = Callable[[str], Any | None]


def build_runtime_drivers(
    profiles: Mapping[str, LoadedProfile],
    driver_catalog: Mapping[str, Any],
    connection_resolver: ConnectionResolver,
) -> dict[str, Any]:
    """Validate a complete Profile set, then construct its device drivers.

    Validation and connection resolution finish before the first constructor is
    called.  Startup therefore fails atomically instead of exposing a partially
    installed workstation when one Profile is invalid.
    """

    prepared: list[tuple[LoadedProfile, str, Any]] = []
    device_ids: set[str] = set()
    for profile in profiles.values():
        binding = profile.driver_binding
        device_id = str(binding.get("device_id") or "")
        driver_key = str(binding.get("driver_key") or "")
        if device_id in device_ids:
            raise ValueError(f"duplicate runtime device_id: {device_id}")
        device_ids.add(device_id)
        driver_cls = driver_catalog.get(driver_key)
        if driver_cls is None:
            raise ValueError(f"runtime driver is not registered: {driver_key}")
        prepared.append((profile, device_id, driver_cls))

    resolved: list[tuple[LoadedProfile, str, Any, Any]] = []
    for profile, device_id, driver_cls in prepared:
        connection_ref = str(
            profile.driver_binding.get("connection_ref") or ""
        )
        connection = connection_resolver(connection_ref)
        if connection is None:
            raise ValueError(
                f"runtime connection could not be resolved: {connection_ref}"
            )
        resolved.append((profile, device_id, driver_cls, connection))

    return {
        device_id: driver_cls(
            plc=connection,
            driver_config=profile.driver_config,
        )
        for profile, device_id, driver_cls, connection in resolved
    }
