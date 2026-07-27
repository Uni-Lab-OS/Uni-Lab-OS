import ast
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .bindings import Binding, LiteralValue, NodeOutputRef, RuntimeParameterRef
from .canonical import (
    ActionInvocation,
    ControlEdge,
    SourceMap,
    SourceMapEntry,
    WorkflowParameter,
    WorkflowRevision,
    WorkflowSourceArtifact,
)

Json = Dict[str, Any]
STATIC_RANGE_EXPANSION_LIMIT = 1_000
MAX_COMPILED_NODES = 10_000
WorkflowSourceResolver = Callable[[str, str], str | None]


@dataclass(frozen=True)
class _ImportedWorkflow:
    workflow_id: str
    module: str
    symbol: str
    document: dict[str, Any]
    outputs: tuple[str, ...]


class PythonWorkflowCompileError(ValueError):
    """Stable fail-closed error for the AST-only Python authoring boundary."""

    def __init__(self, message: str, *, node: ast.AST | None = None):
        super().__init__(message)
        if node is None:
            return
        self.lineno = int(getattr(node, "lineno", 1))
        self.offset = int(getattr(node, "col_offset", 0)) + 1
        self.end_lineno = int(getattr(node, "end_lineno", self.lineno))
        self.end_offset = int(getattr(node, "end_col_offset", self.offset)) + 1


# ---------------- Converter ----------------


class DeviceMethodConverter:
    """
    - 字段统一：resource_name（原 device_class）、template_name（原 action_key）
    - params 单层；inputs 使用 'params.' 前缀
    - SimpleGraph.add_workflow_node 负责变量连线与边
    """

    def __init__(self, device_registry: Optional[Dict[str, Any]] = None):
        from .common import RegistryAdapter, WorkflowGraph

        self.graph = WorkflowGraph()
        self.variable_sources: Dict[
            str, Dict[str, Any]
        ] = {}  # var -> {node_id, output_name}
        self.instance_to_resource: Dict[
            str, Optional[str]
        ] = {}  # 实例名 -> resource_name
        self.node_id_counter: int = 0
        self.registry = RegistryAdapter(device_registry or {})

    # ---- helpers ----
    def _new_node_id(self) -> int:
        nid = self.node_id_counter
        self.node_id_counter += 1
        return nid

    def _assign_targets(self, targets) -> List[str]:
        names: List[str] = []
        import ast

        if isinstance(targets, ast.Tuple):
            for elt in targets.elts:
                if isinstance(elt, ast.Name):
                    names.append(elt.id)
        elif isinstance(targets, ast.Name):
            names.append(targets.id)
        return names

    def _extract_device_instantiation(self, node) -> Optional[Tuple[str, str]]:
        import ast

        if not isinstance(node.value, ast.Call):
            return None
        callee = node.value.func
        if isinstance(callee, ast.Name):
            class_name = callee.id
        elif isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name):
            class_name = callee.attr
        else:
            return None
        if isinstance(node.targets[0], ast.Name):
            instance = node.targets[0].id
            return instance, class_name
        return None

    def _extract_call(self, call) -> Tuple[str, str, Dict[str, Any], str]:
        import ast

        owner_name, method_name, call_kind = "", "", "func"
        if isinstance(call.func, ast.Attribute):
            method_name = call.func.attr
            if isinstance(call.func.value, ast.Name):
                owner_name = call.func.value.id
                call_kind = (
                    "instance"
                    if owner_name in self.instance_to_resource
                    else "class_or_module"
                )
            elif isinstance(call.func.value, ast.Attribute) and isinstance(
                call.func.value.value, ast.Name
            ):
                owner_name = call.func.value.attr
                call_kind = "class_or_module"
        elif isinstance(call.func, ast.Name):
            method_name = call.func.id
            call_kind = "func"

        def pack(node):
            if isinstance(node, ast.Name):
                return {"type": "variable", "value": node.id}
            if isinstance(node, ast.Constant):
                return {"type": "constant", "value": node.value}
            if isinstance(node, ast.Dict):
                return {"type": "dict", "value": self._parse_dict(node)}
            if isinstance(node, ast.List):
                return {"type": "list", "value": self._parse_list(node)}
            return {
                "type": "raw",
                "value": ast.unparse(node) if hasattr(ast, "unparse") else str(node),
            }

        args: Dict[str, Any] = {}
        pos: List[Any] = []
        for a in call.args:
            pos.append(pack(a))
        for kw in call.keywords:
            args[kw.arg] = pack(kw.value)
        if pos:
            args["_positional"] = pos
        return owner_name, method_name, args, call_kind

    def _parse_dict(self, node) -> Dict[str, Any]:
        import ast

        out: Dict[str, Any] = {}
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant):
                key = str(k.value)
                if isinstance(v, ast.Name):
                    out[key] = f"var:{v.id}"
                elif isinstance(v, ast.Constant):
                    out[key] = v.value
                elif isinstance(v, ast.Dict):
                    out[key] = self._parse_dict(v)
                elif isinstance(v, ast.List):
                    out[key] = self._parse_list(v)
        return out

    def _parse_list(self, node) -> List[Any]:
        import ast

        out: List[Any] = []
        for elt in node.elts:
            if isinstance(elt, ast.Name):
                out.append(f"var:{elt.id}")
            elif isinstance(elt, ast.Constant):
                out.append(elt.value)
            elif isinstance(elt, ast.Dict):
                out.append(self._parse_dict(elt))
            elif isinstance(elt, ast.List):
                out.append(self._parse_list(elt))
        return out

    def _normalize_var_tokens(self, x: Any) -> Any:
        if isinstance(x, str) and x.startswith("var:"):
            return {"__var__": x[4:]}
        if isinstance(x, list):
            return [self._normalize_var_tokens(i) for i in x]
        if isinstance(x, dict):
            return {k: self._normalize_var_tokens(v) for k, v in x.items()}
        return x

    def _make_params_payload(
        self,
        resource_name: Optional[str],
        template_name: str,
        call_args: Dict[str, Any],
    ) -> Dict[str, Any]:
        input_keys = (
            self.registry.get_action_input_keys(resource_name, template_name)
            if resource_name
            else []
        )
        defaults = (
            self.registry.get_action_goal_default(resource_name, template_name)
            if resource_name
            else {}
        )
        params: Dict[str, Any] = dict(defaults)

        def unpack(p):
            t, v = p.get("type"), p.get("value")
            if t == "variable":
                return {"__var__": v}
            if t == "dict":
                return self._normalize_var_tokens(v)
            if t == "list":
                return self._normalize_var_tokens(v)
            return v

        for k, p in call_args.items():
            if k == "_positional":
                continue
            params[k] = unpack(p)

        pos = call_args.get("_positional", [])
        if pos:
            if input_keys:
                for i, p in enumerate(pos):
                    if i >= len(input_keys):
                        break
                    name = input_keys[i]
                    if name in params:
                        continue
                    params[name] = unpack(p)
            else:
                for i, p in enumerate(pos):
                    params[f"arg_{i}"] = unpack(p)
        return params

    # ---- handlers ----
    def _on_assign(self, stmt):
        import ast

        inst = self._extract_device_instantiation(stmt)
        if inst:
            instance, code_class = inst
            resource_name = self.registry.resolve_resource_by_classname(code_class)
            self.instance_to_resource[instance] = resource_name
            return

        if isinstance(stmt.value, ast.Call):
            owner, method, call_args, kind = self._extract_call(stmt.value)
            if kind == "instance":
                device_key = owner
                resource_name = self.instance_to_resource.get(owner)
            else:
                device_key = owner
                resource_name = self.registry.resolve_resource_by_classname(owner)

            module = self.registry.get_device_module(resource_name)
            params = self._make_params_payload(resource_name, method, call_args)

            nid = self._new_node_id()
            self.graph.add_workflow_node(
                nid,
                device_key=device_key,
                resource_name=resource_name,  # ✅
                module=module,
                template_name=method,  # ✅
                params=params,
                variable_sources=self.variable_sources,
                add_ready_if_no_vars=True,
                prev_node_id=(nid - 1) if nid > 0 else None,
            )

            out_vars = self._assign_targets(stmt.targets[0])
            for var in out_vars:
                self.variable_sources[var] = {"node_id": nid, "output_name": "result"}

    def _on_expr(self, stmt):
        import ast

        if not isinstance(stmt.value, ast.Call):
            return
        owner, method, call_args, kind = self._extract_call(stmt.value)
        if kind == "instance":
            device_key = owner
            resource_name = self.instance_to_resource.get(owner)
        else:
            device_key = owner
            resource_name = self.registry.resolve_resource_by_classname(owner)

        module = self.registry.get_device_module(resource_name)
        params = self._make_params_payload(resource_name, method, call_args)

        nid = self._new_node_id()
        self.graph.add_workflow_node(
            nid,
            device_key=device_key,
            resource_name=resource_name,  # ✅
            module=module,
            template_name=method,  # ✅
            params=params,
            variable_sources=self.variable_sources,
            add_ready_if_no_vars=True,
            prev_node_id=(nid - 1) if nid > 0 else None,
        )

    def convert(self, python_code: str):
        tree = ast.parse(python_code)
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign):
                self._on_assign(stmt)
            elif isinstance(stmt, ast.Expr):
                self._on_expr(stmt)
        return self


