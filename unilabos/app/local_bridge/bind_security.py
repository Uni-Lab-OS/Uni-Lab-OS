"""Fail-closed network binding policy for the unauthenticated local Runtime."""

from __future__ import annotations

import ipaddress


def require_loopback_runtime_host(host: str) -> None:
    """Reject exposing the Quick Debug Runtime beyond the local machine.

    W1–W2 intentionally has no remote authentication contract.  A future
    remote mode must introduce authenticated transport explicitly rather than
    making ``--host 0.0.0.0`` silently expose physical-action endpoints.
    """

    normalized = host.strip().casefold()
    if normalized == "localhost":
        return
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError as exc:
        raise ValueError(
            "UNSAFE_RUNTIME_BIND: Quick Debug accepts only loopback hosts "
            "until an authenticated remote transport is configured"
        ) from exc
    if not address.is_loopback:
        raise ValueError(
            "UNSAFE_RUNTIME_BIND: Quick Debug accepts only loopback hosts "
            "until an authenticated remote transport is configured"
        )
