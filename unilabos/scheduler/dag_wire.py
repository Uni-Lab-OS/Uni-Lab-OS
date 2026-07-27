"""Stable wire serialization for the internal TaskDag execution IR."""

from __future__ import annotations

from typing import Any

from unilabos.scheduler.dag_model import TaskDag


def serialize_task_dag(dag: TaskDag) -> dict[str, Any]:
    """Serialize a TaskDag without exposing compilation to API clients."""

    nodes: list[dict[str, Any]] = []
    for node in dag.nodes.values():
        payload: dict[str, Any] = {
            "node_id": node.node_id,
            "device_id": node.device_id,
            "action": node.action,
            "action_type": node.action_type,
            "action_args": dict(node.action_args),
            "sample_material": dict(node.sample_material),
            "always_free": node.always_free,
        }
        has_v2_contract = bool(
            dag.workflow_revision_hash
            or node.input_bindings
            or node.input_schema
            or node.output_schema
            or node.material_bindings
            or node.resource_claims
            or node.resource_releases
            or node.resource_dependencies
            or node.effects
            or node.control
            or node.cleanup_for
            or node.source_node_id
            or node.origin_edge_ids
            or node.idempotency_key
            or node.node_type != "action"
            or node.estimated_duration_s
        )
        if has_v2_contract:
            payload.update(
                {
                    "node_type": node.node_type,
                    "estimated_duration_s": node.estimated_duration_s,
                    "input_bindings": dict(node.input_bindings),
                    "input_schema": dict(node.input_schema),
                    "output_schema": dict(node.output_schema),
                    "material_bindings": dict(node.material_bindings),
                    "resource_claims": list(node.resource_claims),
                    "resource_releases": list(node.resource_releases),
                    "resource_dependencies": list(node.resource_dependencies),
                    "effects": list(node.effects),
                    "control": dict(node.control),
                    "cleanup_for": list(node.cleanup_for),
                    "source_node_id": node.source_node_id,
                    "origin_edge_ids": list(node.origin_edge_ids),
                    "idempotency_key": node.idempotency_key,
                    "canonical_index": node.canonical_index,
                }
            )
        nodes.append(payload)

    serialized: dict[str, Any] = {
        "task_id": dag.task_id,
        "notebook_id": dag.notebook_id,
        "server_info": dict(dag.server_info),
        "nodes": nodes,
        "edges": [
            {
                "source_node_uuid": edge.source_node_uuid,
                "target_node_uuid": edge.target_node_uuid,
                **({"branch": edge.branch} if edge.branch is not None else {}),
                **({"activates": False} if not edge.activates else {}),
            }
            for edge in dag.edges
        ],
    }
    if dag.workflow_revision_hash:
        serialized["workflow_revision_hash"] = dag.workflow_revision_hash
    if dag.runtime_parameters:
        serialized["runtime_parameters"] = dict(dag.runtime_parameters)
    if dag.debug:
        serialized["debug"] = dict(dag.debug)
    return serialized