def _canonical_action_ref(call: ast.Call) -> str:
    if not isinstance(call.func, ast.Attribute):
        raise ValueError(
            "workflow calls must use owner.action(...) or device(...).action(...)"
        )
    owner = call.func.value
    if isinstance(owner, ast.Name):
        return f"{owner.id}.{call.func.attr}"
    if (
        isinstance(owner, ast.Call)
        and isinstance(owner.func, ast.Name)
        and owner.func.id == "device"
        and len(owner.args) == 1
        and not owner.keywords
        and isinstance(owner.args[0], ast.Constant)
        and isinstance(owner.args[0].value, str)
        and owner.args[0].value
    ):
        return f"{owner.args[0].value}.{call.func.attr}"
    raise ValueError(
        "workflow calls must use owner.action(...) or "
        "device('exact-device-id').action(...)"
    )


def _canonical_binding(
    node: ast.expr,
    variables: Dict[str, Binding],
    static_values: Dict[str, Any],
):
    if isinstance(node, ast.Name):
        if node.id in static_values:
            return LiteralValue(value=static_values[node.id])
        if node.id not in variables:
            raise ValueError(f"unknown workflow value {node.id!r}")
        return variables[node.id]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "RuntimeParameter"
    ):
        if not node.args or not isinstance(node.args[0], ast.Constant):
            raise ValueError("RuntimeParameter requires a literal parameter name")
        default = None
        for keyword in node.keywords:
            if keyword.arg == "default":
                default = ast.literal_eval(keyword.value)
        return RuntimeParameterRef(parameter=str(node.args[0].value), default=default)
    try:
        return LiteralValue(value=ast.literal_eval(node))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"unsupported workflow binding: {ast.unparse(node)}") from exc


def _node_stem(action_ref: str) -> str:
    action_name = action_ref.rsplit(".", 1)[-1]
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", action_name).strip("-").lower()
    return stem or "action"


