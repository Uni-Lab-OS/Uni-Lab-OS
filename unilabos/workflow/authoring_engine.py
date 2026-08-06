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
from uuid import uuid4

from unilabos.registry.action_result_schema import (
    ActionResultSchemaError,
    parse_action_result_declaration,
)
from unilabos.registry.annotation_schema import (
    NO_DEFAULT,
    AnnotationSchemaError,
    parse_parameter_annotation,
)
from unilabos.registry.utils import parse_docstring
from unilabos.workflow.authoring_identity import authoring_edge
from unilabos.workflow.catalog import (
    CatalogAuthority,
    ResourceTemplateIdentityIndex,
    TemplateCatalog,
    TemplateCatalogError,
    TemplateCatalogSnapshot,
    TemplateCatalogUnavailable,
)
from unilabos.workflow.composite import (
    CompositeAuthoring,
    CompositeExpansion,
    classify_published_workflow_compatibility_projections,
    published_workflow_compatibility_projection,
    published_workflow_projection_is_canonical,
)
from unilabos.workflow.graph_validation import (
    CodedGraphValidationError,
    GraphValidationError,
    validate_graph,
)
from unilabos.workflow.json_codec import (
    clone_json,
    decode_json_bytes,
    encode_json,
    strict_json_equal,
)
from unilabos.workflow.material_source import (
    MaterialSourceAuthorityError,
    MaterialSourceStaticAuthority,
    resolve_resource_ref,
    validate_material_source_authority,
)
from unilabos.workflow.models import (
    CandidateCompilation,
    WorkflowEdgeWrite,
    WorkflowNodeWrite,
    resolve_template_root_param,
    validate_json_value,
    validate_uuid,
)
from unilabos.workflow.schema import (
    WorkflowSchemaError,
    parse_input_contract,
    parse_output_contract,
)
from unilabos.workflow.source_coordinates import (
    codepoint_offset_to_utf16_column,
    require_utf8_text,
    source_lines,
    utf8_offset_to_utf16_column,
    utf16_length,
)
from unilabos.workflow.workflow_io import (
    WorkflowIOValidationError,
    resource_slot_passthrough_is_compatible,
)

_COMPILER_VERSION = "unilab-authoring/v1"
_ZERO_FINGERPRINT = "sha256:" + "0" * 64
_ANCHOR = re.compile(
    r"^# unilab:node_uuid=([^\s#]+)$",
)
_ANCHOR_LIKE = re.compile(r"^#\s*unilab:node_uuid")
_AUTHORING_MODULE = "unilabos.workflow.authoring"
_DEVICE = f"{_AUTHORING_MODULE}:device"
_GROUP = f"{_AUTHORING_MODULE}:group"
_MATERIAL_FLOW_ROLE = f"{_AUTHORING_MODULE}:MaterialFlowRole"
_MATERIAL_SOURCE = f"{_AUTHORING_MODULE}:material_source"
_PARALLEL = f"{_AUTHORING_MODULE}:parallel"
_RESOURCE_REF = f"{_AUTHORING_MODULE}:resource_ref"
_WORKFLOW_DEFINITION = f"{_AUTHORING_MODULE}:workflow_definition"
_WORKFLOW_OUTPUT = f"{_AUTHORING_MODULE}:workflow_output"
_MATERIAL_FLOW_ROLES = {
    "PRIMARY_SAMPLE": "primary_sample",
    "ALIQUOT_SAMPLE": "aliquot_sample",
    "REAGENT": "reagent",
    "CONSUMABLE": "consumable",
}
_OWNED_WORKFLOW_KEYS = {
    "input_contract",
    "output_contract",
    "output_bindings",
}
_OWNED_NODE_KEYS = {"input_bindings", "executor_binding", "resource_refs"}
_NODE_TEMPLATE_NULLABLE_READ_FIELDS = {
    "description",
    "class",
    "schema",
    "icon",
    "header",
    "footer",
}
_HANDLE_TEMPLATE_NULLABLE_READ_FIELDS = {
    "description",
    "data_source",
    "data_key",
}


