"""Backend-shaped Authoring Candidate bundle 的纯验证边界。"""

from __future__ import annotations

from typing import Any

from unilabos.workflow.graph_validation import validate_graph
from unilabos.workflow.json_codec import strict_json_equal
from unilabos.workflow.models import (
    CandidateChangeset,
    WorkflowEdgeWrite,
    WorkflowNodeWrite,
    normalize_json_array,
    normalize_json_object,
    validate_uuid,
)

_GRAPH_FIELDS = {
    "workflow",
    "nodes",
    "edges",
    "node_templates",
    "handle_templates",
}
_WORKFLOW_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "name",
    "tags",
    "revision",
    "description",
}
_WORKFLOW_REQUIRED_FIELDS = _WORKFLOW_FIELDS - {"description"}
_NODE_FIELDS = set(WorkflowNodeWrite.model_fields) | {
    "create_time",
    "update_time",
    "workflow_uuid",
}
_EDGE_FIELDS = set(WorkflowEdgeWrite.model_fields) | {
    "create_time",
    "update_time",
}
_NODE_TEMPLATE_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "resource_template_uuid",
    "name",
    "display_name",
    "goal",
    "goal_default",
    "feedback",
    "result",
    "type",
    "node_type",
    "description",
    "class",
    "schema",
    "icon",
    "header",
    "footer",
}
_NODE_TEMPLATE_REQUIRED_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "resource_template_uuid",
    "name",
    "display_name",
    "goal",
    "goal_default",
    "feedback",
    "result",
    "type",
    "node_type",
}
_HANDLE_TEMPLATE_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "workflow_node_template_uuid",
    "handle_key",
    "io_type",
    "display_name",
    "type",
    "required",
    "description",
    "data_source",
    "data_key",
}
_HANDLE_TEMPLATE_REQUIRED_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "workflow_node_template_uuid",
    "handle_key",
    "io_type",
    "display_name",
    "type",
    "required",
}


class CandidateBundleError(ValueError):
    """Engine 返回值不是可公开的 Backend-shaped Candidate bundle。"""


def _closed_entity(
    value: Any,
    *,
    allowed: set[str],
    required: set[str],
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not required.issubset(value)
        or not set(value).issubset(allowed)
    ):
        raise CandidateBundleError("Candidate entity is not a closed wire object")
    return value


def _required_text(entity: dict[str, Any], fields: set[str]) -> None:
    if any(not isinstance(entity[field], str) or not entity[field] for field in fields):
        raise CandidateBundleError("Candidate text field is invalid")


def _optional_text(entity: dict[str, Any], fields: set[str]) -> None:
    if any(
        field in entity
        and entity[field] is not None
        and not isinstance(entity[field], str)
        for field in fields
    ):
        raise CandidateBundleError("Candidate optional text field is invalid")


def _unique(values: list[str], *, label: str) -> None:
    if len(values) != len(set(values)):
        raise CandidateBundleError(f"Candidate {label} UUID is duplicated")


def _validate_workflow(
    value: Any,
    *,
    workflow_uuid: str,
    revision: int,
) -> dict[str, Any]:
    workflow = _closed_entity(
        value,
        allowed=_WORKFLOW_FIELDS,
        required=_WORKFLOW_REQUIRED_FIELDS,
    )
    if validate_uuid(workflow["uuid"]) != workflow_uuid:
        raise CandidateBundleError("Candidate Workflow UUID does not match request")
    if workflow["revision"] != revision or type(workflow["revision"]) is not int:
        raise CandidateBundleError("Candidate Workflow revision does not match request")
    _required_text(workflow, {"create_time", "update_time", "name"})
    _optional_text(workflow, {"description"})
    normalize_json_object(workflow["meta_data"])
    normalize_json_array(workflow["tags"])
    return workflow


def _validate_nodes(
    values: Any,
    *,
    workflow_uuid: str,
) -> tuple[list[WorkflowNodeWrite], dict[str, dict[str, Any]]]:
    if not isinstance(values, list):
        raise CandidateBundleError("Candidate nodes must be an array")
    models: list[WorkflowNodeWrite] = []
    by_uuid: dict[str, dict[str, Any]] = {}
    for item in values:
        entity = _closed_entity(
            item,
            allowed=_NODE_FIELDS,
            required=set(WorkflowNodeWrite.model_fields)
            - {
                "workflow_node_template_uuid",
                "parent_uuid",
                "material_uuid",
                "icon",
                "footer",
                "action_name",
                "action_type",
                "script",
                "description",
            },
        )
        if (
            "workflow_uuid" in entity
            and validate_uuid(entity["workflow_uuid"]) != workflow_uuid
        ):
            raise CandidateBundleError("Candidate Node belongs to another Workflow")
        for field in ("create_time", "update_time"):
            if field in entity and (
                not isinstance(entity[field], str) or not entity[field]
            ):
                raise CandidateBundleError("Candidate Node timestamp is invalid")
        model = WorkflowNodeWrite.model_validate(
            {
                key: entity[key]
                for key in WorkflowNodeWrite.model_fields
                if key in entity
            }
        )
        models.append(model)
        by_uuid[model.uuid] = entity
    _unique([model.uuid for model in models], label="Node")
    return models, by_uuid


