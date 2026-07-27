"""Deterministic Canonical-to-Python authoring projection."""

from __future__ import annotations

import copy
import keyword
from collections.abc import Mapping
from typing import Any

from .canonical import (
    ActionInvocation,
    WorkflowRevision,
    WorkflowSourceArtifact,
)
from .canonical_ir import (
    _candidate,
    _result,
    _source_artifact,
    _source_spans,
    safe_python_identifier,
)
from .from_python_script import WorkflowSourceResolver, compile_python_script

_GENERATION_FIELDS = frozenset({"base_revision_id", "canonical_ir", "source_uri"})


def _require_request(request: Mapping[str, Any]) -> tuple[str, str]:
    if set(request) != _GENERATION_FIELDS:
        raise ValueError("INVALID_AUTHORING_ENVELOPE: generate fields are invalid")
    base_revision_id = str(request["base_revision_id"])
    source_uri = str(request["source_uri"])
    if not base_revision_id or not source_uri:
        raise ValueError("INVALID_AUTHORING_ENVELOPE: fields must be non-empty")
    return base_revision_id, source_uri


def _binding_source(
    binding: Mapping[str, Any],
    variables: Mapping[str, str],
    binding_aliases: Mapping[tuple[str, str | None], str] | None = None,
) -> str:
    aliases = binding_aliases or {}
    kind = binding.get("kind")
    if kind == "literal":
        return repr(binding.get("value"))
    if kind == "runtime_parameter":
        return safe_python_identifier(str(binding.get("parameter") or "parameter"))
    if kind == "node_result":
        node_id = str(binding.get("node_id"))
        return aliases.get(
            (node_id, None),
            variables.get(node_id, "None"),
        )
    if kind == "node_output":
        node_id = str(binding.get("node_id"))
        output = safe_python_identifier(str(binding.get("output") or "value"))
        alias = aliases.get((node_id, str(binding.get("output") or "value")))
        if alias is not None:
            return alias
        base = variables.get(node_id, "None")
        return f"{base}.{output}"
    if kind == "expression":
        expression_variables = binding.get("variables")
        return _expression_source(
            binding.get("expression"),
            variables=(
                expression_variables
                if isinstance(expression_variables, Mapping)
                else {}
            ),
            node_variables=variables,
            binding_aliases=aliases,
        )
    # Complex bindings remain visible and non-executable instead of disappearing.
    return f"binding_literal({copy.deepcopy(dict(binding))!r})"


def _expression_source(
    expression: Any,
    *,
    variables: Mapping[str, Any],
    node_variables: Mapping[str, str],
    binding_aliases: Mapping[tuple[str, str | None], str] | None = None,
) -> str:
    """Render the compiler's closed expression IR back to safe Python."""

    if not isinstance(expression, Mapping):
        return repr(expression)
    if set(expression) == {"lit"}:
        return repr(expression["lit"])
    if set(expression) == {"var"}:
        name = str(expression["var"])
        binding = variables.get(name)
        if isinstance(binding, Mapping):
            return _binding_source(
                binding,
                node_variables,
                binding_aliases,
            )
        return safe_python_identifier(name, "value")
    if set(expression) == {"field", "name"}:
        base = _expression_source(
            expression["field"],
            variables=variables,
            node_variables=node_variables,
            binding_aliases=binding_aliases,
        )
        return f"{base}.{safe_python_identifier(str(expression['name']), 'field')}"
    if set(expression) == {"index", "key"}:
        base = _expression_source(
            expression["index"],
            variables=variables,
            node_variables=node_variables,
            binding_aliases=binding_aliases,
        )
        key = _expression_source(
            expression["key"],
            variables=variables,
            node_variables=node_variables,
            binding_aliases=binding_aliases,
        )
        return f"{base}[{key}]"
    if set(expression) == {"binop", "left", "right"}:
        operator = str(expression["binop"])
        allowed = {
            "+",
            "-",
            "*",
            "/",
            "//",
            "%",
            "==",
            "!=",
            ">",
            ">=",
            "<",
            "<=",
            "and",
            "or",
        }
        if operator not in allowed:
            return "False"
        left = _expression_source(
            expression["left"],
            variables=variables,
            node_variables=node_variables,
            binding_aliases=binding_aliases,
        )
        right = _expression_source(
            expression["right"],
            variables=variables,
            node_variables=node_variables,
            binding_aliases=binding_aliases,
        )
        return f"({left} {operator} {right})"
    if set(expression) == {"unop", "operand"}:
        operator = str(expression["unop"])
        operand = _expression_source(
            expression["operand"],
            variables=variables,
            node_variables=node_variables,
            binding_aliases=binding_aliases,
        )
        if operator == "not":
            return f"(not {operand})"
        if operator == "neg":
            return f"(-{operand})"
        return "False"
    if set(expression) == {"call", "args"}:
        function = safe_python_identifier(str(expression["call"]), "value")
        arguments = ", ".join(
            _expression_source(
                item,
                variables=variables,
                node_variables=node_variables,
                binding_aliases=binding_aliases,
            )
            for item in expression["args"]
        )
        return f"{function}({arguments})"
    return repr(copy.deepcopy(dict(expression)))