class _AuthoringFailure(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        node: ast.AST | None = None,
        source_range: dict[str, int] | None = None,
        fields: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.node = node
        self.source_range = source_range
        self.fields = fields or {}


def _fail(
    code: str,
    message: str,
    *,
    node: ast.AST | None = None,
    fields: dict[str, Any] | None = None,
) -> Never:
    raise _AuthoringFailure(code, message, node=node, fields=fields)


def _detached(value: Any) -> Any:
    def thaw(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {str(key): thaw(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [thaw(child) for child in item]
        return item

    return decode_json_bytes(encode_json(thaw(value)))


def _catalog_read_entity(
    value: Mapping[str, Any],
    *,
    nullable_fields: set[str],
) -> dict[str, Any]:
    """统一 Catalog snapshot 与 Backend JSON omitempty 的读取形状。"""

    projected = _detached(value)
    for field_name in nullable_fields:
        if projected.get(field_name) is None:
            projected.pop(field_name, None)
    return projected


def _catalog_wire_equal(left: Any, right: Any) -> bool:
    """按 JSON 的单一数字域比较 Catalog DTO。

    浏览器 parse/stringify 往返无法保留 Python 对整数与整数值浮点数的区分。
    Catalog 默认值仍由 UUID 和 fingerprint 保持不可变，因此这里只归一化线上的
    数字表示。
    """

    pending = [(left, right)]
    while pending:
        left_item, right_item = pending.pop()
        if type(left_item) in {int, float} and type(right_item) in {int, float}:
            if left_item != right_item:
                return False
            continue
        if type(left_item) is not type(right_item):
            return False
        if isinstance(left_item, dict):
            if left_item.keys() != right_item.keys():
                return False
            pending.extend((value, right_item[key]) for key, value in left_item.items())
        elif isinstance(left_item, list):
            if len(left_item) != len(right_item):
                return False
            pending.extend(zip(left_item, right_item, strict=True))
        elif left_item != right_item:
            return False
    return True


def _sorted_catalog_read_entities(
    values: Sequence[Mapping[str, Any]],
    *,
    nullable_fields: set[str],
) -> list[dict[str, Any]]:
    return sorted(
        (
            _catalog_read_entity(item, nullable_fields=nullable_fields)
            for item in values
        ),
        key=lambda item: item["uuid"],
    )


def _safe_identifier(value: str, fallback: str) -> str:
    normalized = re.sub(r"\W+", "_", value, flags=re.UNICODE).strip("_")
    if not normalized or not normalized.isidentifier() or keyword.iskeyword(normalized):
        normalized = fallback
    return normalized


def _snake_case(value: str, fallback: str) -> str:
    separated = re.sub(r"(?<!^)(?=[A-Z])", "_", value)
    return _safe_identifier(separated.lower(), fallback)


def _workflow_result_record_name(function_name: str) -> str:
    parts = [part for part in function_name.split("_") if part]
    candidate = "".join(part[:1].upper() + part[1:] for part in parts) + "Result"
    return _safe_identifier(candidate, "WorkflowResult")


def _call_identity(call: ast.Call, imports: Mapping[str, str]) -> str | None:
    if isinstance(call.func, ast.Name):
        return imports.get(call.func.id)
    return None


def _diagnostic(error: _AuthoringFailure, source: str) -> dict[str, Any]:
    item: dict[str, Any] = {
        "severity": "error",
        "code": error.code,
        "message": error.message,
        **error.fields,
    }
    if error.source_range is not None:
        item["source_range"] = error.source_range
        return item
    node = error.node
    if node is None:
        return item
    lines = source_lines(source)
    start_line = min(max(int(getattr(node, "lineno", 1)), 1), len(lines))
    start_offset = max(int(getattr(node, "col_offset", 0)), 0)
    start_column = utf8_offset_to_utf16_column(
        lines[start_line - 1],
        min(start_offset, len(lines[start_line - 1].encode("utf-8"))),
    )
    end_line = min(
        max(int(getattr(node, "end_lineno", start_line)), start_line),
        len(lines),
    )
    end_line_bytes = lines[end_line - 1].encode("utf-8")
    end_offset = max(
        int(getattr(node, "end_col_offset", len(end_line_bytes))),
        0,
    )
    end_column = utf8_offset_to_utf16_column(
        lines[end_line - 1],
        min(end_offset, len(end_line_bytes)),
    )
    if end_line == start_line:
        end_column = max(end_column, start_column)
    item["source_range"] = {
        "start_line": start_line,
        "start_column": start_column,
        "end_line": end_line,
        "end_column": end_column,
    }
    return item


def _syntax_diagnostic(error: SyntaxError, source: str) -> dict[str, Any]:
    lines = source_lines(source)
    line = min(max(int(error.lineno or 1), 1), len(lines))
    start_offset = min(max(int(error.offset or 1) - 1, 0), len(lines[line - 1]))
    column = codepoint_offset_to_utf16_column(lines[line - 1], start_offset)
    end_line = min(max(int(error.end_lineno or line), line), len(lines))
    raw_end_offset = int(error.end_offset or 0)
    end_offset = (
        min(max(raw_end_offset - 1, 0), len(lines[end_line - 1]))
        if raw_end_offset > 0
        else start_offset
    )
    end_column = codepoint_offset_to_utf16_column(
        lines[end_line - 1],
        end_offset,
    )
    if end_line == line:
        end_column = max(end_column, column)
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
    effective_input_contract: dict[str, Any]
    applied_nodes: dict[str, dict[str, Any]]
    catalog: _CatalogIndex
    resource_template_identity_index: ResourceTemplateIdentityIndex | None
    material_source_authority: MaterialSourceStaticAuthority | None
    composite_authoring: CompositeAuthoring | None
    nodes: list[_NodeState] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    results: dict[str, _NodeState] = field(default_factory=dict)
    used_anchors: set[int] = field(default_factory=set)

    def node_uuid(self, statement: ast.stmt) -> str:
        anchor_line = statement.lineno - 1
        if anchor_line in self.anchors:
            self.used_anchors.add(anchor_line)
            return self.anchors[anchor_line]
        return str(uuid4())


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

    def material_source(
        self,
        *,
        node: ast.AST,
    ) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
        template, handles = self.action(
            _MATERIAL_SOURCE,
            "material_source",
            node=node,
        )
        if (
            template.get("type") != "material_source"
            or template.get("node_type") != "material_source"
            or len(handles) != 1
        ):
            _fail(
                "template_catalog_mismatch",
                "MaterialSource framework template 不符合合同",
                node=node,
            )
        handle = handles[0]
        if (
            handle.get("handle_key") != "material"
            or handle.get("io_type") != "source"
            or handle.get("type") != "ResourceSlot"
        ):
            _fail(
                "template_catalog_mismatch",
                "MaterialSource framework Handle 不符合合同",
                node=node,
            )
        return template, handles


class WorkflowAuthoringEngine:
    """把一个稳定 Template Catalog snapshot 深化为三个纯 Authoring transform。"""

    compiler_version = _COMPILER_VERSION

    def __init__(
        self,
        *,
        catalog: TemplateCatalog,
        authority: CatalogAuthority,
        resource_template_identity_index: ResourceTemplateIdentityIndex | None = None,
        material_source_authority: MaterialSourceStaticAuthority | None = None,
        composite_authoring: CompositeAuthoring | None = None,
    ) -> None:
        if not isinstance(catalog, TemplateCatalog):
            raise TypeError("catalog 必须是 TemplateCatalog")
        if not isinstance(authority, CatalogAuthority):
            raise TypeError("authority 必须是 CatalogAuthority")
        self._catalog = catalog
        self._authority = authority
        self._resource_template_identity_index = resource_template_identity_index
        self._material_source_authority = material_source_authority
        if composite_authoring is not None and not isinstance(
            composite_authoring, CompositeAuthoring
        ):
            raise TypeError("composite_authoring 必须是 CompositeAuthoring")
        self._composite_authoring = composite_authoring
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

    @property
    def template_catalog(self) -> TemplateCatalog:
        """返回此 compiler 使用的持久 Catalog 读取 facade。"""

        return self._catalog

    @property
    def catalog_authority(self) -> CatalogAuthority:
        """返回 composition 时选择的 Graph Authority。"""

        return self._authority

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
                    resource_template_identity_index=(
                        self._resource_template_identity_index
                    ),
                    material_source_authority=self._material_source_authority,
                    composite_authoring=self._composite_authoring,
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
                    resource_template_identity_index=(
                        self._resource_template_identity_index
                    ),
                    material_source_authority=self._material_source_authority,
                    composite_authoring=self._composite_authoring,
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
                    resource_template_identity_index=(
                        self._resource_template_identity_index
                    ),
                    material_source_authority=self._material_source_authority,
                    composite_authoring=self._composite_authoring,
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
                    resource_template_identity_index=(
                        self._resource_template_identity_index
                    ),
                    material_source_authority=self._material_source_authority,
                    composite_authoring=self._composite_authoring,
                )
                if not compiled.valid or not _semantic_graph_equal(
                    compiled.graph,
                    _materialize_typed_action_defaults(generated.graph),
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
    require_utf8_text(source)
    require_utf8_text(source_uri)


def _compile_with_snapshot(
    *,
    snapshot: TemplateCatalogSnapshot,
    workflow_uuid: str,
    workflow_revision: int,
    python_source: str,
    source_uri: str,
    applied_graph: dict[str, Any],
    resource_template_identity_index: ResourceTemplateIdentityIndex | None,
    material_source_authority: MaterialSourceStaticAuthority | None,
    composite_authoring: CompositeAuthoring | None,
    prove_normalized: bool = True,
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
        _validate_catalog_projection(snapshot, applied)
        imports = _module_imports(module)
        selectors, function, result_classes = _module_declarations(module, imports)
        displayname, description = _workflow_declaration(
            function,
            imports,
            workflow_uuid=workflow_uuid,
        )
        input_contract = _workflow_parameters(
            function,
            imports,
            resource_template_identity_index=resource_template_identity_index,
        )
        result_declaration = _workflow_result_declaration(
            function,
            result_classes=result_classes,
            imports=imports,
            resource_template_identity_index=resource_template_identity_index,
        )
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
            effective_input_contract=input_contract,
            applied_nodes={item["uuid"]: item for item in applied["nodes"]},
            catalog=_CatalogIndex(snapshot),
            resource_template_identity_index=resource_template_identity_index,
            material_source_authority=material_source_authority,
            composite_authoring=composite_authoring,
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
        input_contract = state.effective_input_contract
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
            declaration=result_declaration,
        )
        graph = _candidate_graph(
            state,
            displayname=displayname,
            description=description,
            input_contract=input_contract,
            output_contract=output_contract,
            output_bindings=output_bindings,
        )
        _validate_built_graph(
            graph,
            material_source_authority=material_source_authority,
        )
        normalized, source_map = _render_graph(
            graph,
            resource_template_identity_index=resource_template_identity_index,
        )
        if prove_normalized:
            proof = _compile_with_snapshot(
                snapshot=snapshot,
                workflow_uuid=workflow_uuid,
                workflow_revision=workflow_revision,
                python_source=normalized,
                source_uri="authoring://normalized-proof",
                applied_graph=graph,
                resource_template_identity_index=resource_template_identity_index,
                material_source_authority=material_source_authority,
                composite_authoring=composite_authoring,
                prove_normalized=False,
            )
            if (
                not proof.valid
                or not _semantic_graph_equal(proof.graph, graph)
                or proof.normalized_python_source != normalized
            ):
                _fail(
                    "round_trip_mismatch",
                    "规范 Python 不能回编译为等价 Candidate graph",
                )
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
    except (CodedGraphValidationError, MaterialSourceAuthorityError) as error:
        failure = _AuthoringFailure(error.code, str(error))
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
    required_workflow_fields = {
        "uuid",
        "create_time",
        "update_time",
        "meta_data",
        "name",
        "tags",
        "revision",
    }
    if not required_workflow_fields.issubset(workflow):
        _fail("candidate_invalid", "Candidate Workflow 读取字段不完整")
    if (
        not isinstance(workflow["create_time"], str)
        or not workflow["create_time"]
        or not isinstance(workflow["update_time"], str)
        or not workflow["update_time"]
        or not isinstance(workflow["meta_data"], dict)
        or not isinstance(workflow["name"], str)
        or not workflow["name"].strip()
        or not isinstance(workflow["tags"], list)
        or (
            workflow.get("description") is not None
            and not isinstance(workflow.get("description"), str)
        )
    ):
        _fail("candidate_invalid", "Candidate Workflow 读取形状不正确")
    try:
        validate_json_value(workflow["meta_data"])
        validate_json_value(workflow["tags"])
    except (TypeError, ValueError):
        _fail("candidate_invalid", "Candidate Workflow JSON 字段不正确")
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
        if any(not isinstance(item, dict) for item in graph[key]):
            _fail("candidate_invalid", f"Candidate graph {key} 成员必须是对象")
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
) -> tuple[dict[str, _Selector], ast.FunctionDef, dict[str, ast.ClassDef]]:
    selectors: dict[str, _Selector] = {}
    functions: list[ast.FunctionDef] = []
    result_classes: dict[str, ast.ClassDef] = {}
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
        if isinstance(statement, ast.ClassDef):
            if statement.name in result_classes:
                _fail(
                    "invalid_module_scope",
                    "Workflow result declaration 名称重复",
                    node=statement,
                )
            result_classes[statement.name] = statement
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
    return selectors, functions[0], result_classes


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
    keyword_names = [str(item.arg) for item in decorator.keywords]
    if len(keyword_names) != len(set(keyword_names)):
        _fail(
            "invalid_workflow_declaration",
            "workflow_definition 命名字段不能重复",
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


@dataclass(frozen=True, slots=True)
class _WorkflowResultDeclaration:
    contract: dict[str, Any] | None
    form: str
    constructor_name: str | None = None


def _workflow_result_declaration(
    function: ast.FunctionDef,
    *,
    result_classes: Mapping[str, ast.ClassDef],
    imports: Mapping[str, str],
    resource_template_identity_index: ResourceTemplateIdentityIndex | None,
) -> _WorkflowResultDeclaration:
    annotation = function.returns
    if annotation is None:
        if result_classes:
            _fail(
                "invalid_module_scope",
                "Workflow result declaration 必须由 return annotation 引用",
                node=next(iter(result_classes.values())),
            )
        return _WorkflowResultDeclaration(None, "legacy")

    declaration: ast.expr | ast.ClassDef | None = annotation
    form = "mapping"
    constructor_name: str | None = None
    if isinstance(annotation, ast.Name):
        result_class = result_classes.get(annotation.id)
        if result_class is None or set(result_classes) != {annotation.id}:
            _fail(
                "invalid_workflow_output",
                "Workflow result annotation 必须引用唯一 module-scope result record",
                node=annotation,
            )
        declaration = result_class
        constructor_name = annotation.id
        form = "mapping" if result_class.bases else "dataclass"
    elif result_classes:
        _fail(
            "invalid_module_scope",
            "Workflow module 包含未引用的 result declaration",
            node=next(iter(result_classes.values())),
        )
    elif isinstance(annotation, ast.Constant) and annotation.value is None:
        form = "none"
    elif not isinstance(annotation, ast.Dict):
        _fail(
            "invalid_workflow_output",
            "Workflow return annotation 不符合 result-record 合同",
            node=annotation,
        )

    try:
        parsed = parse_action_result_declaration(declaration, imports=imports)
    except ActionResultSchemaError as error:
        _fail(error.code, error.message, node=annotation)
    contract = parsed.to_dict()
    descriptor_by_name = {item["name"]: item for item in contract.get("outputs", [])}
    for output_name, symbols in parsed.resource_templates:
        if not symbols:
            continue
        descriptor = descriptor_by_name.get(output_name)
        if descriptor is None:
            _fail(
                "invalid_workflow_output",
                "Workflow result declaration 与资源约束不一致",
                node=annotation,
            )
        resource_template_uuids = _resolve_resource_template_symbols(
            symbols,
            resource_template_identity_index=resource_template_identity_index,
            node=annotation,
        )
        resource_slot_schema = _resource_slot_schema(descriptor["schema"])
        if not isinstance(resource_slot_schema, dict):
            _fail(
                "invalid_schema",
                "ResourceTemplate allowlist 只能约束 ResourceSlot 或其列表",
                node=annotation,
            )
        resource_slot_schema["allowed_resource_template_uuids"] = list(
            resource_template_uuids
        )
    try:
        canonical = parse_output_contract(contract).to_dict()
    except WorkflowSchemaError as error:
        _fail(error.code, error.message, node=annotation)
    return _WorkflowResultDeclaration(canonical, form, constructor_name)


def _resolve_resource_template_symbols(
    symbols: Sequence[Any],
    *,
    resource_template_identity_index: ResourceTemplateIdentityIndex | None,
    node: ast.AST,
) -> tuple[str, ...]:
    if resource_template_identity_index is None:
        _fail(
            "template_catalog_mismatch",
            "当前 authority 无法解析 ResourceTemplate symbol",
            node=node,
        )
    resolved: list[str] = []
    for symbol in symbols:
        try:
            qualified_name = symbol.qualified_name
            resource_template_uuid = validate_uuid(
                resource_template_identity_index.resolve_symbol(qualified_name)
            )
            if (
                resource_template_identity_index.identify_uuid(resource_template_uuid)
                != qualified_name
            ):
                raise LookupError(qualified_name)
        except (AttributeError, LookupError, TypeError, ValueError):
            _fail(
                "template_catalog_mismatch",
                "当前 authority 无法解析 ResourceTemplate symbol",
                node=node,
            )
        resolved.append(resource_template_uuid)
    return tuple(resolved)


def _workflow_parameters(
    function: ast.FunctionDef,
    imports: Mapping[str, str],
    *,
    resource_template_identity_index: ResourceTemplateIdentityIndex | None,
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
        descriptor = parsed.to_dict()
        if parsed.resource_templates:
            resource_template_uuids = _resolve_resource_template_symbols(
                parsed.resource_templates,
                resource_template_identity_index=resource_template_identity_index,
                node=argument,
            )
            resource_slot_schema = _resource_slot_schema(descriptor["schema"])
            if not isinstance(resource_slot_schema, dict):
                _fail(
                    "invalid_schema",
                    "ResourceTemplate allowlist 只能约束 ResourceSlot 或其列表",
                    node=argument,
                )
            resource_slot_schema["allowed_resource_template_uuids"] = list(
                resource_template_uuids
            )
        descriptors.append(descriptor)
    try:
        return parse_input_contract({"version": 1, "parameters": descriptors}).to_dict()
    except WorkflowSchemaError as error:
        _fail(error.code, error.message, node=function)


def _token_source_range(
    token: tokenize.TokenInfo,
    lines: Sequence[str],
) -> dict[str, int]:
    return {
        "start_line": token.start[0],
        "start_column": codepoint_offset_to_utf16_column(
            lines[token.start[0] - 1],
            token.start[1],
        ),
        "end_line": token.end[0],
        "end_column": codepoint_offset_to_utf16_column(
            lines[token.end[0] - 1],
            token.end[1],
        ),
    }


def _source_anchors(source: str) -> tuple[dict[int, str], set[int]]:
    anchors: dict[int, str] = {}
    occurrences: dict[str, list[dict[str, int]]] = {}
    lines = source_lines(source)
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for token in tokens:
            if token.type != tokenize.COMMENT:
                continue
            match = _ANCHOR.fullmatch(token.string)
            if match is None:
                if _ANCHOR_LIKE.match(token.string):
                    raise _AuthoringFailure(
                        "invalid_node_anchor",
                        "Node UUID anchor 必须使用唯一规范格式",
                        source_range=_token_source_range(token, lines),
                    )
                continue
            try:
                node_uuid = validate_uuid(match.group(1))
            except (TypeError, ValueError):
                raise _AuthoringFailure(
                    "invalid_node_anchor",
                    "Node UUID anchor 必须是非 nil UUID",
                    source_range=_token_source_range(token, lines),
                )
            line = token.start[0]
            if line in anchors:
                raise _AuthoringFailure(
                    "invalid_node_anchor",
                    "同一源码行不能声明多个 Node UUID anchor",
                    source_range=_token_source_range(token, lines),
                )
            anchors[line] = node_uuid
            occurrences.setdefault(node_uuid, []).append(
                _token_source_range(token, lines)
            )
    except (IndentationError, tokenize.TokenError):
        _fail("python_syntax_error", "Python source token 不完整")
    for node_uuid, source_ranges in occurrences.items():
        if len(source_ranges) < 2:
            continue
        allocated: set[str] = set()
        alternatives = []
        for retained_index, retained_range in enumerate(source_ranges):
            replacements = []
            for occurrence_index, source_range in enumerate(source_ranges):
                if occurrence_index == retained_index:
                    continue
                replacement_uuid = str(uuid4())
                while replacement_uuid in allocated:
                    replacement_uuid = str(uuid4())
                allocated.add(replacement_uuid)
                replacements.append(
                    {
                        "source_range": source_range,
                        "replacement_uuid": replacement_uuid,
                    }
                )
            alternatives.append(
                {
                    "retained_range": retained_range,
                    "replacements": replacements,
                }
            )
        raise _AuthoringFailure(
            "DUPLICATE_NODE_UUID",
            f"Node UUID {node_uuid} 在源码中重复，必须选择一个保留 identity",
            source_range=source_ranges[0],
            fields={
                "duplicate_uuid": node_uuid,
                "occurrence_ranges": source_ranges,
                "repair_alternatives": alternatives,
            },
        )
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
                "Workflow result 必须是唯一最终 top-level return",
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
            call_identity = (
                _call_identity(statement.value, state.imports)
                if isinstance(statement.value, ast.Call)
                else None
            )
            if call_identity == _MATERIAL_SOURCE:
                segment = _parse_material_source(
                    statement,
                    state=state,
                    available_results=available_results,
                    parent_uuid=parent_uuid,
                )
            elif (
                call_identity is not None
                and state.composite_authoring is not None
                and isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.Name)
            ):
                segment = _parse_composite(
                    statement,
                    source_identity=call_identity,
                    state=state,
                    available_results=available_results,
                    parent_uuid=parent_uuid,
                )
            else:
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


def _parse_composite(
    statement: ast.Assign,
    *,
    source_identity: str,
    state: _BuildState,
    available_results: dict[str, _NodeState],
    parent_uuid: str | None,
) -> _Flow:
    if (
        len(statement.targets) != 1
        or not isinstance(statement.targets[0], ast.Name)
        or not isinstance(statement.value, ast.Call)
        or not isinstance(statement.value.func, ast.Name)
        or ":" not in source_identity
    ):
        _fail(
            "invalid_composite_call",
            "Published Workflow 必须由一个新名字接收 named result object",
            node=statement,
        )
    result_name = statement.targets[0].id
    if (
        result_name in available_results
        or result_name in state.selectors
        or result_name in state.input_names
    ):
        _fail(
            "invalid_composite_call",
            "Published Workflow result 名称不能重绑定",
            node=statement.targets[0],
        )
    call = statement.value
    if call.args or any(item.arg is None for item in call.keywords):
        _fail(
            "invalid_composite_call",
            "Published Workflow 只接受 direct keyword arguments",
            node=call,
        )
    keyword_names = [str(item.arg) for item in call.keywords]
    if len(keyword_names) != len(set(keyword_names)):
        _fail(
            "invalid_composite_call",
            "Published Workflow keyword 不能重复",
            node=call,
        )

    node_uuid = state.node_uuid(statement)
    keyword_arguments: dict[str, object] = {}
    resource_ref_arguments: dict[str, tuple[dict[str, str], str, ast.expr]] = {}
    pending_provider_edges: list[tuple[_NodeState, str, str]] = []
    for keyword_node in call.keywords:
        assert keyword_node.arg is not None
        expression = keyword_node.value
        if isinstance(expression, ast.Name) and expression.id in state.input_names:
            keyword_arguments[keyword_node.arg] = {
                "kind": "workflow_input",
                "parameter": expression.id,
            }
            continue
        producer = _result_reference(expression, available_results)
        if producer is not None:
            source_node, output_name = producer
            source_handle = _source_handle(
                source_node.handles,
                output_name,
                node=expression,
            )
            keyword_arguments[keyword_node.arg] = {
                "kind": "node_output",
                "workflow_node_uuid": source_node.node["uuid"],
                "source_handle_uuid": source_handle["uuid"],
            }
            pending_provider_edges.append(
                (source_node, source_handle["uuid"], keyword_node.arg)
            )
            continue
        resource_reference = _literal_resource_ref(
            expression,
            state=state,
            diagnostic_code="invalid_composite_call",
        )
        if resource_reference is not None:
            resource_slot, resource_id = resource_reference
            keyword_arguments[keyword_node.arg] = resource_slot
            resource_ref_arguments[keyword_node.arg] = (
                resource_slot,
                resource_id,
                expression,
            )
            continue
        try:
            value = ast.literal_eval(expression)
            validate_json_value(value)
        except (TypeError, ValueError):
            _fail(
                "invalid_composite_call",
                "Published Workflow value 必须是 literal、Workflow 参数或 named result output",
                node=expression,
            )
        keyword_arguments[keyword_node.arg] = value

    module, symbol = source_identity.rsplit(":", 1)
    assert state.composite_authoring is not None
    expansion = state.composite_authoring.compile_invocation(
        parent_workflow_uuid=state.workflow_uuid,
        invocation_uuid=node_uuid,
        module=module,
        symbol=symbol,
        keyword_arguments=keyword_arguments,
        parent_input_contract=state.effective_input_contract,
    )
    _require_composite_expansion(expansion, statement)
    assert expansion.invocation_node is not None
    invocation = _detached(expansion.invocation_node)
    invocation["name"] = result_name
    invocation["parent_uuid"] = parent_uuid
    applied = state.applied_nodes.get(node_uuid)
    if applied is not None:
        _assert_composite_pin_compatible(
            applied,
            expansion,
            statement,
        )
        for key in (
            "description",
            "icon",
            "pose",
            "footer",
            "execution_policy",
            "disabled",
            "minimized",
            "script",
        ):
            if key in applied:
                invocation[key] = _detached(applied[key])

    templates = {
        str(template["uuid"]): _detached(template)
        for template in expansion.node_templates
    }
    handles_by_template: dict[str, tuple[dict[str, Any], ...]] = {}
    for raw_handle in expansion.handle_templates:
        handle = _detached(raw_handle)
        template_uuid = str(handle["workflow_node_template_uuid"])
        handles_by_template[template_uuid] = (
            *handles_by_template.get(template_uuid, ()),
            handle,
        )

    invocation_template_uuid = str(invocation["workflow_node_template_uuid"])
    invocation_template = templates.get(invocation_template_uuid)
    if invocation_template is None:
        _fail(
            "composite_catalog_mismatch",
            "Published Workflow invocation template 不完整",
            node=statement,
        )
    invocation_handles = handles_by_template.get(invocation_template_uuid, ())
    target_handles = _handles_by_business_name(
        invocation_handles,
        "target",
        node=call,
    )
    if resource_ref_arguments:
        meta_data = invocation.setdefault("meta_data", {})
        unilab = meta_data.setdefault("unilab", {})
        resource_refs = unilab.setdefault("resource_refs", {})
        for input_name, (
            resource_slot,
            resource_id,
            expression,
        ) in resource_ref_arguments.items():
            target_handle = target_handles.get(input_name)
            if target_handle is None or input_name == "ready":
                _fail(
                    "composite_boundary_mapping_invalid",
                    "Published Workflow resource_ref 未匹配真实 boundary Handle",
                    node=expression,
                )
            _validate_resource_ref_target(
                resource_slot,
                target=target_handle,
                node=expression,
                owner="Published Workflow",
                diagnostic_code="invalid_composite_call",
            )
            resource_refs[target_handle["uuid"]] = {"resource_id": resource_id}
        unilab["resource_refs"] = dict(sorted(resource_refs.items()))
    for source_node, source_handle_uuid, input_name in pending_provider_edges:
        target_handle = target_handles.get(input_name)
        if target_handle is None or input_name == "ready":
            _fail(
                "composite_boundary_mapping_invalid",
                "Published Workflow input 未匹配真实 boundary Handle",
                node=statement,
            )
        invocation.get("param", {}).pop(input_name, None)
        state.edges.append(
            _edge(
                state.workflow_uuid,
                source_node.node["uuid"],
                node_uuid,
                source_handle_uuid,
                target_handle["uuid"],
            )
        )

    invocation_state = _NodeState(
        invocation,
        invocation_template,
        invocation_handles,
        result_name,
        statement,
    )
    state.nodes.append(invocation_state)
    for raw_node in expansion.nodes:
        node = _detached(raw_node)
        template_uuid = str(node["workflow_node_template_uuid"])
        template = templates.get(template_uuid)
        if template is None:
            _fail(
                "composite_catalog_mismatch",
                "Composite internal template 不完整",
                node=statement,
            )
        state.nodes.append(
            _NodeState(
                node,
                template,
                handles_by_template.get(template_uuid, ()),
                None,
                statement,
            )
        )
    state.edges.extend(_detached(list(expansion.edges)))
    state.effective_input_contract = _detached(
        expansion.effective_parent_input_contract
    )
    available_results[result_name] = invocation_state
    state.results[result_name] = invocation_state
    return _Flow((node_uuid,), (node_uuid,))


def _require_composite_expansion(
    expansion: CompositeExpansion,
    statement: ast.stmt,
) -> None:
    if not expansion.diagnostics and expansion.invocation_node is not None:
        return
    diagnostic = expansion.diagnostics[0] if expansion.diagnostics else {}
    code = str(diagnostic.get("code") or "composite_catalog_mismatch")
    path = str(diagnostic.get("path") or "/composite")
    _fail(
        code,
        str(diagnostic.get("message") or "Composite authoring validation failed"),
        node=statement,
        fields={"path": path},
    )


def _assert_composite_pin_compatible(
    applied: Mapping[str, Any],
    expansion: CompositeExpansion,
    statement: ast.stmt,
) -> None:
    try:
        stored = applied["meta_data"]["unilab"]["composite"]
    except (KeyError, TypeError):
        return
    if not isinstance(stored, Mapping):
        _fail(
            "composite_contract_stale",
            "Published Workflow contract pin 不符合合同",
            node=statement,
        )
    current_node = expansion.invocation_node
    current = (
        current_node.get("meta_data", {}).get("unilab", {}).get("composite", {})
        if isinstance(current_node, Mapping)
        else {}
    )
    previous_projection = stored.get("contract_compatibility")
    current_projection = (
        current.get("contract_compatibility") if isinstance(current, Mapping) else None
    )
    stored_is_coherent = isinstance(previous_projection, Mapping) and all(
        stored.get(key) == previous_projection.get(projection_key)
        for key, projection_key in (
            ("child_workflow_uuid", "workflow_uuid"),
            ("contract_digest", "digest"),
            ("composition_allow_transparent", "mode"),
        )
    )
    current_is_coherent = isinstance(current_projection, Mapping) and all(
        expansion.contract_pin.get(key) == current_projection.get(projection_key)
        for key, projection_key in (
            ("child_workflow_uuid", "workflow_uuid"),
            ("contract_digest", "digest"),
            ("composition_allow_transparent", "mode"),
        )
    )
    if not stored_is_coherent or not current_is_coherent:
        _fail(
            "composite_contract_stale",
            "Published Workflow stored contract pin 不自洽",
            node=statement,
        )
    compatibility = classify_published_workflow_compatibility_projections(
        previous_projection,
        current_projection,
    )
    if compatibility == "breaking":
        _fail(
            "composite_contract_stale",
            "Published Workflow contract 已发生 breaking 变化",
            node=statement,
        )


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


def _parse_material_source(
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
        or _call_identity(statement.value, state.imports) != _MATERIAL_SOURCE
    ):
        _fail(
            "invalid_material_source",
            "MaterialSource 必须由一个新名字接收",
            node=statement,
        )
    result_name = statement.targets[0].id
    if (
        result_name in available_results
        or result_name in state.selectors
        or result_name in state.input_names
    ):
        _fail(
            "invalid_material_source",
            "MaterialSource 名称不能重绑定",
            node=statement.targets[0],
        )
    call = statement.value
    if call.args or any(item.arg is None for item in call.keywords):
        _fail(
            "invalid_material_source",
            "material_source 只接受命名参数",
            node=call,
        )
    keywords = {str(item.arg): item.value for item in call.keywords if item.arg}
    required = {
        "resource_template",
        "mode",
        "mount",
        "material_uuid",
        "site",
        "slot_range",
        "flow_role",
    }
    if len(keywords) != len(call.keywords) or set(keywords) != required:
        _fail(
            "invalid_material_source",
            "material_source 字段不符合当前合同",
            node=call,
        )

    resource_expression = keywords["resource_template"]
    if not isinstance(resource_expression, ast.Name):
        _fail(
            "template_catalog_mismatch",
            "resource_template 必须是绝对导入的静态 symbol",
            node=resource_expression,
        )
    qualified_name = state.imports.get(resource_expression.id)
    index = state.resource_template_identity_index
    if not qualified_name or ":" not in qualified_name or index is None:
        _fail(
            "template_catalog_mismatch",
            "当前 authority 无法解析 ResourceTemplate symbol",
            node=resource_expression,
        )
    try:
        resource_template_uuid = validate_uuid(index.resolve_symbol(qualified_name))
        if index.identify_uuid(resource_template_uuid) != qualified_name:
            raise LookupError(qualified_name)
    except (AttributeError, LookupError, TypeError, ValueError):
        _fail(
            "template_catalog_mismatch",
            "当前 authority 无法解析 ResourceTemplate symbol",
            node=resource_expression,
        )

    mode = keywords["mode"]
    if (
        not isinstance(mode, ast.Constant)
        or not isinstance(mode.value, str)
        or mode.value not in {"existing", "create_new"}
    ):
        _fail(
            "invalid_material_source",
            "MaterialSource mode 不在闭合目录中",
            node=mode,
        )
    mount = keywords["mount"]
    if (
        not isinstance(mount, ast.Call)
        or _call_identity(mount, state.imports) != _RESOURCE_REF
        or len(mount.args) != 1
        or mount.keywords
        or not isinstance(mount.args[0], ast.Constant)
        or not isinstance(mount.args[0].value, str)
    ):
        _fail(
            "invalid_material_source",
            "mount 必须是单 resource id literal resource_ref",
            node=mount,
        )
    mount_resource_id = mount.args[0].value
    mount_slot = resolve_resource_ref(
        mount_resource_id,
        state.material_source_authority,
    )
    mount_uuid = mount_slot["uuid"]
    material_uuid_expression = keywords["material_uuid"]
    material_uuid: str | None
    if (
        isinstance(material_uuid_expression, ast.Constant)
        and material_uuid_expression.value is None
    ):
        material_uuid = None
    elif (
        mode.value == "existing"
        and isinstance(material_uuid_expression, ast.Constant)
        and isinstance(material_uuid_expression.value, str)
    ):
        try:
            material_uuid = validate_uuid(material_uuid_expression.value)
        except (TypeError, ValueError):
            _fail(
                "invalid_material_source",
                "material_uuid 必须是 canonical non-nil UUID 或 None",
                node=material_uuid_expression,
            )
    else:
        _fail(
            "invalid_material_source",
            "create_new 禁止指定 material_uuid",
            node=material_uuid_expression,
        )
    site_expression = keywords["site"]
    site: str | None
    if isinstance(site_expression, ast.Constant) and site_expression.value is None:
        site = None
    elif isinstance(site_expression, ast.Constant) and isinstance(
        site_expression.value,
        str,
    ):
        try:
            site = validate_uuid(site_expression.value)
        except (TypeError, ValueError):
            _fail(
                "invalid_material_source",
                "site 必须是 canonical non-nil UUID 或 None",
                node=site_expression,
            )
    else:
        _fail(
            "invalid_material_source",
            "site 必须是 UUID string literal 或 None",
            node=site_expression,
        )

    range_expression = keywords["slot_range"]
    slot_range: list[str] | None
    if isinstance(range_expression, ast.Constant) and range_expression.value is None:
        slot_range = None
    elif isinstance(range_expression, ast.List) and range_expression.elts:
        slot_range = []
        for item in range_expression.elts:
            if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                _fail(
                    "invalid_material_source",
                    "slot_range 只接受 Site UUID string literals",
                    node=item,
                )
            try:
                slot_range.append(validate_uuid(item.value))
            except (TypeError, ValueError):
                _fail(
                    "invalid_material_source",
                    "slot_range 只接受 canonical non-nil UUID",
                    node=item,
                )
        if len(set(slot_range)) != len(slot_range) or slot_range != sorted(slot_range):
            _fail(
                "invalid_material_source",
                "slot_range 必须无重复并按 Site UUID 规范排序",
                node=range_expression,
            )
    else:
        _fail(
            "invalid_material_source",
            "slot_range 必须是非空 Site UUID 数组或 None",
            node=range_expression,
        )
    if site is not None and slot_range is not None:
        _fail(
            "invalid_material_source",
            "site 与 slot_range 互斥",
            node=call,
        )
    role = keywords["flow_role"]
    if not (
        isinstance(role, ast.Attribute)
        and isinstance(role.value, ast.Name)
        and state.imports.get(role.value.id) == _MATERIAL_FLOW_ROLE
        and role.attr in _MATERIAL_FLOW_ROLES
    ):
        _fail(
            "invalid_material_source",
            "flow_role 必须是 MaterialFlowRole 的闭合成员",
            node=role,
        )

    template, handles = state.catalog.material_source(node=call)
    node_uuid = state.node_uuid(statement)
    applied = state.applied_nodes.get(node_uuid, {})
    node = _node_payload(
        applied,
        uuid=node_uuid,
        template_uuid=template["uuid"],
        parent_uuid=parent_uuid,
        name=result_name,
        node_type="material_source",
        param={
            "mode": mode.value,
            "resource_template_uuid": resource_template_uuid,
            "mount": {"uuid": mount_uuid},
            "material_uuid": material_uuid,
            "site": site,
            "slot_range": slot_range,
            "flow_role": _MATERIAL_FLOW_ROLES[role.attr],
        },
        meta_data=_node_metadata(
            applied.get("meta_data"),
            None,
            resource_refs={"mount": {"resource_id": mount_resource_id}},
        ),
        action_name=None,
    )
    node.pop("material_uuid", None)
    node_state = _NodeState(node, template, handles, result_name, statement)
    state.nodes.append(node_state)
    available_results[result_name] = node_state
    state.results[result_name] = node_state
    # MaterialSource 不执行，不参与 ready 控制链；消费者的数据边表达顺序。
    return _Flow()


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
    template, handles = state.catalog.action(
        selector.class_identity,
        call.func.attr,
        node=call,
    )
    targets = _handles_by_business_name(handles, "target", node=call)
    node_uuid = state.node_uuid(statement)
    applied = state.applied_nodes.get(node_uuid, {})
    meta_data = _node_metadata(applied.get("meta_data"), selector)
    input_bindings: dict[str, dict[str, str]] = {}
    resource_refs: dict[str, dict[str, str]] = {}
    schema = template.get("schema")
    action_contract = (
        schema.get("x-unilabos-action-contract")
        if isinstance(schema, Mapping)
        else None
    )
    if isinstance(action_contract, Mapping) and action_contract.get("version") == 1:
        param = _detached(template.get("goal_default") or {})
    else:
        param = resolve_template_root_param(
            template.get("goal_default"),
            template.get("goal"),
        )
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
        resource_reference = _action_resource_ref(
            expression,
            target=target,
            state=state,
        )
        if resource_reference is not None:
            resource_slot, resource_id = resource_reference
            param[keyword_node.arg] = resource_slot
            resource_refs[target["uuid"]] = {"resource_id": resource_id}
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
    if resource_refs:
        meta_data["unilab"]["resource_refs"] = dict(sorted(resource_refs.items()))
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


def _action_resource_ref(
    expression: ast.expr,
    *,
    target: Mapping[str, Any],
    state: _BuildState,
) -> tuple[dict[str, str], str] | None:
    resource_reference = _literal_resource_ref(expression, state=state)
    if resource_reference is None:
        return None
    resource_slot, resource_id = resource_reference
    _validate_resource_ref_target(
        resource_slot,
        target=target,
        node=expression,
        owner="Action",
    )
    return resource_slot, resource_id


def _literal_resource_ref(
    expression: ast.expr,
    *,
    state: _BuildState,
    diagnostic_code: str = "invalid_action_call",
) -> tuple[dict[str, str], str] | None:
    if not isinstance(expression, ast.Call) or _call_identity(
        expression, state.imports
    ) != _RESOURCE_REF:
        return None
    if (
        len(expression.args) != 1
        or expression.keywords
        or not isinstance(expression.args[0], ast.Constant)
        or not isinstance(expression.args[0].value, str)
    ):
        _fail(
            diagnostic_code,
            "resource_ref 必须接收单个非空 resource id literal",
            node=expression,
        )
    resource_id = expression.args[0].value
    if not resource_id.strip() or resource_id != resource_id.strip():
        _fail(
            diagnostic_code,
            "resource_ref 必须接收单个非空 resource id literal",
            node=expression,
        )
    return (
        resolve_resource_ref(resource_id, state.material_source_authority),
        resource_id,
    )


def _validate_resource_ref_target(
    resource_slot: Mapping[str, str],
    *,
    target: Mapping[str, Any],
    node: ast.AST,
    owner: str,
    diagnostic_code: str = "invalid_action_call",
) -> None:
    if str(target.get("type") or "").strip().lower() != "resourceslot":
        _fail(
            diagnostic_code,
            f"resource_ref 只能绑定 ResourceSlot {owner} 参数",
            node=node,
        )
    unilab = target.get("meta_data", {}).get("unilab", {})
    allowlist = (
        unilab.get("allowed_resource_template_uuids")
        if isinstance(unilab, Mapping)
        else None
    )
    if allowlist not in (None, [], ()) and resource_slot[
        "resource_template_uuid"
    ] not in allowlist:
        _fail(
            "material_source_conflict",
            f"resource_ref 物料模板不被 {owner} ResourceSlot 接受",
            node=node,
        )


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
    keyword_names = [str(item.arg) for item in context.keywords]
    if len(keyword_names) != len(set(keyword_names)):
        _fail("invalid_group", "group 命名字段不能重复", node=context)
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
    template, handles = state.catalog.group(node=statement)
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
    if isinstance(expression, ast.Name):
        producer = results.get(expression.id)
        if producer is not None and _is_material_source_template(producer.template):
            return producer, "material"
        return None
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
    if _is_material_source_template(producer.template):
        _fail(
            "invalid_material_source",
            "MaterialSource 变量本身就是唯一 material Handle",
            node=expression,
        )
    return producer, expression.attr


def _is_material_source_template(template: Mapping[str, Any]) -> bool:
    return (
        template.get("class") == _MATERIAL_SOURCE
        and template.get("name") == "material_source"
        and template.get("type") == "material_source"
        and template.get("node_type") == "material_source"
    )


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
    *,
    resource_refs: Mapping[str, Mapping[str, str]] | None = None,
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
    if resource_refs:
        unilab["resource_refs"] = _detached(resource_refs)
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
    return authoring_edge(
        workflow_uuid,
        source_node_uuid,
        target_node_uuid,
        source_handle_uuid,
        target_handle_uuid,
    )


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
    declaration: _WorkflowResultDeclaration,
) -> tuple[dict[str, Any], dict[str, Any]]:
    parameters = {item["name"]: item for item in input_contract.get("parameters", [])}
    outputs: list[dict[str, Any]]
    bindings: dict[str, dict[str, Any]] = {}
    if declaration.contract is None:
        expressions = _legacy_workflow_output_expressions(statement, imports=imports)
        outputs = []
        for output_name, expression in expressions.items():
            descriptor, binding = _workflow_output_binding(
                output_name,
                expression,
                parameters=parameters,
                results=results,
                infer_schema=True,
            )
            outputs.append(descriptor)
            bindings[output_name] = binding
    else:
        outputs = _detached(declaration.contract["outputs"])
        expressions = _declared_workflow_output_expressions(
            statement,
            declaration=declaration,
        )
        expected_names = [item["name"] for item in outputs]
        if set(expressions) != set(expected_names):
            _fail(
                "invalid_workflow_output",
                "Workflow return 必须完整绑定每个声明 result field",
                node=statement,
            )
        for output_name in expected_names:
            _, binding = _workflow_output_binding(
                output_name,
                expressions[output_name],
                parameters=parameters,
                results=results,
                infer_schema=False,
            )
            bindings[output_name] = binding

    _synthesize_implicit_workflow_outputs(
        outputs,
        bindings,
        parameters=parameters,
    )
    try:
        contract = parse_output_contract({"version": 1, "outputs": outputs}).to_dict()
    except WorkflowSchemaError as error:
        _fail(error.code, error.message, node=statement)
    return contract, bindings


def _legacy_workflow_output_expressions(
    statement: ast.Return | None,
    *,
    imports: Mapping[str, str],
) -> dict[str, ast.expr]:
    if statement is None:
        return {}
    value = statement.value
    if (
        not isinstance(value, ast.Call)
        or _call_identity(value, imports) != _WORKFLOW_OUTPUT
    ):
        _fail(
            "invalid_workflow_output",
            "旧 Workflow return 必须调用 workflow_output",
            node=statement,
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
    return {
        str(item.arg): item.value for item in value.keywords if item.arg is not None
    }


def _declared_workflow_output_expressions(
    statement: ast.Return | None,
    *,
    declaration: _WorkflowResultDeclaration,
) -> dict[str, ast.expr]:
    outputs = declaration.contract["outputs"] if declaration.contract else []
    if declaration.form == "none":
        if statement is not None or outputs:
            _fail(
                "invalid_workflow_output",
                "-> None 的 Workflow 不得返回显式 result record",
                node=statement,
            )
        return {}
    if statement is None or statement.value is None:
        _fail(
            "invalid_workflow_output",
            "Workflow result record 缺少最终 return",
            node=statement,
        )
    value = statement.value
    if declaration.form == "mapping":
        if not isinstance(value, ast.Dict) or len(value.keys) != len(value.values):
            _fail(
                "invalid_workflow_output",
                "TypedDict Workflow result 必须返回 closed mapping literal",
                node=value,
            )
        result: dict[str, ast.expr] = {}
        for key, expression in zip(value.keys, value.values, strict=True):
            if (
                not isinstance(key, ast.Constant)
                or not isinstance(key.value, str)
                or key.value in result
            ):
                _fail(
                    "invalid_workflow_output",
                    "Workflow result mapping key 必须是唯一字符串 literal",
                    node=key or value,
                )
            result[key.value] = expression
        return result
    if declaration.form == "dataclass":
        if (
            not isinstance(value, ast.Call)
            or not isinstance(value.func, ast.Name)
            or value.func.id != declaration.constructor_name
            or value.args
            or any(item.arg is None for item in value.keywords)
        ):
            _fail(
                "invalid_workflow_output",
                "dataclass Workflow result 必须调用声明的 frozen constructor",
                node=value,
            )
        names = [str(item.arg) for item in value.keywords]
        if len(names) != len(set(names)):
            _fail(
                "invalid_workflow_output",
                "Workflow result constructor field 重复",
                node=value,
            )
        return {
            str(item.arg): item.value for item in value.keywords if item.arg is not None
        }
    _fail(
        "invalid_workflow_output",
        "未知 Workflow result-record declaration",
        node=statement,
    )


def _workflow_output_binding(
    output_name: str,
    expression: ast.expr,
    *,
    parameters: Mapping[str, Mapping[str, Any]],
    results: Mapping[str, _NodeState],
    infer_schema: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if isinstance(expression, ast.Name) and expression.id in parameters:
        parameter = parameters[expression.id]
        descriptor = {
            "name": output_name,
            "schema": _detached(parameter["schema"]),
        }
        for key in ("title", "description"):
            if key in parameter:
                descriptor[key] = parameter[key]
        return descriptor, {
            "kind": "workflow_input",
            "parameter": expression.id,
        }
    producer = _result_reference(expression, results)
    if producer is None:
        _fail(
            "invalid_workflow_output",
            "Workflow output 必须绑定 Workflow input 或 named result output",
            node=expression,
        )
    node_state, producer_output_name = producer
    handle = _source_handle(
        node_state.handles,
        producer_output_name,
        node=expression,
    )
    descriptor = {
        "name": output_name,
        "schema": _schema_from_handle(handle) if infer_schema else {},
    }
    return descriptor, {
        "kind": "node_output",
        "workflow_node_uuid": node_state.node["uuid"],
        "source_handle_uuid": handle["uuid"],
    }


def _synthesize_implicit_workflow_outputs(
    outputs: list[dict[str, Any]],
    bindings: dict[str, dict[str, Any]],
    *,
    parameters: Mapping[str, Mapping[str, Any]],
) -> None:
    by_name = {item["name"]: item for item in outputs}
    for parameter_name, parameter in parameters.items():
        if _resource_slot_schema(parameter["schema"]) is None:
            continue
        existing = by_name.get(parameter_name)
        if existing is not None:
            if not resource_slot_passthrough_is_compatible(
                parameter["schema"],
                existing["schema"],
            ):
                _fail(
                    "invalid_workflow_output",
                    "同名显式 output 与 ResourceSlot pass-through 不兼容",
                )
            continue
        descriptor = {
            "name": parameter_name,
            "schema": _detached(parameter["schema"]),
            "implicit": True,
        }
        for key in ("title", "description"):
            if key in parameter:
                descriptor[key] = parameter[key]
        outputs.append(descriptor)
        by_name[parameter_name] = descriptor
        bindings[parameter_name] = {
            "kind": "workflow_input",
            "parameter": parameter_name,
        }


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
    refreshed_published_templates = {
        str(item.template["uuid"]): item.template
        for item in state.nodes
        if _is_published_workflow_template(item.template)
    }
    applied_templates = {
        item["uuid"]: item for item in state.applied_graph["node_templates"]
    }
    node_templates = sorted(
        (
            _detached(
                refreshed_published_templates.get(
                    str(item["uuid"]),
                    applied_templates.get(item["uuid"], item),
                )
            )
            for item in state.snapshot.node_templates
            if item["uuid"] in referenced_templates
        ),
        key=lambda item: item["uuid"],
    )
    applied_handles_by_template: dict[str, list[dict[str, Any]]] = {}
    for item in state.applied_graph["handle_templates"]:
        applied_handles_by_template.setdefault(
            item["workflow_node_template_uuid"],
            [],
        ).append(item)
    refreshed_published_handles: dict[str, list[dict[str, Any]]] = {}
    for item in state.nodes:
        template_uuid = str(item.template["uuid"])
        if template_uuid not in refreshed_published_templates:
            continue
        refreshed_published_handles[template_uuid] = list(item.handles)
    snapshot_handles_by_template: dict[str, list[dict[str, Any]]] = {}
    for item in state.snapshot.handle_templates:
        snapshot_handles_by_template.setdefault(
            item["workflow_node_template_uuid"],
            [],
        ).append(item)
    handle_templates = sorted(
        (
            _detached(item)
            for template_uuid in referenced_templates
            for item in (
                refreshed_published_handles.get(template_uuid)
                or (
                    applied_handles_by_template.get(template_uuid, [])
                    if template_uuid in applied_templates
                    else snapshot_handles_by_template.get(template_uuid, [])
                )
            )
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


def _validate_built_graph(
    graph: dict[str, Any],
    *,
    material_source_authority: MaterialSourceStaticAuthority | None = None,
) -> None:
    templates = {item["uuid"]: item for item in graph["node_templates"]}
    handles = {item["uuid"]: item for item in graph["handle_templates"]}
    nodes = [WorkflowNodeWrite.model_validate(item) for item in graph["nodes"]]
    edges = [WorkflowEdgeWrite.model_validate(item) for item in graph["edges"]]
    _validate_typed_action_field_providers(
        nodes=nodes,
        edges=edges,
        templates=templates,
        handles=handles,
    )
    validate_graph(
        nodes=nodes,
        edges=edges,
        templates=templates,
        handles=handles,
        effective_params={node.uuid: node.param or {} for node in nodes},
        workflow_meta_data=graph["workflow"].get("meta_data") or {},
        node_meta_data={node.uuid: node.meta_data for node in nodes},
        validate_workflow_io_contract=True,
    )
    validate_material_source_authority(graph, material_source_authority)


def _validate_typed_action_field_providers(
    *,
    nodes: list[WorkflowNodeWrite],
    edges: list[WorkflowEdgeWrite],
    templates: Mapping[str, dict[str, Any]],
    handles: Mapping[str, dict[str, Any]],
) -> None:
    """在通用图校验前保留 typed Action 字段的稳定诊断坐标。"""

    edge_targets = {(edge.target_node_uuid, edge.target_handle_uuid) for edge in edges}
    for node in nodes:
        template_uuid = node.workflow_node_template_uuid
        template = templates.get(template_uuid or "")
        if not isinstance(template, Mapping):
            continue
        schema = template.get("schema")
        extension = (
            schema.get("x-unilabos-action-contract")
            if isinstance(schema, Mapping)
            else None
        )
        if not isinstance(extension, Mapping) or extension.get("version") != 1:
            continue
        param = node.param or {}
        unilab = node.meta_data.get("unilab")
        bindings = unilab.get("input_bindings") if isinstance(unilab, Mapping) else {}
        if not isinstance(bindings, Mapping):
            bindings = {}
        for handle in handles.values():
            if (
                handle.get("workflow_node_template_uuid") != template_uuid
                or handle.get("io_type") != "target"
                or str(handle.get("handle_key") or "").lower() == "ready"
            ):
                continue
            handle_uuid = str(handle.get("uuid") or "")
            data_key = str(handle.get("data_key") or handle.get("handle_key") or "")
            providers = sum(
                (
                    data_key in param,
                    handle_uuid in bindings,
                    (node.uuid, handle_uuid) in edge_targets,
                )
            )
            fields = {
                "node_id": node.uuid,
                "workflow_handle_template_uuid": handle_uuid,
                "path": (
                    f"/nodes/{node.uuid}/param/"
                    f"{data_key.replace('~', '~0').replace('/', '~1')}"
                ),
            }
            if providers > 1:
                _fail(
                    "candidate_invalid",
                    "target Handle 有多个 provider",
                    fields=fields,
                )
            if handle.get("required") is True and providers == 0:
                _fail(
                    "required_action_parameter_missing",
                    f"{handle.get('display_name') or data_key}为必填参数",
                    fields=fields,
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


def _materialize_typed_action_defaults(
    graph: dict[str, Any],
) -> dict[str, Any]:
    """把 canonical ``@action`` default 物化进 OS normalized Candidate。"""

    normalized = clone_json(graph)
    templates = {
        str(template.get("uuid")): template for template in normalized["node_templates"]
    }
    handles_by_template: dict[str, dict[str, dict[str, Any]]] = {}
    for handle in normalized["handle_templates"]:
        if handle.get("io_type") != "target":
            continue
        template_uuid = str(handle.get("workflow_node_template_uuid") or "")
        data_key = str(handle.get("data_key") or handle.get("handle_key") or "")
        handles_by_template.setdefault(template_uuid, {})[data_key] = handle
    edge_targets = {
        (str(edge.get("target_node_uuid")), str(edge.get("target_handle_uuid")))
        for edge in normalized["edges"]
    }

    for node in normalized["nodes"]:
        template_uuid = str(node.get("workflow_node_template_uuid") or "")
        template = templates.get(template_uuid)
        schema = template.get("schema") if isinstance(template, Mapping) else None
        extension = (
            schema.get("x-unilabos-action-contract")
            if isinstance(schema, Mapping)
            else None
        )
        if not isinstance(extension, Mapping) or extension.get("version") != 1:
            continue
        properties = schema.get("properties")
        goal = properties.get("goal") if isinstance(properties, Mapping) else None
        fields = goal.get("properties") if isinstance(goal, Mapping) else None
        if not isinstance(fields, Mapping):
            _fail("template_catalog_mismatch", "typed Action goal schema 不完整")
        param = node.get("param")
        if not isinstance(param, dict):
            _fail("candidate_invalid", "typed Action param 必须是对象")
        unilab = (node.get("meta_data") or {}).get("unilab") or {}
        bindings = unilab.get("input_bindings") or {}
        if not isinstance(bindings, Mapping):
            _fail("candidate_invalid", "Node input_bindings 必须是对象")
        handles = handles_by_template.get(template_uuid, {})
        for name, value_schema in fields.items():
            if name in param or not isinstance(value_schema, Mapping):
                continue
            handle = handles.get(str(name))
            handle_uuid = str(handle.get("uuid") or "") if handle else ""
            if (
                not handle_uuid
                or handle_uuid in bindings
                or (str(node.get("uuid")), handle_uuid) in edge_targets
                or "default" not in value_schema
            ):
                continue
            param[str(name)] = clone_json(value_schema["default"])
    return normalized


def _generate_with_snapshot(
    *,
    snapshot: TemplateCatalogSnapshot,
    workflow_uuid: str,
    workflow_revision: int,
    graph: dict[str, Any],
    source_uri: str,
    resource_template_identity_index: ResourceTemplateIdentityIndex | None,
    material_source_authority: MaterialSourceStaticAuthority | None,
    composite_authoring: CompositeAuthoring | None,
) -> CandidateCompilation:
    del source_uri
    try:
        candidate = _require_graph_identity(
            graph,
            workflow_uuid=workflow_uuid,
            workflow_revision=workflow_revision,
        )
        stale_published = _validate_catalog_projection(snapshot, candidate)
        _validate_built_graph(
            candidate,
            material_source_authority=material_source_authority,
        )
        normalized_candidate = _materialize_typed_action_defaults(candidate)
        unexpanded_composites = _unexpanded_composite_invocations(normalized_candidate)
        source, source_map = _render_graph(
            normalized_candidate,
            resource_template_identity_index=resource_template_identity_index,
        )
        recompiled = _compile_with_snapshot(
            snapshot=snapshot,
            workflow_uuid=workflow_uuid,
            workflow_revision=workflow_revision,
            python_source=source,
            source_uri="authoring://round-trip-proof",
            applied_graph=normalized_candidate,
            resource_template_identity_index=resource_template_identity_index,
            material_source_authority=material_source_authority,
            composite_authoring=composite_authoring,
            prove_normalized=False,
        )
        if not recompiled.valid:
            diagnostic = recompiled.diagnostics[0] if recompiled.diagnostics else {}
            _fail(
                str(diagnostic.get("code") or "round_trip_mismatch"),
                str(
                    diagnostic.get("message")
                    or "Candidate graph 不能证明为等价的 Python"
                ),
            )
        if (
            not (
                _semantic_graph_equal(recompiled.graph, normalized_candidate)
                or (
                    unexpanded_composites
                    and _semantic_graph_equal(
                        _align_stale_published_projection(
                            _project_unexpanded_composite_boundaries(
                                recompiled.graph,
                                unexpanded_composites,
                            ),
                            normalized_candidate,
                            stale_published,
                        ),
                        normalized_candidate,
                    )
                )
            )
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
    except (CodedGraphValidationError, MaterialSourceAuthorityError) as error:
        return _error_result(
            fingerprint=snapshot.fingerprint,
            diagnostic={
                "severity": "error",
                "code": error.code,
                "message": str(error),
            },
        )
    except GraphValidationError as error:
        code = (
            "round_trip_mismatch"
            if isinstance(error.__cause__, WorkflowIOValidationError)
            else "candidate_invalid"
        )
        return _error_result(
            fingerprint=snapshot.fingerprint,
            diagnostic={
                "severity": "error",
                "code": code,
                "message": "Candidate graph 不满足 Workflow 合同",
            },
        )
    except (TypeError, ValueError):
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
) -> set[str]:
    referenced = {
        node.get("workflow_node_template_uuid")
        for node in graph["nodes"]
        if node.get("workflow_node_template_uuid") is not None
    }
    snapshot_nodes = {
        item["uuid"]: _catalog_read_entity(
            item,
            nullable_fields=_NODE_TEMPLATE_NULLABLE_READ_FIELDS,
        )
        for item in snapshot.node_templates
        if item["uuid"] in referenced
    }
    projected_nodes = {
        item.get("uuid"): _catalog_read_entity(
            item,
            nullable_fields=_NODE_TEMPLATE_NULLABLE_READ_FIELDS,
        )
        for item in graph["node_templates"]
    }
    snapshot_handles = {
        item["uuid"]: _catalog_read_entity(
            item,
            nullable_fields=_HANDLE_TEMPLATE_NULLABLE_READ_FIELDS,
        )
        for item in snapshot.handle_templates
        if item["workflow_node_template_uuid"] in referenced
    }
    projected_handles = {
        item.get("uuid"): _catalog_read_entity(
            item,
            nullable_fields=_HANDLE_TEMPLATE_NULLABLE_READ_FIELDS,
        )
        for item in graph["handle_templates"]
    }
    stale_published = _stale_published_projection_uuids(
        graph=graph,
        snapshot_nodes=snapshot_nodes,
        projected_nodes=projected_nodes,
        snapshot_handles=snapshot_handles,
        projected_handles=projected_handles,
    )
    if (
        set(snapshot_nodes) != referenced
        or len(projected_nodes) != len(graph["node_templates"])
        or set(projected_nodes) != set(snapshot_nodes)
        or any(
            uuid not in stale_published
            and not _catalog_wire_equal(projected_nodes[uuid], snapshot_nodes[uuid])
            for uuid in snapshot_nodes
        )
    ):
        _fail(
            "template_catalog_mismatch",
            "Candidate NodeTemplate projection 不属于当前 authority snapshot",
        )
    exact_snapshot_handles = {
        uuid: handle
        for uuid, handle in snapshot_handles.items()
        if handle.get("workflow_node_template_uuid") not in stale_published
    }
    exact_projected_handles = {
        uuid: handle
        for uuid, handle in projected_handles.items()
        if handle.get("workflow_node_template_uuid") not in stale_published
    }
    if (
        len(projected_handles) != len(graph["handle_templates"])
        or set(exact_projected_handles) != set(exact_snapshot_handles)
        or any(
            not _catalog_wire_equal(
                exact_projected_handles[uuid],
                exact_snapshot_handles[uuid],
            )
            for uuid in exact_snapshot_handles
        )
    ):
        _fail(
            "template_catalog_mismatch",
            "Candidate HandleTemplate projection 不属于当前 authority snapshot",
        )
    return stale_published


def _stale_published_projection_uuids(
    *,
    graph: Mapping[str, Any],
    snapshot_nodes: Mapping[str, Mapping[str, Any]],
    projected_nodes: Mapping[str, Mapping[str, Any]],
    snapshot_handles: Mapping[str, Mapping[str, Any]],
    projected_handles: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    result: set[str] = set()
    graph_nodes = [node for node in graph.get("nodes", []) if isinstance(node, Mapping)]
    for template_uuid, previous_template in projected_nodes.items():
        current_template = snapshot_nodes.get(template_uuid)
        if (
            not isinstance(template_uuid, str)
            or not isinstance(current_template, Mapping)
            or not _is_published_workflow_template(previous_template)
            or not _is_published_workflow_template(current_template)
            or not _published_template_identity_equal(
                previous_template,
                current_template,
            )
        ):
            continue
        previous_handles = [
            handle
            for handle in projected_handles.values()
            if handle.get("workflow_node_template_uuid") == template_uuid
        ]
        current_handles = [
            handle
            for handle in snapshot_handles.values()
            if handle.get("workflow_node_template_uuid") == template_uuid
        ]
        try:
            previous_projection = published_workflow_compatibility_projection(
                previous_template,
                previous_handles,
            )
            current_projection = published_workflow_compatibility_projection(
                current_template,
                current_handles,
            )
        except (KeyError, TypeError, ValueError):
            continue
        if previous_projection == current_projection and (
            _published_implementation_pin(previous_template)
            == _published_implementation_pin(current_template)
        ):
            continue
        invocations = [
            node
            for node in graph_nodes
            if node.get("workflow_node_template_uuid") == template_uuid
        ]
        if not invocations or not all(
            _invocation_projection_matches(node, previous_projection)
            for node in invocations
        ):
            continue
        if not published_workflow_projection_is_canonical(
            previous_template,
            previous_handles,
        ):
            continue
        result.add(template_uuid)
    return result


def _published_implementation_pin(template: Mapping[str, Any]) -> tuple[Any, Any]:
    schema = template.get("schema")
    extension = (
        schema.get("x-unilabos-workflow-contract")
        if isinstance(schema, Mapping)
        else None
    )
    if not isinstance(extension, Mapping):
        return None, None
    return extension.get("workflow_revision"), extension.get("applied_source_hash")


def _published_template_identity_equal(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> bool:
    if any(
        previous.get(key) != current.get(key)
        for key in (
            "uuid",
            "resource_template_uuid",
            "name",
            "type",
            "node_type",
            "class",
        )
    ):
        return False

    def authority(template: Mapping[str, Any]) -> tuple[Any, Any]:
        meta_data = template.get("meta_data")
        unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
        if not isinstance(unilab, Mapping):
            return None, None
        return unilab.get("workflow_source"), unilab.get("framework_owner_only")

    return authority(previous) == authority(current)


def _invocation_projection_matches(
    node: Mapping[str, Any],
    projection: Mapping[str, Any],
) -> bool:
    meta_data = node.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    composite = unilab.get("composite") if isinstance(unilab, Mapping) else None
    if not isinstance(composite, Mapping):
        return False
    return composite.get("contract_compatibility") == projection and all(
        composite.get(key) == projection.get(projection_key)
        for key, projection_key in (
            ("child_workflow_uuid", "workflow_uuid"),
            ("contract_digest", "digest"),
            ("composition_allow_transparent", "mode"),
        )
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
                "start_column": utf16_length(indent) + 1,
                "end_line": end,
                "end_column": utf16_length(f"{indent}{construct}") + 1,
            }
        )


def _render_graph(
    graph: dict[str, Any],
    *,
    resource_template_identity_index: ResourceTemplateIdentityIndex | None,
) -> tuple[str, list[dict[str, Any]]]:
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

    composite_internal_uuids = _composite_internal_node_uuids(nodes, templates)
    visible_nodes = [
        item for item in nodes if item["uuid"] not in composite_internal_uuids
    ]
    selectors, selector_by_node = _render_selectors(nodes, templates)
    material_source_nodes = [
        item
        for item in visible_nodes
        if _is_material_source_template(
            templates.get(str(item.get("workflow_node_template_uuid")), {})
        )
    ]
    material_source_uuids = {item["uuid"] for item in material_source_nodes}
    execution_nodes = [
        item for item in nodes if item["uuid"] not in material_source_uuids
    ]
    execution_edges = [
        item
        for item in graph["edges"]
        if item["source_node_uuid"] not in material_source_uuids
        and item["target_node_uuid"] not in material_source_uuids
    ]
    published_workflow_nodes = [
        item
        for item in visible_nodes
        if _is_published_workflow_template(
            templates.get(str(item.get("workflow_node_template_uuid")), {})
        )
    ]
    explicit_outputs = [
        item for item in output_contract["outputs"] if not item.get("implicit", False)
    ]
    needs = _annotation_import_needs(
        {
            "parameters": [
                *input_contract["parameters"],
                *explicit_outputs,
            ]
        }
    )
    if explicit_outputs:
        needs["typing"].add("TypedDict")
    markers = {"workflow_definition"}
    if selectors:
        markers.add("device")
    group_nodes = [item for item in visible_nodes if _is_group_node(item, templates)]
    # MaterialSource 是函数体内的 selector 声明，不是可执行 action，也不参与
    # parallel/group 的结构分层；多物料源统一先声明，再恢复执行 DAG。
    root_layers = _root_construct_layers(
        execution_nodes,
        execution_edges,
        templates,
    )
    if group_nodes:
        markers.add("group")
    if any(len(layer) > 1 for layer in root_layers):
        markers.add("parallel")
    if material_source_nodes:
        markers.update({"MaterialFlowRole", "material_source", "resource_ref"})
    if any(
        _resource_ref_source(node, handle) is not None
        for node in nodes
        for handle in handles_by_node.get(
            str(node.get("workflow_node_template_uuid")), []
        )
    ):
        markers.add("resource_ref")

    emitter = _Emitter()
    if needs["typing"]:
        emitter.emit(f"from typing import {', '.join(sorted(needs['typing']))}")
    if needs["field"]:
        emitter.emit("from pydantic import Field")
    function_name = _snake_case(str(workflow.get("name") or "workflow"), "workflow")
    result_record_name = _workflow_result_record_name(function_name)
    reserved_import_names = {
        *needs["typing"],
        *markers,
        "AllowedResourceTemplates",
        "Field",
        "JSONValue",
        "ResourceSlot",
        result_record_name,
    }
    class_import_names: dict[str, str] = {}
    for class_identity in sorted({key[0] for key in selectors}):
        module, symbol = class_identity.rsplit(":", 1)
        imported_name = symbol
        suffix = 2
        while imported_name in reserved_import_names:
            imported_name = f"{symbol}_{suffix}"
            suffix += 1
        reserved_import_names.add(imported_name)
        class_import_names[class_identity] = imported_name
        alias = "" if imported_name == symbol else f" as {imported_name}"
        emitter.emit(f"from {module} import {symbol}{alias}")
    workflow_import_names: dict[str, str] = {}
    def workflow_import_sort_key(template_uuid: str) -> tuple[str, str, str]:
        template = templates.get(template_uuid, {})
        meta_data = template.get("meta_data")
        unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
        provenance = (
            unilab.get("workflow_source") if isinstance(unilab, Mapping) else None
        )
        module = provenance.get("module") if isinstance(provenance, Mapping) else ""
        symbol = provenance.get("symbol") if isinstance(provenance, Mapping) else ""
        return (str(module), str(symbol), template_uuid)

    for template_uuid in sorted(
        {str(node["workflow_node_template_uuid"]) for node in published_workflow_nodes},
        key=workflow_import_sort_key,
    ):
        template = templates[template_uuid]
        try:
            provenance = template["meta_data"]["unilab"]["workflow_source"]
            module = provenance["module"]
            symbol = provenance["symbol"]
        except (KeyError, TypeError):
            _fail(
                "composite_catalog_mismatch",
                "Published Workflow provenance 不完整",
            )
        if (
            not isinstance(module, str)
            or not module
            or module.startswith(".")
            or not isinstance(symbol, str)
            or not symbol.isidentifier()
            or template.get("class") != f"{module}:{symbol}"
        ):
            _fail(
                "composite_catalog_mismatch",
                "Published Workflow provenance 不是 absolute import identity",
            )
        imported_name = symbol
        suffix = 2
        while imported_name in reserved_import_names:
            imported_name = f"{symbol}_{suffix}"
            suffix += 1
        reserved_import_names.add(imported_name)
        workflow_import_names[template_uuid] = imported_name
        alias = "" if imported_name == symbol else f" as {imported_name}"
        emitter.emit(f"from {module} import {symbol}{alias}")
    resource_import_names: dict[str, str] = {}
    workflow_contract_resource_template_uuids = {
        resource_template_uuid
        for descriptor in [*input_contract["parameters"], *explicit_outputs]
        for resource_template_uuid in _resource_template_allowlist(descriptor["schema"])
    }
    resource_imports: list[tuple[str, str, str, str]] = []
    for resource_template_uuid in (
        workflow_contract_resource_template_uuids
        | {
            str(item.get("param", {}).get("resource_template_uuid"))
            for item in material_source_nodes
        }
    ):
        if resource_template_identity_index is None:
            _fail(
                "template_catalog_mismatch",
                "当前 authority 无法反查 ResourceTemplate UUID",
            )
        try:
            identity = resource_template_identity_index.identify_uuid(
                validate_uuid(resource_template_uuid)
            )
            if (
                validate_uuid(resource_template_identity_index.resolve_symbol(identity))
                != resource_template_uuid
            ):
                raise LookupError(identity)
            module, symbol = identity.rsplit(":", 1)
            if not module or not symbol:
                raise ValueError(identity)
        except (AttributeError, LookupError, TypeError, ValueError):
            _fail(
                "template_catalog_mismatch",
                "当前 authority 无法反查 ResourceTemplate UUID",
            )
        resource_imports.append(
            (identity, resource_template_uuid, module, symbol)
        )
    for identity, resource_template_uuid, module, symbol in sorted(
        resource_imports
    ):
        imported_name = symbol
        suffix = 2
        while imported_name in reserved_import_names:
            imported_name = f"{symbol}_{suffix}"
            suffix += 1
        reserved_import_names.add(imported_name)
        resource_import_names[resource_template_uuid] = imported_name
        alias = "" if imported_name == symbol else f" as {imported_name}"
        emitter.emit(f"from {module} import {symbol}{alias}")
    registry_annotation_imports = []
    if needs["allowed_resource_templates"]:
        registry_annotation_imports.append("AllowedResourceTemplates")
    if needs["json_value"]:
        registry_annotation_imports.append("JSONValue")
    if registry_annotation_imports:
        emitter.emit(
            "from unilabos.registry.annotations import "
            + ", ".join(registry_annotation_imports)
        )
    if needs["resource_slot"]:
        emitter.emit("from unilabos.registry.placeholder_type import ResourceSlot")
    emitter.emit(
        "from unilabos.workflow.authoring import " + ", ".join(sorted(markers))
    )
    emitter.emit()
    emitter.emit()
    if explicit_outputs:
        emitter.emit(f"class {result_record_name}(TypedDict):")
        for output in explicit_outputs:
            annotation = _annotation_source(
                output["schema"],
                output,
                resource_import_names=resource_import_names,
            )
            emitter.emit(f"    {output['name']}: {annotation}")
        emitter.emit()
        emitter.emit()
    for (class_identity, device_id), local_name in selectors.items():
        symbol = class_import_names[class_identity]
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
    parameters = input_contract["parameters"]
    result_annotation = result_record_name if explicit_outputs else "None"
    if parameters:
        emitter.emit(f"def {function_name}(")
        emitter.emit("    *,")
        for parameter in parameters:
            annotation = _annotation_source(
                parameter["schema"],
                parameter,
                resource_import_names=resource_import_names,
            )
            declaration = f"{parameter['name']}: {annotation}"
            if not parameter["required"]:
                declaration += f" = {parameter['default']!r}"
            emitter.emit(f"    {declaration},")
        emitter.emit(f") -> {result_annotation}:")
    else:
        emitter.emit(f"def {function_name}() -> {result_annotation}:")

    body_indent = "    "
    for material_source_node in material_source_nodes:
        _emit_material_source(
            emitter,
            material_source_node,
            indent=body_indent,
            resource_import_names=resource_import_names,
        )
    for layer in root_layers:
        if len(layer) > 1:
            if all(
                _is_material_source_template(
                    templates.get(
                        str(node.get("workflow_node_template_uuid")),
                        {},
                    )
                )
                for node in layer
            ):
                for material_source_node in layer:
                    _emit_material_source(
                        emitter,
                        material_source_node,
                        indent=body_indent,
                        resource_import_names=resource_import_names,
                    )
                continue
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
                    workflow_import_names=workflow_import_names,
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
                workflow_import_names=workflow_import_names,
            )
        else:
            template = templates.get(
                str(construct.get("workflow_node_template_uuid")),
                {},
            )
            if _is_published_workflow_template(template):
                _emit_published_workflow(
                    emitter,
                    construct,
                    indent=body_indent,
                    template=template,
                    templates=templates,
                    handles_by_node=handles_by_node,
                    handles=handles,
                    node_by_uuid=node_by_uuid,
                    edge_by_target=edge_by_target,
                    workflow_import_names=workflow_import_names,
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

    if explicit_outputs:
        parts = []
        for output in explicit_outputs:
            name = output["name"]
            expression = _output_expression(
                output_bindings[name],
                node_by_uuid,
                handles,
            )
            parts.append(f"{name!r}: {expression}")
        emitter.emit(f"{body_indent}return {{{', '.join(parts)}}}")
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


def _is_published_workflow_template(template: Mapping[str, Any]) -> bool:
    schema = template.get("schema")
    extension = (
        schema.get("x-unilabos-workflow-contract")
        if isinstance(schema, Mapping)
        else None
    )
    return (
        template.get("type") == "workflow"
        and template.get("node_type") == "workflow"
        and isinstance(extension, Mapping)
        and extension.get("version") == 1
    )


def _composite_internal_node_uuids(
    nodes: Sequence[Mapping[str, Any]],
    templates: Mapping[str, dict[str, Any]],
) -> set[str]:
    node_by_uuid = {str(node["uuid"]): node for node in nodes}
    composite_uuids = {
        node_uuid
        for node_uuid, node in node_by_uuid.items()
        if _is_published_workflow_template(
            templates.get(str(node.get("workflow_node_template_uuid")), {})
        )
    }
    internal: set[str] = set()
    for node_uuid, node in node_by_uuid.items():
        parent = node.get("parent_uuid")
        seen = {node_uuid}
        while isinstance(parent, str):
            if parent in seen or parent not in node_by_uuid:
                _fail("candidate_invalid", "Composite parent hierarchy 不完整")
            if parent in composite_uuids:
                internal.add(node_uuid)
                break
            seen.add(parent)
            parent = node_by_uuid[parent].get("parent_uuid")
    return internal


def _root_construct_layers(
    nodes: Sequence[dict[str, Any]],
    edges: Sequence[dict[str, Any]],
    templates: Mapping[str, dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """折叠展示 group 与 Composite 内部节点，恢复唯一可表示的根顺序。"""

    group_uuids = {item["uuid"] for item in nodes if _is_group_node(item, templates)}
    node_by_uuid = {str(item["uuid"]): item for item in nodes}
    roots: dict[str, dict[str, Any]] = {}
    owner_by_node: dict[str, str] = {}
    for node in nodes:
        node_uuid = node["uuid"]
        parent_uuid = node.get("parent_uuid")
        if parent_uuid is None:
            owner_by_node[node_uuid] = node_uuid
            roots[node_uuid] = node
            continue
        owner = parent_uuid
        seen = {node_uuid}
        while True:
            if owner in seen or owner not in node_by_uuid:
                _fail("candidate_invalid", "Node parent hierarchy 不完整")
            seen.add(owner)
            parent = node_by_uuid[owner].get("parent_uuid")
            if parent is None:
                break
            owner = parent
        owner_template = templates.get(
            str(node_by_uuid[owner].get("workflow_node_template_uuid")),
            {},
        )
        if owner not in group_uuids and not _is_published_workflow_template(
            owner_template
        ):
            _fail("candidate_invalid", "Node parent 不是 group 或 Composite")
        owner_by_node[node_uuid] = owner

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
        ready_material_sources = sorted(
            uuid
            for uuid in remaining
            if not (dependencies[uuid] & remaining)
            and _is_material_source_template(
                templates.get(
                    str(roots[uuid].get("workflow_node_template_uuid")),
                    {},
                )
            )
        )
        if ready_material_sources:
            layers.extend([[roots[uuid]] for uuid in ready_material_sources])
            remaining.difference_update(ready_material_sources)
            continue
        ready = sorted(
            (uuid for uuid in remaining if not (dependencies[uuid] & remaining)),
        )
        if not ready:
            _fail("candidate_invalid", "Candidate graph 包含循环依赖")
        if len(ready) > 1:
            ready_are_groups = all(uuid in group_uuids for uuid in ready)
            ready_are_material_sources = all(
                _is_material_source_template(
                    templates.get(
                        str(
                            roots[uuid].get(
                                "workflow_node_template_uuid"
                            )
                        ),
                        {},
                    )
                )
                for uuid in ready
            )
            if not ready_are_groups and not ready_are_material_sources:
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
    selector_key_by_node: dict[str, tuple[str, str | None]] = {}
    internal_uuids = _composite_internal_node_uuids(nodes, templates)
    for node in nodes:
        if node["uuid"] in internal_uuids or _is_group_node(node, templates):
            continue
        template = templates.get(str(node.get("workflow_node_template_uuid")))
        if template is None:
            _fail(
                "template_catalog_mismatch",
                "Node 未引用当前 graph 的 NodeTemplate",
            )
        if _is_material_source_template(template) or _is_published_workflow_template(
            template
        ):
            continue
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
        selector_key_by_node[node["uuid"]] = key

    selectors: dict[tuple[str, str | None], str] = {}
    used: set[str] = set()
    for class_identity, device_id in sorted(
        set(selector_key_by_node.values()),
        key=lambda item: (item[0], item[1] is not None, item[1] or ""),
    ):
        base = _snake_case(class_identity.rsplit(":", 1)[1], "device")
        local = base
        suffix = 2
        while local in used:
            local = f"{base}_{suffix}"
            suffix += 1
        used.add(local)
        selectors[(class_identity, device_id)] = local
    by_node = {
        node_uuid: selectors[key] for node_uuid, key in selector_key_by_node.items()
    }
    return selectors, by_node


def _annotation_import_needs(contract: Mapping[str, Any]) -> dict[str, Any]:
    typing_names: set[str] = set()
    field_needed = False
    resource_slot = False
    allowed_resource_templates = False
    json_value = False
    for parameter in contract.get("parameters", []):
        schema = parameter["schema"]
        constraint_schema = schema["anyOf"][0] if "anyOf" in schema else schema
        if (
            "title" in parameter
            or "description" in parameter
            or any(
                key in constraint_schema
                for key in (
                    "minimum",
                    "maximum",
                    "minLength",
                    "maxLength",
                    "minItems",
                    "maxItems",
                )
            )
        ):
            typing_names.add("Annotated")
            field_needed = True
        if _resource_template_allowlist(schema):
            typing_names.add("Annotated")
            allowed_resource_templates = True
        if _schema_contains_key(constraint_schema, "enum"):
            typing_names.add("Literal")
        if _schema_contains_key(constraint_schema, "$slot"):
            resource_slot = True
        if _schema_contains_kind(constraint_schema, "object"):
            json_value = True
    return {
        "typing": typing_names,
        "field": field_needed,
        "resource_slot": resource_slot,
        "allowed_resource_templates": allowed_resource_templates,
        "json_value": json_value,
    }


def _schema_contains_kind(schema: Mapping[str, Any], kind: str) -> bool:
    if schema.get("type") == kind:
        return True
    items = schema.get("items")
    return isinstance(items, Mapping) and _schema_contains_kind(items, kind)


def _schema_contains_key(schema: Mapping[str, Any], key: str) -> bool:
    if key in schema:
        return True
    items = schema.get("items")
    return isinstance(items, Mapping) and _schema_contains_key(items, key)


def _resource_template_allowlist(schema: Mapping[str, Any]) -> tuple[str, ...]:
    resource_slot_schema = _resource_slot_schema(schema)
    if resource_slot_schema is None:
        return ()
    allowlist = resource_slot_schema.get("allowed_resource_template_uuids")
    if not isinstance(allowlist, list):
        return ()
    return tuple(str(item) for item in allowlist)


def _resource_slot_schema(
    schema: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    members = schema.get("anyOf")
    if isinstance(members, list):
        for member in members:
            if isinstance(member, Mapping) and member.get("type") != "null":
                return _resource_slot_schema(member)
        return None
    if schema.get("$slot") == "ResourceSlot":
        return schema
    items = schema.get("items")
    if schema.get("type") == "array" and isinstance(items, Mapping):
        return _resource_slot_schema(items)
    return None


def _annotation_source(
    schema: Mapping[str, Any],
    descriptor: Mapping[str, Any] | None = None,
    *,
    resource_import_names: Mapping[str, str] | None = None,
) -> str:
    if "anyOf" in schema:
        members = schema["anyOf"]
        base = _base_annotation_source(members[0]) + " | None"
        constraint_schema = members[0]
    else:
        base = _base_annotation_source(schema)
        constraint_schema = schema
    metadata: list[str] = []
    resource_template_uuids = _resource_template_allowlist(schema)
    if resource_template_uuids:
        if resource_import_names is None:
            _fail(
                "template_catalog_mismatch",
                "当前 authority 无法反查 ResourceTemplate UUID",
            )
        resource_symbols: list[str] = []
        for resource_template_uuid in resource_template_uuids:
            resource_symbol = resource_import_names.get(resource_template_uuid)
            if resource_symbol is None:
                _fail(
                    "template_catalog_mismatch",
                    "当前 authority 无法反查 ResourceTemplate UUID",
                )
            resource_symbols.append(resource_symbol)
        metadata.append(f"AllowedResourceTemplates({', '.join(resource_symbols)})")
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
        metadata.append(f"Field({', '.join(field_parts)})")
    if metadata:
        return f"Annotated[{base}, {', '.join(metadata)}]"
    return base


def _base_annotation_source(schema: Mapping[str, Any]) -> str:
    if schema.get("$slot") == "ResourceSlot":
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
    workflow_import_names: Mapping[str, str],
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
        template = templates.get(
            str(child.get("workflow_node_template_uuid")),
            {},
        )
        if _is_published_workflow_template(template):
            _emit_published_workflow(
                emitter,
                child,
                indent=indent + "    ",
                template=template,
                templates=templates,
                handles_by_node=handles_by_node,
                handles=handles,
                node_by_uuid=node_by_uuid,
                edge_by_target=edge_by_target,
                workflow_import_names=workflow_import_names,
            )
        else:
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


def _emit_material_source(
    emitter: _Emitter,
    node: Mapping[str, Any],
    *,
    indent: str,
    resource_import_names: Mapping[str, str],
) -> None:
    param = node.get("param")
    expected_keys = {
        "mode",
        "resource_template_uuid",
        "mount",
        "material_uuid",
        "site",
        "slot_range",
        "flow_role",
    }
    if not isinstance(param, dict) or set(param) != expected_keys:
        _fail("invalid_material_source", "MaterialSource selector 不符合当前合同")
    resource_template_uuid = str(param.get("resource_template_uuid"))
    resource_symbol = resource_import_names.get(resource_template_uuid)
    mount = param.get("mount")
    mode = param.get("mode")
    material_uuid = param.get("material_uuid")
    site = param.get("site")
    slot_range = param.get("slot_range")
    role_member = next(
        (
            member
            for member, wire_value in _MATERIAL_FLOW_ROLES.items()
            if wire_value == param.get("flow_role")
        ),
        None,
    )
    if (
        resource_symbol is None
        or mode not in {"existing", "create_new"}
        or not isinstance(mount, dict)
        or set(mount) != {"uuid"}
        or role_member is None
        or (mode == "create_new" and material_uuid is not None)
    ):
        _fail("invalid_material_source", "MaterialSource selector 不符合当前合同")
    try:
        mount_uuid = validate_uuid(mount["uuid"])
    except (KeyError, TypeError, ValueError):
        _fail("invalid_material_source", "MaterialSource mount UUID 无效")
    if material_uuid is not None:
        try:
            material_uuid = validate_uuid(material_uuid)
        except (TypeError, ValueError):
            _fail("invalid_material_source", "MaterialSource material UUID 无效")
    mount_reference = mount_uuid
    resource_refs = (node.get("meta_data") or {}).get("unilab", {}).get(
        "resource_refs", {}
    )
    binding = resource_refs.get("mount") if isinstance(resource_refs, Mapping) else None
    if isinstance(binding, Mapping) and set(binding) == {"resource_id"}:
        resource_id = binding.get("resource_id")
        if (
            isinstance(resource_id, str)
            and resource_id.strip()
            and resource_id == resource_id.strip()
        ):
            mount_reference = resource_id
    result_name = _safe_identifier(str(node.get("name") or "material"), "material")
    construct = (
        f"{result_name} = material_source("
        f"resource_template={resource_symbol}, mode={mode!r}, "
        f"mount=resource_ref({mount_reference!r}), material_uuid={material_uuid!r}, "
        f"site={site!r}, slot_range={slot_range!r}, "
        f"flow_role=MaterialFlowRole.{role_member})"
    )
    emitter.anchored(str(node["uuid"]), construct, indent=indent)


def _emit_published_workflow(
    emitter: _Emitter,
    node: dict[str, Any],
    *,
    indent: str,
    template: Mapping[str, Any],
    templates: Mapping[str, dict[str, Any]],
    handles_by_node: Mapping[str, list[dict[str, Any]]],
    handles: Mapping[str, dict[str, Any]],
    node_by_uuid: Mapping[str, dict[str, Any]],
    edge_by_target: Mapping[tuple[str, str], dict[str, Any]],
    workflow_import_names: Mapping[str, str],
) -> None:
    template_uuid = str(node.get("workflow_node_template_uuid") or "")
    imported_name = workflow_import_names.get(template_uuid)
    schema = template.get("schema")
    extension = (
        schema.get("x-unilabos-workflow-contract")
        if isinstance(schema, Mapping)
        else None
    )
    if imported_name is None or not isinstance(extension, Mapping):
        _fail(
            "composite_catalog_mismatch",
            "Published Workflow canonical import 不完整",
        )
    input_order = extension.get("input_order")
    if not isinstance(input_order, list) or any(
        not isinstance(name, str) for name in input_order
    ):
        _fail(
            "composite_catalog_mismatch",
            "Published Workflow input order 不符合合同",
        )
    target_by_name = {
        str(handle.get("data_key") or handle.get("handle_key") or ""): handle
        for handle in handles_by_node.get(template_uuid, [])
        if handle.get("io_type") == "target"
        and str(handle.get("data_key") or handle.get("handle_key") or "").lower()
        != "ready"
    }
    if set(target_by_name) != set(input_order):
        _fail(
            "composite_catalog_mismatch",
            "Published Workflow boundary Handles 不符合 input contract",
        )
    input_bindings = (
        (node.get("meta_data") or {}).get("unilab", {}).get("input_bindings", {})
    )
    param = node.get("param") or {}
    if not isinstance(input_bindings, Mapping) or not isinstance(param, Mapping):
        _fail("candidate_invalid", "Published Workflow providers 必须是对象")
    parameters: list[str] = []
    for name in input_order:
        handle = target_by_name[name]
        expression: str | None = None
        binding = input_bindings.get(handle["uuid"])
        if binding is not None:
            if (
                not isinstance(binding, Mapping)
                or set(binding) != {"parameter"}
                or not isinstance(binding.get("parameter"), str)
            ):
                _fail(
                    "candidate_invalid",
                    "Published Workflow input binding 不符合合同",
                )
            expression = str(binding["parameter"])
        edge = edge_by_target.get((node["uuid"], handle["uuid"]))
        if edge is not None:
            if expression is not None:
                _fail("candidate_invalid", "Composite target Handle 有多个 provider")
            producer = node_by_uuid.get(str(edge["source_node_uuid"]))
            source_handle = handles.get(str(edge["source_handle_uuid"]))
            if producer is None or source_handle is None:
                _fail("candidate_invalid", "Composite Edge identity 不完整")
            output_name = str(
                source_handle.get("data_key") or source_handle.get("handle_key") or ""
            )
            if output_name.lower() != "ready":
                producer_name = _safe_identifier(
                    str(producer.get("name") or "result"),
                    "result",
                )
                producer_template = templates.get(
                    str(producer.get("workflow_node_template_uuid")),
                    {},
                )
                if (
                    _is_material_source_template(producer_template)
                    and output_name == "material"
                ):
                    expression = producer_name
                else:
                    expression = (
                        f"{producer_name}."
                        f"{_safe_identifier(output_name, 'value')}"
                    )
        if name in param:
            if expression is not None:
                _fail("candidate_invalid", "Composite target Handle 有多个 provider")
            resource_reference = _resource_ref_source(node, handle)
            expression = (
                resource_reference
                if resource_reference is not None
                else repr(param[name])
            )
        if expression is not None:
            parameters.append(f"{name}={expression}")
    result_name = _safe_identifier(str(node.get("name") or "result"), "result")
    emitter.anchored(
        str(node["uuid"]),
        f"{result_name} = {imported_name}({', '.join(parameters)})",
        indent=indent,
    )


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
                producer_template = templates.get(
                    str(producer.get("workflow_node_template_uuid")),
                    {},
                )
                producer_name = _safe_identifier(str(producer["name"]), "result")
                if (
                    _is_material_source_template(producer_template)
                    and source_name == "material"
                ):
                    expression = producer_name
                else:
                    expression = (
                        f"{producer_name}.{_safe_identifier(source_name, 'value')}"
                    )
        param = node.get("param") or {}
        if name in param:
            if expression is not None:
                _fail("candidate_invalid", "target Handle 有多个 provider")
            resource_reference = _resource_ref_source(node, handle)
            expression = (
                resource_reference
                if resource_reference is not None
                else repr(param[name])
            )
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


def _resource_ref_source(
    node: Mapping[str, Any],
    handle: Mapping[str, Any],
) -> str | None:
    if str(handle.get("type") or "").strip().lower() != "resourceslot":
        return None
    name = str(handle.get("data_key") or handle.get("handle_key") or "").strip()
    param = node.get("param")
    value = param.get(name) if isinstance(param, Mapping) else None
    if not isinstance(value, Mapping) or set(value) != {
        "uuid",
        "resource_template_uuid",
    }:
        return None
    resource_refs = (node.get("meta_data") or {}).get("unilab", {}).get(
        "resource_refs", {}
    )
    binding = resource_refs.get(str(handle.get("uuid") or "")) if isinstance(
        resource_refs, Mapping
    ) else None
    if not isinstance(binding, Mapping) or set(binding) != {"resource_id"}:
        return None
    resource_id = binding.get("resource_id")
    if not isinstance(resource_id, str) or not resource_id.strip() or (
        resource_id != resource_id.strip()
    ):
        return None
    try:
        validate_uuid(value["uuid"])
        validate_uuid(value["resource_template_uuid"])
    except (KeyError, TypeError, ValueError):
        return None
    return f"resource_ref({resource_id!r})"


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
                _catalog_wire_equal(
                    _sorted_catalog_read_entities(
                        left["node_templates"],
                        nullable_fields=_NODE_TEMPLATE_NULLABLE_READ_FIELDS,
                    ),
                    _sorted_catalog_read_entities(
                        right["node_templates"],
                        nullable_fields=_NODE_TEMPLATE_NULLABLE_READ_FIELDS,
                    ),
                ),
                _catalog_wire_equal(
                    _sorted_catalog_read_entities(
                        left["handle_templates"],
                        nullable_fields=_HANDLE_TEMPLATE_NULLABLE_READ_FIELDS,
                    ),
                    _sorted_catalog_read_entities(
                        right["handle_templates"],
                        nullable_fields=_HANDLE_TEMPLATE_NULLABLE_READ_FIELDS,
                    ),
                ),
            )
        )
    except (KeyError, TypeError, ValueError):
        return False


def _unexpanded_composite_invocations(graph: Mapping[str, Any]) -> set[str]:
    templates = {
        str(template.get("uuid")): template
        for template in graph.get("node_templates", [])
        if isinstance(template, Mapping)
    }
    nodes = [node for node in graph.get("nodes", []) if isinstance(node, Mapping)]
    parent_uuids = {
        str(node.get("parent_uuid"))
        for node in nodes
        if isinstance(node.get("parent_uuid"), str)
    }
    result: set[str] = set()
    for node in nodes:
        node_uuid = str(node.get("uuid") or "")
        template = templates.get(str(node.get("workflow_node_template_uuid")), {})
        if not node_uuid or not _is_published_workflow_template(template):
            continue
        meta_data = node.get("meta_data")
        unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
        if node_uuid in parent_uuids:
            if isinstance(unilab, Mapping) and isinstance(
                unilab.get("composite"), Mapping
            ):
                continue
            _fail(
                "candidate_invalid", "未展开 Composite boundary 不得携带 internal Nodes"
            )
        result.add(node_uuid)
    return result


def _project_unexpanded_composite_boundaries(
    graph: Any,
    invocation_uuids: set[str],
) -> dict[str, Any]:
    if not isinstance(graph, Mapping):
        _fail("round_trip_mismatch", "Composite round-trip graph 缺失")
    projected = _detached(graph)
    nodes = projected.get("nodes")
    if not isinstance(nodes, list):
        _fail("round_trip_mismatch", "Composite round-trip Nodes 缺失")
    descendants: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in nodes:
            node_uuid = str(node.get("uuid") or "")
            parent_uuid = node.get("parent_uuid")
            if (
                node_uuid not in invocation_uuids
                and node_uuid not in descendants
                and parent_uuid in invocation_uuids | descendants
            ):
                descendants.add(node_uuid)
                changed = True
    retained_nodes = []
    for node in nodes:
        node_uuid = str(node.get("uuid") or "")
        if node_uuid in descendants:
            continue
        if node_uuid in invocation_uuids:
            meta_data = node.get("meta_data")
            unilab = meta_data.get("unilab") if isinstance(meta_data, dict) else None
            if isinstance(unilab, dict):
                unilab.pop("composite", None)
        retained_nodes.append(node)
    projected["nodes"] = retained_nodes
    retained_uuids = {str(node["uuid"]) for node in retained_nodes}
    projected["edges"] = [
        edge
        for edge in projected.get("edges", [])
        if edge.get("source_node_uuid") in retained_uuids
        and edge.get("target_node_uuid") in retained_uuids
    ]
    template_uuids = {
        str(node.get("workflow_node_template_uuid")) for node in retained_nodes
    }
    projected["node_templates"] = [
        template
        for template in projected.get("node_templates", [])
        if str(template.get("uuid")) in template_uuids
    ]
    projected["handle_templates"] = [
        handle
        for handle in projected.get("handle_templates", [])
        if str(handle.get("workflow_node_template_uuid")) in template_uuids
    ]
    return projected


def _align_stale_published_projection(
    actual: dict[str, Any],
    expected: Mapping[str, Any],
    template_uuids: set[str],
) -> dict[str, Any]:
    """仅在 fixed-point 证明中对齐已认证的旧 Published server projection。"""

    if not template_uuids:
        return actual
    result = _detached(actual)
    expected_nodes = {
        str(node.get("uuid")): node
        for node in expected.get("nodes", [])
        if isinstance(node, Mapping)
    }
    for node in result.get("nodes", []):
        if str(node.get("workflow_node_template_uuid")) not in template_uuids:
            continue
        previous = expected_nodes.get(str(node.get("uuid")))
        if not isinstance(previous, Mapping):
            continue
        current_meta = node.get("meta_data")
        previous_meta = previous.get("meta_data")
        current_unilab = (
            current_meta.get("unilab") if isinstance(current_meta, dict) else None
        )
        previous_unilab = (
            previous_meta.get("unilab") if isinstance(previous_meta, Mapping) else None
        )
        if isinstance(current_unilab, dict) and isinstance(previous_unilab, Mapping):
            current_unilab["composite"] = _detached(
                previous_unilab.get("composite", {})
            )
    result["node_templates"] = [
        *[
            template
            for template in result.get("node_templates", [])
            if str(template.get("uuid")) not in template_uuids
        ],
        *[
            _detached(template)
            for template in expected.get("node_templates", [])
            if str(template.get("uuid")) in template_uuids
        ],
    ]
    result["handle_templates"] = [
        *[
            handle
            for handle in result.get("handle_templates", [])
            if str(handle.get("workflow_node_template_uuid")) not in template_uuids
        ],
        *[
            _detached(handle)
            for handle in expected.get("handle_templates", [])
            if str(handle.get("workflow_node_template_uuid")) in template_uuids
        ],
    ]
    return result


__all__ = ["WorkflowAuthoringEngine"]
