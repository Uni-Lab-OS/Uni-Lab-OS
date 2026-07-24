"""Quick Debug transport lowering guard.

Layer B is absent in W1-W2, so only an explicit transport or exactly one static
candidate may be lowered.  Selection among alternatives is a scheduling task.
"""

from __future__ import annotations

from typing import Any, Iterable


class TransportCompileError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def select_static_transport(
    candidates: Iterable[dict[str, Any]],
    *,
    explicit_id: str | None = None,
) -> dict[str, Any]:
    options = list(candidates)
    if explicit_id is not None:
        matching = [item for item in options if item.get("id") == explicit_id]
        if len(matching) != 1:
            raise TransportCompileError(
                "EXPLICIT_TRANSPORT_NOT_FOUND",
                f"explicit transport {explicit_id!r} is not uniquely available",
            )
        return matching[0]
    if len(options) != 1:
        raise TransportCompileError(
            "TRANSPORT_SELECTION_REQUIRES_PLANNING",
            f"Quick Debug requires exactly one static transport, got {len(options)}",
        )
    return options[0]

