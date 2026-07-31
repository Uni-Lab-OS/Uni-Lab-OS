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
    _argument_list(
        getattr(arguments, "posonlyargs", None),
        "/parameters/positional_only",
    )
    _argument_list(
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
    "ActionContractError",
    "ParsedActionContract",
    "parse_action_contract",
]
