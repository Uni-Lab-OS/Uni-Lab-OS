"""Typed node results and runtime binding materialization for TaskDag v2."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from pydantic import TypeAdapter

from unilabos.scheduler.dag_model import NodeState
from unilabos.workflow.bindings import (
    Binding,
    BindingPreflightError,
    matches_json_type,
    preflight_and_dispatch,
)


@dataclass(frozen=True)
class ResultEnvelope:
    """Named, schema-addressable outputs committed at node terminal."""

    outputs: dict[str, Any] = field(default_factory=dict)
    artifacts: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class NodeExecutionResult:
    """Executor result carrying both terminal state and named outputs."""

    state: NodeState
    envelope: ResultEnvelope = field(default_factory=ResultEnvelope)
    terminal_info: dict[str, Any] = field(default_factory=dict)


_BINDING_ADAPTER = TypeAdapter(Binding)


def materialize_node_inputs(
    *,
    input_bindings: Mapping[str, Any],
    input_schema: Mapping[str, Mapping[str, Any]],
    results: Mapping[str, ResultEnvelope],
    runtime_parameters: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve all bindings without performing a device side effect."""

    parsed = {
        name: _BINDING_ADAPTER.validate_python(binding)
        for name, binding in input_bindings.items()
    }
    resolved: list[dict[str, Any]] = []
    preflight_and_dispatch(
        input_bindings=parsed,
        input_schema=input_schema,
        node_outputs={node_id: envelope.outputs for node_id, envelope in results.items()},
        runtime_parameters=runtime_parameters,
        dispatch=resolved.append,
    )
    if not resolved:
        raise BindingPreflightError("BINDING_RESOLUTION_FAILED", "no resolved payload")
    return resolved[0]


def validate_result_outputs(
    *,
    outputs: Mapping[str, Any],
    output_schema: Mapping[str, Mapping[str, Any]],
) -> None:
    """Reject a successful device result before publishing invalid outputs."""

    for name, value in outputs.items():
        schema = output_schema.get(name)
        if schema is None:
            raise BindingPreflightError(
                "OUTPUT_SCHEMA_MISMATCH",
                f"output {name!r} is not declared by the action contract",
            )
        expected_type = schema.get("type")
        if not matches_json_type(value, expected_type):
            raise BindingPreflightError(
                "OUTPUT_SCHEMA_MISMATCH",
                f"output {name!r} expected {expected_type!r}, "
                f"got {type(value).__name__}",
            )
    for name, schema in output_schema.items():
        if bool(schema.get("required")) and name not in outputs:
            raise BindingPreflightError(
                "OUTPUT_SCHEMA_MISMATCH",
                f"required output {name!r} is missing",
            )
