"""Canonical, versioned workflow representation shared by code and visual UI."""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import rfc8785

from .bindings import (
    Binding,
    NodeOutputRef,
    binding_node_dependencies,
)


class WorkflowParameter(BaseModel):
    """One ordered, externally bindable workflow parameter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    type: Literal["string", "integer", "number", "boolean"]
    required: bool
    default: Any = None
    title: str = ""
    description: str = ""

    @model_validator(mode="after")
    def validate_contract(self) -> "WorkflowParameter":
        if not self.name.isidentifier():
            raise ValueError(
                f"workflow parameter name is not an identifier: {self.name}"
            )
        has_default = "default" in self.model_fields_set
        if self.required and has_default:
            raise ValueError(
                "INVALID_WORKFLOW_PARAMETER_DEFAULT: required parameter has a default"
            )
        if not self.required and not has_default:
            raise ValueError(
                "INVALID_WORKFLOW_PARAMETER_DEFAULT: optional parameter requires a default"
            )
        if has_default and not _matches_parameter_type(self.default, self.type):
            raise ValueError(
                f"INVALID_WORKFLOW_PARAMETER_DEFAULT: {self.name} expects {self.type}"
            )
        return self


def _matches_parameter_type(value: Any, expected: str) -> bool:
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    return False


class ActionInvocation(BaseModel):
    """One invocation of a registry action in a workflow revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str
    action_ref: str
    node_type: str = "action"
    name: str = ""
    description: str = ""
    input_bindings: Dict[str, Binding] = Field(default_factory=dict)
    output_schema: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    material_bindings: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    resource_claims: List[Dict[str, Any]] = Field(default_factory=list)
    effects: List[Dict[str, Any]] = Field(default_factory=list)
    estimated_duration_s: float = Field(default=0, ge=0)
    control: Dict[str, Any] = Field(default_factory=dict)
    cleanup_for: List[str] = Field(default_factory=list)


class ControlEdge(BaseModel):
    """An ordering dependency independent of data bindings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    target: str
    edge_id: str = ""
    branch: str | None = None


class DataEdge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    edge_id: str
    source: str
    source_output: str
    target: str
    target_input: str


class MaterialEdge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    edge_id: str
    source: str
    source_port: str
    target: str
    target_port: str
    material_ref: str


class ConstraintEdge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    edge_id: str
    source: str
    target: str
    constraint_type: str
    parameters: Dict[str, Any] = Field(default_factory=dict)


class ResourceHold(BaseModel):
    """A contract-authorized lease retained across Canonical nodes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hold_id: str = Field(min_length=1)
    resource_ref: str = Field(min_length=1)
    scope: Literal["until_handoff", "workflow_block"]
    acquire_node_id: str = Field(min_length=1)
    release_node_id: str = Field(min_length=1)


class WorkflowDefinition(BaseModel):
    """Stable identity and author-facing metadata shared by its revisions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_id: str
    name: str
    description: str = ""
    tags: List[str] = Field(default_factory=list)


class SourceMapEntry(BaseModel):
    """Trace one compiled invocation back to its Python source span."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str = ""
    line: int = 0
    column: int = 0
    source_step_index: int | None = None
    compiled_node_ids: List[str] = Field(default_factory=list)


