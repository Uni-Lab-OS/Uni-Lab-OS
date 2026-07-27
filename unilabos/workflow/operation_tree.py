"""Profile-selected compiler for a versioned structured Operation Tree."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from unilabos.workflow.bindings import (
    Binding,
    ConditionalBinding,
    ExpressionBinding,
    LiteralValue,
    NodeOutputRef,
    NodeResultRef,
    RuntimeParameterRef,
)
from unilabos.workflow.canonical import (
    ActionInvocation,
    ControlEdge,
    SourceMap,
    SourceMapEntry,
    WorkflowParameter,
    WorkflowRevision,
)


class OperationTreeCompileError(ValueError):
    """The configured Operation Tree cannot be lowered by this codec."""


Resolver = Callable[[str], Mapping[str, Any]]


def _safe_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return normalized or "node"


def _variable_names(expression: Any) -> set[str]:
    if isinstance(expression, Mapping):
        names = {
            str(expression["var"])
            for key in expression
            if key == "var" and isinstance(expression[key], str)
        }
        for value in expression.values():
            names.update(_variable_names(value))
        return names
    if isinstance(expression, list):
        return {name for item in expression for name in _variable_names(item)}
    return set()


class StructuredOperationTreeCodec:
    """Lower data-only operation nodes into the shared Canonical DAG."""

    name = "structured_operation_tree_v1"

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
    ) -> None:
        self._resolver = resolver
        self._invocations: list[ActionInvocation] = []
        self._edges: list[ControlEdge] = []
        self._source_entries: list[SourceMapEntry] = []
        self._variables: dict[str, Binding] = {}
        self._counter = 0
        self._script_stack: list[str] = []

    def compile(
        self,
        document: Mapping[str, Any],
        *,
        parameters: Mapping[str, Any] | None = None,
    ) -> WorkflowRevision:
        del parameters  # values remain run-scoped; only declared defaults compile.
        self._validate_document(document)
        workflow_id = str(document["name"])
        self._script_stack = [workflow_id]
        self._variables = self._initial_variables(document, inputs=None, is_root=True)
        self._compile_block(document.get("body", []), incoming=[])
        if not self._invocations:
            raise OperationTreeCompileError("operation has no executable nodes")
        return WorkflowRevision(
            revision_id=f"import-{_safe_id(workflow_id)}",
            workflow_id=workflow_id,
            parameters=self._root_parameters(document),
            invocations=self._invocations,
            control_edges=self._edges,
            source_map=SourceMap(entries=self._source_entries),
        )

    @staticmethod
    def _root_parameters(document: Mapping[str, Any]) -> list[WorkflowParameter]:
        type_map = {
            "STRING": "string",
            "INT": "integer",
            "FLOAT": "number",
            "BOOL": "boolean",
        }
        parameters: list[WorkflowParameter] = []
        for raw in document.get("vars", []) or []:
            if not isinstance(raw, Mapping) or raw.get("io") != "in":
                continue
            source_type = str(raw.get("type") or "")
            parameter_type = type_map.get(source_type)
            if parameter_type is None:
                raise OperationTreeCompileError(
                    f"unsupported external parameter type: {source_type or '-'}"
                )
            ui = raw.get("ui") if isinstance(raw.get("ui"), Mapping) else {}
            values: dict[str, Any] = {
                "name": str(raw.get("name") or ""),
                "type": parameter_type,
                "required": "default" not in raw,
                "title": str(ui.get("label") or raw.get("name") or ""),
                "description": str(raw.get("comment") or ""),
            }
            if "default" in raw:
                values["default"] = raw.get("default")
            parameters.append(WorkflowParameter(**values))
        return parameters

    @staticmethod
    def _validate_document(document: Mapping[str, Any]) -> None:
        for field in ("schema", "kind", "name", "body"):
            if field not in document:
                raise OperationTreeCompileError(
                    f"operation document is missing {field}"
                )
        if document.get("kind") != "operation":
            raise OperationTreeCompileError("workflow source kind must be operation")
        if not isinstance(document.get("body"), list):
            raise OperationTreeCompileError("operation body must be a list")

    def _initial_variables(
        self,
        document: Mapping[str, Any],
        *,
        inputs: Mapping[str, Binding] | None,
        is_root: bool,
    ) -> dict[str, Binding]:
        variables: dict[str, Binding] = {}
        supplied = dict(inputs or {})
        for raw in document.get("vars", []) or []:
            if not isinstance(raw, Mapping) or not raw.get("name"):
                raise OperationTreeCompileError("operation variable requires name")
            name = str(raw["name"])
            if name in supplied:
                variables[name] = supplied[name]
            elif is_root and raw.get("io") == "in":
                variables[name] = RuntimeParameterRef(
                    parameter=name,
                )
            else:
                variables[name] = LiteralValue(value=raw.get("default"))
        unknown = sorted(set(supplied) - set(variables))
        if unknown:
            raise OperationTreeCompileError(
                f"subworkflow has no input variable {unknown[0]!r}"
            )
        return variables

    def _new_id(self, op: str) -> str:
        self._counter += 1
        script = _safe_id(self._script_stack[-1])
        return f"{script}-{self._counter:03d}-{_safe_id(op)}"

    def _append_invocation(
        self,
        invocation: ActionInvocation,
        incoming: list[tuple[str, str | None]],
    ) -> list[tuple[str, str | None]]:
        self._invocations.append(invocation)
        for source, branch in incoming:
            self._edges.append(
                ControlEdge(
                    source=source,
                    target=invocation.node_id,
                    branch=branch,
                )
            )
        return [(invocation.node_id, None)]

    def _binding_for_expression(self, expression: Any) -> Binding:
        if not isinstance(expression, Mapping):
            raise OperationTreeCompileError("expression must be an object")
        if set(expression) == {"lit"}:
            return LiteralValue(value=expression["lit"])
        if set(expression) == {"var"}:
            name = str(expression["var"])
            if name not in self._variables:
                raise OperationTreeCompileError(
                    f"expression references undeclared variable {name!r}"
                )
            return self._variables[name]
        if (
            set(expression) == {"field", "name"}
            and isinstance(expression.get("field"), Mapping)
            and set(expression["field"]) == {"var"}
        ):
            source = self._variables.get(str(expression["field"]["var"]))
            if isinstance(source, NodeResultRef):
                return NodeOutputRef(
                    node_id=source.node_id,
                    output=str(expression["name"]),
                )
        variables: dict[str, Any] = {}
        for name in sorted(_variable_names(expression)):
            source = self._variables.get(name)
            if source is None:
                raise OperationTreeCompileError(
                    f"expression references undeclared variable {name!r}"
                )
            variables[name] = source
        return ExpressionBinding(
            expression=dict(expression),
            variables=variables,
        )

    def _compile_block(
        self,
        raw_nodes: Any,
        *,
        incoming: list[tuple[str, str | None]],
    ) -> list[tuple[str, str | None]]:
        if not isinstance(raw_nodes, list):
            raise OperationTreeCompileError("operation block must be a list")
        exits = list(incoming)
        for source_index, raw_node in enumerate(raw_nodes):
            if not isinstance(raw_node, Mapping):
                raise OperationTreeCompileError("operation node must be an object")
            before = len(self._invocations)
            exits = self._compile_node(raw_node, incoming=exits)
            compiled_ids = [item.node_id for item in self._invocations[before:]]
            self._source_entries.append(
                SourceMapEntry(
                    node_id=compiled_ids[0] if compiled_ids else "",
                    line=(
                        int(raw_node.get("_source_line") or 0)
                        if not isinstance(raw_node.get("_source_line"), bool)
                        else 0
                    ),
                    column=(
                        int(raw_node.get("_source_column") or 0)
                        if not isinstance(raw_node.get("_source_column"), bool)
                        else 0
                    ),
                    source_step_index=source_index,
                    compiled_node_ids=compiled_ids,
                )
            )
        return exits

    def _compile_node(
        self,
        node: Mapping[str, Any],
        *,
        incoming: list[tuple[str, str | None]],
    ) -> list[tuple[str, str | None]]:
        op = str(node.get("op") or "")
        if op == "comment":
            return incoming
        if op == "call":
            action_ref = str(node.get("action") or "")
            if "." not in action_ref:
                raise OperationTreeCompileError("call requires device.action")
            invocation = ActionInvocation(
                node_id=self._new_id(action_ref),
                action_ref=action_ref,
                input_bindings={
                    str(name): self._binding_for_expression(expression)
                    for name, expression in (node.get("args") or {}).items()
                },
            )
            exits = self._append_invocation(invocation, incoming)
            assignment = node.get("assign")
            if isinstance(assignment, Mapping) and assignment.get("var"):
                name = str(assignment["var"])
                if name not in self._variables:
                    raise OperationTreeCompileError(
                        f"call assigns undeclared variable {name!r}"
                    )
                self._variables[name] = NodeResultRef(node_id=invocation.node_id)
            return exits
        if op == "human":
            if node.get("kind") != "confirm":
                raise OperationTreeCompileError(
                    "only confirm human gates are executable in this slice"
                )
            prompt = node.get("prompt", {"lit": ""})
            return self._append_invocation(
                ActionInvocation(
                    node_id=self._new_id("human"),
                    action_ref="host_node.manual_confirm",
                    node_type="manual_confirm",
                    input_bindings={"prompt": self._binding_for_expression(prompt)},
                    control={"on_cancel": str(node.get("on_cancel") or "raise")},
                ),
                incoming,
            )
        if op == "if":
            return self._compile_if(node, incoming=incoming)
        if op == "run_script":
            return self._compile_subworkflow(node, incoming=incoming)
        if op == "try":
            return self._compile_try(node, incoming=incoming)
        if op == "group":
            return self._compile_group(node, incoming=incoming)
        if op == "parallel":
            return self._compile_parallel(node, incoming=incoming)
        raise OperationTreeCompileError(
            f"operation {op!r} is not executable in this runtime slice"
        )

    def _compile_group(
        self,
        node: Mapping[str, Any],
        *,
        incoming: list[tuple[str, str | None]],
    ) -> list[tuple[str, str | None]]:
        name = str(node.get("name") or "")
        if not name:
            raise OperationTreeCompileError("group requires name")
        marker = ActionInvocation(
            node_id=self._new_id("group"),
            action_ref="os_control.group",
            node_type="group",
            control={"name": name},
        )
        exits = self._append_invocation(marker, incoming)
        return self._compile_block(node.get("body", []), incoming=exits)

    def _compile_parallel(
        self,
        node: Mapping[str, Any],
        *,
        incoming: list[tuple[str, str | None]],
    ) -> list[tuple[str, str | None]]:
        body = node.get("body", [])
        if not isinstance(body, list) or not body:
            raise OperationTreeCompileError("parallel requires child nodes")
        marker = ActionInvocation(
            node_id=self._new_id("fork"),
            action_ref="os_control.fork",
            node_type="fork",
        )
        marker_exits = self._append_invocation(marker, incoming)
        variables = dict(self._variables)
        exits: list[tuple[str, str | None]] = []
        for child in body:
            # Each sibling receives the same control predecessor. Assigned
            # variables have unique names in the accepted Python subset and
            # are merged back for expressions after the parallel block.
            self._variables = dict(variables)
            child_exits = self._compile_block([child], incoming=marker_exits)
            variables.update(self._variables)
            exits.extend(child_exits)
        self._variables = variables
        return self._append_invocation(
            ActionInvocation(
                node_id=self._new_id("join"),
                action_ref="os_control.join",
                node_type="join",
            ),
            exits,
        )

    def _compile_if(
        self,
        node: Mapping[str, Any],
        *,
        incoming: list[tuple[str, str | None]],
    ) -> list[tuple[str, str | None]]:
        branch_id = self._new_id("branch")
        self._append_invocation(
            ActionInvocation(
                node_id=branch_id,
                action_ref="os_control.branch",
                node_type="branch",
                input_bindings={
                    "condition": self._binding_for_expression(node.get("cond"))
                },
                output_schema={"branch": {"type": "string", "required": True}},
            ),
            incoming,
        )
        entry_variables = dict(self._variables)
        self._variables = dict(entry_variables)
        true_exits = self._compile_block(
            node.get("then", []),
            incoming=[(branch_id, "true")],
        )
        true_variables = dict(self._variables)
        self._variables = dict(entry_variables)
        false_exits = self._compile_block(
            node.get("else", []),
            incoming=[(branch_id, "false")],
        )
        false_variables = dict(self._variables)
        self._variables = {
            name: (
                true_variables[name]
                if true_variables[name] == false_variables[name]
                else ConditionalBinding(
                    branch_node_id=branch_id,
                    true_value=true_variables[name],
                    false_value=false_variables[name],
                )
            )
            for name in entry_variables
        }
        join_id = self._new_id("join")
        return self._append_invocation(
            ActionInvocation(
                node_id=join_id,
                action_ref="os_control.join",
                node_type="join",
            ),
            [*true_exits, *false_exits],
        )

    def _compile_subworkflow(
        self,
        node: Mapping[str, Any],
        *,
        incoming: list[tuple[str, str | None]],
    ) -> list[tuple[str, str | None]]:
        if self._resolver is None:
            raise OperationTreeCompileError("subworkflow resolver is not configured")
        name = str(node.get("script") or "")
        if not name:
            raise OperationTreeCompileError("run_script requires script")
        if name in self._script_stack:
            raise OperationTreeCompileError(f"recursive subworkflow: {name}")
        document = self._resolver(name)
        self._validate_document(document)
        caller_variables = self._variables
        supplied = {
            str(input_name): self._binding_for_expression(expression)
            for input_name, expression in (node.get("inputs") or {}).items()
        }
        module = str(node.get("module") or "")
        symbol = str(node.get("callable") or "")
        marker_control: dict[str, Any] = {
            "name": f"subworkflow::{name}",
        }
        if module and symbol:
            marker_control["callable"] = {
                "module": module,
                "name": symbol,
                "inputs": {
                    input_name: binding.model_dump(
                        mode="json",
                        exclude_none=True,
                    )
                    for input_name, binding in supplied.items()
                },
                "outputs": {},
            }
        marker_index = len(self._invocations)
        marker = ActionInvocation(
            node_id=self._new_id("group"),
            action_ref="os_control.group",
            node_type="group",
            control=marker_control,
        )
        marker_exits = self._append_invocation(marker, incoming)
        self._variables = self._initial_variables(
            document,
            inputs=supplied,
            is_root=False,
        )
        self._script_stack.append(name)
        source_entry_start = len(self._source_entries)
        try:
            exits = self._compile_block(
                document.get("body", []),
                incoming=marker_exits,
            )
            child_variables = self._variables
            child_returns = {
                str(output_name): self._binding_for_expression(expression)
                for output_name, expression in (
                    document.get("returns") or {}
                ).items()
            }
        finally:
            self._script_stack.pop()
            self._variables = caller_variables
        call_outputs: dict[str, Any] = {}
        for child_name, target in (node.get("outputs") or {}).items():
            if not isinstance(target, Mapping) or not target.get("var"):
                raise OperationTreeCompileError("run_script output target is invalid")
            source = child_returns.get(
                str(child_name),
                child_variables.get(str(child_name)),
            )
            target_name = str(target["var"])
            if source is None or target_name not in self._variables:
                raise OperationTreeCompileError("run_script output is unresolved")
            self._variables[target_name] = source
            call_outputs[str(child_name)] = {
                "target": target_name,
                "binding": source.model_dump(mode="json", exclude_none=True),
            }
        if "callable" in marker_control:
            marker_control["callable"]["outputs"] = call_outputs
            self._invocations[marker_index] = marker.model_copy(
                update={"control": marker_control}
            )
        # Imported child line numbers belong to another file. In the composite
        # source every expanded child maps to the visible function-call line.
        call_line = int(node.get("_source_line") or 0)
        call_column = int(node.get("_source_column") or 0)
        for index in range(source_entry_start, len(self._source_entries)):
            self._source_entries[index] = self._source_entries[index].model_copy(
                update={"line": call_line, "column": call_column}
            )
        return exits

    def _compile_try(
        self,
        node: Mapping[str, Any],
        *,
        incoming: list[tuple[str, str | None]],
    ) -> list[tuple[str, str | None]]:
        if node.get("catch"):
            raise OperationTreeCompileError(
                "try/catch is not executable in this runtime slice"
            )
        final_nodes = node.get("finally", [])
        if not isinstance(final_nodes, list) or any(
            not isinstance(item, Mapping)
            or str(item.get("op") or "") not in {"call", "comment"}
            for item in final_nodes
        ):
            raise OperationTreeCompileError(
                "finally cleanup does not support control flow in this runtime slice"
            )
        body_start = len(self._invocations)
        body_exits = self._compile_block(node.get("body", []), incoming=incoming)
        protected = [item.node_id for item in self._invocations[body_start:]]
        cleanup_start = len(self._invocations)
        exits = self._compile_block(final_nodes, incoming=body_exits)
        for index in range(cleanup_start, len(self._invocations)):
            invocation = self._invocations[index]
            self._invocations[index] = invocation.model_copy(
                update={
                    "node_type": "cleanup",
                    "cleanup_for": protected,
                }
            )
        return exits


def compile_operation_tree(
    document: Mapping[str, Any],
    *,
    parameters: Mapping[str, Any] | None = None,
    resolver: Resolver | None = None,
) -> WorkflowRevision:
    return StructuredOperationTreeCodec(resolver=resolver).compile(
        document,
        parameters=parameters,
    )
