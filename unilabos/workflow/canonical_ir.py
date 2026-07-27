"""Pure authoring services for Python source and Canonical workflow revisions.

This module is deliberately transport-, persistence-, and execution-free.  It
only produces immutable authoring candidates that a caller may validate and
explicitly apply later.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from .canonical import WorkflowRevision
from .contracts import validate_workflow_revision
from .from_python_script import (
    PythonWorkflowCompileError,
    WorkflowSourceResolver,
    compile_python_script,
)


AuthoringResult = dict[str, Any]

_AUTHORING_COMPILE_FIELDS = frozenset(
    {"base_revision_id", "python_source", "source_uri"}
)
_AUTHORING_VALIDATE_FIELDS = frozenset({"base_revision_id", "candidate"})
_CONTROL_ACTION_PREFIX = "os_control."


class _ActionCatalogValidationError(ValueError):
    """Semantic catalog mismatch attached to one Canonical invocation."""

    def __init__(self, code: str, message: str, *, node_id: str):
        super().__init__(message)
        self.code = code
        self.node_id = node_id


def _canonical_document(revision: WorkflowRevision) -> dict[str, Any]:
    document = revision.model_dump(mode="json", exclude_none=True)
    for index, parameter in enumerate(revision.parameters or []):
        if "default" not in parameter.model_fields_set:
            document["parameters"][index].pop("default", None)
    return document


def _source_artifact(source: str, source_uri: str) -> dict[str, str]:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return {
        "format": "python",
        "text": source,
        "uri": source_uri,
        "content_hash": f"sha256:{digest}",
    }


def _source_spans(
    revision: WorkflowRevision,
    source: str,
) -> list[dict[str, Any]]:
    lines = source.splitlines()
    known = {invocation.node_id for invocation in revision.invocations}
    spans: list[dict[str, Any]] = []
    mapped: set[str] = set()

    for entry in revision.source_map.entries:
        node_ids = [
            node_id
            for node_id in [entry.node_id, *entry.compiled_node_ids]
            if node_id in known
        ]
        line = max(int(entry.line or 1), 1)
        column = max(int(entry.column or 0) + 1, 1)
        line_text = lines[line - 1] if line <= len(lines) else ""
        end_column = max(len(line_text) + 1, column)
        for node_id in node_ids:
            if node_id in mapped:
                continue
            mapped.add(node_id)
            spans.append(
                {
                    "node_id": node_id,
                    "start_line": line,
                    "start_column": column,
                    "end_line": line,
                    "end_column": end_column,
                }
            )

    fallback_line = max(len(lines), 1)
    fallback_end = max(
        len(lines[fallback_line - 1]) + 1 if lines else 1,
        1,
    )
    for invocation in revision.invocations:
        if invocation.node_id in mapped:
            continue
        spans.append(
            {
                "node_id": invocation.node_id,
                "start_line": fallback_line,
                "start_column": 1,
                "end_line": fallback_line,
                "end_column": fallback_end,
            }
        )
    return spans


def _candidate(
    *,
    base_revision_id: str,
    revision: WorkflowRevision,
    python_source: str,
    authoring_surface: str,
    diagnostics: list[dict[str, Any]],
    source_map: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    canonical = _canonical_document(revision)
    content_hash = revision.content_hash
    document = {
        "revision_id": f"authoring-{authoring_surface}-{content_hash[:20]}",
        "parent_revision_id": base_revision_id,
        "schema_version": "runtime/v1",
        "content_hash": content_hash,
        "canonical_ir": canonical,
        "python_source": python_source,
        "source_map": (
            copy.deepcopy(source_map)
            if source_map is not None
            else _source_spans(revision, python_source)
        ),
        "authoring_surface": authoring_surface,
        "diagnostics": copy.deepcopy(diagnostics),
        "view_metadata": {},
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    return validate_workflow_revision(document)


def _diagnostic(
    *,
    code: str,
    message: str,
    node: ast.AST | None = None,
    error: BaseException | None = None,
) -> dict[str, Any]:
    line = int(getattr(error, "lineno", None) or getattr(node, "lineno", None) or 1)
    column = int(
        getattr(error, "offset", None)
        or (int(getattr(node, "col_offset", 0)) + 1 if node is not None else 1)
    )
    end_line = int(
        getattr(error, "end_lineno", None) or getattr(node, "end_lineno", None) or line
    )
    end_column = int(
        getattr(error, "end_offset", None)
        or (
            int(getattr(node, "end_col_offset", column)) + 1
            if node is not None
            else column
        )
    )
    return {
        "severity": "error",
        "code": code,
        "message": message or code,
        "start_line": max(line, 1),
        "start_column": max(column, 1),
        "end_line": max(end_line, line, 1),
        "end_column": max(end_column, 1),
    }


def _unsupported_python_node(tree: ast.AST) -> tuple[str, ast.AST] | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.While, ast.AsyncFor, ast.AsyncWith)):
            return "UNSUPPORTED_CONTROL_FLOW", node
        if isinstance(node, ast.Try) and (node.handlers or node.orelse):
            return "UNSUPPORTED_TRY_EXCEPT", node
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"__import__", "eval", "exec"}:
                return "UNSAFE_PYTHON_CALL", node
    return None


def _result(
    base_revision_id: str,
    *,
    candidate: Mapping[str, Any] | None,
    diagnostics: list[dict[str, Any]],
) -> AuthoringResult:
    return {
        "base_revision_id": base_revision_id,
        "candidate": (
            copy.deepcopy(dict(candidate)) if candidate is not None else None
        ),
        "diagnostics": copy.deepcopy(diagnostics),
    }


def _require_exact_fields(
    request: Mapping[str, Any],
    expected: frozenset[str],
) -> None:
    if set(request) != expected:
        missing = sorted(expected - set(request))
        extra = sorted(set(request) - expected)
        raise ValueError(
            f"INVALID_AUTHORING_ENVELOPE: missing={missing or '-'} extra={extra or '-'}"
        )


def compile_authoring_revision(
    request: Mapping[str, Any],
    *,
    action_catalog: Mapping[str, Mapping[str, Any]],
    workflow_source_resolver: WorkflowSourceResolver | None = None,
) -> AuthoringResult:
    """Compile Python through the AST-only compiler into an unapplied candidate."""

    _require_exact_fields(request, _AUTHORING_COMPILE_FIELDS)
    base_revision_id = str(request["base_revision_id"])
    source = str(request["python_source"])
    source_uri = str(request["source_uri"])
    if not base_revision_id or not source or not source_uri:
        raise ValueError("INVALID_AUTHORING_ENVELOPE: fields must be non-empty")

    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        diagnostic = _diagnostic(
            code="PYTHON_SYNTAX_ERROR",
            message=str(error),
            error=error,
        )
        return _result(
            base_revision_id,
            candidate=None,
            diagnostics=[diagnostic],
        )

    unsupported = _unsupported_python_node(tree)
    if unsupported is not None:
        code, node = unsupported
        diagnostic = _diagnostic(
            code=code,
            message=f"{code}: {type(node).__name__} is outside the authoring subset",
            node=node,
        )
        return _result(
            base_revision_id,
            candidate=None,
            diagnostics=[diagnostic],
        )

    try:
        revision = compile_python_script(
            source,
            action_catalog={
                name: dict(definition) for name, definition in action_catalog.items()
            },
            source_artifact=_source_artifact(source, source_uri),
            workflow_source_resolver=workflow_source_resolver,
        )
        candidate = _candidate(
            base_revision_id=base_revision_id,
            revision=revision,
            python_source=source,
            authoring_surface="code",
            diagnostics=[],
        )
    except (PythonWorkflowCompileError, ValidationError, ValueError) as error:
        cause = error
        if not any(
            getattr(error, attribute, None)
            for attribute in ("lineno", "offset", "end_lineno", "end_offset")
        ) and isinstance(error.__cause__, BaseException):
            cause = error.__cause__
        diagnostic = _diagnostic(
            code="PYTHON_COMPILE_ERROR",
            message=str(error),
            error=cause,
        )
        return _result(
            base_revision_id,
            candidate=None,
            diagnostics=[diagnostic],
        )
    return _result(base_revision_id, candidate=candidate, diagnostics=[])


def _validate_action_catalog(
    revision: WorkflowRevision,
    action_catalog: Mapping[str, Mapping[str, Any]],
) -> None:
    for invocation in revision.invocations:
        if invocation.action_ref.startswith(
            _CONTROL_ACTION_PREFIX
        ) or invocation.node_type in {"branch", "fork", "join", "group", "parallel"}:
            continue
        definition = action_catalog.get(invocation.action_ref)
        if definition is None:
            raise _ActionCatalogValidationError(
                "UNKNOWN_ACTION",
                f"UNKNOWN_ACTION: {invocation.action_ref}",
                node_id=invocation.node_id,
            )
        raw_inputs = definition.get("inputs", {})
        declared_inputs = set(raw_inputs) if isinstance(raw_inputs, Mapping) else set()
        unknown_inputs = sorted(set(invocation.input_bindings) - declared_inputs)
        if unknown_inputs:
            raise _ActionCatalogValidationError(
                "UNKNOWN_ACTION_INPUT",
                (
                    f"unknown input {unknown_inputs[0]!r} for action "
                    f"{invocation.action_ref!r}"
                ),
                node_id=invocation.node_id,
            )


def _catalog_diagnostic(
    error: _ActionCatalogValidationError,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    diagnostic = {
        "severity": "error",
        "code": error.code,
        "message": str(error),
        "node_id": error.node_id,
    }
    span = next(
        (
            item
            for item in candidate.get("source_map", [])
            if item.get("node_id") == error.node_id
        ),
        None,
    )
    if isinstance(span, Mapping):
        diagnostic.update(
            {
                name: int(span[name])
                for name in (
                    "start_line",
                    "start_column",
                    "end_line",
                    "end_column",
                )
            }
        )
    return diagnostic


def validate_authoring_revision(
    request: Mapping[str, Any],
    *,
    action_catalog: Mapping[str, Mapping[str, Any]],
) -> AuthoringResult:
    """Fail closed on a candidate without mutating any active workflow."""

    _require_exact_fields(request, _AUTHORING_VALIDATE_FIELDS)
    base_revision_id = str(request["base_revision_id"])
    candidate_value = request["candidate"]
    if not base_revision_id or not isinstance(candidate_value, Mapping):
        raise ValueError("INVALID_AUTHORING_ENVELOPE: candidate is required")
    try:
        candidate = validate_workflow_revision(candidate_value)
        if candidate["parent_revision_id"] != base_revision_id:
            raise ValueError("STALE_AUTHORING_BASE_REVISION")
        revision = WorkflowRevision.model_validate(candidate["canonical_ir"])
        _validate_action_catalog(revision, action_catalog)
    except _ActionCatalogValidationError as error:
        return _result(
            base_revision_id,
            candidate=None,
            diagnostics=[_catalog_diagnostic(error, candidate_value)],
        )
    except (ValidationError, ValueError) as error:
        diagnostic = _diagnostic(
            code=(
                "UNKNOWN_ACTION"
                if "UNKNOWN_ACTION" in str(error)
                else "INVALID_CANONICAL_WORKFLOW"
            ),
            message=str(error),
        )
        return _result(
            base_revision_id,
            candidate=None,
            diagnostics=[diagnostic],
        )
    return _result(
        base_revision_id,
        candidate=candidate,
        diagnostics=list(candidate["diagnostics"]),
    )


def safe_python_identifier(value: str, fallback: str = "workflow") -> str:
    """Create a readable identifier without executing or importing user code."""

    identifier = re.sub(r"\W+", "_", value).strip("_")
    if not identifier:
        identifier = fallback
    if identifier[0].isdigit():
        identifier = f"{fallback}_{identifier}"
    return identifier


__all__ = [
    "compile_authoring_revision",
    "safe_python_identifier",
    "validate_authoring_revision",
]