def _validate_edges(
    values: Any,
) -> tuple[list[WorkflowEdgeWrite], dict[str, dict[str, Any]]]:
    if not isinstance(values, list):
        raise CandidateBundleError("Candidate edges must be an array")
    models: list[WorkflowEdgeWrite] = []
    by_uuid: dict[str, dict[str, Any]] = {}
    for item in values:
        entity = _closed_entity(
            item,
            allowed=_EDGE_FIELDS,
            required=set(WorkflowEdgeWrite.model_fields) - {"description"},
        )
        for field in ("create_time", "update_time"):
            if field in entity and (
                not isinstance(entity[field], str) or not entity[field]
            ):
                raise CandidateBundleError("Candidate Edge timestamp is invalid")
        model = WorkflowEdgeWrite.model_validate(
            {
                key: entity[key]
                for key in WorkflowEdgeWrite.model_fields
                if key in entity
            }
        )
        models.append(model)
        by_uuid[model.uuid] = entity
    _unique([model.uuid for model in models], label="Edge")
    return models, by_uuid


def _validate_node_templates(values: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(values, list):
        raise CandidateBundleError("Candidate node_templates must be an array")
    result: dict[str, dict[str, Any]] = {}
    for item in values:
        entity = _closed_entity(
            item,
            allowed=_NODE_TEMPLATE_FIELDS,
            required=_NODE_TEMPLATE_REQUIRED_FIELDS,
        )
        template_uuid = validate_uuid(entity["uuid"])
        validate_uuid(entity["resource_template_uuid"])
        _required_text(
            entity,
            {
                "create_time",
                "update_time",
                "name",
                "display_name",
                "type",
                "node_type",
            },
        )
        _optional_text(
            entity,
            {"description", "class", "schema", "icon", "header", "footer"},
        )
        for field in ("meta_data", "goal", "goal_default", "feedback", "result"):
            normalize_json_object(entity[field])
        result[template_uuid] = entity
    _unique(list(result), label="NodeTemplate")
    if len(result) != len(values):
        raise CandidateBundleError("Candidate NodeTemplate UUID is duplicated")
    return result


def _validate_handle_templates(values: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(values, list):
        raise CandidateBundleError("Candidate handle_templates must be an array")
    result: dict[str, dict[str, Any]] = {}
    for item in values:
        entity = _closed_entity(
            item,
            allowed=_HANDLE_TEMPLATE_FIELDS,
            required=_HANDLE_TEMPLATE_REQUIRED_FIELDS,
        )
        handle_uuid = validate_uuid(entity["uuid"])
        validate_uuid(entity["workflow_node_template_uuid"])
        _required_text(
            entity,
            {
                "create_time",
                "update_time",
                "handle_key",
                "io_type",
                "display_name",
                "type",
            },
        )
        _optional_text(entity, {"description", "data_source", "data_key"})
        if type(entity["required"]) is not bool:
            raise CandidateBundleError("Candidate Handle required must be boolean")
        normalize_json_object(entity["meta_data"])
        result[handle_uuid] = entity
    if len(result) != len(values):
        raise CandidateBundleError("Candidate HandleTemplate UUID is duplicated")
    return result


def _semantic_node(value: dict[str, Any]) -> dict[str, Any]:
    return WorkflowNodeWrite.model_validate(
        {key: value[key] for key in WorkflowNodeWrite.model_fields if key in value}
    ).model_dump()


def _semantic_edge(value: dict[str, Any]) -> dict[str, Any]:
    return WorkflowEdgeWrite.model_validate(
        {key: value[key] for key in WorkflowEdgeWrite.model_fields if key in value}
    ).model_dump()


def _changeset_expected(
    *,
    graph: dict[str, Any],
    base_graph: dict[str, Any],
) -> tuple[dict[str, set[str]], bool, bool]:
    candidate_nodes = {item["uuid"]: _semantic_node(item) for item in graph["nodes"]}
    base_nodes = {item["uuid"]: _semantic_node(item) for item in base_graph["nodes"]}
    candidate_edges = {item["uuid"]: _semantic_edge(item) for item in graph["edges"]}
    base_edges = {item["uuid"]: _semantic_edge(item) for item in base_graph["edges"]}
    expected = {
        "created_node_uuids": set(candidate_nodes) - set(base_nodes),
        "updated_node_uuids": {
            uuid
            for uuid in set(candidate_nodes) & set(base_nodes)
            if not strict_json_equal(candidate_nodes[uuid], base_nodes[uuid])
        },
        "deleted_node_uuids": set(base_nodes) - set(candidate_nodes),
        "created_edge_uuids": set(candidate_edges) - set(base_edges),
        "updated_edge_uuids": {
            uuid
            for uuid in set(candidate_edges) & set(base_edges)
            if not strict_json_equal(candidate_edges[uuid], base_edges[uuid])
        },
        "deleted_edge_uuids": set(base_edges) - set(candidate_edges),
    }
    candidate_unilab = (graph["workflow"].get("meta_data") or {}).get("unilab")
    base_unilab = (base_graph["workflow"].get("meta_data") or {}).get("unilab")
    reserved_changed = not strict_json_equal(candidate_unilab, base_unilab)
    workflow_changed = any(
        not strict_json_equal(
            graph["workflow"].get(field),
            base_graph["workflow"].get(field),
        )
        for field in ("name", "description")
    )
    return expected, reserved_changed, workflow_changed


def validate_candidate_bundle(
    *,
    graph: Any,
    base_graph: Any,
    workflow_uuid: str,
    revision: int,
    source_map: list[dict[str, Any]],
    changeset: dict[str, Any],
    require_unchanged_graph: bool,
) -> dict[str, Any]:
    """验证成功 transform 的完整公开 graph/source-map/changeset 关系。"""

    workflow_uuid = validate_uuid(workflow_uuid)
    if type(revision) is not int or revision < 1:
        raise CandidateBundleError("request revision is invalid")
    candidate = _closed_entity(
        graph,
        allowed=_GRAPH_FIELDS,
        required=_GRAPH_FIELDS,
    )
    base = _closed_entity(
        base_graph,
        allowed=_GRAPH_FIELDS,
        required=_GRAPH_FIELDS,
    )
    workflow = _validate_workflow(
        candidate["workflow"],
        workflow_uuid=workflow_uuid,
        revision=revision,
    )
    _validate_workflow(
        base["workflow"],
        workflow_uuid=workflow_uuid,
        revision=revision,
    )
    nodes, nodes_by_uuid = _validate_nodes(
        candidate["nodes"],
        workflow_uuid=workflow_uuid,
    )
    edges, _edges_by_uuid = _validate_edges(candidate["edges"])
    templates = _validate_node_templates(candidate["node_templates"])
    handles = _validate_handle_templates(candidate["handle_templates"])
    _validate_nodes(base["nodes"], workflow_uuid=workflow_uuid)
    _validate_edges(base["edges"])

    referenced_templates = {
        node.workflow_node_template_uuid
        for node in nodes
        if node.workflow_node_template_uuid is not None
    }
    if set(templates) != referenced_templates:
        raise CandidateBundleError("Candidate Catalog projection is not minimal")
    if any(
        handle["workflow_node_template_uuid"] not in templates
        for handle in handles.values()
    ):
        raise CandidateBundleError("Candidate Handle parent is outside projection")

    validate_graph(
        nodes=nodes,
        edges=edges,
        templates=templates,
        handles=handles,
        effective_params={node.uuid: node.param or {} for node in nodes},
        workflow_meta_data=workflow["meta_data"],
        node_meta_data={node.uuid: node.meta_data for node in nodes},
    )
    if any(item["workflow_node_uuid"] not in nodes_by_uuid for item in source_map):
        raise CandidateBundleError("Source map references a Node outside Candidate")

    normalized_changeset = CandidateChangeset.model_validate(changeset).model_dump()
    lifecycle_fields = (
        "created_node_uuids",
        "updated_node_uuids",
        "deleted_node_uuids",
        "created_edge_uuids",
        "updated_edge_uuids",
        "deleted_edge_uuids",
    )
    lifecycle = [normalized_changeset[field] for field in lifecycle_fields]
    if any(len(items) != len(set(items)) for items in lifecycle):
        raise CandidateBundleError("Changeset contains duplicate UUIDs")
    if any(
        set(lifecycle[left]) & set(lifecycle[right])
        for left in range(len(lifecycle))
        for right in range(left + 1, len(lifecycle))
    ):
        raise CandidateBundleError("Changeset lifecycle UUID sets overlap")

    expected, reserved_changed, workflow_changed = _changeset_expected(
        graph=candidate,
        base_graph=base,
    )
    if any(set(normalized_changeset[field]) != expected[field] for field in expected):
        raise CandidateBundleError("Changeset does not describe Candidate graph")
    if normalized_changeset["reserved_metadata_changed"] is not reserved_changed:
        raise CandidateBundleError("Changeset reserved metadata flag is inaccurate")
    graph_changed = reserved_changed or workflow_changed or any(expected.values())
    expected_kind = "graph" if graph_changed else "source_only"
    if normalized_changeset["kind"] != expected_kind:
        raise CandidateBundleError("Changeset kind does not match graph semantics")
    if require_unchanged_graph and not strict_json_equal(candidate, base):
        raise CandidateBundleError("Source-only transform changed Candidate graph")
    return candidate


__all__ = ["CandidateBundleError", "validate_candidate_bundle"]