def _workflow_decorator_values(
    function: ast.FunctionDef,
    *,
    fallback_workflow_id: str,
    fallback_revision_id: str,
) -> tuple[str, str, dict[str, dict[str, Any]]]:
    workflow_id = fallback_workflow_id
    revision_id = fallback_revision_id
    parameter_ui: dict[str, dict[str, Any]] = {}
    decorator = next(
        (
            item
            for item in function.decorator_list
            if isinstance(item, ast.Call)
            and isinstance(item.func, ast.Name)
            and item.func.id == "workflow_definition"
        ),
        None,
    )
    if decorator is None:
        raise ValueError("workflow function requires @workflow_definition")
    for keyword in decorator.keywords:
        if keyword.arg not in {"workflow_id", "revision", "parameter_ui"}:
            raise ValueError(f"unsupported workflow_definition field {keyword.arg!r}")
        try:
            value = ast.literal_eval(keyword.value)
        except (ValueError, TypeError) as exc:
            raise ValueError("workflow_definition values must be literals") from exc
        if keyword.arg == "parameter_ui":
            if not isinstance(value, dict) or any(
                not isinstance(name, str) or not isinstance(metadata, dict)
                for name, metadata in value.items()
            ):
                raise ValueError("workflow_definition parameter_ui must be a mapping")
            parameter_ui = value
            continue
        if not isinstance(value, str) or not value:
            raise ValueError("workflow_definition values must be non-empty strings")
        if keyword.arg == "workflow_id":
            workflow_id = value
        else:
            revision_id = value
    return workflow_id, revision_id, parameter_ui


def _workflow_function_parameters(
    function: ast.FunctionDef,
    *,
    parameter_ui: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[WorkflowParameter], dict[str, RuntimeParameterRef]]:
    if function.args.posonlyargs or function.args.vararg or function.args.kwarg:
        raise ValueError("workflow parameters must be ordinary named arguments")
    arguments = [*function.args.args, *function.args.kwonlyargs]
    positional_default_offset = len(function.args.args) - len(function.args.defaults)
    defaults: dict[str, ast.expr] = {
        argument.arg: function.args.defaults[index - positional_default_offset]
        for index, argument in enumerate(function.args.args)
        if index >= positional_default_offset
    }
    defaults.update(
        {
            argument.arg: default
            for argument, default in zip(
                function.args.kwonlyargs,
                function.args.kw_defaults,
            )
            if default is not None
        }
    )
    type_map = {
        "str": "string",
        "int": "integer",
        "float": "number",
        "bool": "boolean",
    }
    parameters: list[WorkflowParameter] = []
    bindings: dict[str, RuntimeParameterRef] = {}
    ui_by_name = dict(parameter_ui or {})
    unknown_ui = sorted(set(ui_by_name) - {argument.arg for argument in arguments})
    if unknown_ui:
        raise ValueError(
            f"workflow parameter_ui references unknown parameter {unknown_ui[0]!r}"
        )
    for argument in arguments:
        if not isinstance(argument.annotation, ast.Name):
            raise ValueError(
                f"workflow parameter {argument.arg!r} requires a type annotation"
            )
        parameter_type = type_map.get(argument.annotation.id)
        if parameter_type is None:
            raise ValueError(
                f"unsupported workflow parameter type: {argument.annotation.id}"
            )
        ui = ui_by_name.get(argument.arg, {})
        unknown_ui_fields = sorted(set(ui) - {"title", "description"})
        if unknown_ui_fields:
            raise ValueError(
                f"unsupported workflow parameter_ui field {unknown_ui_fields[0]!r}"
            )
        values: dict[str, Any] = {
            "name": argument.arg,
            "type": parameter_type,
            "required": argument.arg not in defaults,
            "title": str(ui.get("title") or argument.arg),
            "description": str(ui.get("description") or ""),
        }
        if argument.arg in defaults:
            try:
                values["default"] = ast.literal_eval(defaults[argument.arg])
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"workflow parameter {argument.arg!r} default must be a literal"
                ) from exc
        parameters.append(WorkflowParameter(**values))
        bindings[argument.arg] = RuntimeParameterRef(parameter=argument.arg)
    return parameters, bindings


def _workflow_return_spec(
    function: ast.FunctionDef,
) -> tuple[tuple[str, ast.expr], ...]:
    """Read named final outputs without treating return as executable code."""

    statements = [
        statement
        for statement in function.body
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        )
    ]
    nested_returns = [
        node
        for statement in statements
        for node in ast.walk(statement)
        if isinstance(node, ast.Return)
    ]
    if not nested_returns:
        return ()
    if (
        len(nested_returns) != 1
        or not isinstance(statements[-1], ast.Return)
        or nested_returns[0] is not statements[-1]
    ):
        raise ValueError("workflow return must be one final top-level statement")
    value = nested_returns[0].value
    if value is None:
        return ()
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "workflow_output"
    ):
        if value.args or any(keyword.arg is None for keyword in value.keywords):
            raise ValueError(
                "workflow_output requires named arguments without unpacking"
            )
        names = [
            str(keyword.arg)
            for keyword in value.keywords
            if keyword.arg is not None
        ]
        if not names or len(set(names)) != len(names):
            raise ValueError("workflow_output names must be non-empty and unique")
        return tuple(
            (str(keyword.arg), keyword.value)
            for keyword in value.keywords
            if keyword.arg is not None
        )
    expressions = list(value.elts) if isinstance(value, ast.Tuple) else [value]
    if not expressions or any(not isinstance(item, ast.Name) for item in expressions):
        raise ValueError(
            "workflow return must use workflow_output(name=value)"
        )
    names = tuple(item.id for item in expressions if isinstance(item, ast.Name))
    if len(set(names)) != len(names):
        raise ValueError("workflow return values must be unique")
    return tuple(
        (item.id, item)
        for item in expressions
        if isinstance(item, ast.Name)
    )


def _workflow_return_names(function: ast.FunctionDef) -> tuple[str, ...]:
    return tuple(name for name, _ in _workflow_return_spec(function))


def _workflow_executable_body(function: ast.FunctionDef) -> list[ast.stmt]:
    outputs = _workflow_return_names(function)
    if not outputs:
        return list(function.body)
    return list(function.body[:-1])


