"""Test-owned fixtures for the generic workflow authoring boundary."""

from __future__ import annotations

import copy
import importlib
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

from unilabos.registry.action_catalog import scan_decorated_device_package


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
PTLC_PACKAGE_ROOT = (
    WORKSPACE_ROOT
    / "Uni-Lab-Templates"
    / "packages"
    / "ptlc_station"
    / "ptlc_station"
)
GOLDEN_SOURCE_PATH = PTLC_PACKAGE_ROOT / "workflows" / "develop_prepare.py"
GOLDEN_SOURCE_URI = (
    "packages/ptlc_station/ptlc_station/workflows/develop_prepare.py"
)
GOLDEN_SOURCE = GOLDEN_SOURCE_PATH.read_text(encoding="utf-8")
BASE_REVISION_ID = "authoring-base-revision"

HOST_ACTION_CATALOG: dict[str, dict[str, Any]] = {
    "host_node.manual_confirm": {
        "inputs": {
            "prompt": {"type": "string", "required": True},
            "on_cancel": {"type": "string", "default": "raise"},
        },
        "outputs": {},
    }
}

CONTROL_ACTION_CATALOG: dict[str, dict[str, Any]] = {
    "station.prepare": {
        "inputs": {"sample_id": {"type": "string", "required": True}},
        "outputs": {"sample": {"type": "string"}},
    },
    "station.mix": {
        "inputs": {
            "sample": {"type": "string", "required": True},
            "amount": {"type": "integer", "required": True},
        },
        "outputs": {"mixture": {"type": "string"}},
    },
    "station.inspect": {
        "inputs": {"sample": {"type": "string", "required": True}},
        "outputs": {
            "ok": {"type": "boolean"},
            "image": {"type": "string"},
        },
    },
    "station.finish": {
        "inputs": {"sample": {"type": "string", "required": True}},
        "outputs": {},
    },
    "station.cleanup": {"inputs": {}, "outputs": {}},
}

CONTROL_FLOW_SOURCE = '''\
from unilabos.workflow.authoring import group, host_node, parallel, workflow_definition

@workflow_definition(workflow_id="control_flow", revision="authoring-v1")
def control_flow(sample_id: str) -> None:
    """Static control flow remains compile-only."""
    with group(name="preparation"):
        prepared = station.prepare(sample_id=sample_id)
        for amount in [1, 2]:
            station.mix(sample=prepared.sample, amount=amount)
    with parallel():
        left = station.inspect(sample=prepared.sample)
        right = station.inspect(sample=prepared.sample)
    if left.ok and right.ok:
        host_node.manual_confirm(prompt="Continue?", on_cancel="raise")
    try:
        station.finish(sample=prepared.sample)
    finally:
        station.cleanup()
'''


def golden_action_catalog() -> dict[str, dict[str, Any]]:
    return {
        **scan_decorated_device_package(PTLC_PACKAGE_ROOT),
        **HOST_ACTION_CATALOG,
    }


def authoring_request(
    *,
    source: str = GOLDEN_SOURCE,
    source_uri: str = GOLDEN_SOURCE_URI,
    base_revision_id: str = BASE_REVISION_ID,
) -> dict[str, Any]:
    return {
        "base_revision_id": base_revision_id,
        "python_source": source,
        "source_uri": source_uri,
    }


def as_mapping(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        document = model_dump(mode="json")
        assert isinstance(document, dict)
        return document
    pytest.fail(
        f"authoring result must be a mapping or Pydantic model, got {type(value)!r}",
        pytrace=False,
    )


def require_authoring_functions() -> tuple[
    Callable[..., object],
    Callable[..., object],
    Callable[..., object],
]:
    try:
        canonical_ir = importlib.import_module("unilabos.workflow.canonical_ir")
    except ModuleNotFoundError as error:
        if error.name != "unilabos.workflow.canonical_ir":
            raise
        pytest.fail(
            "MISSING_AUTHORING_CANONICAL_IR: "
            "unilabos.workflow.canonical_ir is required",
            pytrace=False,
        )
    try:
        to_python = importlib.import_module("unilabos.workflow.to_python_script")
    except ModuleNotFoundError as error:
        if error.name != "unilabos.workflow.to_python_script":
            raise
        pytest.fail(
            "MISSING_AUTHORING_DECOMPILER: "
            "unilabos.workflow.to_python_script is required",
            pytrace=False,
        )

    compile_revision = getattr(canonical_ir, "compile_authoring_revision", None)
    validate_revision = getattr(canonical_ir, "validate_authoring_revision", None)
    generate_revision = getattr(to_python, "generate_python_revision", None)
    if not all(
        callable(function)
        for function in (compile_revision, validate_revision, generate_revision)
    ):
        pytest.fail(
            "MISSING_AUTHORING_FUNCTIONS: compile_authoring_revision, "
            "validate_authoring_revision, and generate_python_revision are required",
            pytrace=False,
        )
    return compile_revision, validate_revision, generate_revision


def compile_result(
    *,
    request: Mapping[str, Any] | None = None,
    action_catalog: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    compile_revision, _, _ = require_authoring_functions()
    return as_mapping(
        compile_revision(
            dict(request or authoring_request()),
            action_catalog=dict(action_catalog or golden_action_catalog()),
        )
    )


def error_diagnostics(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    diagnostics = result.get("diagnostics")
    assert isinstance(diagnostics, list)
    return [
        dict(diagnostic)
        for diagnostic in diagnostics
        if isinstance(diagnostic, Mapping)
        and diagnostic.get("severity") == "error"
    ]