def _call_source(
    invocation: ActionInvocation,
    variables: Mapping[str, str],
    binding_aliases: Mapping[tuple[str, str | None], str] | None = None,
) -> str:
    argument_values = {
        name: _binding_source(
            binding.model_dump(mode="json"),
            variables,
            binding_aliases,
        )
        for name, binding in invocation.input_bindings.items()
    }
    if invocation.node_type == "manual_confirm":
        argument_values.setdefault(
            "on_cancel",
            repr(str(invocation.control.get("on_cancel") or "raise")),
        )
    arguments = ", ".join(
        f"{safe_python_identifier(name, 'parameter')}={value}"
        for name, value in sorted(argument_values.items())
    )
    owner, separator, action = invocation.action_ref.rpartition(".")
    if (
        not separator
        or not action.isidentifier()
        or keyword.iskeyword(action)
    ):
        raise ValueError(
            f"action ref cannot be represented in Python: {invocation.action_ref}"
        )
    owner_source = (
        owner
        if owner.isidentifier() and not keyword.iskeyword(owner)
        else f"device({owner!r})"
    )
    return f"{owner_source}.{action}({arguments})"


def _variable_names(revision: WorkflowRevision) -> dict[str, str]:
    referenced: set[str] = set()
    preferred: dict[str, str] = {}

    def collect(value: Any) -> None:
        if isinstance(value, Mapping):
            if value.get("kind") in {"node_result", "node_output"}:
                referenced.add(str(value.get("node_id")))
            if value.get("kind") == "expression":
                expression_variables = value.get("variables")
                if isinstance(expression_variables, Mapping):
                    for name, source in expression_variables.items():
                        if isinstance(source, Mapping) and source.get("kind") in {
                            "node_result",
                            "node_output",
                        }:
                            preferred.setdefault(
                                str(source.get("node_id")),
                                safe_python_identifier(str(name), "value"),
                            )
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    for invocation in revision.invocations:
        collect(invocation.model_dump(mode="json")["input_bindings"])
    names: dict[str, str] = {}
    used: set[str] = {parameter.name for parameter in revision.parameters or []}
    for invocation in revision.invocations:
        if invocation.node_id not in referenced:
            continue
        base = preferred.get(
            invocation.node_id,
            f"{safe_python_identifier(invocation.action_ref)}_value",
        )
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        names[invocation.node_id] = candidate
    return names


def _parameter_source(revision: WorkflowRevision) -> str:
    type_names = {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
    }
    parameters: list[str] = []
    for parameter in revision.parameters or []:
        source = f"{parameter.name}: {type_names[parameter.type]}"
        if "default" in parameter.model_fields_set:
            source += f" = {parameter.default!r}"
        parameters.append(source)
    return ", ".join(parameters)