class SourceMap(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: List[SourceMapEntry] = Field(default_factory=list)


class WorkflowSourceArtifact(BaseModel):
    """Authoring source kept for audit and editing, never execution hashing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    format: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=2 * 1024 * 1024)
    uri: str = Field(min_length=1, max_length=1024)
    content_hash: str = Field(min_length=1, max_length=80)

    @field_validator("uri")
    @classmethod
    def validate_uri(cls, value: str) -> str:
        if (
            "\x00" in value
            or "\\" in value
            or "://" in value
            or re.match(r"^[A-Za-z]:/", value) is not None
        ):
            raise ValueError("source artifact uri must be a safe relative path")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("source artifact uri must not use path traversal")
        return value

    @model_validator(mode="after")
    def validate_content_hash(self) -> "WorkflowSourceArtifact":
        encoded = self.text.encode("utf-8")
        git_blob = hashlib.sha1(
            f"blob {len(encoded)}\0".encode() + encoded,
            usedforsecurity=False,
        ).hexdigest()
        sha256 = hashlib.sha256(encoded).hexdigest()
        supported = {
            git_blob,
            f"git-blob:{git_blob}",
            f"sha256:{sha256}",
        }
        if self.content_hash not in supported:
            raise ValueError(
                "source artifact content_hash must be a matching Git blob or sha256 hash"
            )
        return self


class WorkflowRevision(BaseModel):
    """Immutable execution content plus mutable, hash-excluded editor layout."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["2"] = "2"
    revision_id: str
    workflow_id: str
    parameters: List[WorkflowParameter] | None = None
    invocations: List[ActionInvocation]
    control_edges: List[ControlEdge] = Field(default_factory=list)
    data_edges: List[DataEdge] = Field(default_factory=list)
    material_edges: List[MaterialEdge] = Field(default_factory=list)
    constraint_edges: List[ConstraintEdge] = Field(default_factory=list)
    resource_holds: List[ResourceHold] = Field(default_factory=list)
    layout: Dict[str, Any] = Field(default_factory=dict)
    source_map: SourceMap = Field(default_factory=SourceMap)
    source_artifact: WorkflowSourceArtifact | None = None

    @model_validator(mode="after")
    def validate_graph(self) -> "WorkflowRevision":
        if self.parameters is not None:
            parameter_names = [parameter.name for parameter in self.parameters]
            if len(parameter_names) != len(set(parameter_names)):
                raise ValueError(
                    "workflow parameter names must be unique; duplicate found"
                )
        node_ids = [node.node_id for node in self.invocations]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("Canonical workflow has duplicate node_id")
        known = set(node_ids)
        invocations_by_id = {
            invocation.node_id: invocation for invocation in self.invocations
        }
        dependencies: set[tuple[str, str]] = set()
        for edge in [
            *self.control_edges,
            *self.data_edges,
            *self.material_edges,
            *self.constraint_edges,
        ]:
            if edge.source not in known or edge.target not in known:
                raise ValueError(
                    f"Canonical edge references unknown node: {edge.source}->{edge.target}"
                )
            dependencies.add((edge.source, edge.target))
        data_targets: set[tuple[str, str]] = set()
        for edge in self.data_edges:
            target_key = (edge.target, edge.target_input)
            if target_key in data_targets:
                raise ValueError(
                    "Canonical data edges repeat target input: "
                    f"{edge.target}.{edge.target_input}"
                )
            data_targets.add(target_key)
            binding = invocations_by_id[edge.target].input_bindings.get(
                edge.target_input
            )
            if not isinstance(binding, NodeOutputRef) or (
                binding.node_id != edge.source or binding.output != edge.source_output
            ):
                raise ValueError(
                    "Canonical data edge contradicts target input binding: "
                    f"{edge.source}.{edge.source_output} -> "
                    f"{edge.target}.{edge.target_input}"
                )
        for invocation in self.invocations:
            for binding in invocation.input_bindings.values():
                for source_node_id in binding_node_dependencies(binding):
                    if source_node_id not in known:
                        raise ValueError(
                            f"binding references unknown node: {source_node_id}"
                        )
                    dependencies.add((source_node_id, invocation.node_id))
            for protected_node_id in invocation.cleanup_for:
                if protected_node_id not in known:
                    raise ValueError(
                        "cleanup references unknown protected node: "
                        f"{protected_node_id}"
                    )
        hold_ids: set[str] = set()
        for hold in self.resource_holds:
            if hold.hold_id in hold_ids:
                raise ValueError(f"duplicate resource hold: {hold.hold_id}")
            hold_ids.add(hold.hold_id)
            if hold.acquire_node_id not in known or hold.release_node_id not in known:
                raise ValueError(
                    "resource hold references unknown acquire/release node: "
                    f"{hold.acquire_node_id}->{hold.release_node_id}"
                )
        indegree = {node_id: 0 for node_id in node_ids}
        adjacency = {node_id: [] for node_id in node_ids}
        for source, target in dependencies:
            indegree[target] += 1
            adjacency[source].append(target)
        ready = [node_id for node_id in node_ids if indegree[node_id] == 0]
        processed = 0
        while ready:
            node_id = ready.pop()
            processed += 1
            for target in adjacency[node_id]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
        if processed != len(node_ids):
            raise ValueError("Canonical workflow dependency graph contains a cycle")
        adjacency_sets = {
            node_id: set(targets) for node_id, targets in adjacency.items()
        }
        for hold in self.resource_holds:
            if hold.acquire_node_id == hold.release_node_id:
                continue
            pending = [hold.acquire_node_id]
            visited: set[str] = set()
            while pending:
                current = pending.pop()
                if current in visited:
                    continue
                visited.add(current)
                pending.extend(adjacency_sets[current] - visited)
            if hold.release_node_id not in visited:
                raise ValueError(
                    "resource hold release must be downstream of acquire: "
                    f"{hold.acquire_node_id}->{hold.release_node_id}"
                )
        return self

    @property
    def content_hash(self) -> str:
        execution_payload = self.model_dump(
            mode="json",
            exclude={"revision_id", "layout", "source_map", "source_artifact"},
            exclude_none=True,
        )
        for parameter in execution_payload.get("parameters") or []:
            parameter.pop("title", None)
            parameter.pop("description", None)
        for invocation in execution_payload.get("invocations") or []:
            invocation.pop("name", None)
            invocation.pop("description", None)
        _normalize_execution_node_ids(execution_payload)
        try:
            canonical_json = rfc8785.dumps(execution_payload)
        except rfc8785.CanonicalizationError as exc:
            raise ValueError(
                "Canonical workflow contains a value outside the RFC 8785 JSON domain"
            ) from exc
        return hashlib.sha256(canonical_json).hexdigest()


def revalidate_workflow_revision(revision: WorkflowRevision) -> WorkflowRevision:
    """Re-run deep validation at compile, persistence, and projection boundaries."""

    payload = revision.model_dump(mode="python")
    for index, parameter in enumerate(revision.parameters or []):
        if "default" not in parameter.model_fields_set:
            payload["parameters"][index].pop("default", None)
    return WorkflowRevision.model_validate(payload)


def _normalize_execution_node_ids(execution_payload: Dict[str, Any]) -> None:
    """Make execution hashes independent of authoring-only node labels.

    Node order and every graph/reference relationship remain part of the hash.
    Only the spelling inherited from a source format (for example an inlined
    YAML sub-workflow prefix) is replaced by a stable ordinal identity.
    """

    invocations = execution_payload.get("invocations") or []
    node_ids = {
        invocation["node_id"]: execution_node_identity(index)
        for index, invocation in enumerate(invocations)
    }

    def remap_binding(binding: Any) -> None:
        if not isinstance(binding, dict):
            return
        kind = binding.get("kind")
        if kind in {"node_output", "node_result"}:
            binding["node_id"] = node_ids[binding["node_id"]]
        elif kind == "expression":
            for variable in binding.get("variables", {}).values():
                remap_binding(variable)
        elif kind == "conditional":
            binding["branch_node_id"] = node_ids[binding["branch_node_id"]]
            remap_binding(binding.get("true_value"))
            remap_binding(binding.get("false_value"))

    for invocation in invocations:
        original_node_id = invocation["node_id"]
        invocation["node_id"] = node_ids[original_node_id]
        invocation["cleanup_for"] = [
            node_ids[node_id] for node_id in invocation.get("cleanup_for", [])
        ]
        for binding in invocation.get("input_bindings", {}).values():
            remap_binding(binding)

    for edge_kind in (
        "control_edges",
        "data_edges",
        "material_edges",
        "constraint_edges",
    ):
        for edge in execution_payload.get(edge_kind, []):
            edge.pop("edge_id", None)
            edge["source"] = node_ids[edge["source"]]
            edge["target"] = node_ids[edge["target"]]

    for hold in execution_payload.get("resource_holds", []):
        hold["acquire_node_id"] = node_ids[hold["acquire_node_id"]]
        hold["release_node_id"] = node_ids[hold["release_node_id"]]


def execution_node_identity(index: int) -> str:
    """Return the source-format-independent identity for one Canonical ordinal."""

    return f"node-{index:06d}"
