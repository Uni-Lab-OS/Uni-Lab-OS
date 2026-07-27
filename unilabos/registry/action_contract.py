"""Versioned execution contract for actions exposed to the OS DAG runtime.

The contract is deliberately separate from the Python callable signature.  A
driver can therefore keep its v1 ``@action`` metadata while actions that need
material, resource, timing, or recovery semantics opt in to schema version 2.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExecutionKind(str, Enum):
    """Boundary at which the OS observes an action as one executable node."""

    ATOMIC = "atomic"
    DEVICE_MACRO = "device_macro"
    MANUAL = "manual"
    CODE = "code"


class MaterialMode(str, Enum):
    """How an action participates in material-flow tracking."""

    NONE = "none"
    PASS_THROUGH = "pass_through"
    CONSUME = "consume"
    PRODUCE = "produce"
    TRANSFORM = "transform"
    MOVE = "move"
    OBSERVE = "observe"


class MaterialEffectTemplate(BaseModel):
    """A declarative material effect instantiated for one action invocation."""

    model_config = ConfigDict(frozen=True)

    port: str
    op: str
    quantity_from: Optional[str] = None
    unit: Optional[str] = None
    location_from: Optional[str] = None
    location_to: Optional[str] = None
    state_patch: Optional[dict] = None


class ResourceClaimTemplate(BaseModel):
    """A resource request that Layer A resolves before dispatch."""

    model_config = ConfigDict(frozen=True)

    resource_type: str = Field(min_length=1)
    selector: str = Field(min_length=1)
    resource_kind: Literal["device", "material", "slot"] = "device"
    quantity: int = Field(default=1, ge=1)
    scope: Literal[
        "action",
        "workflow_block",
        "until_handoff",
        "persistent",
    ] = "action"
    mode: Literal["exclusive", "shared"] = "exclusive"


class TimingContract(BaseModel):
    """Expected and protective timing values for an action."""

    model_config = ConfigDict(frozen=True)

    estimated_duration_s: float = Field(ge=0)
    timeout_s: Optional[float] = Field(default=None, gt=0)
    setup_s: float = Field(default=0, ge=0)
    cleanup_s: float = Field(default=0, ge=0)


class RecoveryContract(BaseModel):
    """Required behavior after ambiguous or interrupted device execution."""

    model_config = ConfigDict(frozen=True)

    idempotency: str
    cancel: str
    timeout: str
    disconnect: str
    estop: str


class ActionContract(BaseModel):
    """Canonical v2 contract attached to an ``@action`` declaration."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["2"] = "2"
    execution_kind: ExecutionKind
    material_mode: MaterialMode = MaterialMode.NONE
    effects: Tuple[MaterialEffectTemplate, ...] = ()
    resource_claims: Tuple[ResourceClaimTemplate, ...] = ()
    timing: TimingContract
    recovery: Optional[RecoveryContract] = None

    @model_validator(mode="after")
    def validate_semantics(self) -> "ActionContract":
        if self.material_mode == MaterialMode.NONE and self.effects:
            raise ValueError("material effect requires a non-none material_mode")
        if self.execution_kind == ExecutionKind.DEVICE_MACRO and self.recovery is None:
            raise ValueError("device_macro actions require a recovery contract")
        return self


class ActionContractValidationError(ValueError):
    """Typed cross-field error raised while attaching a contract to an action."""