def _emit_invocation(
    *,
    invocation: ActionInvocation,
    indent: str,
    lines: list[str],
    spans: list[dict[str, Any]],
    variables: Mapping[str, str],
    binding_aliases: Mapping[tuple[str, str | None], str],
    diagnostics: list[dict[str, Any]],
) -> None:
    line_number = len(lines) + 1
    supported = invocation.node_type in {
        "action",
        "cleanup",
        "manual_confirm",
    }
    if supported:
        call = _call_source(invocation, variables, binding_aliases)
        variable = variables.get(invocation.node_id)
        lines.append(f"{indent}{variable + ' = ' if variable else ''}{call}")
    else:
        lines.append(
            f"{indent}unsupported_node("
            f"node_id={invocation.node_id!r}, "
            f"node_type={invocation.node_type!r})"
        )
        diagnostics.append(
            {
                "severity": "error",
                "code": "UNSUPPORTED_NODE",
                "message": (
                    "Canonical node cannot yet be represented by the "
                    f"Python authoring subset: {invocation.node_type}"
                ),
                "node_id": invocation.node_id,
            }
        )
    spans.append(
        {
            "node_id": invocation.node_id,
            "start_line": line_number,
            "start_column": len(indent) + 1,
            "end_line": line_number,
            "end_column": max(len(lines[-1]) + 1, len(indent) + 1),
        }
    )


def _control_span(
    *,
    node_id: str,
    indent: str,
    lines: list[str],
    spans: list[dict[str, Any]],
    source: str,
) -> None:
    line_number = len(lines) + 1
    lines.append(f"{indent}{source}")
    spans.append(
        {
            "node_id": node_id,
            "start_line": line_number,
            "start_column": len(indent) + 1,
            "end_line": line_number,
            "end_column": len(lines[-1]) + 1,
        }
    )


