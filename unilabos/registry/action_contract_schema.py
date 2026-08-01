"""Action 参数与 named result 的纯 AST canonical contract facade。"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Never, Self

from unilabos.registry.action_result_schema import (
    ActionResultSchemaError,
    parse_action_result_declaration,
)
from unilabos.registry.annotation_schema import (
    NO_DEFAULT,
    AnnotationSchemaError,
    ResourceTemplateSymbol,
    parse_parameter_annotation,
)
from unilabos.registry.module_scope import (
    DefinitionNode,
    ModuleScopeError,
    resolve_module_scope,
)
from unilabos.registry.utils import parse_docstring
from unilabos.workflow.json_codec import strict_json_equal
from unilabos.workflow.schema import (
    WorkflowInputContract,
    WorkflowOutputContract,
    WorkflowSchemaError,
    parse_input_contract,
    parse_output_contract,
)

_ERROR_MESSAGE = "Action 定义不符合 Workflow 版本 1 合同"
_PARSED_ACTION_CONTRACT_TOKEN = object()
_FRAMEWORK_PARAMETERS = frozenset({"sample_uuids"})


class ActionContractError(ValueError):
    """可稳定投影为 Registry 或编译诊断的 Action Contract 错误。"""

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message


class ActionCompatibilityError(ValueError):
    """A legacy decorator assertion conflicts with the canonical schema."""

    def __init__(self, code: str, path: str) -> None:
        super().__init__(code)
        self.code = code
        self.path = path
        self.message = "Action 兼容声明与 canonical contract 冲突"


@dataclass(frozen=True, slots=True, init=False)
class ParsedActionContract:
    """一个不保留源码语法形式的不可变 Action Contract。"""

    _input_contract: WorkflowInputContract
    _output_contract: WorkflowOutputContract
    input_resource_templates: tuple[
        tuple[str, tuple[ResourceTemplateSymbol, ...]],
        ...,
    ]
    output_resource_templates: tuple[
        tuple[str, tuple[ResourceTemplateSymbol, ...]],
        ...,
    ]

    def __new__(cls, *_args: Any, **_kwargs: Any) -> Never:
        raise TypeError("请通过 parse_action_contract 创建 ParsedActionContract")

    @classmethod
    def _from_canonical(
        cls,
        input_contract: WorkflowInputContract,
        output_contract: WorkflowOutputContract,
        input_resource_templates: tuple[
            tuple[str, tuple[ResourceTemplateSymbol, ...]],
            ...,
        ],
        output_resource_templates: tuple[
            tuple[str, tuple[ResourceTemplateSymbol, ...]],
            ...,
        ],
        *,
        token: object,
    ) -> Self:
        if token is not _PARSED_ACTION_CONTRACT_TOKEN:
            raise TypeError("ParsedActionContract 只能由模块内 parser 创建")
        contract = object.__new__(cls)
        object.__setattr__(contract, "_input_contract", input_contract)
        object.__setattr__(contract, "_output_contract", output_contract)
        object.__setattr__(
            contract,
            "input_resource_templates",
            input_resource_templates,
        )
        object.__setattr__(
            contract,
            "output_resource_templates",
            output_resource_templates,
        )
        return contract

    def to_dict(self) -> dict[str, Any]:
        """返回与内部 canonical contracts 不共享容器的完整 descriptor。"""

        return {
            "input_contract": self._input_contract.to_dict(),
            "output_contract": self._output_contract.to_dict(),
        }

    def to_action_schema(
        self,
        *,
        action_name: str,
        description: str = "",
    ) -> dict[str, Any]:
        """Project the one canonical contract into the existing Action schema.

        The returned envelope is the only typed authority stored by
        PackageCatalog and Registry.  Order and source symbols live in the
        versioned extension; input/output contract dumps are intentionally not
        copied beside it.
        """

        descriptor = self.to_dict()
        inputs = descriptor["input_contract"]["parameters"]
        outputs = descriptor["output_contract"]["outputs"]
        goal_properties: dict[str, Any] = {}
        required_inputs: list[str] = []
        for parameter in inputs:
            name = parameter["name"]
            field = _action_value_schema(parameter["schema"])
            if "default" in parameter:
                field["default"] = parameter["default"]
            for key in ("title", "description"):
                if key in parameter:
                    field[key] = parameter[key]
            goal_properties[name] = field
            if parameter["required"]:
                required_inputs.append(name)

        result_properties: dict[str, Any] = {}
        for output in outputs:
            field = _action_value_schema(output["schema"])
            for key in ("title", "description"):
                if key in output:
                    field[key] = output[key]
            result_properties[output["name"]] = field

        return {
            "title": f"{action_name}参数",
            "description": description or f"{action_name}的参数schema",
            "type": "object",
            "properties": {
                "goal": {
                    "type": "object",
                    "properties": goal_properties,
                    "required": required_inputs,
                    "additionalProperties": False,
                },
                "feedback": {},
                "result": {
                    "type": "object",
                    "properties": result_properties,
                    "required": [output["name"] for output in outputs],
                    "additionalProperties": False,
                },
            },
            "required": ["goal"],
            "x-unilabos-action-contract": {
                "version": 1,
                "input_order": [parameter["name"] for parameter in inputs],
                "output_order": [output["name"] for output in outputs],
                "resource_template_symbols": {
                    "goal": _symbol_projection(self.input_resource_templates),
                    "result": _symbol_projection(self.output_resource_templates),
                },
            },
        }


def _action_value_schema(value: Mapping[str, Any]) -> dict[str, Any]:
    """Render the strict Workflow value schema as JSON Schema presentation."""

    rendered = {key: _copy_json(item) for key, item in value.items()}
    if rendered.get("type") == "object":
        rendered["additionalProperties"] = True
    return rendered


def _copy_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _copy_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_json(item) for item in value]
    return value


def _symbol_projection(
    groups: tuple[tuple[str, tuple[ResourceTemplateSymbol, ...]], ...],
) -> dict[str, list[str]]:
    return {
        name: [symbol.qualified_name for symbol in symbols]
        for name, symbols in groups
        if symbols
    }


def canonical_goal_defaults(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the compatibility default projection from one Action schema."""

    properties = schema["properties"]["goal"]["properties"]
    return {
        str(name): _copy_json(value["default"])
        for name, value in properties.items()
        if "default" in value
    }


