"""Generic Runtime workflow submission to Canonical WorkflowRevision.

This is the only server-side lowering boundary used by the local Runtime API.
Clients submit authoring/source fields; action contracts remain authoritative
inside the OS action catalog.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from pydantic import ValidationError

from unilabos.scheduler.dag_model import DagValidationError, TaskDag

from .canonical import ActionInvocation, ControlEdge, WorkflowRevision
from .dag_compile import WorkflowCompileError, compile_workflow_revision


def _first_present(value: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if value.get(key) is not None:
            return value[key]
    return None


def _literal_bindings(values: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(name): {"kind": "literal", "value": value}
        for name, value in values.items()
    }


def workflow_submission_to_revision(
    workflow: Mapping[str, Any],
) -> WorkflowRevision:
    """Normalize a generic UI/source workflow into the Canonical authority."""

    raw_nodes = workflow.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise DagValidationError("缺少非空 nodes 列表")
    raw_edges = workflow.get("edges") or []
    if not isinstance(raw_edges, list):
        raise DagValidationError("edges 必须是列表")

    invocations: list[ActionInvocation] = []
    for raw in raw_nodes:
        if not isinstance(raw, Mapping):
            raise DagValidationError("节点必须是对象")
        inner_value = raw.get("data")
        inner = inner_value if isinstance(inner_value, Mapping) else {}
        node_id = _first_present(raw, "node_id", "id")
        device_id = _first_present(raw, "device_id", "deviceId") or _first_present(
            inner,
            "device_id",
            "deviceId",
        )
        action = _first_present(raw, "action", "method") or _first_present(
            inner,
            "action",
            "method",
        )
        action_ref = raw.get("action_ref") or inner.get("action_ref")
        if not action_ref and device_id and action:
            action_ref = f"{device_id}.{action}"
        if not node_id or not action_ref:
            raise DagValidationError(
                "节点缺少 node_id 或 device_id/action（或 action_ref）"
            )

        bindings = raw.get("input_bindings") or inner.get("input_bindings")
        if bindings is None:
            params = (
                _first_present(raw, "action_args", "params")
                or _first_present(inner, "action_args", "params")
                or {}
            )
            bindings = _literal_bindings(params) if isinstance(params, Mapping) else {}
        if not isinstance(bindings, Mapping):
            raise DagValidationError(f"节点 {node_id} input_bindings 必须是对象")

        invocations.append(
            ActionInvocation.model_validate(
                {
                    "node_id": str(node_id),
                    "action_ref": str(action_ref),
                    "node_type": str(
                        raw.get("node_type")
                        or inner.get("node_type")
                        or "action"
                    ),
                    "name": str(raw.get("name") or inner.get("label") or ""),
                    "description": str(raw.get("description") or ""),
                    "input_bindings": dict(bindings),
                    "output_schema": dict(
                        raw.get("output_schema")
                        or inner.get("output_schema")
                        or {}
                    ),
                    "material_bindings": dict(
                        raw.get("material_bindings")
                        or inner.get("material_bindings")
                        or {}
                    ),
                    # These remain source hints only.  When an action exists in
                    # the catalog the compiler replaces them with its contract.
                    "resource_claims": list(
                        raw.get("resource_claims")
                        or inner.get("resource_claims")
                        or []
                    ),
                    "effects": list(raw.get("effects") or inner.get("effects") or []),
                    "estimated_duration_s": float(
                        raw.get("estimated_duration_s")
                        or inner.get("estimated_duration_s")
                        or 0
                    ),
                }
            )
        )

    edges: list[ControlEdge] = []
    for index, raw in enumerate(raw_edges):
        if not isinstance(raw, Mapping):
            raise DagValidationError("边必须是对象")
        source = _first_present(raw, "source_node_uuid", "source")
        target = _first_present(raw, "target_node_uuid", "target")
        if not source or not target:
            raise DagValidationError("边缺少 source/target")
        edges.append(
            ControlEdge(
                source=str(source),
                target=str(target),
                edge_id=str(raw.get("edge_id") or raw.get("id") or f"edge-{index}"),
                branch=(
                    str(raw["branch"])
                    if raw.get("branch") is not None
                    else None
                ),
            )
        )

    name = str(workflow.get("name") or workflow.get("workflow_id") or "workflow")
    source_json = json.dumps(
        {"name": name, "nodes": raw_nodes, "edges": raw_edges},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    source_hash = hashlib.sha256(source_json.encode("utf-8")).hexdigest()
    try:
        return WorkflowRevision(
            revision_id=f"submission-{source_hash[:16]}",
            workflow_id=str(workflow.get("workflow_id") or name),
            invocations=invocations,
            control_edges=edges,
        )
    except ValidationError as exc:
        raise DagValidationError(str(exc)) from exc


def compile_workflow_submission(
    workflow: Mapping[str, Any],
    *,
    task_id: str,
    action_catalog: Mapping[str, Mapping[str, Any]] | None = None,
    runtime_parameters: Mapping[str, Any] | None = None,
) -> TaskDag:
    """Validate source, build Canonical revision, and compile one TaskDag."""

    revision = workflow_submission_to_revision(workflow)
    try:
        return compile_workflow_revision(
            revision,
            task_id=task_id,
            action_catalog=action_catalog,
            runtime_parameters=runtime_parameters,
        )
    except WorkflowCompileError as exc:
        raise DagValidationError(f"{exc.code}: {exc}") from exc
