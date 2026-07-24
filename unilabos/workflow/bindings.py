"""Canonical tagged bindings and the pre-dispatch binding safety gate."""

from __future__ import annotations

from typing import Annotated, Any, Callable, Dict, Literal, Mapping, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class LiteralValue(BaseModel):
    """An inline JSON-compatible invocation value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["literal"] = "literal"
    value: Any


class RuntimeParameterRef(BaseModel):
    """A value supplied when a workflow run is created."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["runtime_parameter"] = "runtime_parameter"
    parameter: str
    default: Any = None


class NodeOutputRef(BaseModel):
    """A named output produced by an upstream invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["node_output"] = "node_output"
    node_id: str
    output: str


class NodeResultRef(BaseModel):
    """The complete named-output mapping produced by an upstream node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["node_result"] = "node_result"
    node_id: str


class ExpressionBinding(BaseModel):
    """A safe structured expression over explicitly declared bindings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["expression"] = "expression"
    expression: Dict[str, Any]
    variables: Dict[str, "Binding"] = Field(default_factory=dict)


class ConditionalBinding(BaseModel):
    """A branch-selected value merged at a structured control-flow join."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["conditional"] = "conditional"
    branch_node_id: str
    true_value: "Binding"
    false_value: "Binding"


Binding = Annotated[
    Union[
        LiteralValue,
        RuntimeParameterRef,
        NodeOutputRef,
        NodeResultRef,
        ExpressionBinding,
        ConditionalBinding,
    ],
    Field(discriminator="kind"),
]

ExpressionBinding.model_rebuild(_types_namespace={"Binding": Binding})
ConditionalBinding.model_rebuild(_types_namespace={"Binding": Binding})


def binding_node_dependencies(binding: Binding) -> set[str]:
    """Return every node result needed to resolve a tagged binding."""

    if isinstance(binding, (NodeOutputRef, NodeResultRef)):
        return {binding.node_id}
    if isinstance(binding, ExpressionBinding):
        return {
            dependency
            for source in binding.variables.values()
            for dependency in binding_node_dependencies(source)
        }
    if isinstance(binding, ConditionalBinding):
        return {
            binding.branch_node_id,
            *binding_node_dependencies(binding.true_value),
            *binding_node_dependencies(binding.false_value),
        }
    return set()


class BindingPreflightError(ValueError):
    """A stable, machine-readable error raised before any device dispatch."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def matches_json_type(value: Any, expected_type: Optional[str]) -> bool:
    if expected_type is None:
        return True
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "null":
        return value is None
    return True


def _resolve_binding(
    binding: Binding,
    *,
    node_outputs: Mapping[str, Mapping[str, Any]],
    runtime_parameters: Mapping[str, Any],
) -> Any:
    if isinstance(binding, LiteralValue):
        return binding.value
    if isinstance(binding, RuntimeParameterRef):
        if binding.parameter in runtime_parameters:
            return runtime_parameters[binding.parameter]
        if binding.default is not None:
            return binding.default
        raise BindingPreflightError(
            "MISSING_RUNTIME_PARAMETER",
            f"runtime parameter {binding.parameter!r} is missing",
        )
    if isinstance(binding, NodeResultRef):
        outputs = node_outputs.get(binding.node_id)
        if outputs is None:
            raise BindingPreflightError(
                "MISSING_NODE_OUTPUT",
                f"node result {binding.node_id} is missing",
            )
        return dict(outputs)
    if isinstance(binding, ConditionalBinding):
        branch_outputs = node_outputs.get(binding.branch_node_id)
        selection = None if branch_outputs is None else branch_outputs.get("branch")
        if selection == "true":
            selected = binding.true_value
        elif selection == "false":
            selected = binding.false_value
        else:
            raise BindingPreflightError(
                "MISSING_BRANCH_SELECTION",
                f"branch selection {binding.branch_node_id}.branch is missing",
            )
        return _resolve_binding(
            selected,
            node_outputs=node_outputs,
            runtime_parameters=runtime_parameters,
        )
    if isinstance(binding, ExpressionBinding):
        from .expression import StructuredExpressionError, evaluate_expression

        resolved_variables = {
            name: _resolve_binding(
                source,
                node_outputs=node_outputs,
                runtime_parameters=runtime_parameters,
            )
            for name, source in binding.variables.items()
        }
        try:
            return evaluate_expression(
                binding.expression,
                read=resolved_variables.__getitem__,
            )
        except (KeyError, StructuredExpressionError) as exc:
            raise BindingPreflightError(
                "INVALID_EXPRESSION",
                str(exc),
            ) from exc
    outputs = node_outputs.get(binding.node_id)
    if outputs is None or binding.output not in outputs:
        raise BindingPreflightError(
            "MISSING_NODE_OUTPUT",
            f"node output {binding.node_id}.{binding.output} is missing",
        )
    return outputs[binding.output]


def preflight_and_dispatch(
    *,
    input_bindings: Mapping[str, Binding],
    input_schema: Mapping[str, Mapping[str, Any]],
    node_outputs: Mapping[str, Mapping[str, Any]],
    runtime_parameters: Mapping[str, Any],
    dispatch: Callable[[Dict[str, Any]], Any],
) -> Any:
    """Resolve and validate every input before invoking ``dispatch`` once."""

    # A non-empty schema is authoritative.  Empty schemas remain compatible
    # with legacy TaskDag producers; Canonical compilation itself always has
    # the registry contract and rejects unknown inputs before dispatch.
    unknown_inputs = (
        sorted(set(input_bindings) - set(input_schema)) if input_schema else []
    )
    if unknown_inputs:
        raise BindingPreflightError(
            "UNKNOWN_INPUT",
            f"input {unknown_inputs[0]!r} is not declared by the action contract",
        )

    resolved: Dict[str, Any] = {}
    for name, raw_schema in input_schema.items():
        schema = raw_schema if isinstance(raw_schema, Mapping) else {}
        if name in input_bindings:
            continue
        if "default" in schema:
            resolved[name] = schema["default"]
            continue
        if schema.get("required") is True:
            raise BindingPreflightError(
                "MISSING_REQUIRED_INPUT",
                f"required input {name!r} is missing",
            )
    for name, binding in input_bindings.items():
        value = _resolve_binding(
            binding,
            node_outputs=node_outputs,
            runtime_parameters=runtime_parameters,
        )
        expected_type = input_schema.get(name, {}).get("type")
        if not matches_json_type(value, expected_type):
            raise BindingPreflightError(
                "BINDING_TYPE_MISMATCH",
                f"input {name!r} expected {expected_type!r}, got {type(value).__name__}",
            )
        schema = input_schema.get(name, {})
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            minimum = schema.get("minimum")
            maximum = schema.get("maximum")
            if minimum is not None and value < minimum:
                raise BindingPreflightError(
                    "INPUT_BELOW_MINIMUM",
                    f"input {name!r} must be >= {minimum}",
                )
            if maximum is not None and value > maximum:
                raise BindingPreflightError(
                    "INPUT_ABOVE_MAXIMUM",
                    f"input {name!r} must be <= {maximum}",
                )
        resolved[name] = value
    return dispatch(resolved)