def _python_projection(
    revision: WorkflowRevision,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    lines = [
        (
            "from unilabos.workflow.authoring import "
            "device, group, parallel, workflow_definition"
        ),
        "",
        "# Generated from Canonical IR; edit and compile before applying.",
        (
            f"@workflow_definition(workflow_id={revision.workflow_id!r}, "
            f"revision={revision.revision_id!r})"
        ),
        (
            f"def {safe_python_identifier(revision.workflow_id)}"
            f"({_parameter_source(revision)}) -> None:"
        ),
        '    """Generated deterministically from Canonical workflow IR."""',
    ]
    spans: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    variables = _variable_names(revision)
    invocations = revision.invocations
    invocation_by_id = {invocation.node_id: invocation for invocation in invocations}
    ordered_ids = [invocation.node_id for invocation in invocations]
    index_by_id = {node_id: index for index, node_id in enumerate(ordered_ids)}
    outgoing: dict[str, list[tuple[str, str | None]]] = {}
    for edge in revision.control_edges:
        outgoing.setdefault(edge.source, []).append((edge.target, edge.branch))

    scope_by_marker: dict[str, list[str]] = {}
    for entry in revision.source_map.entries:
        compiled = [
            node_id
            for node_id in entry.compiled_node_ids
            if node_id in invocation_by_id
        ]
        if (
            entry.node_id in invocation_by_id
            and compiled
            and compiled[0] == entry.node_id
            and len(compiled) > len(scope_by_marker.get(entry.node_id, []))
        ):
            scope_by_marker[entry.node_id] = compiled

    callable_groups: dict[str, Mapping[str, Any]] = {}
    for invocation in invocations:
        if invocation.node_type != "group":
            continue
        raw_callable = invocation.control.get("callable")
        if (
            isinstance(raw_callable, Mapping)
            and isinstance(raw_callable.get("module"), str)
            and isinstance(raw_callable.get("name"), str)
            and raw_callable["module"]
            and raw_callable["name"]
        ):
            callable_groups[invocation.node_id] = raw_callable
    top_level_callable_groups = {
        node_id
        for node_id in callable_groups
        if not any(
            node_id in scope_by_marker.get(parent_id, [])[1:]
            for parent_id in callable_groups
            if parent_id != node_id
        )
    }
    imports_by_module: dict[str, set[str]] = {}
    for node_id in top_level_callable_groups:
        callable_metadata = callable_groups[node_id]
        imports_by_module.setdefault(
            str(callable_metadata["module"]),
            set(),
        ).add(str(callable_metadata["name"]))
    import_lines: list[str] = []
    for module, names in sorted(imports_by_module.items()):
        ordered_names = sorted(names)
        if len(ordered_names) == 1:
            import_lines.append(
                f"from {module} import {ordered_names[0]}"
            )
        else:
            import_lines.append(f"from {module} import (")
            import_lines.extend(f"    {name}," for name in ordered_names)
            import_lines.append(")")
    if import_lines:
        lines[1:1] = [*import_lines, ""]

    binding_aliases: dict[tuple[str, str | None], str] = {}
    call_targets: dict[str, list[str]] = {}
    for node_id in top_level_callable_groups:
        raw_outputs = callable_groups[node_id].get("outputs")
        targets: list[str] = []
        if isinstance(raw_outputs, Mapping):
            for raw_output in raw_outputs.values():
                if not isinstance(raw_output, Mapping):
                    continue
                target = safe_python_identifier(
                    str(raw_output.get("target") or "result"),
                    "result",
                )
                binding = raw_output.get("binding")
                if not isinstance(binding, Mapping):
                    continue
                kind = binding.get("kind")
                binding_node_id = str(binding.get("node_id") or "")
                if kind == "node_result" and binding_node_id:
                    binding_aliases[(binding_node_id, None)] = target
                elif kind == "node_output" and binding_node_id:
                    binding_aliases[
                        (
                            binding_node_id,
                            str(binding.get("output") or "value"),
                        )
                    ] = target
                targets.append(target)
        call_targets[node_id] = targets

    cleanup_by_start: dict[str, tuple[list[str], str]] = {}
    for cleanup in invocations:
        if cleanup.node_type != "cleanup" or not cleanup.cleanup_for:
            continue
        protected = sorted(
            (node_id for node_id in cleanup.cleanup_for if node_id in index_by_id),
            key=index_by_id.__getitem__,
        )
        if protected and index_by_id[protected[-1]] < index_by_id[cleanup.node_id]:
            cleanup_by_start[protected[0]] = (
                protected,
                cleanup.node_id,
            )

    def reaches(
        source: str,
        target: str,
        *,
        available: set[str],
    ) -> bool:
        if source == target:
            return True
        pending = [source]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current in visited or current not in available:
                continue
            visited.add(current)
            for candidate, _ in outgoing.get(current, []):
                if candidate == target:
                    return True
                if candidate in available and candidate not in visited:
                    pending.append(candidate)
        return False

    def matching_join(
        node_id: str,
        available: list[str],
    ) -> str | None:
        """Find the earliest join reached by every direct control branch."""

        available_set = set(available)
        successors = [
            target for target, _ in outgoing.get(node_id, []) if target in available_set
        ]
        if not successors:
            return None
        start = available.index(node_id)
        for candidate in available[start + 1 :]:
            if invocation_by_id[candidate].node_type != "join":
                continue
            if all(
                reaches(
                    successor,
                    candidate,
                    available=available_set,
                )
                for successor in successors
            ):
                return candidate
        return None

    def marker_scope(
        node_id: str,
        available: list[str],
    ) -> list[str]:
        declared = scope_by_marker.get(node_id)
        if declared is not None and all(item in available for item in declared):
            return list(declared)
        invocation = invocation_by_id[node_id]
        start = available.index(node_id)
        if invocation.node_type == "group":
            return available[start:]
        if invocation.node_type in {"fork", "branch"}:
            join_id = matching_join(node_id, available)
            if join_id is not None:
                return available[start : available.index(join_id) + 1]
        return [node_id]

    def branch_nodes(
        branch_id: str,
        join_id: str,
        body_ids: list[str],
        branch_name: str,
    ) -> set[str]:
        pending = [
            target
            for target, edge_branch in outgoing.get(branch_id, [])
            if edge_branch == branch_name and target != join_id
        ]
        body = set(body_ids)
        reached: set[str] = set()
        while pending:
            current = pending.pop()
            if current == join_id or current not in body or current in reached:
                continue
            reached.add(current)
            pending.extend(
                target for target, _ in outgoing.get(current, []) if target != join_id
            )
        return reached

    def emit_sequence(node_ids: list[str], indent: str) -> None:
        position = 0
        while position < len(node_ids):
            node_id = node_ids[position]
            invocation = invocation_by_id[node_id]

            cleanup_group = cleanup_by_start.get(node_id)
            if cleanup_group is not None:
                protected, cleanup_id = cleanup_group
                consumed = [*protected, cleanup_id]
                if all(item in node_ids for item in consumed):
                    lines.append(f"{indent}try:")
                    emit_sequence(protected, f"{indent}    ")
                    lines.append(f"{indent}finally:")
                    _emit_invocation(
                        invocation=invocation_by_id[cleanup_id],
                        indent=f"{indent}    ",
                        lines=lines,
                        spans=spans,
                        variables=variables,
                        binding_aliases=binding_aliases,
                        diagnostics=diagnostics,
                    )
                    position = max(node_ids.index(item) for item in consumed) + 1
                    continue

            if invocation.node_type == "group":
                scope = marker_scope(node_id, node_ids)
                callable_metadata = callable_groups.get(node_id)
                if (
                    node_id in top_level_callable_groups
                    and callable_metadata is not None
                ):
                    raw_inputs = callable_metadata.get("inputs")
                    arguments = ", ".join(
                        (
                            f"{safe_python_identifier(str(name), 'parameter')}="
                            f"{_binding_source(binding, variables, binding_aliases)}"
                        )
                        for name, binding in sorted(
                            (
                                (name, binding)
                                for name, binding in (
                                    raw_inputs.items()
                                    if isinstance(raw_inputs, Mapping)
                                    else []
                                )
                                if isinstance(binding, Mapping)
                            ),
                            key=lambda item: str(item[0]),
                        )
                    )
                    call = (
                        f"{safe_python_identifier(str(callable_metadata['name']))}"
                        f"({arguments})"
                    )
                    targets = call_targets.get(node_id, [])
                    if len(targets) == 1:
                        call = f"{targets[0]} = {call}"
                    elif len(targets) > 1:
                        call = f"{', '.join(targets)} = {call}"
                    line_number = len(lines) + 1
                    _control_span(
                        node_id=node_id,
                        indent=indent,
                        lines=lines,
                        spans=spans,
                        source=call,
                    )
                    for child_id in scope[1:]:
                        spans.append(
                            {
                                "node_id": child_id,
                                "start_line": line_number,
                                "start_column": len(indent) + 1,
                                "end_line": line_number,
                                "end_column": len(lines[-1]) + 1,
                            }
                        )
                    position = max(node_ids.index(item) for item in scope) + 1
                    continue
                name = str(invocation.control.get("name") or "group")
                _control_span(
                    node_id=node_id,
                    indent=indent,
                    lines=lines,
                    spans=spans,
                    source=f"with group(name={name!r}):",
                )
                body = scope[1:]
                if body:
                    emit_sequence(body, f"{indent}    ")
                else:
                    lines.append(f"{indent}    pass")
                position = max(node_ids.index(item) for item in scope) + 1
                continue

            if invocation.node_type == "fork":
                scope = marker_scope(node_id, node_ids)
                if len(scope) < 3 or invocation_by_id[scope[-1]].node_type != "join":
                    _emit_invocation(
                        invocation=invocation,
                        indent=indent,
                        lines=lines,
                        spans=spans,
                        variables=variables,
                        binding_aliases=binding_aliases,
                        diagnostics=diagnostics,
                    )
                    position += 1
                    continue
                _control_span(
                    node_id=node_id,
                    indent=indent,
                    lines=lines,
                    spans=spans,
                    source="with parallel():",
                )
                emit_sequence(scope[1:-1], f"{indent}    ")
                _control_span(
                    node_id=scope[-1],
                    indent=indent,
                    lines=lines,
                    spans=spans,
                    source=f"# join: {scope[-1]}",
                )
                position = max(node_ids.index(item) for item in scope) + 1
                continue

            if invocation.node_type == "branch":
                scope = marker_scope(node_id, node_ids)
                if len(scope) < 2 or invocation_by_id[scope[-1]].node_type != "join":
                    _emit_invocation(
                        invocation=invocation,
                        indent=indent,
                        lines=lines,
                        spans=spans,
                        variables=variables,
                        binding_aliases=binding_aliases,
                        diagnostics=diagnostics,
                    )
                    position += 1
                    continue
                condition = invocation.input_bindings.get("condition")
                condition_source = (
                    "False"
                    if condition is None
                    else _binding_source(
                        condition.model_dump(mode="json"),
                        variables,
                        binding_aliases,
                    )
                )
                _control_span(
                    node_id=node_id,
                    indent=indent,
                    lines=lines,
                    spans=spans,
                    source=f"if {condition_source}:",
                )
                join_id = scope[-1]
                body_ids = scope[1:-1]
                true_nodes = branch_nodes(
                    node_id,
                    join_id,
                    body_ids,
                    "true",
                )
                false_nodes = branch_nodes(
                    node_id,
                    join_id,
                    body_ids,
                    "false",
                )
                true_body = [item for item in body_ids if item in true_nodes]
                false_body = [item for item in body_ids if item in false_nodes]
                if true_body:
                    emit_sequence(true_body, f"{indent}    ")
                else:
                    lines.append(f"{indent}    pass")
                    diagnostics.append(
                        {
                            "severity": "error",
                            "code": "UNSUPPORTED_EMPTY_TRUE_BRANCH",
                            "message": (
                                "Canonical branch with an empty true path "
                                "cannot be represented by the Python subset"
                            ),
                            "node_id": node_id,
                        }
                    )
                if false_body:
                    lines.append(f"{indent}else:")
                    emit_sequence(false_body, f"{indent}    ")
                _control_span(
                    node_id=join_id,
                    indent=indent,
                    lines=lines,
                    spans=spans,
                    source=f"# join: {join_id}",
                )
                position = max(node_ids.index(item) for item in scope) + 1
                continue

            if invocation.node_type == "join":
                diagnostics.append(
                    {
                        "severity": "error",
                        "code": "UNMATCHED_JOIN",
                        "message": (
                            "Canonical join is not paired with a structured "
                            "fork or branch"
                        ),
                        "node_id": node_id,
                    }
                )
                _emit_invocation(
                    invocation=invocation,
                    indent=indent,
                    lines=lines,
                    spans=spans,
                    variables=variables,
                    binding_aliases=binding_aliases,
                    diagnostics=diagnostics,
                )
                position += 1
                continue

            _emit_invocation(
                invocation=invocation,
                indent=indent,
                lines=lines,
                spans=spans,
                variables=variables,
                binding_aliases=binding_aliases,
                diagnostics=diagnostics,
            )
            position += 1

    emit_sequence(ordered_ids, "    ")

    if not invocations:
        lines.append("    pass")
    return "\n".join(lines) + "\n", spans, diagnostics


def _verified_python_source(
    revision: WorkflowRevision,
    *,
    action_catalog: Mapping[str, Mapping[str, Any]],
    workflow_source_resolver: WorkflowSourceResolver | None,
) -> str | None:
    """Reuse an AST-verified source artifact only when it represents this IR."""

    artifact = revision.source_artifact
    if artifact is None or artifact.format != "python":
        return None
    try:
        compiled = compile_python_script(
            artifact.text,
            action_catalog={
                name: dict(definition) for name, definition in action_catalog.items()
            },
            source_artifact=artifact,
            workflow_source_resolver=workflow_source_resolver,
        )
    except (TypeError, ValueError):
        return None
    if compiled.content_hash != revision.content_hash:
        return None
    return artifact.text


def generate_python_revision(
    request: Mapping[str, Any],
    *,
    action_catalog: Mapping[str, Mapping[str, Any]],
    workflow_source_resolver: WorkflowSourceResolver | None = None,
) -> dict[str, Any]:
    """Project Canonical IR to readable Python without applying or executing it."""

    base_revision_id, source_uri = _require_request(request)
    revision = WorkflowRevision.model_validate(request["canonical_ir"])
    python_source = _verified_python_source(
        revision,
        action_catalog=action_catalog,
        workflow_source_resolver=workflow_source_resolver,
    )
    if python_source is None:
        python_source, spans, diagnostics = _python_projection(revision)
    else:
        spans = _source_spans(revision, python_source)
        diagnostics = []
    revision = revision.model_copy(
        update={
            "source_artifact": WorkflowSourceArtifact.model_validate(
                _source_artifact(python_source, source_uri)
            )
        }
    )
    candidate = _candidate(
        base_revision_id=base_revision_id,
        revision=revision,
        python_source=python_source,
        authoring_surface="graph",
        diagnostics=diagnostics,
        source_map=spans,
    )
    return _result(
        base_revision_id,
        candidate=candidate,
        diagnostics=diagnostics,
    )


__all__ = ["generate_python_revision"]
