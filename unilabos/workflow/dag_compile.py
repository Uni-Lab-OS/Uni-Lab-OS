"""Canonical WorkflowRevision to the executable TaskDag v2 boundary."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

from unilabos.scheduler.dag_model import TaskDag

from .bindings import binding_node_dependencies
from .canonical import (
    WorkflowRevision,
    execution_node_identity,
    revalidate_workflow_revision,
)


class WorkflowCompileError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_BUILTIN_ACTIONS: dict[str, dict[str, Any]] = {
    "os_control.branch": {
        "inputs": {"condition": {"type": "boolean", "required": True}},
        "outputs": {"branch": {"type": "string", "required": True}},
    },
    "os_control.join": {"inputs": {}, "outputs": {}},
    "host_node.manual_confirm": {
        "inputs": {
            "prompt": {"type": "string", "default": ""},
            "timeout_seconds": {"type": "integer", "default": 3600},
            "assignee_user_ids": {"type": "array", "default": []},
        },
        "outputs": {},
    },
}


def _with_builtin_actions(
    action_catalog: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Mapping[str, Any]]:
    catalog: dict[str, Mapping[str, Any]] = dict(_BUILTIN_ACTIONS)
    catalog.update(action_catalog or {})
    return catalog


def _binding_payload(binding: Any) -> dict[str, Any]:
    return binding.model_dump(mode="json", exclude_none=True)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json", exclude_none=True)
        if isinstance(dumped, Mapping):
            return dumped
    return {}


def _authoritative_contract_fields(
    action_info: Mapping[str, Any],
) -> dict[str, Any]:
    contract = _as_mapping(action_info.get("contract", {}))
    timing = _as_mapping(contract.get("timing", action_info.get("timing", {})))
    return {
        "output_schema": dict(action_info.get("outputs") or {}),
        "resource_claims": list(
            contract.get(
                "resource_claims",
                action_info.get("resource_claims", []),
            )
            or []
        ),
        "effects": list(
            contract.get("effects", action_info.get("effects", [])) or []
        ),
        "estimated_duration_s": float(
            timing.get("estimated_duration_s", 0) or 0
        ),
    }


def materialize_action_contracts(
    revision: WorkflowRevision,
    *,
    action_catalog: Mapping[str, Mapping[str, Any]] | None = None,
) -> WorkflowRevision:
    """Return Canonical content with registry-owned action contracts installed."""

    try:
        revision = revalidate_workflow_revision(revision)
    except ValueError as exc:
        raise WorkflowCompileError("INVALID_CANONICAL", str(exc)) from exc

    catalog = _with_builtin_actions(action_catalog)
    sanitized_invocations = []
    for invocation in revision.invocations:
        action_info = _as_mapping(catalog.get(invocation.action_ref, {}))
        if not action_info:
            raise WorkflowCompileError(
                "ACTION_NOT_FOUND",
                f"action is not registered: {invocation.action_ref}",
            )
        sanitized_invocations.append(
            invocation.model_copy(
                update=_authoritative_contract_fields(action_info)
            )
        )
    materialized = revision.model_copy(update={"invocations": sanitized_invocations})
    try:
        return revalidate_workflow_revision(materialized)
    except ValueError as exc:
        raise WorkflowCompileError("INVALID_ACTION_CONTRACT", str(exc)) from exc


def _dependency_adjacency(
    revision: WorkflowRevision,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    forward = {item.node_id: set() for item in revision.invocations}
    reverse = {item.node_id: set() for item in revision.invocations}
    dependencies = {
        (edge.source, edge.target)
        for edge in [
            *revision.control_edges,
            *revision.data_edges,
            *revision.material_edges,
            *revision.constraint_edges,
        ]
    }
    for invocation in revision.invocations:
        for binding in invocation.input_bindings.values():
            dependencies.update(
                (source_node_id, invocation.node_id)
                for source_node_id in binding_node_dependencies(binding)
            )
    for source, target in dependencies:
        forward[source].add(target)
        reverse[target].add(source)
    return forward, reverse


def _reachable(start: str, adjacency: Mapping[str, set[str]]) -> set[str]:
    pending = [start]
    visited: set[str] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        pending.extend(adjacency[current] - visited)
    return visited


def compile_workflow_revision(
    revision: WorkflowRevision,
    *,
    task_id: str,
    action_catalog: Mapping[str, Mapping[str, Any]] | None = None,
    runtime_parameters: Mapping[str, Any] | None = None,
) -> TaskDag:
    """Lower one validated Canonical revision without installing a schedule."""

    catalog = _with_builtin_actions(action_catalog)
    if revision.constraint_edges:
        raise WorkflowCompileError(
            "SCHEDULING_NOT_INSTALLED",
            "constraint edges require the optional scheduling layer",
        )
    if revision.material_edges:
        raise WorkflowCompileError(
            "TRANSPORT_PLANNING_NOT_INSTALLED",
            "material edges require explicit transport or a transport compiler",
        )
    revision = materialize_action_contracts(
        revision,
        action_catalog=catalog,
    )
    workflow_revision_hash = revision.content_hash

    invocations_by_id = {
        invocation.node_id: invocation for invocation in revision.invocations
    }
    release_directives: dict[str, list[dict[str, str]]] = {}
    dependency_directives: dict[str, list[dict[str, str]]] = {}
    forward, reverse = _dependency_adjacency(revision)
    for hold in revision.resource_holds:
        acquire = invocations_by_id[hold.acquire_node_id]
        matching_claims = [
            claim
            for claim in acquire.resource_claims
            if str(claim.get("resource_ref") or "") == hold.resource_ref
        ]
        if len(matching_claims) != 1:
            raise WorkflowCompileError(
                "INVALID_RESOURCE_HOLD",
                "resource hold must reference exactly one authoritative acquire "
                f"claim: {hold.hold_id} -> {hold.resource_ref}",
            )
        updated_claims = [
            {
                **claim,
                **(
                    {"scope": hold.scope}
                    if str(claim.get("resource_ref") or "") == hold.resource_ref
                    else {}
                ),
            }
            for claim in acquire.resource_claims
        ]
        invocations_by_id[hold.acquire_node_id] = acquire.model_copy(
            update={"resource_claims": updated_claims}
        )
        interval = _reachable(hold.acquire_node_id, forward) & _reachable(
            hold.release_node_id,
            reverse,
        )
        for node_id in interval - {hold.acquire_node_id}:
            invocation = invocations_by_id[node_id]
            filtered_claims = [
                claim
                for claim in invocation.resource_claims
                if str(claim.get("resource_ref") or "") != hold.resource_ref
            ]
            if len(filtered_claims) == len(invocation.resource_claims):
                continue
            invocations_by_id[node_id] = invocation.model_copy(
                update={"resource_claims": filtered_claims}
            )
            dependency_directives.setdefault(node_id, []).append(
                {
                    "hold_id": hold.hold_id,
                    "acquire_node_id": hold.acquire_node_id,
                    "resource_ref": hold.resource_ref,
                    "scope": hold.scope,
                }
            )
        release_directives.setdefault(hold.release_node_id, []).append(
            {
                "hold_id": hold.hold_id,
                "acquire_node_id": hold.acquire_node_id,
                "resource_ref": hold.resource_ref,
                "scope": hold.scope,
            }
        )
    revision = revision.model_copy(
        update={
            "invocations": [
                invocations_by_id[item.node_id] for item in revision.invocations
            ]
        }
    )

    nodes: list[dict[str, Any]] = []
    edges: dict[tuple[str, str], dict[str, Any]] = {}
    for index, invocation in enumerate(revision.invocations):
        owner, separator, action_name = invocation.action_ref.rpartition(".")
        if not separator:
            raise WorkflowCompileError(
                "INVALID_ACTION_REF",
                f"action_ref must use device.action syntax: {invocation.action_ref}",
            )
        action_info = _as_mapping(catalog.get(invocation.action_ref, {}))
        input_schema = action_info.get("inputs", {})
        input_schema = input_schema if isinstance(input_schema, Mapping) else {}
        unknown_inputs = sorted(set(invocation.input_bindings) - set(input_schema))
        if unknown_inputs:
            raise WorkflowCompileError(
                "UNKNOWN_INPUT",
                f"action {invocation.action_ref!r} has no input {unknown_inputs[0]!r}",
            )
        missing_required = sorted(
            name
            for name, raw_schema in input_schema.items()
            if isinstance(raw_schema, Mapping)
            and raw_schema.get("required") is True
            and "default" not in raw_schema
            and name not in invocation.input_bindings
        )
        if missing_required:
            raise WorkflowCompileError(
                "MISSING_REQUIRED_INPUT",
                f"action {invocation.action_ref!r} requires input {missing_required[0]!r}",
            )
        output_schema = invocation.output_schema
        contract = _as_mapping(action_info.get("contract", {}))
        timing = _as_mapping(contract.get("timing", action_info.get("timing", {})))
        resource_claims = invocation.resource_claims
        effects = invocation.effects
        duration = invocation.estimated_duration_s or float(
            timing.get("estimated_duration_s", 0) or 0
        )
        nodes.append(
            {
                "node_id": invocation.node_id,
                "device_id": owner,
                "action": action_name,
                "node_type": invocation.node_type,
                "input_bindings": {
                    name: _binding_payload(binding)
                    for name, binding in invocation.input_bindings.items()
                },
                "input_schema": dict(input_schema),
                "output_schema": dict(output_schema)
                if isinstance(output_schema, Mapping)
                else {},
                "material_bindings": dict(invocation.material_bindings),
                "resource_claims": list(resource_claims or []),
                "resource_releases": list(
                    release_directives.get(invocation.node_id, [])
                ),
                "resource_dependencies": list(
                    dependency_directives.get(invocation.node_id, [])
                ),
                "effects": list(effects or []),
                "control": dict(invocation.control),
                "cleanup_for": list(invocation.cleanup_for),
                "estimated_duration_s": duration,
                "source_node_id": invocation.node_id,
                "canonical_index": index,
                "idempotency_key": hashlib.sha256(
                    f"{workflow_revision_hash}:{execution_node_identity(index)}".encode(
                        "utf-8"
                    )
                ).hexdigest(),
            }
        )
        for binding in invocation.input_bindings.values():
            for source_node_id in binding_node_dependencies(binding):
                edges[(source_node_id, invocation.node_id)] = {
                    "source_node_uuid": source_node_id,
                    "target_node_uuid": invocation.node_id,
                    "activates": False,
                }
    for edge in revision.control_edges:
        payload = {
            "source_node_uuid": edge.source,
            "target_node_uuid": edge.target,
        }
        if edge.branch is not None:
            payload["branch"] = edge.branch
        edges[(edge.source, edge.target)] = payload
    for edge in [
        *revision.data_edges,
        *revision.material_edges,
        *revision.constraint_edges,
    ]:
        edges.setdefault(
            (edge.source, edge.target),
            {
                "source_node_uuid": edge.source,
                "target_node_uuid": edge.target,
                "activates": False,
            },
        )
    return TaskDag.from_message(
        {
            "task_id": task_id,
            "workflow_revision_hash": workflow_revision_hash,
            "runtime_parameters": dict(runtime_parameters or {}),
            "nodes": nodes,
            "edges": list(edges.values()),
        }
    )
