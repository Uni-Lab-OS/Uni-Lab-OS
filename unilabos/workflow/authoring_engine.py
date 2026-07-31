"""Authority-scoped、AST-only Workflow Authoring 深模块。

本模块是 Python↔Backend-shaped graph 的唯一解释器。它不执行 Authoring source、不做
Catalog 同步、不持久化，也不创建 Task/Job。
"""

from __future__ import annotations

import ast
import io
import keyword
import re
import tokenize
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Never
from uuid import UUID, uuid5

from unilabos.registry.annotation_schema import (
    NO_DEFAULT,
    AnnotationSchemaError,
    parse_parameter_annotation,
)
from unilabos.registry.utils import parse_docstring
from unilabos.workflow.catalog import (
    CatalogAuthority,
    TemplateCatalog,
    TemplateCatalogError,
    TemplateCatalogSnapshot,
    TemplateCatalogUnavailable,
)
from unilabos.workflow.graph_validation import GraphValidationError, validate_graph
from unilabos.workflow.json_codec import (
    decode_json_bytes,
    encode_json,
    strict_json_equal,
)
from unilabos.workflow.models import (
    CandidateCompilation,
    WorkflowEdgeWrite,
    WorkflowNodeWrite,
    validate_json_value,
    validate_uuid,
)
from unilabos.workflow.schema import (
    WorkflowSchemaError,
    parse_input_contract,
    parse_output_contract,
)

_COMPILER_VERSION = "unilab-authoring/v1"
_ZERO_FINGERPRINT = "sha256:" + "0" * 64
_ANCHOR = re.compile(
    r"^#\s*unilab:node_uuid=([^\s#]+)\s*$",
)
_AUTHORING_MODULE = "unilabos.workflow.authoring"
_DEVICE = f"{_AUTHORING_MODULE}:device"
_GROUP = f"{_AUTHORING_MODULE}:group"
_PARALLEL = f"{_AUTHORING_MODULE}:parallel"
_WORKFLOW_DEFINITION = f"{_AUTHORING_MODULE}:workflow_definition"
_WORKFLOW_OUTPUT = f"{_AUTHORING_MODULE}:workflow_output"
_OWNED_WORKFLOW_KEYS = {
    "input_contract",
    "output_contract",
    "output_bindings",
}
_OWNED_NODE_KEYS = {"input_bindings", "executor_binding"}