def validate_legacy_action_assertions(
    schema: Mapping[str, Any],
    *,
    action_name: str,
    goal_default: Any = None,
    handles: Any = None,
) -> dict[str, Any]:
    """Validate legacy decorator values without merging a second contract."""

    defaults = canonical_goal_defaults(schema)
    if goal_default not in (None, {}):
        if not isinstance(goal_default, Mapping):
            _compatibility_fail(
                "action_default_contract_conflict",
                f"/actions/{action_name}/goal_default",
            )
        for key in sorted(set(goal_default) | set(defaults)):
            if (
                key not in goal_default
                or key not in defaults
                or not strict_json_equal(goal_default[key], defaults[key])
            ):
                _compatibility_fail(
                    "action_default_contract_conflict",
                    f"/actions/{action_name}/goal_default/{key}",
                )
    _validate_legacy_handles(handles, schema=schema, action_name=action_name)
    return defaults


def _validate_legacy_handles(
    raw: Any,
    *,
    schema: Mapping[str, Any],
    action_name: str,
) -> None:
    if raw in (None, [], {}):
        return
    if not isinstance(raw, list):
        _compatibility_fail(
            "action_handle_contract_conflict",
            f"/actions/{action_name}/handles",
        )
    actual: dict[tuple[str, str], tuple[str, str, str]] = {}
    for index, handle in enumerate(raw):
        if not isinstance(handle, Mapping):
            _compatibility_fail(
                "action_handle_contract_conflict",
                f"/actions/{action_name}/handles/{index}",
            )
        call = str(handle.get("$call") or handle.get("_call") or "")
        kwargs = handle.get("kwargs")
        values = kwargs if isinstance(kwargs, Mapping) else handle
        if call.endswith(("ActionInputHandle", "InputHandle")):
            io_type = "target"
            default_source = "goal"
        elif call.endswith(("ActionOutputHandle", "OutputHandle")):
            io_type = "source"
            default_source = "result"
        else:
            _compatibility_fail(
                "action_handle_contract_conflict",
                f"/actions/{action_name}/handles/{index}",
            )
        key = values.get("key")
        if not isinstance(key, str) or not key:
            _compatibility_fail(
                "action_handle_contract_conflict",
                f"/actions/{action_name}/handles/{index}",
            )
        actual[(io_type, key)] = (
            str(values.get("data_type") or ""),
            str(values.get("data_source") or default_source).lower(),
            str(values.get("data_key") or key),
        )

    expected: dict[tuple[str, str], tuple[str, str, str]] = {}
    goal = schema["properties"]["goal"]
    for name, value_schema in goal["properties"].items():
        expected[("target", name)] = (
            _legacy_value_type(value_schema),
            "goal",
            name,
        )
    result = schema["properties"]["result"]
    for name, value_schema in result["properties"].items():
        expected[("source", name)] = (
            _legacy_value_type(value_schema),
            "result",
            name,
        )
    for name, value_schema in goal["properties"].items():
        if (
            _base_value_schema(value_schema).get("$slot") == "ResourceSlot"
            and (
                "source",
                name,
            )
            not in expected
        ):
            expected[("source", name)] = ("ResourceSlot", "result", name)
    if actual != expected:
        _compatibility_fail(
            "action_handle_contract_conflict",
            f"/actions/{action_name}/handles",
        )


