"""Runtime/v1 authoring-contract validation backed by shared JSON Schema."""

from __future__ import annotations

import copy
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

from .canonical import WorkflowRevision as CanonicalWorkflowRevision


_SCHEMA_NAMES = (
    "canonical-workflow.schema.json",
    "workflow-source-map.schema.json",
    "workflow-revision.schema.json",
    "workflow-change-proposal.schema.json",
)


def _schema_root() -> Path:
    configured = os.environ.get("UNILAB_CONTRACTS_DIR")
    if configured:
        return Path(configured).expanduser().resolve() / "runtime" / "v1"
    return Path(__file__).resolve().parent / "schemas" / "runtime" / "v1"


@lru_cache(maxsize=1)
def _validators() -> dict[str, Draft202012Validator]:
    root = _schema_root()
    schemas: dict[str, dict[str, Any]] = {}
    for name in _SCHEMA_NAMES:
        path = root / name
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"runtime/v1 schema unavailable: {path}") from exc
        if not isinstance(value, dict) or not isinstance(value.get("$id"), str):
            raise ValueError(f"runtime/v1 schema has no stable $id: {path}")
        schemas[name] = value

    registry = Registry().with_resources(
        (
            schema["$id"],
            Resource.from_contents(schema),
        )
        for schema in schemas.values()
    )
    return {
        name: Draft202012Validator(
            schema,
            registry=registry,
            format_checker=FormatChecker(),
        )
        for name, schema in schemas.items()
    }


def _validate_schema(name: str, payload: Mapping[str, Any]) -> None:
    errors = sorted(
        _validators()[name].iter_errors(payload),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if not errors:
        return
    error: ValidationError = errors[0]
    location = ".".join(str(part) for part in error.absolute_path) or "$"
    raise ValueError(
        f"RUNTIME_V1_SCHEMA_INVALID: {name}:{location}: {error.message}"
    )


def _span_is_ordered(span: Mapping[str, Any]) -> bool:
    start = (int(span["start_line"]), int(span["start_column"]))
    end = (int(span["end_line"]), int(span["end_column"]))
    return end >= start


_UNSAFE_PROPOSAL_FIELDS = frozenset(
    {
        "databasecredentials",
        "devicetoken",
        "executionrequest",
    }
)


def _reject_unsafe_proposal_fields(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = "".join(
                character.lower()
                for character in str(key).strip()
                if character.isalnum()
            )
            if normalized in _UNSAFE_PROPOSAL_FIELDS:
                raise ValueError(f"WORKFLOW_PROPOSAL_UNSAFE_FIELD: {path}.{key}")
            _reject_unsafe_proposal_fields(nested, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_unsafe_proposal_fields(nested, f"{path}[{index}]")


def validate_workflow_revision(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one authoring envelope without persisting or executing it."""

    document = copy.deepcopy(dict(payload))
    _validate_schema("workflow-revision.schema.json", document)
    canonical = CanonicalWorkflowRevision.model_validate(document["canonical_ir"])
    if canonical.content_hash != document["content_hash"]:
        raise ValueError("WORKFLOW_CONTENT_HASH_MISMATCH")

    node_ids = {invocation.node_id for invocation in canonical.invocations}
    for span in document["source_map"]:
        if span["node_id"] not in node_ids:
            raise ValueError(f"SOURCE_MAP_UNKNOWN_NODE: {span['node_id']}")
        if not _span_is_ordered(span):
            raise ValueError(f"SOURCE_MAP_INVALID_SPAN: {span['node_id']}")
    for diagnostic in document["diagnostics"]:
        node_id = diagnostic.get("node_id")
        if node_id is not None and node_id not in node_ids:
            raise ValueError(f"DIAGNOSTIC_UNKNOWN_NODE: {node_id}")
        coordinate_fields = {
            "start_line",
            "start_column",
            "end_line",
            "end_column",
        }
        present_coordinates = coordinate_fields.intersection(diagnostic)
        if present_coordinates and present_coordinates != coordinate_fields:
            raise ValueError(
                f"DIAGNOSTIC_PARTIAL_SPAN: {diagnostic['code']}"
            )
        if present_coordinates and not _span_is_ordered(diagnostic):
            raise ValueError(f"DIAGNOSTIC_INVALID_SPAN: {diagnostic['code']}")
    return document


def validate_workflow_change_proposal(
    payload: Mapping[str, Any],
    *,
    current_revision_id: str,
) -> dict[str, Any]:
    """Validate a proposal against the currently displayed base revision."""

    document = copy.deepcopy(dict(payload))
    _validate_schema("workflow-change-proposal.schema.json", document)
    _reject_unsafe_proposal_fields(document)
    if document["base_revision_id"] != current_revision_id:
        raise ValueError("WORKFLOW_PROPOSAL_STALE_BASE_REVISION")
    return document


__all__ = [
    "validate_workflow_change_proposal",
    "validate_workflow_revision",
]