def _workflow_raw_parameters(
    function: ast.FunctionDef,
    *,
    parameter_ui: Mapping[str, Mapping[str, Any]],
) -> tuple[list[WorkflowParameter], list[dict[str, Any]]]:
    parameters, _ = _workflow_function_parameters(
        function,
        parameter_ui=parameter_ui,
    )
    parameter_names = {parameter.name for parameter in parameters}
    assigned_names = {
        target.id
        for node in ast.walk(function)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    rebound = sorted(assigned_names & parameter_names)
    if rebound:
        raise ValueError(f"workflow parameter cannot be rebound: {rebound[0]}")
    output_names = set(_workflow_return_names(function))
    type_map = {
        "string": "STRING",
        "integer": "INT",
        "number": "FLOAT",
        "boolean": "BOOL",
    }
    raw_parameters: list[dict[str, Any]] = []
    for parameter in parameters:
        raw: dict[str, Any] = {
            "name": parameter.name,
            "scope": "local",
            "type": type_map[parameter.type],
            "io": "in",
        }
        if "default" in parameter.model_fields_set:
            raw["default"] = parameter.default
        if parameter.title != parameter.name:
            raw["ui"] = {"label": parameter.title}
        if parameter.description:
            raw["comment"] = parameter.description
        raw_parameters.append(raw)
    for name in sorted((assigned_names | output_names) - parameter_names):
        raw_parameters.append(
            {
                "name": name,
                "scope": "local",
                "type": "DICT",
                "io": "out" if name in output_names else "var",
                "default": {},
            }
        )
    return parameters, raw_parameters


_PYTHON_EXPRESSION_BINARY_OPERATORS: dict[type[ast.operator | ast.cmpop], str] = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
    ast.FloorDiv: "//",
    ast.Mod: "%",
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Lt: "<",
    ast.LtE: "<=",
}


