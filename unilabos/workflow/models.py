"""Backend-shaped value objects for the local Workflow authority.

The models in this module are deliberately transport-independent.  They use
the frozen Backend field spelling, validate stable UUID identities at the
boundary, and never carry legacy Run identifiers.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

JsonObject = Dict[str, Any]
JsonArray = List[Any]


def validate_uuid(value: str) -> str:
    """Return the canonical spelling of one non-nil UUID."""

    parsed = UUID(str(value))
    if parsed.int == 0:
        raise ValueError("UUID must not be nil")
    return str(parsed)


class WorkflowNodeWrite(BaseModel):
    """Complete WorkflowNode payload used by full-graph reconciliation."""

    model_config = ConfigDict(extra="ignore")

    uuid: str
    workflow_node_template_uuid: Optional[str] = None
    parent_uuid: Optional[str] = None
    material_uuid: Optional[str] = None
    name: str
    status: str
    type: str
    icon: Optional[str] = None
    pose: JsonObject = Field(default_factory=dict)
    param: Optional[JsonObject] = None
    footer: Optional[str] = None
    action_name: Optional[str] = None
    action_type: Optional[str] = None
    execution_policy: JsonObject = Field(default_factory=dict)
    disabled: bool = False
    minimized: bool = False
    script: Optional[str] = None
    description: Optional[str] = None
    meta_data: JsonObject = Field(default_factory=dict)

    @field_validator(
        "uuid",
        "workflow_node_template_uuid",
        "parent_uuid",
        "material_uuid",
    )
    @classmethod
    def _valid_uuid(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else validate_uuid(value)

    @field_validator("name", "status", "type")
    @classmethod
    def _required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator(
        "icon",
        "footer",
        "action_name",
        "action_type",
        "script",
        "description",
    )
    @classmethod
    def _optional_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class WorkflowEdgeWrite(BaseModel):
    """Complete WorkflowEdge payload used by full-graph reconciliation."""

    model_config = ConfigDict(extra="ignore")

    uuid: str
    source_node_uuid: str
    target_node_uuid: str
    source_handle_uuid: str
    target_handle_uuid: str
    description: Optional[str] = None
    meta_data: JsonObject = Field(default_factory=dict)

    @field_validator(
        "uuid",
        "source_node_uuid",
        "target_node_uuid",
        "source_handle_uuid",
        "target_handle_uuid",
    )
    @classmethod
    def _valid_uuid(cls, value: str) -> str:
        return validate_uuid(value)

    @field_validator("description")
    @classmethod
    def _optional_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class CandidateCompilation(BaseModel):
    """One compiler result before the service issues a Candidate hash."""

    model_config = ConfigDict(extra="forbid")

    diagnostics: List[JsonObject] = Field(default_factory=list)
    graph: Optional[JsonObject] = None
    normalized_python_source: Optional[str] = None
    source_map: List[JsonObject] = Field(default_factory=list)
    changeset: Optional[JsonObject] = None
    compiler_version: str
    template_catalog_fingerprint: str

    @property
    def valid(self) -> bool:
        return (
            self.graph is not None
            and self.normalized_python_source is not None
            and self.changeset is not None
            and not any(
                str(item.get("severity", "")).lower() == "error"
                for item in self.diagnostics
            )
        )


__all__ = [
    "CandidateCompilation",
    "JsonArray",
    "JsonObject",
    "WorkflowEdgeWrite",
    "WorkflowNodeWrite",
    "validate_uuid",
]
