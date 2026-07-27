"""Type-friendly markers for Python-authored Uni-Lab workflows.

Workflow modules are compiled from their Python AST and are never executed to
build the Canonical DAG.  These lightweight objects keep package sources
importable and editor-friendly without introducing a second runtime engine.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypeVar

WorkflowFunction = TypeVar("WorkflowFunction", bound=Callable[..., Any])


def workflow_definition(
    *,
    workflow_id: str,
    revision: str,
    parameter_ui: Mapping[str, Mapping[str, Any]] | None = None,
) -> Callable[[WorkflowFunction], WorkflowFunction]:
    """Mark one function as the compile-only workflow entry point."""

    del workflow_id, revision, parameter_ui

    def decorate(function: WorkflowFunction) -> WorkflowFunction:
        return function

    return decorate


class _HostNodeAuthoringHandle:
    """Static authoring surface for OS-owned human interaction nodes."""

    def manual_confirm(
        self,
        *,
        prompt: str,
        on_cancel: str = "raise",
    ) -> None:
        del prompt, on_cancel
        raise RuntimeError("workflow authoring calls are compile-only")


host_node = _HostNodeAuthoringHandle()


class _DeviceAuthoringHandle:
    """Compile-only handle for device ids that are not Python identifiers."""

    def __init__(self, device_id: str):
        self.device_id = device_id

    def __getattr__(self, action: str) -> Callable[..., Any]:
        def invoke(**_kwargs: Any) -> Any:
            raise RuntimeError(
                "workflow authoring calls are compile-only: "
                f"{self.device_id}.{action}"
            )

        return invoke


def device(device_id: str) -> _DeviceAuthoringHandle:
    """Address an exact OS device id, for example ``device("pump-1").dose``."""

    if not device_id:
        raise ValueError("device_id must be non-empty")
    return _DeviceAuthoringHandle(device_id)


class _CompileOnlyBlock:
    """Marker used by the AST compiler; entering it at runtime is forbidden."""

    def __enter__(self) -> None:
        raise RuntimeError("workflow authoring blocks are compile-only")

    def __exit__(self, *_args: object) -> None:
        return None


def group(*, name: str) -> _CompileOnlyBlock:
    """Mark a named sequential subgraph for the authoring compiler."""

    del name
    return _CompileOnlyBlock()


def parallel() -> _CompileOnlyBlock:
    """Mark sibling nodes that may run concurrently."""

    return _CompileOnlyBlock()


def workflow_output(**values: Any) -> Any:
    """Name values returned to a statically compiled parent workflow."""

    if len(values) == 1:
        return next(iter(values.values()))
    return tuple(values.values())


__all__ = [
    "device",
    "group",
    "host_node",
    "parallel",
    "workflow_definition",
    "workflow_output",
]
