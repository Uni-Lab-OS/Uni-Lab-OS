"""Edge control plane endpoint derivation rules."""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse


def derive_scheduler_address(backend_address: str) -> str:
    """Derive the Scheduler Authority origin from a Backend API address.

    Local split deployments expose Backend API and Scheduler on adjacent ports,
    while ingress deployments without an explicit port route both contracts
    through the same origin.
    """

    value = str(backend_address or "")
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return value
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = (
        f"{hostname}:{parsed.port + 1}" if parsed.port is not None else parsed.netloc
    )
    return urlunparse((parsed.scheme, netloc, "", "", "", ""))