class _AuthoringFailure(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        node: ast.AST | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.node = node


def _fail(
    code: str,
    message: str,
    *,
    node: ast.AST | None = None,
) -> Never:
    raise _AuthoringFailure(code, message, node=node)


def _detached(value: Any) -> Any:
    def thaw(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {str(key): thaw(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [thaw(child) for child in item]
        return item

    return decode_json_bytes(encode_json(thaw(value)))


def _safe_identifier(value: str, fallback: str) -> str:
    normalized = re.sub(r"\W+", "_", value, flags=re.UNICODE).strip("_")
    if not normalized or not normalized.isidentifier() or keyword.iskeyword(normalized):
        normalized = fallback
    return normalized


def _snake_case(value: str, fallback: str) -> str:
    separated = re.sub(r"(?<!^)(?=[A-Z])", "_", value)
    return _safe_identifier(separated.lower(), fallback)


def _call_identity(call: ast.Call, imports: Mapping[str, str]) -> str | None:
    if isinstance(call.func, ast.Name):
        return imports.get(call.func.id)
    return None


def _diagnostic(error: _AuthoringFailure, source: str) -> dict[str, Any]:
    item: dict[str, Any] = {
        "severity": "error",
        "code": error.code,
        "message": error.message,
    }
    node = error.node
    if node is None:
        return item
    lines = source.splitlines() or [""]
    start_line = min(max(int(getattr(node, "lineno", 1)), 1), len(lines))
    start_column = max(int(getattr(node, "col_offset", 0)) + 1, 1)
    end_line = min(
        max(int(getattr(node, "end_lineno", start_line)), start_line),
        len(lines),
    )
    end_column = max(
        int(getattr(node, "end_col_offset", len(lines[end_line - 1]))) + 1,
        start_column,
    )
    item["source_range"] = {
        "start_line": start_line,
        "start_column": start_column,
        "end_line": end_line,
        "end_column": end_column,
    }
    return item


def _syntax_diagnostic(error: SyntaxError, source: str) -> dict[str, Any]:
    lines = source.splitlines() or [""]
    line = min(max(int(error.lineno or 1), 1), len(lines))
    column = max(int(error.offset or 1), 1)
    end_line = min(max(int(error.end_lineno or line), line), len(lines))
    end_column = max(int(error.end_offset or column), column)
    return {
        "severity": "error",
        "code": "python_syntax_error",
        "message": "Python 源码语法不正确",
        "source_range": {
            "start_line": line,
            "start_column": column,
            "end_line": end_line,
            "end_column": end_column,
        },
    }


def _error_result(
    *,
    fingerprint: str,
    diagnostic: dict[str, Any],
) -> CandidateCompilation:
    return CandidateCompilation(
        diagnostics=[diagnostic],
        graph=None,
        normalized_python_source=None,
        source_map=[],
        changeset=None,
        compiler_version=_COMPILER_VERSION,
        template_catalog_fingerprint=fingerprint,
    )


@dataclass(frozen=True, slots=True)
class _Selector:
    local_name: str
    class_identity: str
    device_id: str | None


@dataclass(slots=True)
class _NodeState:
    node: dict[str, Any]
    template: dict[str, Any]
    handles: tuple[dict[str, Any], ...]
    result_name: str | None
    statement: ast.stmt


@dataclass(frozen=True, slots=True)
class _Flow:
    starts: tuple[str, ...] = ()
    ends: tuple[str, ...] = ()


@dataclass(slots=True)
class _BuildState:
    workflow_uuid: str
    applied_graph: dict[str, Any]
    snapshot: TemplateCatalogSnapshot
    imports: dict[str, str]
    selectors: dict[str, _Selector]
    anchors: dict[int, str]
    anchor_lines: set[int]
    input_names: set[str]
    nodes: list[_NodeState] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    results: dict[str, _NodeState] = field(default_factory=dict)
    used_anchors: set[int] = field(default_factory=set)
    allocated_occurrences: dict[str, int] = field(default_factory=dict)

    @property
    def applied_nodes(self) -> dict[str, dict[str, Any]]:
        return {
            str(item.get("uuid")): item
            for item in self.applied_graph.get("nodes", [])
            if isinstance(item, dict)
        }

    def node_uuid(self, statement: ast.stmt) -> str:
        anchor_line = statement.lineno - 1
        if anchor_line in self.anchors:
            self.used_anchors.add(anchor_line)
            return self.anchors[anchor_line]
        structural = ast.dump(statement, include_attributes=False)
        occurrence = self.allocated_occurrences.get(structural, 0)
        self.allocated_occurrences[structural] = occurrence + 1
        return str(
            uuid5(
                UUID(self.workflow_uuid),
                f"authoring-node:{structural}:{occurrence}",
            )
        )


class _CatalogIndex:
    def __init__(self, snapshot: TemplateCatalogSnapshot) -> None:
        self.snapshot = snapshot
        self.nodes = [_detached(item) for item in snapshot.node_templates]
        self.handles = [_detached(item) for item in snapshot.handle_templates]
        self._handles_by_node: dict[str, list[dict[str, Any]]] = {}
        for handle in self.handles:
            self._handles_by_node.setdefault(
                handle["workflow_node_template_uuid"],
                [],
            ).append(handle)

    def action(
        self,
        class_identity: str,
        action_name: str,
        *,
        node: ast.AST,
    ) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
        matches = [
            item
            for item in self.nodes
            if item.get("class") == class_identity and item.get("name") == action_name
        ]
        if len(matches) != 1:
            _fail(
                "template_catalog_mismatch",
                "当前 Catalog 无法唯一解析设备模板与 Action",
                node=node,
            )
        template = matches[0]
        return template, tuple(self._handles_by_node.get(template["uuid"], []))

    def group(
        self,
        *,
        node: ast.AST,
    ) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
        return self.action(_GROUP.rsplit(":", 1)[0] + ":group", "group", node=node)


class WorkflowAuthoringEngine:
    """把一个稳定 Template Catalog snapshot 深化为三个纯 Authoring transform。"""

    compiler_version = _COMPILER_VERSION
    catalog_projection_policy = "referenced_snapshot"

    def __init__(
        self,
        *,
        catalog: TemplateCatalog,
        authority: CatalogAuthority,
    ) -> None:
        if not isinstance(catalog, TemplateCatalog):
            raise TypeError("catalog 必须是 TemplateCatalog")
        if not isinstance(authority, CatalogAuthority):
            raise TypeError("authority 必须是 CatalogAuthority")
        self._catalog = catalog
        self._authority = authority
        self._active_snapshot: ContextVar[TemplateCatalogSnapshot | None] = ContextVar(
            f"authoring_snapshot_{id(self)}",
            default=None,
        )

    @property
    def template_catalog_fingerprint(self) -> str:
        active = self._active_snapshot.get()
        if active is not None:
            return active.fingerprint
        with self._catalog.snapshot(self._authority) as snapshot:
            return snapshot.fingerprint

    @contextmanager
    def catalog_snapshot(self) -> Iterator[str]:
        active = self._active_snapshot.get()
        if active is not None:
            yield active.fingerprint
            return
        with self._catalog.snapshot(self._authority) as snapshot:
            token = self._active_snapshot.set(snapshot)
            try:
                yield snapshot.fingerprint
            finally:
                self._active_snapshot.reset(token)

    @contextmanager
    def _snapshot(self) -> Iterator[TemplateCatalogSnapshot]:
        active = self._active_snapshot.get()
        if active is not None:
            yield active
            return
        with self._catalog.snapshot(self._authority) as snapshot:
            token = self._active_snapshot.set(snapshot)
            try:
                yield snapshot
            finally:
                self._active_snapshot.reset(token)

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> CandidateCompilation:
        workflow_uuid, workflow_revision = _programming_identity(
            workflow_uuid,
            workflow_revision,
        )
        _programming_source(python_source, source_uri)
        fingerprint = _ZERO_FINGERPRINT
        try:
            with self._snapshot() as snapshot:
                fingerprint = snapshot.fingerprint
                return _compile_with_snapshot(
                    snapshot=snapshot,
                    workflow_uuid=workflow_uuid,
                    workflow_revision=workflow_revision,
                    python_source=python_source,
                    source_uri=source_uri,
                    applied_graph=applied_graph,
                )
        except TemplateCatalogUnavailable:
            return _error_result(
                fingerprint=fingerprint,
                diagnostic={
                    "severity": "error",
                    "code": "template_catalog_unavailable",
                    "message": "当前 Graph Authority 的模板 Catalog 尚不可用",
                },
            )
        except TemplateCatalogError as error:
            return _error_result(
                fingerprint=fingerprint,
                diagnostic={
                    "severity": "error",
                    "code": error.code,
                    "message": "当前 Graph Authority 的模板 Catalog 不匹配",
                },
            )

    def generate_python(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        graph: dict[str, Any],
        source_uri: str,
    ) -> CandidateCompilation:
        workflow_uuid, workflow_revision = _programming_identity(
            workflow_uuid,
            workflow_revision,
        )
        _programming_source("", source_uri)
        fingerprint = _ZERO_FINGERPRINT
        try:
            with self._snapshot() as snapshot:
                fingerprint = snapshot.fingerprint
                return _generate_with_snapshot(
                    snapshot=snapshot,
                    workflow_uuid=workflow_uuid,
                    workflow_revision=workflow_revision,
                    graph=graph,
                    source_uri=source_uri,
                )
        except TemplateCatalogUnavailable:
            return _error_result(
                fingerprint=fingerprint,
                diagnostic={
                    "severity": "error",
                    "code": "template_catalog_unavailable",
                    "message": "当前 Graph Authority 的模板 Catalog 尚不可用",
                },
            )
        except TemplateCatalogError as error:
            return _error_result(
                fingerprint=fingerprint,
                diagnostic={
                    "severity": "error",
                    "code": error.code,
                    "message": "当前 Graph Authority 的模板 Catalog 不匹配",
                },
            )

    def validate(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        graph: dict[str, Any],
        python_source: str,
        source_uri: str,
    ) -> CandidateCompilation:
        workflow_uuid, workflow_revision = _programming_identity(
            workflow_uuid,
            workflow_revision,
        )
        _programming_source(python_source, source_uri)
        fingerprint = _ZERO_FINGERPRINT
        try:
            with self._snapshot() as snapshot:
                fingerprint = snapshot.fingerprint
                generated = _generate_with_snapshot(
                    snapshot=snapshot,
                    workflow_uuid=workflow_uuid,
                    workflow_revision=workflow_revision,
                    graph=graph,
                    source_uri=source_uri,
                )
                if not generated.valid:
                    return generated
                compiled = _compile_with_snapshot(
                    snapshot=snapshot,
                    workflow_uuid=workflow_uuid,
                    workflow_revision=workflow_revision,
                    python_source=python_source,
                    source_uri=source_uri,
                    applied_graph=graph,
                )
                if not compiled.valid or not _semantic_graph_equal(
                    compiled.graph,
                    graph,
                ):
                    return _error_result(
                        fingerprint=fingerprint,
                        diagnostic={
                            "severity": "error",
                            "code": "round_trip_mismatch",
                            "message": "Python 与 Candidate graph 不能证明语义等价",
                        },
                    )
                return CandidateCompilation(
                    diagnostics=[],
                    graph=_detached(graph),
                    normalized_python_source=compiled.normalized_python_source,
                    source_map=compiled.source_map,
                    changeset=_source_only_changeset(),
                    compiler_version=_COMPILER_VERSION,
                    template_catalog_fingerprint=fingerprint,
                )
        except TemplateCatalogUnavailable:
            return _error_result(
                fingerprint=fingerprint,
                diagnostic={
                    "severity": "error",
                    "code": "template_catalog_unavailable",
                    "message": "当前 Graph Authority 的模板 Catalog 尚不可用",
                },
            )
        except TemplateCatalogError as error:
            return _error_result(
                fingerprint=fingerprint,
                diagnostic={
                    "severity": "error",
                    "code": error.code,
                    "message": "当前 Graph Authority 的模板 Catalog 不匹配",
                },
            )


def _programming_identity(workflow_uuid: str, revision: int) -> tuple[str, int]:
    if not isinstance(workflow_uuid, str):
        raise TypeError("workflow_uuid 必须是字符串")
    try:
        normalized = validate_uuid(workflow_uuid)
    except (TypeError, ValueError):
        raise ValueError("workflow_uuid 必须是非 nil UUID") from None
    if type(revision) is not int or revision < 1:
        raise ValueError("workflow_revision 必须是正整数")
    return normalized, revision


def _programming_source(source: str, source_uri: str) -> None:
    if not isinstance(source, str) or not isinstance(source_uri, str):
        raise TypeError("Python source 与 source_uri 必须是字符串")
    if not source_uri.strip():
        raise ValueError("source_uri 不能为空")


def _compile_with_snapshot(
    *,
    snapshot: TemplateCatalogSnapshot,
    workflow_uuid: str,
    workflow_revision: int,
    python_source: str,
    source_uri: str,
    applied_graph: dict[str, Any],
) -> CandidateCompilation:
    del source_uri
    try:
        module = ast.parse(python_source)
    except SyntaxError as error:
        return _error_result(
            fingerprint=snapshot.fingerprint,
            diagnostic=_syntax_diagnostic(error, python_source),
        )
    try:
        applied = _require_graph_identity(
            applied_graph,
            workflow_uuid=workflow_uuid,
            workflow_revision=workflow_revision,
        )
        imports = _module_imports(module)
        selectors, function = _module_declarations(module, imports)
        displayname, description = _workflow_declaration(
            function,
            imports,
            workflow_uuid=workflow_uuid,
        )
        input_contract = _workflow_parameters(function, imports)
        anchors, anchor_lines = _source_anchors(python_source)
        state = _BuildState(
            workflow_uuid=workflow_uuid,
            applied_graph=applied,
            snapshot=snapshot,
            imports=imports,
            selectors=selectors,
            anchors=anchors,
            anchor_lines=anchor_lines,
            input_names={item["name"] for item in input_contract["parameters"]},
        )
        executable, return_statement = _function_body(function)
        if executable == [None]:
            executable = []
        _parse_sequence(
            [item for item in executable if isinstance(item, ast.stmt)],
            state=state,
            available_results=state.results,
            parent_uuid=None,
            allow_parallel=True,
        )
        unused_anchors = anchor_lines - state.used_anchors
        if unused_anchors:
            _fail(
                "invalid_node_anchor",
                "Node UUID anchor 必须紧邻一个 persisted construct",
            )
        output_contract, output_bindings = _workflow_outputs(
            return_statement,
            input_contract=input_contract,
            results=state.results,
            imports=imports,
        )
        graph = _candidate_graph(
            state,
            displayname=displayname,
            description=description,
            input_contract=input_contract,
            output_contract=output_contract,
            output_bindings=output_bindings,
        )
        _validate_built_graph(graph)
        normalized, source_map = _render_graph(graph)
        changeset = _changeset(graph, applied)
        return CandidateCompilation(
            diagnostics=[],
            graph=graph,
            normalized_python_source=normalized,
            source_map=source_map,
            changeset=changeset,
            compiler_version=_COMPILER_VERSION,
            template_catalog_fingerprint=snapshot.fingerprint,
        )
    except _AuthoringFailure as error:
        return _error_result(
            fingerprint=snapshot.fingerprint,
            diagnostic=_diagnostic(error, python_source),
        )
    except AnnotationSchemaError as error:
        failure = _AuthoringFailure(
            error.code,
            error.message,
        )
        return _error_result(
            fingerprint=snapshot.fingerprint,
            diagnostic=_diagnostic(failure, python_source),
        )
    except WorkflowSchemaError as error:
        failure = _AuthoringFailure(error.code, error.message)
        return _error_result(
            fingerprint=snapshot.fingerprint,
            diagnostic=_diagnostic(failure, python_source),
        )
    except (GraphValidationError, TypeError, ValueError):
        failure = _AuthoringFailure(
            "candidate_invalid",
            "生成的 Candidate graph 不满足 Workflow 合同",
        )
        return _error_result(
            fingerprint=snapshot.fingerprint,
            diagnostic=_diagnostic(failure, python_source),
        )


def _require_graph_identity(
    graph: Any,
    *,
    workflow_uuid: str,
    workflow_revision: int,
) -> dict[str, Any]:
    if not isinstance(graph, dict) or set(graph) != {
        "workflow",
        "nodes",
        "edges",
        "node_templates",
        "handle_templates",
    }:
        _fail("candidate_invalid", "Candidate graph 必须包含完整五集合")
    workflow = graph.get("workflow")
    if not isinstance(workflow, dict):
        _fail("candidate_invalid", "Candidate graph 缺少 Workflow")
    if (
        workflow.get("uuid") != workflow_uuid
        or workflow.get("revision") != workflow_revision
    ):
        _fail(
            "workflow_identity_mismatch",
            "Workflow identity 或 revision 与转换请求不一致",
        )
    for key in ("nodes", "edges", "node_templates", "handle_templates"):
        if not isinstance(graph.get(key), list):
            _fail("candidate_invalid", f"Candidate graph {key} 必须是数组")
    return _detached(graph)


def _module_imports(module: ast.Module) -> dict[str, str]:
    imports: dict[str, str] = {}
    for statement in module.body:
        if isinstance(statement, ast.ImportFrom):
            if statement.level or not statement.module:
                _fail(
                    "invalid_module_scope",
                    "Workflow source 只允许绝对 import",
                    node=statement,
                )
            for alias in statement.names:
                if alias.name == "*":
                    _fail(
                        "invalid_module_scope",
                        "Workflow source 禁止 import star",
                        node=statement,
                    )
                local = alias.asname or alias.name
                if local in imports:
                    _fail(
                        "invalid_module_scope",
                        "Workflow source import 名称重复",
                        node=statement,
                    )
                imports[local] = f"{statement.module}:{alias.name}"
        elif isinstance(statement, ast.Import):
            for alias in statement.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                if local in imports:
                    _fail(
                        "invalid_module_scope",
                        "Workflow source import 名称重复",
                        node=statement,
                    )
                imports[local] = alias.name
    return imports


def _module_declarations(
    module: ast.Module,
    imports: Mapping[str, str],
) -> tuple[dict[str, _Selector], ast.FunctionDef]:
    selectors: dict[str, _Selector] = {}
    functions: list[ast.FunctionDef] = []
    for statement in module.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(statement, ast.AnnAssign):
            selector = _device_selector(statement, imports)
            if selector.local_name in selectors:
                _fail(
                    "invalid_device_selector",
                    "device selector 名称重复",
                    node=statement,
                )
            selectors[selector.local_name] = selector
            continue
        if isinstance(statement, ast.FunctionDef) and _has_workflow_decorator(
            statement,
            imports,
        ):
            functions.append(statement)
            continue
        _fail(
            "invalid_module_scope",
            "Workflow module 顶层只允许 import、typed device selector 和唯一 Workflow",
            node=statement,
        )
    if len(functions) != 1:
        _fail(
            "invalid_workflow_declaration",
            "Workflow module 必须恰有一个 @workflow_definition 函数",
        )
    return selectors, functions[0]


def _has_workflow_decorator(
    function: ast.FunctionDef,
    imports: Mapping[str, str],
) -> bool:
    return any(
        isinstance(item, ast.Call)
        and _call_identity(item, imports) == _WORKFLOW_DEFINITION
        for item in function.decorator_list
    )


def _device_selector(
    statement: ast.AnnAssign,
    imports: Mapping[str, str],
) -> _Selector:
    if (
        not isinstance(statement.target, ast.Name)
        or not isinstance(statement.annotation, ast.Name)
        or not isinstance(statement.value, ast.Call)
        or _call_identity(statement.value, imports) != _DEVICE
    ):
        _fail(
            "invalid_device_selector",
            "device selector 必须是 module-scope typed device() assignment",
            node=statement,
        )
    class_identity = imports.get(statement.annotation.id)
    if not class_identity or ":" not in class_identity:
        _fail(
            "invalid_device_selector",
            "device selector annotation 必须是绝对导入的模板 symbol",
            node=statement,
        )
    call = statement.value
    if call.keywords or len(call.args) > 1:
        _fail(
            "invalid_device_selector",
            "device() 只允许零个参数或一个固定 device_id",
            node=statement,
        )
    device_id: str | None = None
    if call.args:
        argument = call.args[0]
        if not isinstance(argument, ast.Constant) or not isinstance(
            argument.value,
            str,
        ):
            _fail(
                "invalid_device_selector",
                "固定 device_id 必须是非空字符串 literal",
                node=statement,
            )
        device_id = argument.value
        if not device_id.strip() or device_id != device_id.strip():
            _fail(
                "invalid_device_selector",
                "固定 device_id 必须是非空规范字符串",
                node=statement,
            )
    return _Selector(statement.target.id, class_identity, device_id)


def _workflow_declaration(
    function: ast.FunctionDef,
    imports: Mapping[str, str],
    *,
    workflow_uuid: str,
) -> tuple[str, str | None]:
    decorators = [
        item
        for item in function.decorator_list
        if isinstance(item, ast.Call)
        and _call_identity(item, imports) == _WORKFLOW_DEFINITION
    ]
    if len(decorators) != 1 or len(function.decorator_list) != 1:
        _fail(
            "invalid_workflow_declaration",
            "Workflow 函数只能有一个 workflow_definition decorator",
            node=function,
        )
    decorator = decorators[0]
    if decorator.args or any(item.arg is None for item in decorator.keywords):
        _fail(
            "invalid_workflow_declaration",
            "workflow_definition 只接受命名 literal",
            node=decorator,
        )
    raw = {item.arg: item.value for item in decorator.keywords if item.arg}
    if set(raw) - {"workflow_uuid", "displayname", "description"} or not {
        "workflow_uuid",
        "displayname",
    }.issubset(raw):
        _fail(
            "invalid_workflow_declaration",
            "workflow_definition 字段不符合版本 1 合同",
            node=decorator,
        )
    values: dict[str, str | None] = {}
    for name, expression in raw.items():
        if not isinstance(expression, ast.Constant) or not isinstance(
            expression.value,
            str,
        ):
            _fail(
                "invalid_workflow_declaration",
                "workflow_definition 值必须是字符串 literal",
                node=expression,
            )
        value = expression.value.strip()
        if not value:
            _fail(
                "invalid_workflow_declaration",
                "workflow_definition 值不能为空",
                node=expression,
            )
        values[name] = value
    try:
        declared_uuid = validate_uuid(str(values["workflow_uuid"]))
    except (TypeError, ValueError):
        _fail(
            "workflow_identity_mismatch",
            "decorator workflow_uuid 不是有效 UUID",
            node=decorator,
        )
    if declared_uuid != workflow_uuid:
        _fail(
            "workflow_identity_mismatch",
            "decorator workflow_uuid 与请求 Workflow 不一致",
            node=decorator,
        )
    return str(values["displayname"]), values.get("description")


def _workflow_parameters(
    function: ast.FunctionDef,
    imports: Mapping[str, str],
) -> dict[str, Any]:
    arguments = function.args
    if (
        arguments.posonlyargs
        or arguments.args
        or arguments.vararg is not None
        or arguments.kwarg is not None
    ):
        _fail(
            "invalid_workflow_signature",
            "Workflow 参数必须全部是 keyword-only",
            node=function,
        )
    if len(arguments.kwonlyargs) != len(arguments.kw_defaults):
        _fail(
            "invalid_workflow_signature",
            "Workflow keyword default 列表不完整",
            node=function,
        )
    try:
        doc = parse_docstring(ast.get_docstring(function, clean=True))
    except (AttributeError, IndexError, TypeError, ValueError):
        _fail(
            "invalid_workflow_signature",
            "Workflow docstring 参数 metadata 不正确",
            node=function,
        )
    titles = doc.get("param_display_names", {})
    descriptions = doc.get("params", {})
    if not isinstance(titles, dict) or not isinstance(descriptions, dict):
        _fail(
            "invalid_workflow_signature",
            "Workflow docstring 参数 metadata 不正确",
            node=function,
        )
    descriptors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for argument, default in zip(
        arguments.kwonlyargs,
        arguments.kw_defaults,
        strict=True,
    ):
        if argument.arg in seen or argument.annotation is None:
            _fail(
                "invalid_workflow_signature",
                "Workflow 参数名称或 annotation 不正确",
                node=argument,
            )
        seen.add(argument.arg)
        try:
            parsed = parse_parameter_annotation(
                argument.arg,
                argument.annotation,
                default=NO_DEFAULT if default is None else default,
                imports=imports,
                doc_title=titles.get(argument.arg),
                doc_description=descriptions.get(argument.arg),
            )
        except AnnotationSchemaError as error:
            _fail(error.code, error.message, node=argument)
        if parsed.resource_templates:
            _fail(
                "template_catalog_mismatch",
                "当前 Catalog 尚未发布 ResourceTemplate symbol identity",
                node=argument,
            )
        descriptors.append(parsed.to_dict())
    try:
        return parse_input_contract({"version": 1, "parameters": descriptors}).to_dict()
    except WorkflowSchemaError as error:
        _fail(error.code, error.message, node=function)


def _source_anchors(source: str) -> tuple[dict[int, str], set[int]]:
    anchors: dict[int, str] = {}
    seen_uuids: set[str] = set()
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for token in tokens:
            if token.type != tokenize.COMMENT:
                continue
            match = _ANCHOR.fullmatch(token.string)
            if match is None:
                continue
            try:
                node_uuid = validate_uuid(match.group(1))
            except (TypeError, ValueError):
                _fail(
                    "invalid_node_anchor",
                    "Node UUID anchor 必须是非 nil UUID",
                )
            line = token.start[0]
            if line in anchors or node_uuid in seen_uuids:
                _fail(
                    "invalid_node_anchor",
                    "Node UUID anchor 不能重复",
                )
            anchors[line] = node_uuid
            seen_uuids.add(node_uuid)
    except (IndentationError, tokenize.TokenError):
        _fail("python_syntax_error", "Python source token 不完整")
    return anchors, set(anchors)


def _function_body(
    function: ast.FunctionDef,
) -> tuple[list[ast.stmt | None], ast.Return | None]:
    body = list(function.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body.pop(0)
    returns = [
        node
        for statement in body
        for node in ast.walk(statement)
        if isinstance(node, ast.Return)
    ]
    if returns:
        if (
            len(returns) != 1
            or not body
            or not isinstance(body[-1], ast.Return)
            or returns[0] is not body[-1]
        ):
            _fail(
                "invalid_workflow_output",
                "workflow_output 必须是唯一最终 top-level return",
                node=function,
            )
        return_statement = body.pop()
    else:
        return_statement = None
    if len(body) == 1 and isinstance(body[0], ast.Pass):
        return [None], return_statement
    if any(isinstance(item, ast.Pass) for item in body):
        _fail(
            "unsupported_authoring_syntax",
            "pass 只允许表示完全空的 Workflow",
            node=next(item for item in body if isinstance(item, ast.Pass)),
        )
    return body, return_statement


def _parse_sequence(
    statements: Sequence[ast.stmt],
    *,
    state: _BuildState,
    available_results: dict[str, _NodeState],
    parent_uuid: str | None,
    allow_parallel: bool,
) -> _Flow:
    first: tuple[str, ...] = ()
    previous: tuple[str, ...] = ()
    for statement in statements:
        if isinstance(statement, ast.Assign):
            segment = _parse_action(
                statement,
                state=state,
                available_results=available_results,
                parent_uuid=parent_uuid,
            )
        elif isinstance(statement, ast.With):
            kind = _with_kind(statement, state.imports)
            if kind == "group":
                segment = _parse_group(
                    statement,
                    state=state,
                    available_results=available_results,
                    outer_parent_uuid=parent_uuid,
                    allow_parallel=allow_parallel,
                )
            elif kind == "parallel" and allow_parallel and parent_uuid is None:
                segment = _parse_parallel(
                    statement,
                    state=state,
                    available_results=available_results,
                )
            else:
                _fail(
                    "unsupported_authoring_syntax",
                    "当前 lexical scope 不支持该 Workflow block",
                    node=statement,
                )
        else:
            _fail(
                "unsupported_authoring_syntax",
                "02D 只支持 action、group 和 parallel",
                node=statement,
            )
        if not first:
            first = segment.starts
        if previous and segment.starts:
            _connect_dependencies(state, previous, segment.starts, node=statement)
        if segment.ends:
            previous = segment.ends
    return _Flow(first, previous)


def _with_kind(statement: ast.With, imports: Mapping[str, str]) -> str | None:
    if len(statement.items) != 1 or statement.items[0].optional_vars is not None:
        return None
    context = statement.items[0].context_expr
    if not isinstance(context, ast.Call):
        return None
    identity = _call_identity(context, imports)
    if identity == _GROUP:
        return "group"
    if identity == _PARALLEL:
        return "parallel"
    return None


def _parse_action(
    statement: ast.Assign,
    *,
    state: _BuildState,
    available_results: dict[str, _NodeState],
    parent_uuid: str | None,
) -> _Flow:
    if (
        len(statement.targets) != 1
        or not isinstance(statement.targets[0], ast.Name)
        or not isinstance(statement.value, ast.Call)
    ):
        _fail(
            "invalid_action_result",
            "Action 必须由一个新名字接收唯一 result object",
            node=statement,
        )
    result_name = statement.targets[0].id
    if (
        result_name in available_results
        or result_name in state.selectors
        or result_name in state.input_names
    ):
        _fail(
            "invalid_action_result",
            "Action result 名称不能重绑定",
            node=statement.targets[0],
        )
    call = statement.value
    if not isinstance(call.func, ast.Attribute) or not isinstance(
        call.func.value, ast.Name
    ):
        _fail(
            "invalid_action_call",
            "Action call 必须是 selector.action(...) 的静态形状",
            node=call,
        )
    selector = state.selectors.get(call.func.value.id)
    if selector is None:
        _fail(
            "invalid_action_call",
            "Action owner 不是已声明 device selector",
            node=call,
        )
    if call.args or any(item.arg is None for item in call.keywords):
        _fail(
            "invalid_action_call",
            "Action 只接受 keyword arguments",
            node=call,
        )
    keyword_names = [str(item.arg) for item in call.keywords]
    if len(keyword_names) != len(set(keyword_names)):
        _fail(
            "invalid_action_call",
            "Action keyword 不能重复",
            node=call,
        )
    catalog = _CatalogIndex(state.snapshot)
    template, handles = catalog.action(
        selector.class_identity,
        call.func.attr,
        node=call,
    )
    targets = _handles_by_business_name(handles, "target", node=call)
    node_uuid = state.node_uuid(statement)
    applied = state.applied_nodes.get(node_uuid, {})
    meta_data = _node_metadata(applied.get("meta_data"), selector)
    input_bindings: dict[str, dict[str, str]] = {}
    param = _detached(template.get("goal_default") or {})
    if not isinstance(param, dict):
        param = {}
    pending_edges: list[dict[str, Any]] = []
    input_names = state.input_names
    for keyword_node in call.keywords:
        assert keyword_node.arg is not None
        target = targets.get(keyword_node.arg)
        if target is None or keyword_node.arg == "ready":
            _fail(
                "template_catalog_mismatch",
                "Action keyword 未唯一匹配真实 target Handle",
                node=keyword_node,
            )
        expression = keyword_node.value
        if isinstance(expression, ast.Name) and expression.id in input_names:
            input_bindings[target["uuid"]] = {"parameter": expression.id}
            param.pop(keyword_node.arg, None)
            continue
        producer = _result_reference(expression, available_results)
        if producer is not None:
            source_node, output_name = producer
            source_handle = _source_handle(
                source_node.handles, output_name, node=expression
            )
            pending_edges.append(
                _edge(
                    state.workflow_uuid,
                    source_node.node["uuid"],
                    node_uuid,
                    source_handle["uuid"],
                    target["uuid"],
                )
            )
            param.pop(keyword_node.arg, None)
            continue
        try:
            value = ast.literal_eval(expression)
            validate_json_value(value)
        except (TypeError, ValueError):
            _fail(
                "invalid_action_call",
                "Action value 必须是 literal、Workflow 参数或 named result output",
                node=expression,
            )
        param[keyword_node.arg] = value
    meta_data["unilab"]["input_bindings"] = dict(sorted(input_bindings.items()))
    node_type = str(template.get("node_type") or template.get("type") or "compute")
    node = _node_payload(
        applied,
        uuid=node_uuid,
        template_uuid=template["uuid"],
        parent_uuid=parent_uuid,
        name=result_name,
        node_type=node_type,
        param=param,
        meta_data=meta_data,
        action_name=call.func.attr,
    )
    node_state = _NodeState(node, template, handles, result_name, statement)
    state.nodes.append(node_state)
    state.edges.extend(pending_edges)
    available_results[result_name] = node_state
    state.results[result_name] = node_state
    return _Flow((node_uuid,), (node_uuid,))


def _parse_group(
    statement: ast.With,
    *,
    state: _BuildState,
    available_results: dict[str, _NodeState],
    outer_parent_uuid: str | None,
    allow_parallel: bool,
) -> _Flow:
    if outer_parent_uuid is not None:
        _fail(
            "unsupported_authoring_syntax",
            "02D 不支持嵌套 group",
            node=statement,
        )
    context = statement.items[0].context_expr
    assert isinstance(context, ast.Call)
    if context.args or any(item.arg is None for item in context.keywords):
        _fail(
            "invalid_group",
            "group 只接受 name keyword",
            node=context,
        )
    keywords = {item.arg: item.value for item in context.keywords if item.arg}
    if set(keywords) != {"name"}:
        _fail(
            "invalid_group",
            "group 必须且只能声明 name",
            node=context,
        )
    name_node = keywords["name"]
    if not isinstance(name_node, ast.Constant) or not isinstance(name_node.value, str):
        _fail("invalid_group", "group name 必须是字符串 literal", node=name_node)
    group_name = name_node.value.strip()
    if not group_name:
        _fail("invalid_group", "group name 不能为空", node=name_node)
    template, handles = _CatalogIndex(state.snapshot).group(node=statement)
    group_uuid = state.node_uuid(statement)
    applied = state.applied_nodes.get(group_uuid, {})
    meta_data = _node_metadata(applied.get("meta_data"), None)
    group_node = _node_payload(
        applied,
        uuid=group_uuid,
        template_uuid=template["uuid"],
        parent_uuid=None,
        name=group_name,
        node_type="group",
        param={},
        meta_data=meta_data,
        action_name=None,
    )
    state.nodes.append(_NodeState(group_node, template, handles, None, statement))
    if not statement.body or any(isinstance(item, ast.Pass) for item in statement.body):
        _fail("invalid_group", "group 必须包含可执行 action", node=statement)
    return _parse_sequence(
        statement.body,
        state=state,
        available_results=available_results,
        parent_uuid=group_uuid,
        allow_parallel=allow_parallel,
    )


def _parse_parallel(
    statement: ast.With,
    *,
    state: _BuildState,
    available_results: dict[str, _NodeState],
) -> _Flow:
    context = statement.items[0].context_expr
    assert isinstance(context, ast.Call)
    if context.args or context.keywords:
        _fail("invalid_parallel", "parallel 不接受参数", node=context)
    if len(statement.body) < 2 or any(
        not isinstance(item, ast.With) or _with_kind(item, state.imports) != "group"
        for item in statement.body
    ):
        _fail(
            "invalid_parallel",
            "parallel 必须直接包含至少两个 group branch",
            node=statement,
        )
    starts: list[str] = []
    ends: list[str] = []
    base_results = dict(available_results)
    new_results: dict[str, _NodeState] = {}
    for branch in statement.body:
        assert isinstance(branch, ast.With)
        local_results = dict(base_results)
        flow = _parse_group(
            branch,
            state=state,
            available_results=local_results,
            outer_parent_uuid=None,
            allow_parallel=False,
        )
        branch_new = {
            name: value
            for name, value in local_results.items()
            if name not in base_results
        }
        overlap = set(branch_new) & set(new_results)
        if overlap:
            _fail(
                "invalid_action_result",
                "parallel branches 不能声明同名 result",
                node=branch,
            )
        new_results.update(branch_new)
        starts.extend(flow.starts)
        ends.extend(flow.ends)
    available_results.update(new_results)
    state.results.update(new_results)
    return _Flow(tuple(starts), tuple(ends))


def _handles_by_business_name(
    handles: Sequence[dict[str, Any]],
    io_type: str,
    *,
    node: ast.AST,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for handle in handles:
        if handle.get("io_type") != io_type:
            continue
        name = str(handle.get("data_key") or handle.get("handle_key") or "").strip()
        if not name or name in result:
            _fail(
                "template_catalog_mismatch",
                "Handle business identity 不唯一",
                node=node,
            )
        result[name] = handle
    return result


def _result_reference(
    expression: ast.expr,
    results: Mapping[str, _NodeState],
) -> tuple[_NodeState, str] | None:
    if not (
        isinstance(expression, ast.Attribute) and isinstance(expression.value, ast.Name)
    ):
        return None
    producer = results.get(expression.value.id)
    if producer is None:
        _fail(
            "invalid_action_call",
            "named result output 必须来自当前可见的先前 Action",
            node=expression,
        )
    return producer, expression.attr


def _source_handle(
    handles: Sequence[dict[str, Any]],
    output_name: str,
    *,
    node: ast.AST,
) -> dict[str, Any]:
    sources = _handles_by_business_name(handles, "source", node=node)
    handle = sources.get(output_name)
    if handle is None or output_name == "ready":
        _fail(
            "template_catalog_mismatch",
            "named result 未匹配真实 source Handle",
            node=node,
        )
    return handle


def _node_metadata(
    raw: Any,
    selector: _Selector | None,
) -> dict[str, Any]:
    meta_data = _detached(raw) if isinstance(raw, dict) else {}
    unilab = meta_data.get("unilab")
    if not isinstance(unilab, dict):
        unilab = {}
    else:
        unilab = dict(unilab)
    for key in _OWNED_NODE_KEYS:
        unilab.pop(key, None)
    unilab["input_bindings"] = {}
    if selector is not None and selector.device_id is not None:
        unilab["executor_binding"] = {
            "mode": "fixed",
            "device_id": selector.device_id,
        }
    meta_data["unilab"] = unilab
    return meta_data


def _node_payload(
    applied: Mapping[str, Any],
    *,
    uuid: str,
    template_uuid: str,
    parent_uuid: str | None,
    name: str,
    node_type: str,
    param: dict[str, Any],
    meta_data: dict[str, Any],
    action_name: str | None,
) -> dict[str, Any]:
    base = {
        key: _detached(value)
        for key, value in applied.items()
        if key in WorkflowNodeWrite.model_fields
    }
    base.update(
        {
            "uuid": uuid,
            "workflow_node_template_uuid": template_uuid,
            "parent_uuid": parent_uuid,
            "name": name,
            "status": str(applied.get("status") or "idle"),
            "type": node_type,
            "pose": _detached(applied.get("pose") or {}),
            "param": param,
            "action_name": action_name,
            "execution_policy": _detached(applied.get("execution_policy") or {}),
            "disabled": bool(applied.get("disabled", False)),
            "minimized": bool(applied.get("minimized", False)),
            "meta_data": meta_data,
        }
    )
    return WorkflowNodeWrite.model_validate(base).model_dump(exclude_none=True)


def _edge(
    workflow_uuid: str,
    source_node_uuid: str,
    target_node_uuid: str,
    source_handle_uuid: str,
    target_handle_uuid: str,
) -> dict[str, Any]:
    edge_uuid = str(
        uuid5(
            UUID(workflow_uuid),
            "authoring-edge:"
            f"{source_node_uuid}:{source_handle_uuid}:"
            f"{target_node_uuid}:{target_handle_uuid}",
        )
    )
    return WorkflowEdgeWrite(
        uuid=edge_uuid,
        source_node_uuid=source_node_uuid,
        target_node_uuid=target_node_uuid,
        source_handle_uuid=source_handle_uuid,
        target_handle_uuid=target_handle_uuid,
        meta_data={},
    ).model_dump(exclude_none=True)


def _connect_dependencies(
    state: _BuildState,
    sources: Sequence[str],
    targets: Sequence[str],
    *,
    node: ast.AST,
) -> None:
    nodes = {item.node["uuid"]: item for item in state.nodes}
    for source_uuid in sources:
        for target_uuid in targets:
            if source_uuid == target_uuid:
                continue
            if any(
                edge["source_node_uuid"] == source_uuid
                and edge["target_node_uuid"] == target_uuid
                for edge in state.edges
            ):
                continue
            source = nodes[source_uuid]
            target = nodes[target_uuid]
            source_handle = _ready_handle(source.handles, "source", node=node)
            target_handle = _ready_handle(target.handles, "target", node=node)
            state.edges.append(
                _edge(
                    state.workflow_uuid,
                    source_uuid,
                    target_uuid,
                    source_handle["uuid"],
                    target_handle["uuid"],
                )
            )


def _ready_handle(
    handles: Sequence[dict[str, Any]],
    io_type: str,
    *,
    node: ast.AST,
) -> dict[str, Any]:
    matches = [
        handle
        for handle in handles
        if handle.get("io_type") == io_type
        and str(handle.get("handle_key") or "").strip().lower() == "ready"
    ]
    if len(matches) != 1:
        _fail(
            "template_catalog_mismatch",
            "source-order dependency 缺少唯一真实 ready Handle",
            node=node,
        )
    return matches[0]


def _workflow_outputs(
    statement: ast.Return | None,
    *,
    input_contract: dict[str, Any],
    results: Mapping[str, _NodeState],
    imports: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if statement is None:
        return {"version": 1, "outputs": []}, {}
    value = statement.value
    if not isinstance(value, ast.Call):
        _fail(
            "invalid_workflow_output",
            "Workflow return 必须调用 workflow_output",
            node=statement,
        )
    if _call_identity(value, imports) != _WORKFLOW_OUTPUT:
        _fail(
            "invalid_workflow_output",
            "Workflow return 必须调用 workflow_output",
            node=value,
        )
    if value.args or any(item.arg is None for item in value.keywords):
        _fail(
            "invalid_workflow_output",
            "workflow_output 只接受命名参数",
            node=value,
        )
    names = [str(item.arg) for item in value.keywords]
    if len(names) != len(set(names)) or any(not name for name in names):
        _fail(
            "invalid_workflow_output",
            "workflow_output 名称必须非空且唯一",
            node=value,
        )
    parameters = {item["name"]: item for item in input_contract.get("parameters", [])}
    outputs: list[dict[str, Any]] = []
    bindings: dict[str, dict[str, Any]] = {}
    for keyword_node in value.keywords:
        assert keyword_node.arg is not None
        expression = keyword_node.value
        if isinstance(expression, ast.Name) and expression.id in parameters:
            parameter = parameters[expression.id]
            descriptor = {
                "name": keyword_node.arg,
                "schema": _detached(parameter["schema"]),
            }
            for key in ("title", "description"):
                if key in parameter:
                    descriptor[key] = parameter[key]
            outputs.append(descriptor)
            bindings[keyword_node.arg] = {
                "kind": "workflow_input",
                "parameter": expression.id,
            }
            continue
        producer = _result_reference(expression, results)
        if producer is None:
            _fail(
                "invalid_workflow_output",
                "Workflow output 必须绑定 Workflow input 或 named result output",
                node=expression,
            )
        node_state, output_name = producer
        handle = _source_handle(node_state.handles, output_name, node=expression)
        outputs.append(
            {
                "name": keyword_node.arg,
                "schema": _schema_from_handle(handle),
            }
        )
        bindings[keyword_node.arg] = {
            "kind": "node_output",
            "workflow_node_uuid": node_state.node["uuid"],
            "source_handle_uuid": handle["uuid"],
        }
    try:
        contract = parse_output_contract({"version": 1, "outputs": outputs}).to_dict()
    except WorkflowSchemaError as error:
        _fail(error.code, error.message, node=statement)
    return contract, bindings


def _schema_from_handle(handle: Mapping[str, Any]) -> dict[str, Any]:
    raw = str(handle.get("type") or "").strip().lower()
    scalars = {
        "str": "string",
        "string": "string",
        "int": "integer",
        "integer": "integer",
        "float": "number",
        "number": "number",
        "bool": "boolean",
        "boolean": "boolean",
        "dict": "object",
        "object": "object",
        "json": "object",
    }
    if raw == "resourceslot":
        schema: dict[str, Any] = {"$slot": "ResourceSlot"}
    elif raw.startswith("list[") and raw.endswith("]"):
        inner = raw[5:-1].strip()
        if inner == "resourceslot":
            items: dict[str, Any] = {"$slot": "ResourceSlot"}
        elif inner in scalars:
            items = {"type": scalars[inner]}
        else:
            _fail(
                "template_catalog_mismatch",
                "Handle type 不属于 Workflow v1 schema",
            )
        schema = {"type": "array", "items": items}
    elif raw in scalars:
        schema = {"type": scalars[raw]}
    else:
        _fail(
            "template_catalog_mismatch",
            "Handle type 不属于 Workflow v1 schema",
        )
    unilab = handle.get("meta_data", {}).get("unilab", {})
    if isinstance(unilab, dict):
        allowlist = unilab.get("allowed_resource_template_uuids")
        if allowlist is not None:
            target = schema.get("items", schema)
            if isinstance(target, dict) and target.get("$slot") == "ResourceSlot":
                target["allowed_resource_template_uuids"] = _detached(allowlist)
    return schema


def _candidate_graph(
    state: _BuildState,
    *,
    displayname: str,
    description: str | None,
    input_contract: dict[str, Any],
    output_contract: dict[str, Any],
    output_bindings: dict[str, Any],
) -> dict[str, Any]:
    workflow = _detached(state.applied_graph["workflow"])
    workflow["name"] = displayname
    if description is None:
        workflow.pop("description", None)
    else:
        workflow["description"] = description
    meta_data = workflow.get("meta_data")
    if not isinstance(meta_data, dict):
        meta_data = {}
    else:
        meta_data = dict(meta_data)
    unilab = meta_data.get("unilab")
    if not isinstance(unilab, dict):
        unilab = {}
    else:
        unilab = dict(unilab)
    for key in _OWNED_WORKFLOW_KEYS:
        unilab.pop(key, None)
    unilab.update(
        {
            "input_contract": input_contract,
            "output_contract": output_contract,
            "output_bindings": output_bindings,
        }
    )
    meta_data["unilab"] = unilab
    workflow["meta_data"] = meta_data

    referenced_templates = {item.template["uuid"] for item in state.nodes}
    node_templates = sorted(
        (
            _detached(item)
            for item in state.snapshot.node_templates
            if item["uuid"] in referenced_templates
        ),
        key=lambda item: item["uuid"],
    )
    handle_templates = sorted(
        (
            _detached(item)
            for item in state.snapshot.handle_templates
            if item["workflow_node_template_uuid"] in referenced_templates
        ),
        key=lambda item: item["uuid"],
    )
    return {
        "workflow": workflow,
        "nodes": [item.node for item in state.nodes],
        "edges": sorted(state.edges, key=lambda item: item["uuid"]),
        "node_templates": node_templates,
        "handle_templates": handle_templates,
    }


def _validate_built_graph(graph: dict[str, Any]) -> None:
    templates = {item["uuid"]: item for item in graph["node_templates"]}
    handles = {item["uuid"]: item for item in graph["handle_templates"]}
    nodes = [WorkflowNodeWrite.model_validate(item) for item in graph["nodes"]]
    edges = [WorkflowEdgeWrite.model_validate(item) for item in graph["edges"]]
    validate_graph(
        nodes=nodes,
        edges=edges,
        templates=templates,
        handles=handles,
        effective_params={node.uuid: node.param or {} for node in nodes},
        workflow_meta_data=graph["workflow"].get("meta_data") or {},
        node_meta_data={node.uuid: node.meta_data for node in nodes},
    )


def _changeset(
    graph: dict[str, Any],
    applied: dict[str, Any],
) -> dict[str, Any]:
    candidate_nodes = {item["uuid"]: _semantic_node(item) for item in graph["nodes"]}
    applied_nodes = {item["uuid"]: _semantic_node(item) for item in applied["nodes"]}
    candidate_edges = {item["uuid"]: _semantic_edge(item) for item in graph["edges"]}
    applied_edges = {item["uuid"]: _semantic_edge(item) for item in applied["edges"]}
    created_nodes = sorted(set(candidate_nodes) - set(applied_nodes))
    deleted_nodes = sorted(set(applied_nodes) - set(candidate_nodes))
    updated_nodes = sorted(
        uuid
        for uuid in set(candidate_nodes) & set(applied_nodes)
        if not strict_json_equal(candidate_nodes[uuid], applied_nodes[uuid])
    )
    created_edges = sorted(set(candidate_edges) - set(applied_edges))
    deleted_edges = sorted(set(applied_edges) - set(candidate_edges))
    updated_edges = sorted(
        uuid
        for uuid in set(candidate_edges) & set(applied_edges)
        if not strict_json_equal(candidate_edges[uuid], applied_edges[uuid])
    )
    candidate_unilab = (graph["workflow"].get("meta_data") or {}).get("unilab")
    applied_unilab = (applied["workflow"].get("meta_data") or {}).get("unilab")
    reserved_changed = not strict_json_equal(candidate_unilab, applied_unilab)
    workflow_changed = any(
        graph["workflow"].get(key) != applied["workflow"].get(key)
        for key in ("name", "description")
    )
    changed = (
        any(
            (
                created_nodes,
                updated_nodes,
                deleted_nodes,
                created_edges,
                updated_edges,
                deleted_edges,
            )
        )
        or reserved_changed
        or workflow_changed
    )
    return {
        "kind": "graph" if changed else "source_only",
        "created_node_uuids": created_nodes,
        "updated_node_uuids": updated_nodes,
        "deleted_node_uuids": deleted_nodes,
        "created_edge_uuids": created_edges,
        "updated_edge_uuids": updated_edges,
        "deleted_edge_uuids": deleted_edges,
        "reserved_metadata_changed": reserved_changed,
    }


def _semantic_node(item: Mapping[str, Any]) -> dict[str, Any]:
    return WorkflowNodeWrite.model_validate(
        {key: item[key] for key in WorkflowNodeWrite.model_fields if key in item}
    ).model_dump()


def _semantic_edge(item: Mapping[str, Any]) -> dict[str, Any]:
    return WorkflowEdgeWrite.model_validate(
        {key: item[key] for key in WorkflowEdgeWrite.model_fields if key in item}
    ).model_dump()


def _source_only_changeset() -> dict[str, Any]:
    return {
        "kind": "source_only",
        "created_node_uuids": [],
        "updated_node_uuids": [],
        "deleted_node_uuids": [],
        "created_edge_uuids": [],
        "updated_edge_uuids": [],
        "deleted_edge_uuids": [],
        "reserved_metadata_changed": False,
    }


def _generate_with_snapshot(
    *,
    snapshot: TemplateCatalogSnapshot,
    workflow_uuid: str,
    workflow_revision: int,
    graph: dict[str, Any],
    source_uri: str,
) -> CandidateCompilation:
    del source_uri
    try:
        candidate = _require_graph_identity(
            graph,
            workflow_uuid=workflow_uuid,
            workflow_revision=workflow_revision,
        )
        _validate_catalog_projection(snapshot, candidate)
        _validate_built_graph(candidate)
        source, source_map = _render_graph(candidate)
        recompiled = _compile_with_snapshot(
            snapshot=snapshot,
            workflow_uuid=workflow_uuid,
            workflow_revision=workflow_revision,
            python_source=source,
            source_uri="authoring://round-trip-proof",
            applied_graph=candidate,
        )
        if (
            not recompiled.valid
            or not _semantic_graph_equal(recompiled.graph, candidate)
            or recompiled.normalized_python_source != source
        ):
            _fail(
                "round_trip_mismatch",
                "Candidate graph 不能证明为等价的 Python",
            )
        return CandidateCompilation(
            diagnostics=[],
            graph=candidate,
            normalized_python_source=source,
            source_map=source_map,
            changeset=_source_only_changeset(),
            compiler_version=_COMPILER_VERSION,
            template_catalog_fingerprint=snapshot.fingerprint,
        )
    except _AuthoringFailure as error:
        return _error_result(
            fingerprint=snapshot.fingerprint,
            diagnostic=_diagnostic(error, ""),
        )
    except (GraphValidationError, TypeError, ValueError):
        return _error_result(
            fingerprint=snapshot.fingerprint,
            diagnostic={
                "severity": "error",
                "code": "candidate_invalid",
                "message": "Candidate graph 不满足 Workflow 合同",
            },
        )


def _validate_catalog_projection(
    snapshot: TemplateCatalogSnapshot,
    graph: dict[str, Any],
) -> None:
    referenced = {
        node.get("workflow_node_template_uuid")
        for node in graph["nodes"]
        if node.get("workflow_node_template_uuid") is not None
    }
    snapshot_nodes = {
        item["uuid"]: _detached(item)
        for item in snapshot.node_templates
        if item["uuid"] in referenced
    }
    projected_nodes = {item.get("uuid"): item for item in graph["node_templates"]}
    if (
        set(snapshot_nodes) != referenced
        or len(projected_nodes) != len(graph["node_templates"])
        or set(projected_nodes) != set(snapshot_nodes)
        or any(
            not strict_json_equal(projected_nodes[uuid], snapshot_nodes[uuid])
            for uuid in snapshot_nodes
        )
    ):
        _fail(
            "template_catalog_mismatch",
            "Candidate NodeTemplate projection 不属于当前 authority snapshot",
        )
    snapshot_handles = {
        item["uuid"]: _detached(item)
        for item in snapshot.handle_templates
        if item["workflow_node_template_uuid"] in referenced
    }
    projected_handles = {item.get("uuid"): item for item in graph["handle_templates"]}
    if (
        len(projected_handles) != len(graph["handle_templates"])
        or set(projected_handles) != set(snapshot_handles)
        or any(
            not strict_json_equal(projected_handles[uuid], snapshot_handles[uuid])
            for uuid in snapshot_handles
        )
    ):
        _fail(
            "template_catalog_mismatch",
            "Candidate HandleTemplate projection 不属于当前 authority snapshot",
        )


@dataclass(slots=True)
class _Emitter:
    lines: list[str] = field(default_factory=list)
    source_map: list[dict[str, Any]] = field(default_factory=list)

    def emit(self, line: str = "") -> int:
        self.lines.append(line)
        return len(self.lines)

    def anchored(self, node_uuid: str, construct: str, *, indent: str) -> None:
        start = self.emit(f"{indent}# unilab:node_uuid={node_uuid}")
        end = self.emit(f"{indent}{construct}")
        self.source_map.append(
            {
                "workflow_node_uuid": node_uuid,
                "start_line": start,
                "start_column": len(indent) + 1,
                "end_line": end,
                "end_column": len(indent) + len(construct) + 1,
            }
        )


def _render_graph(graph: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    workflow = graph["workflow"]
    unilab = (workflow.get("meta_data") or {}).get("unilab") or {}
    try:
        input_contract = parse_input_contract(
            unilab.get("input_contract", {"version": 1, "parameters": []})
        ).to_dict()
        output_contract = parse_output_contract(
            unilab.get("output_contract", {"version": 1, "outputs": []})
        ).to_dict()
    except WorkflowSchemaError as error:
        _fail(error.code, error.message)
    output_bindings = unilab.get("output_bindings", {})
    if not isinstance(output_bindings, dict) or set(output_bindings) != {
        item["name"] for item in output_contract["outputs"]
    }:
        _fail("candidate_invalid", "Workflow output bindings 不完整")

    templates = {item["uuid"]: item for item in graph["node_templates"]}
    handles = {item["uuid"]: item for item in graph["handle_templates"]}
    handles_by_node: dict[str, list[dict[str, Any]]] = {}
    for handle in graph["handle_templates"]:
        handles_by_node.setdefault(
            handle["workflow_node_template_uuid"],
            [],
        ).append(handle)
    nodes = list(graph["nodes"])
    node_by_uuid = {item["uuid"]: item for item in nodes}
    if len(node_by_uuid) != len(nodes):
        _fail("candidate_invalid", "Candidate Node UUID 重复")
    edge_by_target: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in graph["edges"]:
        key = (edge["target_node_uuid"], edge["target_handle_uuid"])
        if key in edge_by_target:
            _fail("candidate_invalid", "同一 target Handle 有多条入边")
        edge_by_target[key] = edge

    selectors, selector_by_node = _render_selectors(nodes, templates)
    needs = _annotation_import_needs(input_contract)
    emitter = _Emitter()
    if needs["typing"]:
        emitter.emit(f"from typing import {', '.join(sorted(needs['typing']))}")
    if needs["field"]:
        emitter.emit("from pydantic import Field")
    device_imports: dict[str, set[str]] = {}
    for class_identity, _device_id in selectors:
        module, symbol = class_identity.rsplit(":", 1)
        device_imports.setdefault(module, set()).add(symbol)
    for module in sorted(device_imports):
        emitter.emit(
            f"from {module} import {', '.join(sorted(device_imports[module]))}"
        )
    if needs["json_value"]:
        emitter.emit("from unilabos.registry.annotations import JSONValue")
    if needs["resource_slot"]:
        emitter.emit("from unilabos.registry.placeholder_type import ResourceSlot")
    markers = {"workflow_definition", "workflow_output"}
    if selectors:
        markers.add("device")
    group_nodes = [item for item in nodes if _is_group_node(item, templates)]
    root_layers = _root_construct_layers(nodes, graph["edges"], templates)
    if group_nodes:
        markers.add("group")
    if any(len(layer) > 1 for layer in root_layers):
        markers.add("parallel")
    emitter.emit(
        "from unilabos.workflow.authoring import " + ", ".join(sorted(markers))
    )
    emitter.emit()
    emitter.emit()
    for (class_identity, device_id), local_name in selectors.items():
        symbol = class_identity.rsplit(":", 1)[1]
        argument = "" if device_id is None else repr(device_id)
        emitter.emit(f"{local_name}: {symbol} = device({argument})")
    if selectors:
        emitter.emit()
        emitter.emit()

    description = workflow.get("description")
    emitter.emit("@workflow_definition(")
    emitter.emit(f"    workflow_uuid={workflow['uuid']!r},")
    emitter.emit(f"    displayname={workflow['name']!r},")
    if description is not None:
        emitter.emit(f"    description={description!r},")
    emitter.emit(")")
    function_name = _snake_case(str(workflow.get("name") or "workflow"), "workflow")
    parameters = input_contract["parameters"]
    if parameters:
        emitter.emit(f"def {function_name}(")
        emitter.emit("    *,")
        for parameter in parameters:
            declaration = f"{parameter['name']}: {_annotation_source(parameter['schema'], parameter)}"
            if not parameter["required"]:
                declaration += f" = {parameter['default']!r}"
            emitter.emit(f"    {declaration},")
        emitter.emit("):")
    else:
        emitter.emit(f"def {function_name}():")

    body_indent = "    "
    for layer in root_layers:
        if len(layer) > 1:
            emitter.emit(f"{body_indent}with parallel():")
            for group_node in layer:
                _emit_group(
                    emitter,
                    group_node,
                    indent=body_indent + "    ",
                    nodes=nodes,
                    edges=graph["edges"],
                    templates=templates,
                    handles_by_node=handles_by_node,
                    handles=handles,
                    node_by_uuid=node_by_uuid,
                    edge_by_target=edge_by_target,
                    selector_by_node=selector_by_node,
                )
            continue
        construct = layer[0]
        if _is_group_node(construct, templates):
            _emit_group(
                emitter,
                construct,
                indent=body_indent,
                nodes=nodes,
                edges=graph["edges"],
                templates=templates,
                handles_by_node=handles_by_node,
                handles=handles,
                node_by_uuid=node_by_uuid,
                edge_by_target=edge_by_target,
                selector_by_node=selector_by_node,
            )
        else:
            _emit_action(
                emitter,
                construct,
                indent=body_indent,
                templates=templates,
                handles_by_node=handles_by_node,
                handles=handles,
                node_by_uuid=node_by_uuid,
                edge_by_target=edge_by_target,
                selector_by_node=selector_by_node,
            )

    if output_contract["outputs"]:
        parts = []
        for output in output_contract["outputs"]:
            name = output["name"]
            parts.append(
                f"{name}={_output_expression(output_bindings[name], node_by_uuid, handles)}"
            )
        emitter.emit(f"{body_indent}return workflow_output({', '.join(parts)})")
    elif not nodes:
        emitter.emit(f"{body_indent}pass")
    source = "\n".join(emitter.lines).rstrip() + "\n"
    return source, emitter.source_map


def _is_group_node(
    node: Mapping[str, Any],
    templates: Mapping[str, dict[str, Any]],
) -> bool:
    template = templates.get(str(node.get("workflow_node_template_uuid")), {})
    return (
        str(node.get("type") or "").lower() == "group"
        or str(template.get("node_type") or "").lower() == "group"
    )


def _root_construct_layers(
    nodes: Sequence[dict[str, Any]],
    edges: Sequence[dict[str, Any]],
    templates: Mapping[str, dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Collapse presentation groups and recover the only representable root order."""

    group_uuids = {item["uuid"] for item in nodes if _is_group_node(item, templates)}
    roots: dict[str, dict[str, Any]] = {}
    owner_by_node: dict[str, str] = {}
    for node in nodes:
        node_uuid = node["uuid"]
        parent_uuid = node.get("parent_uuid")
        if node_uuid in group_uuids:
            if parent_uuid is not None:
                _fail("candidate_invalid", "02D 不支持嵌套 group graph")
            owner_by_node[node_uuid] = node_uuid
            roots[node_uuid] = node
            continue
        if parent_uuid is None:
            owner_by_node[node_uuid] = node_uuid
            roots[node_uuid] = node
            continue
        if parent_uuid not in group_uuids:
            _fail("candidate_invalid", "Action parent 不是当前 graph 的 group")
        owner_by_node[node_uuid] = parent_uuid

    dependencies: dict[str, set[str]] = {uuid: set() for uuid in roots}
    for edge in edges:
        source_owner = owner_by_node.get(str(edge.get("source_node_uuid")))
        target_owner = owner_by_node.get(str(edge.get("target_node_uuid")))
        if source_owner is None or target_owner is None:
            _fail("candidate_invalid", "Edge 引用了未知 Node")
        if source_owner != target_owner:
            dependencies[target_owner].add(source_owner)

    remaining = set(roots)
    layers: list[list[dict[str, Any]]] = []
    while remaining:
        ready = sorted(
            (uuid for uuid in remaining if not (dependencies[uuid] & remaining)),
        )
        if not ready:
            _fail("candidate_invalid", "Candidate graph 包含循环依赖")
        if len(ready) > 1 and any(uuid not in group_uuids for uuid in ready):
            _fail(
                "round_trip_mismatch",
                "02D Python 无法表达未分组的并行 action",
            )
        layers.append([roots[uuid] for uuid in ready])
        remaining.difference_update(ready)
    return layers


def _render_selectors(
    nodes: Sequence[dict[str, Any]],
    templates: Mapping[str, dict[str, Any]],
) -> tuple[
    dict[tuple[str, str | None], str],
    dict[str, str],
]:
    selectors: dict[tuple[str, str | None], str] = {}
    by_node: dict[str, str] = {}
    used: set[str] = set()
    for node in nodes:
        if _is_group_node(node, templates):
            continue
        template = templates.get(str(node.get("workflow_node_template_uuid")))
        if template is None:
            _fail(
                "template_catalog_mismatch",
                "Node 未引用当前 graph 的 NodeTemplate",
            )
        class_identity = template.get("class")
        if not isinstance(class_identity, str) or ":" not in class_identity:
            _fail(
                "template_catalog_mismatch",
                "NodeTemplate class 不是 module:symbol identity",
            )
        executor = (
            (node.get("meta_data") or {}).get("unilab", {}).get("executor_binding")
        )
        device_id: str | None = None
        if executor is not None:
            if (
                not isinstance(executor, dict)
                or executor.get("mode") != "fixed"
                or not isinstance(executor.get("device_id"), str)
                or not executor["device_id"]
                or set(executor) != {"mode", "device_id"}
            ):
                _fail("candidate_invalid", "executor_binding 不符合固定 selector 合同")
            device_id = executor["device_id"]
        key = (class_identity, device_id)
        local = selectors.get(key)
        if local is None:
            base = _snake_case(class_identity.rsplit(":", 1)[1], "device")
            local = base
            suffix = 2
            while local in used:
                local = f"{base}_{suffix}"
                suffix += 1
            used.add(local)
            selectors[key] = local
        by_node[node["uuid"]] = local
    return selectors, by_node


def _annotation_import_needs(contract: Mapping[str, Any]) -> dict[str, Any]:
    typing_names: set[str] = set()
    field_needed = False
    resource_slot = False
    json_value = False
    for parameter in contract.get("parameters", []):
        schema = parameter["schema"]
        rendered = _annotation_source(schema, parameter)
        if "Annotated[" in rendered:
            typing_names.add("Annotated")
        if "Literal[" in rendered:
            typing_names.add("Literal")
        if "Field(" in rendered:
            field_needed = True
        if "ResourceSlot" in rendered:
            resource_slot = True
        if "JSONValue" in rendered:
            json_value = True
    return {
        "typing": typing_names,
        "field": field_needed,
        "resource_slot": resource_slot,
        "json_value": json_value,
    }


def _annotation_source(
    schema: Mapping[str, Any],
    descriptor: Mapping[str, Any] | None = None,
) -> str:
    if "anyOf" in schema:
        members = schema["anyOf"]
        base = _base_annotation_source(members[0]) + " | None"
        constraint_schema = members[0]
    else:
        base = _base_annotation_source(schema)
        constraint_schema = schema
    field_parts: list[str] = []
    descriptor = descriptor or {}
    if "title" in descriptor:
        field_parts.append(f"title={descriptor['title']!r}")
    if "description" in descriptor:
        field_parts.append(f"description={descriptor['description']!r}")
    for schema_key, field_key in (
        ("minimum", "ge"),
        ("maximum", "le"),
        ("minLength", "min_length"),
        ("maxLength", "max_length"),
        ("minItems", "min_length"),
        ("maxItems", "max_length"),
    ):
        if schema_key in constraint_schema:
            field_parts.append(f"{field_key}={constraint_schema[schema_key]!r}")
    if field_parts:
        return f"Annotated[{base}, Field({', '.join(field_parts)})]"
    return base


def _base_annotation_source(schema: Mapping[str, Any]) -> str:
    if schema.get("$slot") == "ResourceSlot":
        if schema.get("allowed_resource_template_uuids"):
            _fail(
                "template_catalog_mismatch",
                "当前版本无法从 UUID allowlist 恢复 ResourceTemplate symbol",
            )
        return "ResourceSlot"
    kind = schema.get("type")
    if kind == "array":
        return f"list[{_base_annotation_source(schema['items'])}]"
    if "enum" in schema:
        return f"Literal[{', '.join(repr(item) for item in schema['enum'])}]"
    names = {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "object": "dict[str, JSONValue]",
    }
    result = names.get(kind)
    if result is None:
        _fail("invalid_schema", "Workflow schema 不能投影为 Python annotation")
    return result


def _emit_group(
    emitter: _Emitter,
    group_node: dict[str, Any],
    *,
    indent: str,
    nodes: Sequence[dict[str, Any]],
    edges: Sequence[dict[str, Any]],
    templates: Mapping[str, dict[str, Any]],
    handles_by_node: Mapping[str, list[dict[str, Any]]],
    handles: Mapping[str, dict[str, Any]],
    node_by_uuid: Mapping[str, dict[str, Any]],
    edge_by_target: Mapping[tuple[str, str], dict[str, Any]],
    selector_by_node: Mapping[str, str],
) -> None:
    emitter.anchored(
        group_node["uuid"],
        f"with group(name={group_node['name']!r}):",
        indent=indent,
    )
    children = _ordered_group_children(group_node["uuid"], nodes, edges)
    if not children:
        _fail("candidate_invalid", "group 没有 executable child")
    for child in children:
        if _is_group_node(child, templates):
            _fail("candidate_invalid", "02D 不支持嵌套 group graph")
        _emit_action(
            emitter,
            child,
            indent=indent + "    ",
            templates=templates,
            handles_by_node=handles_by_node,
            handles=handles,
            node_by_uuid=node_by_uuid,
            edge_by_target=edge_by_target,
            selector_by_node=selector_by_node,
        )


def _ordered_group_children(
    group_uuid: str,
    nodes: Sequence[dict[str, Any]],
    edges: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    children = {
        item["uuid"]: item for item in nodes if item.get("parent_uuid") == group_uuid
    }
    dependencies: dict[str, set[str]] = {uuid: set() for uuid in children}
    for edge in edges:
        source_uuid = str(edge.get("source_node_uuid"))
        target_uuid = str(edge.get("target_node_uuid"))
        if source_uuid in children and target_uuid in children:
            dependencies[target_uuid].add(source_uuid)
    remaining = set(children)
    ordered: list[dict[str, Any]] = []
    while remaining:
        ready = sorted(
            uuid for uuid in remaining if not (dependencies[uuid] & remaining)
        )
        if len(ready) != 1:
            _fail(
                "round_trip_mismatch",
                "group 内 action 不是可证明的唯一顺序",
            )
        ordered.append(children[ready[0]])
        remaining.remove(ready[0])
    return ordered


def _emit_action(
    emitter: _Emitter,
    node: dict[str, Any],
    *,
    indent: str,
    templates: Mapping[str, dict[str, Any]],
    handles_by_node: Mapping[str, list[dict[str, Any]]],
    handles: Mapping[str, dict[str, Any]],
    node_by_uuid: Mapping[str, dict[str, Any]],
    edge_by_target: Mapping[tuple[str, str], dict[str, Any]],
    selector_by_node: Mapping[str, str],
) -> None:
    template_uuid = node.get("workflow_node_template_uuid")
    template = templates.get(str(template_uuid))
    if template is None:
        _fail("template_catalog_mismatch", "Action NodeTemplate 不存在")
    input_bindings = (
        (node.get("meta_data") or {})
        .get("unilab", {})
        .get(
            "input_bindings",
            {},
        )
    )
    if not isinstance(input_bindings, dict):
        _fail("candidate_invalid", "Node input_bindings 必须是对象")
    parameters: list[str] = []
    for handle in sorted(
        handles_by_node.get(str(template_uuid), []),
        key=lambda item: str(item.get("data_key") or item.get("handle_key") or ""),
    ):
        if handle.get("io_type") != "target":
            continue
        name = str(handle.get("data_key") or handle.get("handle_key") or "").strip()
        if name.lower() == "ready":
            continue
        expression: str | None = None
        binding = input_bindings.get(handle["uuid"])
        if binding is not None:
            if not isinstance(binding, dict) or set(binding) - {"parameter", "source"}:
                _fail("candidate_invalid", "Node input binding 不符合合同")
            expression = str(binding.get("parameter") or "")
            if not expression:
                _fail("candidate_invalid", "Node input binding 缺少 parameter")
        edge = edge_by_target.get((node["uuid"], handle["uuid"]))
        if edge is not None:
            if expression is not None:
                _fail("candidate_invalid", "target Handle 有多个 provider")
            producer = node_by_uuid.get(edge["source_node_uuid"])
            source_handle = handles.get(edge["source_handle_uuid"])
            if producer is None or source_handle is None:
                _fail("candidate_invalid", "Edge identity 不完整")
            source_name = str(
                source_handle.get("data_key") or source_handle.get("handle_key") or ""
            )
            if source_name.lower() != "ready":
                expression = (
                    f"{_safe_identifier(str(producer['name']), 'result')}."
                    f"{_safe_identifier(source_name, 'value')}"
                )
        param = node.get("param") or {}
        if name in param:
            if expression is not None:
                _fail("candidate_invalid", "target Handle 有多个 provider")
            expression = repr(param[name])
        if expression is not None:
            parameters.append(f"{name}={expression}")
    result_name = _safe_identifier(str(node.get("name") or "result"), "result")
    selector = selector_by_node.get(node["uuid"])
    if selector is None:
        _fail("candidate_invalid", "Action Node 缺少 device selector")
    action = _safe_identifier(str(template.get("name") or "action"), "action")
    call = f"{selector}.{action}({', '.join(parameters)})"
    emitter.anchored(
        node["uuid"],
        f"{result_name} = {call}",
        indent=indent,
    )


def _output_expression(
    binding: Mapping[str, Any],
    nodes: Mapping[str, dict[str, Any]],
    handles: Mapping[str, dict[str, Any]],
) -> str:
    if binding.get("kind") == "workflow_input":
        parameter = binding.get("parameter")
        if not isinstance(parameter, str) or not parameter:
            _fail("candidate_invalid", "Workflow input output binding 不正确")
        return parameter
    if binding.get("kind") == "node_output":
        node = nodes.get(str(binding.get("workflow_node_uuid")))
        handle = handles.get(str(binding.get("source_handle_uuid")))
        if node is None or handle is None or handle.get("io_type") != "source":
            _fail("candidate_invalid", "Node output binding identity 不正确")
        output_name = str(handle.get("data_key") or handle.get("handle_key") or "")
        return (
            f"{_safe_identifier(str(node.get('name') or 'result'), 'result')}."
            f"{_safe_identifier(output_name, 'value')}"
        )
    _fail("candidate_invalid", "Workflow output binding kind 不支持")


def _semantic_graph_equal(left: Any, right: Any) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    try:
        left_workflow = _detached(left["workflow"])
        right_workflow = _detached(right["workflow"])
        for workflow in (left_workflow, right_workflow):
            workflow.pop("create_time", None)
            workflow.pop("update_time", None)
        left_nodes = sorted(
            (_semantic_node(item) for item in left["nodes"]),
            key=lambda item: item["uuid"],
        )
        right_nodes = sorted(
            (_semantic_node(item) for item in right["nodes"]),
            key=lambda item: item["uuid"],
        )
        left_edges = sorted(
            (_semantic_edge(item) for item in left["edges"]),
            key=lambda item: item["uuid"],
        )
        right_edges = sorted(
            (_semantic_edge(item) for item in right["edges"]),
            key=lambda item: item["uuid"],
        )
        return all(
            (
                strict_json_equal(left_workflow, right_workflow),
                strict_json_equal(left_nodes, right_nodes),
                strict_json_equal(left_edges, right_edges),
                strict_json_equal(
                    sorted(left["node_templates"], key=lambda item: item["uuid"]),
                    sorted(right["node_templates"], key=lambda item: item["uuid"]),
                ),
                strict_json_equal(
                    sorted(left["handle_templates"], key=lambda item: item["uuid"]),
                    sorted(right["handle_templates"], key=lambda item: item["uuid"]),
                ),
            )
        )
    except (KeyError, TypeError, ValueError):
        return False


__all__ = ["WorkflowAuthoringEngine"]