def _python_expression(
    node: ast.expr,
    *,
    static_values: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Lower the closed Python expression subset to the shared safe IR."""

    values = static_values or {}
    if isinstance(node, ast.Name):
        if node.id in values:
            return {"lit": values[node.id]}
        return {"var": node.id}
    if isinstance(node, ast.Constant):
        return {"lit": node.value}
    if isinstance(node, ast.Attribute):
        return {
            "field": _python_expression(node.value, static_values=values),
            "name": node.attr,
        }
    if isinstance(node, ast.Subscript):
        return {
            "index": _python_expression(node.value, static_values=values),
            "key": _python_expression(node.slice, static_values=values),
        }
    if isinstance(node, ast.BinOp):
        operator_name = _PYTHON_EXPRESSION_BINARY_OPERATORS.get(type(node.op))
        if operator_name is None:
            raise ValueError(
                f"unsupported workflow binary operator: {type(node.op).__name__}"
            )
        return {
            "binop": operator_name,
            "left": _python_expression(node.left, static_values=values),
            "right": _python_expression(node.right, static_values=values),
        }
    if isinstance(node, ast.BoolOp):
        operator_name = "and" if isinstance(node.op, ast.And) else "or"
        expression = _python_expression(node.values[0], static_values=values)
        for item in node.values[1:]:
            expression = {
                "binop": operator_name,
                "left": expression,
                "right": _python_expression(item, static_values=values),
            }
        return expression
    if isinstance(node, ast.Compare) and len(node.ops) == len(node.comparators) == 1:
        operator_name = _PYTHON_EXPRESSION_BINARY_OPERATORS.get(type(node.ops[0]))
        if operator_name is None:
            raise ValueError(
                f"unsupported workflow comparison: {type(node.ops[0]).__name__}"
            )
        return {
            "binop": operator_name,
            "left": _python_expression(node.left, static_values=values),
            "right": _python_expression(node.comparators[0], static_values=values),
        }
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            operator_name = "not"
        elif isinstance(node.op, ast.USub):
            operator_name = "neg"
        else:
            raise ValueError(
                f"unsupported workflow unary operator: {type(node.op).__name__}"
            )
        return {
            "unop": operator_name,
            "operand": _python_expression(node.operand, static_values=values),
        }
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.keywords:
            raise ValueError("safe workflow expression calls require positional args")
        return {
            "call": node.func.id,
            "args": [
                _python_expression(argument, static_values=values)
                for argument in node.args
            ],
        }
    raise ValueError(f"unsupported workflow expression: {ast.unparse(node)}")


def _static_iter_values(iterator: ast.expr) -> list[Any]:
    if isinstance(iterator, (ast.List, ast.Tuple)):
        if len(iterator.elts) > STATIC_RANGE_EXPANSION_LIMIT:
            raise ValueError(
                "literal loop exceeds static expansion limit "
                f"{STATIC_RANGE_EXPANSION_LIMIT}"
            )
        return [ast.literal_eval(item) for item in iterator.elts]
    if (
        isinstance(iterator, ast.Call)
        and isinstance(iterator.func, ast.Name)
        and iterator.func.id == "range"
        and not iterator.keywords
    ):
        args = [ast.literal_eval(item) for item in iterator.args]
        if not all(isinstance(item, int) for item in args):
            raise ValueError("finite range arguments must be integer literals")
        candidate = range(*args)
        if len(candidate) > STATIC_RANGE_EXPANSION_LIMIT:
            raise ValueError(
                f"range exceeds static expansion limit {STATIC_RANGE_EXPANSION_LIMIT}"
            )
        return list(candidate)
    raise ValueError("for loops require a literal collection or finite range")


def _validate_action_input_names(
    action_ref: str,
    input_names: set[str],
    *,
    action_catalog: Mapping[str, Mapping[str, Any]],
) -> None:
    """Treat the registry input contract as authoritative at compile time."""

    raw_inputs = action_catalog[action_ref].get("inputs", {})
    declared_inputs = set(raw_inputs) if isinstance(raw_inputs, Mapping) else set()
    unknown = sorted(input_names - declared_inputs)
    if unknown:
        raise ValueError(f"unknown input {unknown[0]!r} for action {action_ref!r}")


def _python_call_operation(
    call: ast.Call,
    *,
    action_catalog: Mapping[str, Mapping[str, Any]],
    assignment: str | None,
    statement: ast.stmt,
    static_values: Mapping[str, Any],
) -> dict[str, Any]:
    action_ref = _canonical_action_ref(call)
    if call.args:
        raise ValueError("canonical workflow calls require named arguments")
    keywords = {
        keyword.arg: keyword.value
        for keyword in call.keywords
        if keyword.arg is not None
    }
    if len(keywords) != len(call.keywords):
        raise ValueError("workflow calls do not support **kwargs")
    source = {
        "_source_line": statement.lineno,
        "_source_column": statement.col_offset,
    }
    if action_ref == "host_node.manual_confirm":
        if assignment is not None:
            raise ValueError("manual_confirm cannot be assigned")
        unknown = sorted(set(keywords) - {"prompt", "on_cancel"})
        if unknown:
            raise ValueError(f"manual_confirm argument is unsupported: {unknown[0]}")
        return {
            **source,
            "op": "human",
            "kind": "confirm",
            "prompt": _python_expression(
                keywords.get("prompt", ast.Constant(value="")),
                static_values=static_values,
            ),
            "on_cancel": ast.literal_eval(
                keywords.get("on_cancel", ast.Constant(value="raise"))
            ),
        }
    if action_ref not in action_catalog:
        raise ValueError(f"unknown action {action_ref!r}")
    _validate_action_input_names(
        action_ref,
        set(keywords),
        action_catalog=action_catalog,
    )
    operation: dict[str, Any] = {
        **source,
        "op": "call",
        "action": action_ref,
        "args": {
            name: _python_expression(value, static_values=static_values)
            for name, value in keywords.items()
        },
    }
    if assignment is not None:
        operation["assign"] = {"var": assignment}
    return operation


def _located_python_call_operation(
    call: ast.Call,
    *,
    action_catalog: Mapping[str, Mapping[str, Any]],
    assignment: str | None,
    statement: ast.stmt,
    static_values: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep semantic call failures attached to their authoring statement."""

    try:
        return _python_call_operation(
            call,
            action_catalog=action_catalog,
            assignment=assignment,
            statement=statement,
            static_values=static_values,
        )
    except PythonWorkflowCompileError:
        raise
    except (OverflowError, TypeError, ValueError) as error:
        raise PythonWorkflowCompileError(str(error), node=statement) from error


def _python_subworkflow_operation(
    call: ast.Call,
    *,
    workflow: _ImportedWorkflow,
    assignment: ast.expr | None,
    statement: ast.stmt,
    static_values: Mapping[str, Any],
) -> dict[str, Any]:
    if call.args:
        raise ValueError("subworkflow calls require named arguments")
    if any(keyword.arg is None for keyword in call.keywords):
        raise ValueError("subworkflow calls do not support **kwargs")
    input_definitions = {
        str(raw["name"]): raw
        for raw in workflow.document.get("vars", [])
        if isinstance(raw, Mapping) and raw.get("io") == "in" and raw.get("name")
    }
    keywords = {
        str(keyword.arg): keyword.value
        for keyword in call.keywords
        if keyword.arg is not None
    }
    unknown = sorted(set(keywords) - set(input_definitions))
    if unknown:
        raise ValueError(
            f"subworkflow {workflow.workflow_id!r} has no input {unknown[0]!r}"
        )
    missing = sorted(
        name
        for name, definition in input_definitions.items()
        if "default" not in definition and name not in keywords
    )
    if missing:
        raise ValueError(
            f"subworkflow {workflow.workflow_id!r} requires input {missing[0]!r}"
        )

    targets: tuple[str, ...] = ()
    if assignment is not None:
        expressions = (
            list(assignment.elts)
            if isinstance(assignment, (ast.Tuple, ast.List))
            else [assignment]
        )
        if any(not isinstance(item, ast.Name) for item in expressions):
            raise ValueError(
                "subworkflow assignment requires simple variable names"
            )
        targets = tuple(
            item.id for item in expressions if isinstance(item, ast.Name)
        )
    if len(targets) != len(workflow.outputs):
        if workflow.outputs:
            raise ValueError(
                f"subworkflow {workflow.workflow_id!r} returns "
                f"{len(workflow.outputs)} value(s)"
            )
        raise ValueError(
            f"subworkflow {workflow.workflow_id!r} does not return a value"
        )
    return {
        "op": "run_script",
        "script": workflow.workflow_id,
        "module": workflow.module,
        "callable": workflow.symbol,
        "inputs": {
            name: _python_expression(value, static_values=static_values)
            for name, value in keywords.items()
        },
        "outputs": {
            output: {"var": target}
            for output, target in zip(workflow.outputs, targets)
        },
        "_source_line": statement.lineno,
        "_source_column": statement.col_offset,
    }


def _python_block_operations(
    statements: list[ast.stmt],
    *,
    action_catalog: Mapping[str, Mapping[str, Any]],
    imported_workflows: Mapping[str, _ImportedWorkflow] | None = None,
    static_values: Mapping[str, Any] | None = None,
    node_budget: list[int] | None = None,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    values = dict(static_values or {})
    subworkflows = dict(imported_workflows or {})
    budget = node_budget if node_budget is not None else [MAX_COMPILED_NODES]

    def consume_nodes(count: int) -> None:
        budget[0] -= count
        if budget[0] < 0:
            raise ValueError(
                f"workflow exceeds compiled node limit {MAX_COMPILED_NODES}"
            )

    for statement in statements:
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            if (
                isinstance(statement.value.func, ast.Name)
                and statement.value.func.id in subworkflows
            ):
                consume_nodes(1)
                operations.append(
                    _python_subworkflow_operation(
                        statement.value,
                        workflow=subworkflows[statement.value.func.id],
                        assignment=None,
                        statement=statement,
                        static_values=values,
                    )
                )
                continue
            consume_nodes(1)
            operations.append(
                _located_python_call_operation(
                    statement.value,
                    action_catalog=action_catalog,
                    assignment=None,
                    statement=statement,
                    static_values=values,
                )
            )
            continue
        if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Call):
            if len(statement.targets) != 1 or not isinstance(
                statement.targets[0], (ast.Name, ast.Tuple, ast.List)
            ):
                raise ValueError(
                    "workflow assignments require one simple target"
                )
            if (
                isinstance(statement.value.func, ast.Name)
                and statement.value.func.id in subworkflows
            ):
                consume_nodes(1)
                operations.append(
                    _python_subworkflow_operation(
                        statement.value,
                        workflow=subworkflows[statement.value.func.id],
                        assignment=statement.targets[0],
                        statement=statement,
                        static_values=values,
                    )
                )
                continue
            if not isinstance(statement.targets[0], ast.Name):
                raise ValueError(
                    "action assignments require one simple variable"
                )
            consume_nodes(1)
            operations.append(
                _located_python_call_operation(
                    statement.value,
                    action_catalog=action_catalog,
                    assignment=statement.targets[0].id,
                    statement=statement,
                    static_values=values,
                )
            )
            continue
        if isinstance(statement, ast.If):
            consume_nodes(2)
            operations.append(
                {
                    "op": "if",
                    "cond": _python_expression(statement.test, static_values=values),
                    "then": _python_block_operations(
                        statement.body,
                        action_catalog=action_catalog,
                        imported_workflows=subworkflows,
                        static_values=values,
                        node_budget=budget,
                    ),
                    "else": _python_block_operations(
                        statement.orelse,
                        action_catalog=action_catalog,
                        imported_workflows=subworkflows,
                        static_values=values,
                        node_budget=budget,
                    ),
                    "_source_line": statement.lineno,
                    "_source_column": statement.col_offset,
                }
            )
            continue
        if isinstance(statement, ast.Try):
            if statement.handlers or statement.orelse or not statement.finalbody:
                raise ValueError("workflow try only supports a non-empty finally block")
            control_nodes = (
                ast.If,
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.Try,
                ast.Match,
            )
            if any(
                isinstance(node, control_nodes)
                for child in statement.finalbody
                for node in ast.walk(child)
            ):
                raise ValueError("workflow finally cannot contain control flow")
            operations.append(
                {
                    "op": "try",
                    "body": _python_block_operations(
                        statement.body,
                        action_catalog=action_catalog,
                        imported_workflows=subworkflows,
                        static_values=values,
                        node_budget=budget,
                    ),
                    "finally": _python_block_operations(
                        statement.finalbody,
                        action_catalog=action_catalog,
                        imported_workflows=subworkflows,
                        static_values=values,
                        node_budget=budget,
                    ),
                    "_source_line": statement.lineno,
                    "_source_column": statement.col_offset,
                }
            )
            continue
        if isinstance(statement, ast.For) and isinstance(statement.target, ast.Name):
            if statement.orelse:
                raise ValueError("workflow for loops do not support else")
            for value in _static_iter_values(statement.iter):
                iteration_values = {**values, statement.target.id: value}
                operations.extend(
                    _python_block_operations(
                        statement.body,
                        action_catalog=action_catalog,
                        imported_workflows=subworkflows,
                        static_values=iteration_values,
                        node_budget=budget,
                    )
                )
            continue
        if isinstance(statement, ast.With):
            if (
                len(statement.items) != 1
                or statement.items[0].optional_vars is not None
            ):
                raise ValueError("workflow blocks require one unbound context")
            context = statement.items[0].context_expr
            if not isinstance(context, ast.Call) or not isinstance(
                context.func, ast.Name
            ):
                raise ValueError("workflow block must be group(...) or parallel()")
            if context.func.id == "group":
                if context.args:
                    raise ValueError("group requires a named keyword")
                keywords = {
                    keyword.arg: keyword.value
                    for keyword in context.keywords
                    if keyword.arg is not None
                }
                if set(keywords) != {"name"}:
                    raise ValueError("group requires exactly name=...")
                name = ast.literal_eval(keywords["name"])
                if not isinstance(name, str) or not name:
                    raise ValueError("group name must be a non-empty string")
                consume_nodes(1)
                operations.append(
                    {
                        "op": "group",
                        "name": name,
                        "body": _python_block_operations(
                            statement.body,
                            action_catalog=action_catalog,
                            imported_workflows=subworkflows,
                            static_values=values,
                            node_budget=budget,
                        ),
                        "_source_line": statement.lineno,
                        "_source_column": statement.col_offset,
                    }
                )
                continue
            if context.func.id == "parallel":
                if context.args or context.keywords:
                    raise ValueError("parallel does not accept arguments")
                consume_nodes(1)
                operations.append(
                    {
                        "op": "parallel",
                        "body": _python_block_operations(
                            statement.body,
                            action_catalog=action_catalog,
                            imported_workflows=subworkflows,
                            static_values=values,
                            node_budget=budget,
                        ),
                        "_source_line": statement.lineno,
                        "_source_column": statement.col_offset,
                    }
                )
                continue
            raise ValueError("workflow block must be group(...) or parallel()")
        raise ValueError(
            "workflow source only supports action calls, assignments, if, "
            "try/finally, finite for loops, group, and parallel"
        )
    return operations


def _workflow_document(
    function: ast.FunctionDef,
    *,
    action_catalog: Mapping[str, Mapping[str, Any]],
    fallback_workflow_id: str,
    fallback_revision_id: str,
    imported_workflows: Mapping[str, _ImportedWorkflow],
) -> tuple[
    dict[str, Any],
    str,
    str,
    list[WorkflowParameter],
    tuple[str, ...],
]:
    workflow_id, revision_id, parameter_ui = _workflow_decorator_values(
        function,
        fallback_workflow_id=fallback_workflow_id,
        fallback_revision_id=fallback_revision_id,
    )
    parameters, raw_parameters = _workflow_raw_parameters(
        function,
        parameter_ui=parameter_ui,
    )
    document = {
        "schema": "unilab.python/v1",
        "kind": "operation",
        "name": workflow_id,
        "vars": raw_parameters,
        "body": _python_block_operations(
            _workflow_executable_body(function),
            action_catalog=action_catalog,
            imported_workflows=imported_workflows,
        ),
        "returns": {
            name: _python_expression(expression)
            for name, expression in _workflow_return_spec(function)
        },
    }
    return (
        document,
        workflow_id,
        revision_id,
        parameters,
        _workflow_return_names(function),
    )


def _workflow_function_from_module(
    tree: ast.Module,
    *,
    expected_symbol: str | None,
) -> ast.FunctionDef:
    functions = [
        item for item in tree.body if isinstance(item, ast.FunctionDef)
    ]
    if expected_symbol is None:
        if len(functions) != 1:
            raise ValueError("workflow source must contain exactly one function")
        return functions[0]
    matches = [item for item in functions if item.name == expected_symbol]
    if len(matches) != 1:
        raise ValueError(
            f"imported workflow module must define {expected_symbol!r}"
        )
    return matches[0]


def _resolve_imported_workflows(
    tree: ast.Module,
    *,
    action_catalog: Mapping[str, Mapping[str, Any]],
    workflow_source_resolver: WorkflowSourceResolver | None,
    documents: dict[str, dict[str, Any]],
    cache: dict[tuple[str, str], _ImportedWorkflow],
    stack: tuple[tuple[str, str], ...],
) -> dict[str, _ImportedWorkflow]:
    if workflow_source_resolver is None:
        return {}
    imported: dict[str, _ImportedWorkflow] = {}
    for statement in tree.body:
        if not isinstance(statement, ast.ImportFrom) or not statement.module:
            continue
        for alias in statement.names:
            source = workflow_source_resolver(statement.module, alias.name)
            if source is None:
                continue
            key = (statement.module, alias.name)
            local_name = alias.asname or alias.name
            if key in stack:
                chain = " -> ".join(
                    symbol for _, symbol in (*stack, key)
                )
                raise ValueError(f"recursive subworkflow import: {chain}")
            workflow = cache.get(key)
            if workflow is None:
                imported_tree = ast.parse(source)
                imported_function = _workflow_function_from_module(
                    imported_tree,
                    expected_symbol=alias.name,
                )
                nested = _resolve_imported_workflows(
                    imported_tree,
                    action_catalog=action_catalog,
                    workflow_source_resolver=workflow_source_resolver,
                    documents=documents,
                    cache=cache,
                    stack=(*stack, key),
                )
                (
                    document,
                    workflow_id,
                    _,
                    _,
                    outputs,
                ) = _workflow_document(
                    imported_function,
                    action_catalog=action_catalog,
                    fallback_workflow_id=alias.name,
                    fallback_revision_id="imported",
                    imported_workflows=nested,
                )
                existing = documents.get(workflow_id)
                if existing is not None and existing != document:
                    raise ValueError(
                        f"conflicting imported workflow {workflow_id!r}"
                    )
                documents[workflow_id] = document
                workflow = _ImportedWorkflow(
                    workflow_id=workflow_id,
                    module=statement.module,
                    symbol=alias.name,
                    document=document,
                    outputs=outputs,
                )
                cache[key] = workflow
            imported[local_name] = workflow
    return imported


def _compile_workflow_function(
    function: ast.FunctionDef,
    *,
    action_catalog: Mapping[str, Mapping[str, Any]],
    fallback_workflow_id: str,
    fallback_revision_id: str,
    source_artifact: WorkflowSourceArtifact | None,
    imported_workflows: Mapping[str, _ImportedWorkflow],
    imported_documents: Mapping[str, Mapping[str, Any]],
) -> WorkflowRevision:
    (
        document,
        _,
        revision_id,
        _,
        _,
    ) = _workflow_document(
        function,
        action_catalog=action_catalog,
        fallback_workflow_id=fallback_workflow_id,
        fallback_revision_id=fallback_revision_id,
        imported_workflows=imported_workflows,
    )
    from .operation_tree import compile_operation_tree

    revision = compile_operation_tree(
        document,
        resolver=lambda name: imported_documents[name],
    )
    return revision.model_copy(
        update={
            "revision_id": revision_id,
            "source_artifact": source_artifact,
        }
    )


def _compile_python_script(
    source: str,
    *,
    action_catalog: Dict[str, Dict[str, Any]],
    workflow_id: str = "python-workflow",
    revision_id: str = "draft",
    source_artifact: WorkflowSourceArtifact | Mapping[str, Any] | None = None,
    workflow_source_resolver: WorkflowSourceResolver | None = None,
) -> WorkflowRevision:
    """Compile compact, declarative Python calls to a Canonical revision.

    The source is parsed but never executed.  Assignment names become references
    to registry-declared named outputs, keeping authoring concise without hiding
    the binding contract used by the DAG executor.
    """

    tree = ast.parse(source)
    functions = [item for item in tree.body if isinstance(item, ast.FunctionDef)]
    artifact = (
        None
        if source_artifact is None
        else WorkflowSourceArtifact.model_validate(source_artifact)
    )
    if functions:
        allowed_top_level = (
            ast.FunctionDef,
            ast.Import,
            ast.ImportFrom,
            ast.Assign,
            ast.AnnAssign,
        )
        if len(functions) != 1 or any(
            not isinstance(item, allowed_top_level)
            and not (
                isinstance(item, ast.Expr)
                and isinstance(item.value, ast.Constant)
                and isinstance(item.value.value, str)
            )
            for item in tree.body
        ):
            raise ValueError("workflow source must contain exactly one function")
        imported_documents: dict[str, dict[str, Any]] = {}
        imported_workflows = _resolve_imported_workflows(
            tree,
            action_catalog=action_catalog,
            workflow_source_resolver=workflow_source_resolver,
            documents=imported_documents,
            cache={},
            stack=(),
        )
        return _compile_workflow_function(
            functions[0],
            action_catalog=action_catalog,
            fallback_workflow_id=workflow_id,
            fallback_revision_id=revision_id,
            source_artifact=artifact,
            imported_workflows=imported_workflows,
            imported_documents=imported_documents,
        )
    parameters: list[WorkflowParameter] | None = None
    workflow_parameter_names: set[str] = set()
    initial_variables: dict[str, Binding] = {}
    statements: list[ast.stmt] = list(tree.body)
    invocations: List[ActionInvocation] = []
    edges: List[ControlEdge] = []
    variables: Dict[str, Binding] = dict(initial_variables)
    source_entries: List[SourceMapEntry] = []

    def emit_call(
        call: ast.Call,
        *,
        targets: List[ast.expr],
        statement: ast.stmt,
        static_values: Dict[str, Any],
    ) -> None:
        action_ref = _canonical_action_ref(call)
        if action_ref not in action_catalog:
            raise ValueError(f"unknown action {action_ref!r}")
        if call.args:
            raise ValueError("canonical workflow calls require named arguments")
        if any(keyword.arg is None for keyword in call.keywords):
            raise ValueError("workflow calls do not support **kwargs keyword unpacking")
        if len(invocations) >= MAX_COMPILED_NODES:
            raise ValueError(
                f"workflow exceeds compiled node limit {MAX_COMPILED_NODES}"
            )

        node_id = f"{_node_stem(action_ref)}-{len(invocations) + 1}"
        input_bindings = {
            keyword.arg: _canonical_binding(keyword.value, variables, static_values)
            for keyword in call.keywords
            if keyword.arg is not None
        }
        catalog_entry = action_catalog[action_ref]
        _validate_action_input_names(
            action_ref,
            set(input_bindings),
            action_catalog=action_catalog,
        )
        output_schema = dict(catalog_entry.get("outputs", {}))
        invocations.append(
            ActionInvocation(
                node_id=node_id,
                action_ref=action_ref,
                input_bindings=input_bindings,
                output_schema=output_schema,
            )
        )
        source_entries.append(
            SourceMapEntry(
                node_id=node_id,
                line=statement.lineno,
                column=statement.col_offset,
            )
        )
        if len(invocations) > 1:
            edges.append(ControlEdge(source=invocations[-2].node_id, target=node_id))

        if not targets:
            return
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        if len(names) != len(targets):
            raise ValueError("workflow assignments require simple variable names")
        rebound = sorted(set(names) & workflow_parameter_names)
        if rebound:
            raise ValueError(f"workflow parameter cannot be rebound: {rebound[0]}")
        output_names = list(output_schema)
        if len(names) == 1 and len(output_names) == 1:
            variables[names[0]] = NodeOutputRef(
                node_id=node_id,
                output=output_names[0],
            )
        elif len(names) == len(output_names):
            for name, output_name in zip(names, output_names):
                variables[name] = NodeOutputRef(
                    node_id=node_id,
                    output=output_name,
                )
        else:
            raise ValueError(
                f"assignment for {action_ref!r} does not match named outputs"
            )

    def emit_statement(statement: ast.stmt, static_values: Dict[str, Any]) -> None:
        targets: List[ast.expr] = []
        if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Call):
            call = statement.value
            targets = list(statement.targets)
        elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            call = statement.value
        elif isinstance(statement, ast.For) and isinstance(statement.target, ast.Name):
            if statement.orelse:
                raise ValueError("workflow for loops do not support for-else")
            for value in _static_iter_values(statement.iter):
                iteration_values = dict(static_values)
                iteration_values[statement.target.id] = value
                for child in statement.body:
                    emit_statement(child, iteration_values)
            return
        else:
            raise ValueError(
                "workflow source only supports action calls, assignments, "
                "and finite for loops"
            )
        emit_call(
            call,
            targets=targets,
            statement=statement,
            static_values=static_values,
        )

    for statement in statements:
        emit_statement(statement, {})

    return WorkflowRevision(
        revision_id=revision_id,
        workflow_id=workflow_id,
        parameters=parameters,
        invocations=invocations,
        control_edges=edges,
        source_map=SourceMap(entries=source_entries),
        source_artifact=artifact,
    )


def compile_python_script(
    source: str,
    *,
    action_catalog: Dict[str, Dict[str, Any]],
    workflow_id: str = "python-workflow",
    revision_id: str = "draft",
    source_artifact: WorkflowSourceArtifact | Mapping[str, Any] | None = None,
    workflow_source_resolver: WorkflowSourceResolver | None = None,
) -> WorkflowRevision:
    """Compile Python authoring source without leaking parser/runtime errors."""

    try:
        return _compile_python_script(
            source,
            action_catalog=action_catalog,
            workflow_id=workflow_id,
            revision_id=revision_id,
            source_artifact=source_artifact,
            workflow_source_resolver=workflow_source_resolver,
        )
    except PythonWorkflowCompileError:
        raise
    except (SyntaxError, OverflowError, TypeError, ValueError) as exc:
        raise PythonWorkflowCompileError(str(exc)) from exc