def _base_value_schema(schema: Mapping[str, Any]) -> Mapping[str, Any]:
    members = schema.get("anyOf")
    if isinstance(members, list):
        return next(
            (
                item
                for item in members
                if isinstance(item, Mapping) and item.get("type") != "null"
            ),
            {},
        )
    return schema


def _legacy_value_type(schema: Mapping[str, Any]) -> str:
    base = _base_value_schema(schema)
    if base.get("$slot") == "ResourceSlot":
        return "ResourceSlot"
    return str(base.get("type") or "object")


def _compatibility_fail(code: str, path: str) -> Never:
    raise ActionCompatibilityError(code, path)


def _fail(
    path: str,
    *,
    code: str = "invalid_action_contract",
    message: str = _ERROR_MESSAGE,
) -> Never:
    raise ActionContractError(code, path, message)


def _action_context(
    module: ast.Module,
    action: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    """返回 Action 是否为顶层 class 的直接 method，并验证真实包含关系。"""

    body = getattr(module, "body", None)
    if type(body) is not list:
        _fail(
            "/module/body",
            code="invalid_module_scope",
            message="模块作用域不符合 Workflow 静态解析合同",
        )
    for statement in body:
        if statement is action:
            return False
        if isinstance(statement, ast.ClassDef):
            class_body = getattr(statement, "body", None)
            if type(class_body) is not list:
                _fail("/action")
            if any(item is action for item in class_body):
                return True
    _fail("/action")


def _argument_list(value: object, path: str) -> list[ast.arg]:
    if type(value) is not list:
        _fail(path)
    arguments = value
    for index, argument in enumerate(arguments):
        if not isinstance(argument, ast.arg):
            _fail(f"{path}/{index}")
    return arguments


def _default_list(value: object, path: str) -> list[ast.expr]:
    if type(value) is not list:
        _fail(path)
    defaults = value
    for index, default in enumerate(defaults):
        if not isinstance(default, ast.expr):
            _fail(f"{path}/{index}")
    return defaults


def _keyword_defaults(value: object, expected: int) -> list[ast.expr | None]:
    path = "/parameters/keyword_defaults"
    if type(value) is not list or len(value) != expected:
        _fail(path)
    defaults = value
    for index, default in enumerate(defaults):
        if default is not None and not isinstance(default, ast.expr):
            _fail(f"{path}/{index}")
    return defaults


def _annotation_path(parameter_index: int, child_path: str) -> str:
    base = f"/parameters/{parameter_index}"
    if child_path in {"", "/"}:
        return base
    canonical_prefix = "/parameters/0"
    if child_path == canonical_prefix:
        return base
    if child_path.startswith(f"{canonical_prefix}/"):
        return f"{base}{child_path[len(canonical_prefix) :]}"
    return f"{base}{child_path}"


def _action_doc_metadata(
    action: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[dict[str, str], dict[str, str]]:
    body = getattr(action, "body", None)
    if type(body) is not list:
        _fail("/action/body")
    for index, statement in enumerate(body):
        if not isinstance(statement, ast.stmt):
            _fail(f"/action/body/{index}")
    try:
        parsed = parse_docstring(ast.get_docstring(action, clean=True))
    except (AttributeError, IndexError, TypeError, ValueError):
        _fail("/action/body")
    descriptions = parsed.get("params", {})
    titles = parsed.get("param_display_names", {})
    if not isinstance(descriptions, dict) or not isinstance(titles, dict):
        _fail("/action/body")
    return titles, descriptions


def _validate_action_shape(
    action: ast.FunctionDef | ast.AsyncFunctionDef,
) -> None:
    """在 module resolver 前把 action-local forged AST 定位到 Action Contract。"""

    body = getattr(action, "body", None)
    if type(body) is not list:
        _fail("/action/body")
    for index, statement in enumerate(body):
        if not isinstance(statement, ast.stmt):
            _fail(f"/action/body/{index}")

    arguments = getattr(action, "args", None)
    if not isinstance(arguments, ast.arguments):
        _fail("/parameters")
    positional_only = _argument_list(
        getattr(arguments, "posonlyargs", None),
        "/parameters/positional_only",
    )
    positional = _argument_list(
        getattr(arguments, "args", None),
        "/parameters/positional",
    )
    keyword_only = _argument_list(
        getattr(arguments, "kwonlyargs", None),
        "/parameters/keyword_only",
    )
    _default_list(
        getattr(arguments, "defaults", None),
        "/parameters/defaults",
    )
    _keyword_defaults(
        getattr(arguments, "kw_defaults", None),
        len(keyword_only),
    )

    seen_names: set[str] = set()
    for index, argument in enumerate(positional_only + positional + keyword_only):
        name = getattr(argument, "arg", None)
        if type(name) is not str or not name or name in seen_names:
            _fail(f"/parameters/{index}/name")
        seen_names.add(name)


def _parse_parameters(
    action: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    is_method: bool,
    imports: Mapping[str, str],
) -> tuple[
    WorkflowInputContract,
    tuple[tuple[str, tuple[ResourceTemplateSymbol, ...]], ...],
]:
    arguments = getattr(action, "args", None)
    if not isinstance(arguments, ast.arguments):
        _fail("/parameters")

    positional_only = _argument_list(
        getattr(arguments, "posonlyargs", None),
        "/parameters/positional_only",
    )
    positional = _argument_list(
        getattr(arguments, "args", None),
        "/parameters/positional",
    )
    keyword_only = _argument_list(
        getattr(arguments, "kwonlyargs", None),
        "/parameters/keyword_only",
    )
    defaults = _default_list(
        getattr(arguments, "defaults", None),
        "/parameters/defaults",
    )
    keyword_defaults = _keyword_defaults(
        getattr(arguments, "kw_defaults", None),
        len(keyword_only),
    )
    if getattr(arguments, "vararg", None) is not None:
        _fail("/parameters/vararg")
    if getattr(arguments, "kwarg", None) is not None:
        _fail("/parameters/kwarg")

    all_positional = positional_only + positional
    if len(defaults) > len(all_positional):
        _fail("/parameters/defaults")
    first_default = len(all_positional) - len(defaults)
    titles, descriptions = _action_doc_metadata(action)

    scheduled: list[tuple[ast.arg, ast.expr | object]] = []
    for index, argument in enumerate(all_positional):
        default_index = index - first_default
        default = defaults[default_index] if default_index >= 0 else NO_DEFAULT
        scheduled.append((argument, default))
    scheduled.extend(
        (argument, default if default is not None else NO_DEFAULT)
        for argument, default in zip(
            keyword_only,
            keyword_defaults,
            strict=True,
        )
    )

    descriptors: list[dict[str, Any]] = []
    resource_templates: list[tuple[str, tuple[ResourceTemplateSymbol, ...]]] = []
    seen_names: set[str] = set()
    first_positional = all_positional[0] if all_positional else None
    for argument, default in scheduled:
        name = getattr(argument, "arg", None)
        if type(name) is not str or not name:
            _fail(f"/parameters/{len(descriptors)}/name")
        is_receiver = (
            is_method and argument is first_positional and name in {"self", "cls"}
        )
        if is_receiver or name in _FRAMEWORK_PARAMETERS:
            continue
        parameter_index = len(descriptors)
        if name in seen_names:
            _fail(f"/parameters/{parameter_index}/name")
        seen_names.add(name)
        annotation = getattr(argument, "annotation", None)
        if not isinstance(annotation, ast.expr):
            _fail(f"/parameters/{parameter_index}/annotation")
        try:
            parsed = parse_parameter_annotation(
                name,
                annotation,
                default=default,
                imports=imports,
                doc_title=titles.get(name),
                doc_description=descriptions.get(name),
            )
        except AnnotationSchemaError as error:
            _fail(
                _annotation_path(parameter_index, error.path),
                code="invalid_annotation",
                message=error.message,
            )
        except RecursionError:
            _fail(
                f"/parameters/{parameter_index}/annotation",
                code="invalid_annotation",
            )
        except (AttributeError, IndexError, KeyError, TypeError):
            _fail(f"/parameters/{parameter_index}/annotation")
        descriptors.append(parsed.to_dict())
        resource_templates.append((name, parsed.resource_templates))

    try:
        contract = parse_input_contract({"version": 1, "parameters": descriptors})
    except WorkflowSchemaError as error:
        _fail(
            error.path or "/parameters",
            code="invalid_schema",
            message=error.message,
        )
    return contract, tuple(resource_templates)


def _parse_results(
    action: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    definitions: Mapping[str, DefinitionNode],
    imports: Mapping[str, str],
) -> tuple[
    WorkflowOutputContract,
    tuple[tuple[str, tuple[ResourceTemplateSymbol, ...]], ...],
]:
    declaration = getattr(action, "returns", None)
    if declaration is None:
        _fail("/return")
    if isinstance(declaration, ast.expr) and _opaque_dict_result(
        declaration,
        imports,
    ):
        try:
            return parse_output_contract({"version": 1, "outputs": []}), ()
        except WorkflowSchemaError as error:  # defensive: constant schema
            _fail(error.path or "/return", code="invalid_schema")
    if isinstance(declaration, ast.Name):
        name = getattr(declaration, "id", None)
        if type(name) is not str:
            _fail("/return", code="invalid_action_result")
        resolved = definitions.get(name)
        if not isinstance(resolved, ast.ClassDef):
            _fail("/return", code="invalid_action_result")
        declaration = resolved
    try:
        parsed = parse_action_result_declaration(
            declaration,
            imports=imports,
        )
    except ActionResultSchemaError as error:
        _fail(
            error.path,
            code="invalid_action_result",
            message=error.message,
        )
    except RecursionError:
        _fail("/return", code="invalid_action_result")
    except (AttributeError, IndexError, KeyError, TypeError):
        _fail("/return", code="invalid_action_result")
    try:
        contract = parse_output_contract(parsed.to_dict())
    except WorkflowSchemaError as error:
        _fail(
            error.path or "/return",
            code="invalid_schema",
            message=error.message,
        )
    return contract, parsed.resource_templates


def _opaque_dict_result(
    declaration: ast.expr | ast.ClassDef,
    imports: Mapping[str, str],
) -> bool:
    if isinstance(declaration, ast.Name):
        return declaration.id == "dict" and declaration.id not in imports
    if not isinstance(declaration, ast.Subscript):
        return False
    value = declaration.value
    if isinstance(value, ast.Name):
        if value.id == "dict" and value.id not in imports:
            return True
        return imports.get(value.id) == "typing:Dict"
    if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
        return imports.get(value.value.id) == "typing" and value.attr == "Dict"
    return False


def parse_action_contract(
    module: ast.Module,
    action: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    module_name: str,
) -> ParsedActionContract:
    """从真实 defining module AST 解析一个完整 canonical Action Contract。"""

    if not isinstance(action, (ast.FunctionDef, ast.AsyncFunctionDef)):
        _fail("/action")
    _validate_action_shape(action)
    try:
        scope = resolve_module_scope(module, module_name=module_name)
    except ModuleScopeError as error:
        _fail(error.path, code=error.code, message=error.message)
    except RecursionError:
        _fail("/module")

    is_method = _action_context(module, action)
    input_contract, input_templates = _parse_parameters(
        action,
        is_method=is_method,
        imports=scope.annotation_bindings,
    )
    output_contract, output_templates = _parse_results(
        action,
        definitions=scope.definitions,
        imports=scope.annotation_bindings,
    )
    return ParsedActionContract._from_canonical(
        input_contract,
        output_contract,
        input_templates,
        output_templates,
        token=_PARSED_ACTION_CONTRACT_TOKEN,
    )


__all__ = [
    "ActionCompatibilityError",
    "ActionContractError",
    "ParsedActionContract",
    "canonical_goal_defaults",
    "parse_action_contract",
    "validate_legacy_action_assertions",
]
