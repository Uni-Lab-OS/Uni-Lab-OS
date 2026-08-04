"""Published Workflow Contract 与 Composite authoring 的唯一深 Module。

R1 只发布 Applied child 的 typed contract。静态展开、compatibility 与 authoring
fixed-point 在后续 C1 rounds 继续放入本 Module，不把算法复制到 PackageCatalog、
TemplateCatalog、HTTP handler 或 frontend。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import rfc8785

from unilabos.workflow.authoring_identity import authoring_edge, expanded_node_uuid
from unilabos.workflow.catalog import (
    CatalogAuthority,
    NodeTemplateImport,
    TemplateCatalog,
    TemplateCatalogError,
    TemplateCatalogMismatch,
    TemplateCatalogSnapshot,
)
from unilabos.workflow.handle_projection import (
    resource_slot_schema,
    structural_ready_handle,
    workflow_handle_type,
)
from unilabos.workflow.models import WorkflowNodeWrite, validate_uuid
from unilabos.workflow.schema import WorkflowSchemaError, parse_input_contract
from unilabos.workflow.store import StoreNotFound, WorkflowStore
from unilabos.workflow.workflow_io import (
    ValidatedWorkflowIO,
    WorkflowIOValidationError,
    schema_is_assignable,
    validate_workflow_graph_io,
)


@dataclass(frozen=True, slots=True)
class PublishedWorkflowSource:
    """PackageCatalog 冻结的一条可移植 Workflow source identity。"""

    workflow_uuid: str
    definition_fqid: str
    module: str
    symbol: str
    package_catalog_digest: str
    definition_content_hash: str


class PublishedWorkflowResolver(Protocol):
    """absolute module/symbol 到 PackageCatalog Workflow identity 的 Interface。"""

    def resolve(self, module: str, symbol: str) -> PublishedWorkflowSource: ...


class CompositeCatalogMismatch(TemplateCatalogMismatch):
    """Published Workflow publication 无法与当前 Composite authority 对齐。"""

    code = "composite_catalog_mismatch"


@dataclass(frozen=True, slots=True)
class CompositeExpansion:
    """一次 Composite invocation 的完整 server-owned authoring 投影。"""

    invocation_node: Mapping[str, Any] | None
    nodes: tuple[Mapping[str, Any], ...]
    edges: tuple[Mapping[str, Any], ...]
    target_mappings: Mapping[str, tuple[Mapping[str, str], ...]]
    source_mappings: Mapping[str, Mapping[str, str]]
    structural_mappings: Mapping[str, tuple[Mapping[str, str], ...]]
    node_templates: tuple[Mapping[str, Any], ...]
    handle_templates: tuple[Mapping[str, Any], ...]
    contract_pin: Mapping[str, Any]
    effective_parent_input_contract: Mapping[str, Any]
    diagnostics: tuple[Mapping[str, str], ...]


class _CompositeFailure(RuntimeError):
    def __init__(self, code: str, path: str) -> None:
        super().__init__(path)
        self.code = code
        self.path = path


@dataclass(slots=True)
class _ExpandedChild:
    invocation_node: dict[str, Any]
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    target_mappings: dict[str, list[dict[str, str]]]
    source_mappings: dict[str, dict[str, str]]
    structural_mappings: dict[str, list[dict[str, str]]]
    contract_pin: dict[str, Any]
    effective_input_contract: dict[str, Any]
    paths: dict[tuple[str, ...], str]


class PublishedWorkflowCatalogPublisher:
    """把 frozen Registry/framework imports 与 eligible child 一次完整发布。"""

    def __init__(
        self,
        *,
        catalog: TemplateCatalog,
        authority: CatalogAuthority,
        store: WorkflowStore,
        sources: Sequence[PublishedWorkflowSource],
        base_templates: Sequence[NodeTemplateImport],
        host_node_resource_template_uuid: str | None,
        resource_template_identities: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(catalog, TemplateCatalog):
            raise TypeError("catalog 必须是 TemplateCatalog")
        if not isinstance(authority, CatalogAuthority):
            raise TypeError("authority 必须是 CatalogAuthority")
        if not isinstance(store, WorkflowStore):
            raise TypeError("store 必须是 WorkflowStore")
        frozen_sources = tuple(sources)
        if any(
            not isinstance(item, PublishedWorkflowSource) for item in frozen_sources
        ):
            raise TypeError("sources 必须只包含 PublishedWorkflowSource")
        frozen_templates = tuple(base_templates)
        if any(not isinstance(item, NodeTemplateImport) for item in frozen_templates):
            raise TypeError("base_templates 必须只包含 NodeTemplateImport")
        self._catalog = catalog
        self._authority = authority
        self._store = store
        self._sources = tuple(
            sorted(frozen_sources, key=lambda item: item.definition_fqid)
        )
        self._base_templates = frozen_templates
        self._host_node_resource_template_uuid = host_node_resource_template_uuid
        self._resource_template_identities = (
            dict(resource_template_identities)
            if resource_template_identities is not None
            else None
        )

    def publish(self) -> TemplateCatalogSnapshot:
        """在同一 Catalog guard 内读取 Applied facts 并执行唯一 complete replace。"""

        with self._store.catalog_guard():
            templates = list(self._base_templates)
            group_is_published = any(
                item.template.get("name") == "group"
                and item.template.get("class")
                == "unilabos.workflow.authoring:group"
                for item in templates
            )
            if (
                self._sources
                and self._host_node_resource_template_uuid is not None
                and not group_is_published
            ):
                templates.append(
                    _group_template(
                        self._host_node_resource_template_uuid,
                        authority_id=self._authority.authority_id,
                    )
                )
            for source in self._sources:
                try:
                    applied_snapshot = self._store.get_published_workflow_snapshot(
                        source.workflow_uuid
                    )
                except StoreNotFound:
                    continue
                projected = project_published_workflow_contract(
                    source=source,
                    applied_snapshot=applied_snapshot,
                    host_node_resource_template_uuid=(
                        self._host_node_resource_template_uuid
                    ),
                )
                if projected is not None:
                    templates.append(projected)
            return self._catalog.replace(
                self._authority,
                templates,
                resource_template_identities=self._resource_template_identities,
            )

    def invalidate(self) -> None:
        """使已提交 graph 后发布失败的 authority 立即 fail closed。"""

        self._catalog.invalidate(self._authority)

    @property
    def authority_id(self) -> str:
        """需要在 workflow mutation transaction 内失效的 Catalog authority。"""

        return self._authority.authority_id


class CompositeAuthoring:
    """Published Workflow invocation 的唯一静态展开 Interface。"""

    def __init__(
        self,
        *,
        store: WorkflowStore,
        catalog: TemplateCatalog,
        authority: CatalogAuthority,
        resolver: PublishedWorkflowResolver,
    ) -> None:
        if not isinstance(store, WorkflowStore):
            raise TypeError("store 必须是 WorkflowStore")
        if not isinstance(catalog, TemplateCatalog):
            raise TypeError("catalog 必须是 TemplateCatalog")
        if not isinstance(authority, CatalogAuthority):
            raise TypeError("authority 必须是 CatalogAuthority")
        if not callable(getattr(resolver, "resolve", None)):
            raise TypeError("resolver 必须实现 PublishedWorkflowResolver")
        self._store = store
        self._catalog = catalog
        self._authority = authority
        self._resolver = resolver

    def compile_invocation(
        self,
        *,
        parent_workflow_uuid: str,
        invocation_uuid: str,
        module: str,
        symbol: str,
        keyword_arguments: Mapping[str, object],
        parent_input_contract: Mapping[str, object] | None = None,
    ) -> CompositeExpansion:
        """只读编译一个 child invocation；任何失败都返回零写诊断。"""

        try:
            parent_uuid = _canonical_uuid(
                parent_workflow_uuid,
                "composite_boundary_mapping_invalid",
                "/parent_workflow_uuid",
            )
            invocation = _canonical_uuid(
                invocation_uuid,
                "composite_boundary_mapping_invalid",
                "/invocation_uuid",
            )
            if not isinstance(module, str) or not module or not isinstance(symbol, str):
                raise _CompositeFailure(
                    "composite_child_not_found",
                    "/source",
                )
            if not isinstance(keyword_arguments, Mapping) or any(
                not isinstance(key, str) for key in keyword_arguments
            ):
                raise _CompositeFailure(
                    "composite_boundary_mapping_invalid",
                    "/keyword_arguments",
                )
            try:
                source = self._resolver.resolve(module, symbol)
            except (KeyError, LookupError):
                raise _CompositeFailure(
                    "composite_child_not_found",
                    "/source",
                ) from None
            if not isinstance(source, PublishedWorkflowSource):
                raise _CompositeFailure(
                    "composite_catalog_mismatch",
                    "/source",
                )
            with self._catalog.snapshot(self._authority) as catalog_snapshot:
                parent_graph = self._store.get_graph(parent_uuid)
                try:
                    effective_parent_contract = (
                        _parent_input_contract(parent_graph)
                        if parent_input_contract is None
                        else parse_input_contract(parent_input_contract).to_dict()
                    )
                except (TypeError, ValueError, WorkflowSchemaError):
                    raise _CompositeFailure(
                        "composite_boundary_mapping_invalid",
                        "/parent/io_contract",
                    ) from None
                expanded = self._expand(
                    source=source,
                    invocation_uuid=invocation,
                    parent_uuid=None,
                    keyword_arguments=dict(keyword_arguments),
                    catalog=catalog_snapshot,
                    root_parent_workflow_uuid=parent_uuid,
                    workflow_stack=(parent_uuid,),
                    base_node=None,
                )
                effective = _effective_parent_input_contract(
                    effective_parent_contract,
                    expanded.effective_input_contract,
                    keyword_arguments,
                )
                _reject_private_providers(keyword_arguments, expanded)
                node_templates, handle_templates = _referenced_templates(
                    catalog_snapshot,
                    expanded.invocation_node,
                    expanded.nodes,
                )
                return CompositeExpansion(
                    invocation_node=expanded.invocation_node,
                    nodes=tuple(expanded.nodes),
                    edges=tuple(expanded.edges),
                    target_mappings={
                        key: tuple(value)
                        for key, value in expanded.target_mappings.items()
                    },
                    source_mappings=expanded.source_mappings,
                    structural_mappings={
                        key: tuple(value)
                        for key, value in expanded.structural_mappings.items()
                    },
                    node_templates=tuple(node_templates),
                    handle_templates=tuple(handle_templates),
                    contract_pin=expanded.contract_pin,
                    effective_parent_input_contract=effective,
                    diagnostics=(),
                )
        except _CompositeFailure as exc:
            return _failed_expansion(exc.code, exc.path)
        except TemplateCatalogError as exc:
            return _failed_expansion(
                getattr(exc, "code", "composite_catalog_mismatch"),
                getattr(exc, "path", "/catalog"),
            )
        except StoreNotFound:
            return _failed_expansion(
                "composite_child_not_found",
                "/workflow",
            )

    def _expand(
        self,
        *,
        source: PublishedWorkflowSource,
        invocation_uuid: str,
        parent_uuid: str | None,
        keyword_arguments: Mapping[str, object],
        catalog: TemplateCatalogSnapshot,
        root_parent_workflow_uuid: str,
        workflow_stack: tuple[str, ...],
        base_node: Mapping[str, Any] | None,
    ) -> _ExpandedChild:
        if source.workflow_uuid in workflow_stack:
            raise _CompositeFailure(
                "composite_recursive_reference",
                "/composite/child_workflow_uuid",
            )
        try:
            applied = self._store.get_published_workflow_snapshot(source.workflow_uuid)
        except StoreNotFound:
            raise _CompositeFailure(
                "composite_child_not_found",
                "/composite/child_workflow_uuid",
            ) from None
        workflow = _mapping(applied.get("workflow"), "/child/workflow")
        revision = workflow.get("revision")
        applied_source = applied.get("applied_source")
        if not isinstance(applied_source, Mapping) or (
            applied_source.get("workflow_revision") != revision
        ):
            raise _CompositeFailure(
                "composite_child_unapplied",
                "/child/applied_source",
            )
        if workflow.get("uuid") != source.workflow_uuid:
            raise _CompositeFailure(
                "composite_catalog_mismatch",
                "/child/workflow/uuid",
            )
        template, boundary_handles, pin = _published_template(
            catalog,
            source,
            revision=revision,
            applied_source_hash=applied_source.get("source_hash"),
        )
        try:
            validated_io = _validate_composite_graph_io(applied, catalog=catalog)
        except (WorkflowIOValidationError, WorkflowSchemaError, TypeError, ValueError):
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                "/child/io_contract",
            ) from None

        raw_nodes = [
            _mapping(item, "/child/nodes")
            for item in _sequence(applied.get("nodes"), "/child/nodes")
        ]
        raw_edges = [
            _mapping(item, "/child/edges")
            for item in _sequence(applied.get("edges"), "/child/edges")
        ]
        node_by_uuid = {
            _canonical_uuid(
                node.get("uuid"),
                "composite_boundary_mapping_invalid",
                "/child/nodes/uuid",
            ): node
            for node in raw_nodes
        }
        if len(node_by_uuid) != len(raw_nodes):
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                "/child/nodes/uuid",
            )
        template_by_node = {
            node_uuid: _node_template(catalog, node)
            for node_uuid, node in node_by_uuid.items()
        }
        composite_nodes = {
            node_uuid
            for node_uuid, node_template in template_by_node.items()
            if _is_published_workflow_template(node_template)
        }
        parent_by_node = {
            node_uuid: node.get("parent_uuid")
            for node_uuid, node in node_by_uuid.items()
        }
        _validate_parent_tree(parent_by_node)
        hidden_by_composite = {
            node_uuid
            for node_uuid in node_by_uuid
            if _composite_ancestor(
                node_uuid,
                parent_by_node=parent_by_node,
                composite_nodes=composite_nodes,
            )
            is not None
        }
        mapped_visible = {
            node_uuid: expanded_node_uuid(invocation_uuid, node_uuid)
            for node_uuid in node_by_uuid
            if node_uuid not in hidden_by_composite
        }
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        paths: dict[tuple[str, ...], str] = {}
        endpoint_aliases = dict(mapped_visible)
        next_stack = (*workflow_stack, source.workflow_uuid)
        effective_input_contract = validated_io.input_contract.to_dict()

        for node_uuid in sorted(mapped_visible):
            raw_node = node_by_uuid[node_uuid]
            mapped_uuid = mapped_visible[node_uuid]
            raw_parent = raw_node.get("parent_uuid")
            mapped_parent = (
                invocation_uuid
                if raw_parent is None
                else mapped_visible.get(str(raw_parent))
            )
            if mapped_parent is None:
                raise _CompositeFailure(
                    "composite_boundary_mapping_invalid",
                    "/child/nodes/parent_uuid",
                )
            if node_uuid in composite_nodes:
                nested_source = _source_from_template(
                    self._resolver,
                    template_by_node[node_uuid],
                )
                nested_arguments = _node_keyword_arguments(
                    raw_node,
                    template_by_node[node_uuid],
                    catalog,
                )
                nested = self._expand(
                    source=nested_source,
                    invocation_uuid=mapped_uuid,
                    parent_uuid=mapped_parent,
                    keyword_arguments=nested_arguments,
                    catalog=catalog,
                    root_parent_workflow_uuid=root_parent_workflow_uuid,
                    workflow_stack=next_stack,
                    base_node=raw_node,
                )
                _assert_pinned_nested(raw_node, nested.contract_pin)
                effective_input_contract = _effective_parent_input_contract(
                    effective_input_contract,
                    nested.effective_input_contract,
                    nested_arguments,
                )
                nodes.append(nested.invocation_node)
                nodes.extend(nested.nodes)
                edges.extend(nested.edges)
                paths[(node_uuid,)] = mapped_uuid
                for nested_path, final_uuid in nested.paths.items():
                    paths[(node_uuid, *nested_path)] = final_uuid
                    endpoint_aliases[_derive_path(node_uuid, nested_path)] = final_uuid
            else:
                nodes.append(
                    _copy_node(
                        raw_node,
                        uuid=mapped_uuid,
                        parent_uuid=mapped_parent,
                    )
                )
                paths[(node_uuid,)] = mapped_uuid

        for raw_edge in raw_edges:
            source_uuid = endpoint_aliases.get(str(raw_edge.get("source_node_uuid")))
            target_uuid = endpoint_aliases.get(str(raw_edge.get("target_node_uuid")))
            if source_uuid is None or target_uuid is None:
                raise _CompositeFailure(
                    "composite_boundary_mapping_invalid",
                    "/child/edges/node",
                )
            edges.append(
                authoring_edge(
                    root_parent_workflow_uuid,
                    source_uuid,
                    target_uuid,
                    _canonical_uuid(
                        raw_edge.get("source_handle_uuid"),
                        "composite_boundary_mapping_invalid",
                        "/child/edges/source_handle_uuid",
                    ),
                    _canonical_uuid(
                        raw_edge.get("target_handle_uuid"),
                        "composite_boundary_mapping_invalid",
                        "/child/edges/target_handle_uuid",
                    ),
                    description=raw_edge.get("description"),
                    meta_data=_edge_meta_data(raw_edge),
                )
            )
        edges = _unique_edges(edges)
        _assert_acyclic(nodes, edges)

        target_mappings = _target_mappings(
            validated_io.input_contract.to_dict(),
            validated_io.input_bindings,
            boundary_handles,
            endpoint_aliases,
        )
        source_mappings = _source_mappings(
            validated_io.output_contract.to_dict(),
            validated_io.output_bindings,
            boundary_handles,
            endpoint_aliases,
        )
        structural = _structural_mappings(
            nodes,
            edges,
            catalog,
        )
        invocation_node = _invocation_node(
            source=source,
            template_uuid=str(template["uuid"]),
            invocation_uuid=invocation_uuid,
            parent_uuid=parent_uuid,
            keyword_arguments=keyword_arguments,
            boundary_handles=boundary_handles,
            base_node=base_node,
            pin=pin,
            contract_compatibility=_compatibility_projection(
                template,
                boundary_handles,
            ),
            target_mappings=target_mappings,
            source_mappings=source_mappings,
            structural_mappings=structural,
        )
        return _ExpandedChild(
            invocation_node=invocation_node,
            nodes=nodes,
            edges=edges,
            target_mappings=target_mappings,
            source_mappings=source_mappings,
            structural_mappings=structural,
            contract_pin=pin,
            effective_input_contract=effective_input_contract,
            paths=paths,
        )


def _failed_expansion(code: str, path: str) -> CompositeExpansion:
    return CompositeExpansion(
        invocation_node=None,
        nodes=(),
        edges=(),
        target_mappings={},
        source_mappings={},
        structural_mappings={},
        node_templates=(),
        handle_templates=(),
        contract_pin={},
        effective_parent_input_contract={},
        diagnostics=(
            {
                "code": code,
                "path": path,
                "severity": "error",
                "message": "Composite authoring contract validation failed",
            },
        ),
    )


def _canonical_uuid(value: Any, code: str, path: str) -> str:
    try:
        canonical = validate_uuid(value)
    except (TypeError, ValueError):
        raise _CompositeFailure(code, path) from None
    if canonical != value:
        raise _CompositeFailure(code, path)
    return canonical


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _CompositeFailure("composite_boundary_mapping_invalid", path)
    return _plain(value)


def _published_template(
    catalog: TemplateCatalogSnapshot,
    source: PublishedWorkflowSource,
    *,
    revision: Any,
    applied_source_hash: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    matches = [
        _plain(template)
        for template in catalog.node_templates
        if template.get("name") == f"workflow:{source.workflow_uuid}"
    ]
    if len(matches) != 1:
        raise _CompositeFailure(
            "composite_catalog_mismatch",
            "/catalog/published_workflow",
        )
    template = matches[0]
    if not _is_published_workflow_template(template):
        raise _CompositeFailure(
            "composite_catalog_mismatch",
            "/catalog/published_workflow/type",
        )
    schema = _mapping(template.get("schema"), "/catalog/workflow/schema")
    extension = _mapping(
        schema.get("x-unilabos-workflow-contract"),
        "/catalog/workflow/contract",
    )
    if (
        extension.get("version") != 1
        or extension.get("compatibility_version") != 1
        or extension.get("workflow_uuid") != source.workflow_uuid
        or extension.get("workflow_revision") != revision
        or extension.get("applied_source_hash") != applied_source_hash
        or not isinstance(extension.get("composition_allow_transparent"), bool)
    ):
        raise _CompositeFailure(
            "composite_catalog_mismatch",
            "/catalog/workflow/contract",
        )
    contract_digest = extension.get("contract_digest")
    try:
        _sha256(contract_digest, "/catalog/workflow/contract_digest")
        _sha256(applied_source_hash, "/child/applied_source/source_hash")
    except CompositeCatalogMismatch:
        raise _CompositeFailure(
            "composite_catalog_mismatch",
            "/catalog/workflow/contract",
        ) from None
    meta_data = _mapping(template.get("meta_data"), "/catalog/workflow/meta_data")
    unilab = _mapping(meta_data.get("unilab"), "/catalog/workflow/meta_data/unilab")
    provenance = _mapping(
        unilab.get("workflow_source"),
        "/catalog/workflow/source",
    )
    expected_source = {
        "kind": "package",
        "definition_fqid": source.definition_fqid,
        "module": source.module,
        "symbol": source.symbol,
        "package_catalog_digest": source.package_catalog_digest,
        "definition_content_hash": source.definition_content_hash,
    }
    if provenance != expected_source or unilab.get("framework_owner_only") is not True:
        raise _CompositeFailure(
            "composite_catalog_mismatch",
            "/catalog/workflow/source",
        )
    template_uuid = str(template.get("uuid"))
    handles = [
        _plain(handle)
        for handle in catalog.handle_templates
        if handle.get("workflow_node_template_uuid") == template_uuid
    ]
    if not handles:
        raise _CompositeFailure(
            "composite_catalog_mismatch",
            "/catalog/workflow/handles",
        )
    pin = {
        "child_workflow_uuid": source.workflow_uuid,
        "child_workflow_revision": revision,
        "child_applied_source_hash": applied_source_hash,
        "contract_digest": contract_digest,
        "composition_allow_transparent": extension["composition_allow_transparent"],
    }
    return template, handles, pin


def _node_template(
    catalog: TemplateCatalogSnapshot,
    node: Mapping[str, Any],
) -> dict[str, Any]:
    template_uuid = node.get("workflow_node_template_uuid")
    if not isinstance(template_uuid, str):
        raise _CompositeFailure(
            "composite_boundary_mapping_invalid",
            "/child/nodes/workflow_node_template_uuid",
        )
    try:
        return _plain(catalog.require_node(template_uuid))
    except TemplateCatalogMismatch:
        raise _CompositeFailure(
            "composite_catalog_mismatch",
            "/catalog/node_templates/uuid",
        ) from None


def _is_published_workflow_template(template: Mapping[str, Any]) -> bool:
    schema = template.get("schema")
    return (
        template.get("type") == "workflow"
        and template.get("node_type") == "workflow"
        and isinstance(schema, Mapping)
        and isinstance(schema.get("x-unilabos-workflow-contract"), Mapping)
    )


_WORKFLOW_CONTRACT_FIELDS = {
    "version",
    "compatibility_version",
    "workflow_uuid",
    "workflow_revision",
    "applied_source_hash",
    "contract_digest",
    "composition_allow_transparent",
    "input_order",
    "output_order",
}
_WORKFLOW_SOURCE_FIELDS = {
    "kind",
    "definition_fqid",
    "module",
    "symbol",
    "package_catalog_digest",
    "definition_content_hash",
}
_WORKFLOW_HANDLE_FIELDS = {
    "uuid",
    "workflow_node_template_uuid",
    "handle_key",
    "io_type",
    "display_name",
    "description",
    "type",
    "required",
    "data_source",
    "data_key",
    "meta_data",
    "create_time",
    "update_time",
}
_WORKFLOW_BUSINESS_HANDLE_METADATA_FIELDS = {
    "value_schema",
    "editor_control",
    "allowed_resource_template_uuids",
    "implicit_passthrough",
}
_WORKFLOW_READY_HANDLE_METADATA_FIELDS = {
    *_WORKFLOW_BUSINESS_HANDLE_METADATA_FIELDS,
    "structural_role",
}


def _is_framework_published_workflow_template(
    template: Mapping[str, Any],
    handles: Sequence[Mapping[str, Any]],
    *,
    host_resource_template_uuid: str | None = None,
) -> bool:
    if not _is_published_workflow_template(template):
        return False
    template_uuid = template.get("uuid")
    resource_template_uuid = template.get("resource_template_uuid")
    if not _is_canonical_uuid(template_uuid) or not _is_canonical_uuid(
        resource_template_uuid
    ):
        return False
    if (
        host_resource_template_uuid is not None
        and resource_template_uuid != host_resource_template_uuid
    ):
        return False
    schema = template.get("schema")
    if not isinstance(schema, Mapping):
        return False
    extension = schema.get("x-unilabos-workflow-contract")
    if (
        not isinstance(extension, Mapping)
        or set(extension) != _WORKFLOW_CONTRACT_FIELDS
    ):
        return False
    workflow_uuid = extension.get("workflow_uuid")
    revision = extension.get("workflow_revision")
    input_order = _closed_string_order(extension.get("input_order"))
    output_order = _closed_string_order(extension.get("output_order"))
    if (
        extension.get("version") != 1
        or extension.get("compatibility_version") != 1
        or not _is_canonical_uuid(workflow_uuid)
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or not _is_sha256_string(extension.get("applied_source_hash"))
        or not _is_sha256_string(extension.get("contract_digest"))
        or not isinstance(extension.get("composition_allow_transparent"), bool)
        or input_order is None
        or output_order is None
        or template.get("name") != f"workflow:{workflow_uuid}"
    ):
        return False
    meta_data = template.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    source = unilab.get("workflow_source") if isinstance(unilab, Mapping) else None
    if (
        not isinstance(unilab, Mapping)
        or unilab.get("framework_owner_only") is not True
        or not isinstance(source, Mapping)
        or set(source) != _WORKFLOW_SOURCE_FIELDS
        or source.get("kind") != "package"
        or not _is_dotted_identifier(source.get("definition_fqid"))
        or not _is_dotted_identifier(source.get("module"))
        or not isinstance(source.get("symbol"), str)
        or not source["symbol"].isidentifier()
        or not _is_sha256_string(source.get("package_catalog_digest"))
        or not _is_sha256_string(source.get("definition_content_hash"))
        or template.get("class") != f"{source['module']}:{source['symbol']}"
        or not _workflow_schema_matches_orders(schema, input_order, output_order)
    ):
        return False
    return _published_workflow_handles_match(
        str(template_uuid),
        handles,
        schema=schema,
        input_order=input_order,
        output_order=output_order,
    ) and _published_workflow_contract_digest_matches(
        template,
        handles,
        schema=schema,
        input_order=input_order,
        output_order=output_order,
    )


def _is_canonical_uuid(value: Any) -> bool:
    try:
        return validate_uuid(value) == value
    except (TypeError, ValueError):
        return False


def _is_sha256_string(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _is_dotted_identifier(value: Any) -> bool:
    return (
        isinstance(value, str)
        and not value.startswith(".")
        and all(part.isidentifier() for part in value.split("."))
    )


def _closed_string_order(value: Any) -> list[str] | None:
    if (
        not isinstance(value, (list, tuple))
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
    ):
        return None
    return list(value)


def _workflow_schema_matches_orders(
    schema: Mapping[str, Any],
    input_order: Sequence[str],
    output_order: Sequence[str],
) -> bool:
    if (
        set(schema)
        != {
            "type",
            "additionalProperties",
            "properties",
            "required",
            "x-unilabos-workflow-contract",
        }
        or schema.get("type") != "object"
        or schema.get("additionalProperties") is not False
    ):
        return False
    properties = schema.get("properties")
    required = schema.get("required")
    if (
        not isinstance(properties, Mapping)
        or set(properties) != {"goal", "result"}
        or not isinstance(required, (list, tuple))
        or list(required) != ["goal", "result"]
    ):
        return False
    goal = properties.get("goal")
    result = properties.get("result")
    if not _workflow_envelope_matches(goal, input_order, require_all=False):
        return False
    return _workflow_envelope_matches(result, output_order, require_all=True)


def _workflow_envelope_matches(
    envelope: Any,
    order: Sequence[str],
    *,
    require_all: bool,
) -> bool:
    if (
        not isinstance(envelope, Mapping)
        or set(envelope) != {"type", "additionalProperties", "properties", "required"}
        or envelope.get("type") != "object"
        or envelope.get("additionalProperties") is not False
    ):
        return False
    properties = envelope.get("properties")
    required = envelope.get("required")
    if (
        not isinstance(properties, Mapping)
        or set(properties) != set(order)
        or not isinstance(required, (list, tuple))
        or any(item not in order for item in required)
        or len(set(required)) != len(required)
    ):
        return False
    return not require_all or list(required) == list(order)


def _published_workflow_handles_match(
    template_uuid: str,
    handles: Sequence[Mapping[str, Any]],
    *,
    schema: Mapping[str, Any],
    input_order: Sequence[str],
    output_order: Sequence[str],
) -> bool:
    owned = [
        handle
        for handle in handles
        if handle.get("workflow_node_template_uuid") == template_uuid
    ]
    if any(
        not _is_canonical_uuid(handle.get("uuid"))
        or not set(handle).issubset(_WORKFLOW_HANDLE_FIELDS)
        or not _WORKFLOW_HANDLE_FIELDS.difference(
            {"description", "create_time", "update_time"}
        ).issubset(handle)
        or not isinstance(handle.get("meta_data"), Mapping)
        or set(handle["meta_data"]) != {"unilab"}
        for handle in owned
    ):
        return False
    business: dict[tuple[str, str], Mapping[str, Any]] = {}
    ready: dict[str, Mapping[str, Any]] = {}
    for handle in owned:
        io_type = handle.get("io_type")
        meta_data = handle.get("meta_data")
        unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
        if io_type not in {"target", "source"} or not isinstance(unilab, Mapping):
            return False
        if unilab.get("structural_role") == "ready":
            if io_type in ready or not _ready_handle_shape_matches(handle, unilab):
                return False
            ready[str(io_type)] = handle
            continue
        data_key = handle.get("data_key")
        if not isinstance(data_key, str) or (str(io_type), data_key) in business:
            return False
        business[(str(io_type), data_key)] = handle
    if set(ready) != {"target", "source"} or set(business) != {
        *(("target", name) for name in input_order),
        *(("source", name) for name in output_order),
    }:
        return False
    properties = schema["properties"]
    for io_type, order, envelope_name in (
        ("target", input_order, "goal"),
        ("source", output_order, "result"),
    ):
        schemas = properties[envelope_name]["properties"]
        for name in order:
            handle = business[(io_type, name)]
            unilab = handle["meta_data"]["unilab"]
            if (
                handle.get("handle_key") != name
                or handle.get("data_key") != name
                or handle.get("data_source")
                != ("goal" if io_type == "target" else "result")
                or not _business_handle_shape_matches(
                    handle,
                    unilab,
                    schema=schemas[name],
                    required=(
                        name in properties[envelope_name]["required"]
                        if io_type == "target"
                        else False
                    ),
                    io_type=io_type,
                )
            ):
                return False
    return True


def _business_handle_shape_matches(
    handle: Mapping[str, Any],
    unilab: Mapping[str, Any],
    *,
    schema: Mapping[str, Any],
    required: bool,
    io_type: str,
) -> bool:
    # TemplateCatalog 在只读快照中把 JSON 数组冻结为 tuple；共享投影 helper
    # 接收 JSON 外形，因此先恢复权威 schema 再派生 Handle 展示字段。
    plain_schema = _plain(schema)
    # Goal property schemas additionally carry the invocation default, whereas
    # Handle value_schema describes only the accepted value shape.
    handle_value_schema = dict(plain_schema)
    handle_value_schema.pop("default", None)
    slot_schema = resource_slot_schema(plain_schema)
    expected_allowlist = (
        _plain(slot_schema.get("allowed_resource_template_uuids"))
        if slot_schema is not None
        else None
    )
    expected_control = (
        "material_port" if slot_schema is not None else "variable_selector"
    )
    implicit = unilab.get("implicit_passthrough")
    return (
        set(unilab) == _WORKFLOW_BUSINESS_HANDLE_METADATA_FIELDS
        and handle.get("type") == workflow_handle_type(plain_schema)
        and handle.get("required") is required
        and _plain(unilab.get("value_schema")) == handle_value_schema
        and unilab.get("editor_control") == expected_control
        and _plain(unilab.get("allowed_resource_template_uuids")) == expected_allowlist
        and isinstance(implicit, bool)
        and (io_type == "source" or implicit is False)
    )


def _ready_handle_shape_matches(
    handle: Mapping[str, Any],
    unilab: Mapping[str, Any],
) -> bool:
    return (
        set(unilab) == _WORKFLOW_READY_HANDLE_METADATA_FIELDS
        and handle.get("handle_key") == "ready"
        and handle.get("data_key") == "ready"
        and handle.get("data_source") == "dependency"
        and handle.get("display_name") == "Ready"
        and handle.get("description") == "Lexical source-order dependency"
        and handle.get("type") == "boolean"
        and handle.get("required") is False
        and unilab.get("value_schema") == {"type": "boolean"}
        and unilab.get("editor_control") == "variable_selector"
        and unilab.get("allowed_resource_template_uuids") is None
        and unilab.get("implicit_passthrough") is False
    )


def _published_workflow_contract_digest_matches(
    template: Mapping[str, Any],
    handles: Sequence[Mapping[str, Any]],
    *,
    schema: Mapping[str, Any],
    input_order: Sequence[str],
    output_order: Sequence[str],
) -> bool:
    goal = template.get("goal")
    goal_default = template.get("goal_default")
    result = template.get("result")
    if (
        not isinstance(goal, Mapping)
        or _plain(goal) != {name: name for name in input_order}
        or not isinstance(goal_default, Mapping)
        or any(name not in input_order for name in goal_default)
        or not isinstance(result, Mapping)
        or _plain(result) != {name: name for name in output_order}
    ):
        return False
    properties = schema["properties"]
    goal_schema = properties["goal"]
    result_schema = properties["result"]
    required = set(goal_schema["required"])
    inputs: list[dict[str, Any]] = []
    for name in input_order:
        input_schema = _plain(goal_schema["properties"][name])
        schema_has_default = "default" in input_schema
        contract_has_default = name in goal_default
        if schema_has_default != contract_has_default or (
            schema_has_default
            and _plain(input_schema["default"]) != _plain(goal_default[name])
        ):
            return False
        input_schema.pop("default", None)
        descriptor: dict[str, Any] = {
            "name": name,
            "schema": input_schema,
            "required": name in required,
        }
        if name in goal_default:
            descriptor["default"] = _plain(goal_default[name])
        inputs.append(descriptor)
    source_by_name = {
        str(handle.get("data_key")): handle
        for handle in handles
        if handle.get("workflow_node_template_uuid") == template.get("uuid")
        and handle.get("io_type") == "source"
        and isinstance(handle.get("meta_data"), Mapping)
        and isinstance(handle["meta_data"].get("unilab"), Mapping)
        and handle["meta_data"]["unilab"].get("structural_role") is None
    }
    if set(source_by_name) != set(output_order):
        return False
    outputs = [
        {
            "name": name,
            "schema": _plain(result_schema["properties"][name]),
            "implicit": source_by_name[name]["meta_data"]["unilab"][
                "implicit_passthrough"
            ],
        }
        for name in output_order
    ]
    extension = schema["x-unilabos-workflow-contract"]
    return extension.get("contract_digest") == _contract_digest(
        inputs=inputs,
        outputs=outputs,
        composition_allow_transparent=extension["composition_allow_transparent"],
    )


def _source_from_template(
    resolver: PublishedWorkflowResolver,
    template: Mapping[str, Any],
) -> PublishedWorkflowSource:
    try:
        provenance = template["meta_data"]["unilab"]["workflow_source"]
        module = provenance["module"]
        symbol = provenance["symbol"]
        source = resolver.resolve(module, symbol)
    except (KeyError, LookupError, TypeError):
        raise _CompositeFailure(
            "composite_child_not_found",
            "/nested/source",
        ) from None
    if not isinstance(source, PublishedWorkflowSource):
        raise _CompositeFailure(
            "composite_catalog_mismatch",
            "/nested/source",
        )
    return source


def _validate_parent_tree(parent_by_node: Mapping[str, Any]) -> None:
    for node_uuid in parent_by_node:
        seen = {node_uuid}
        parent = parent_by_node[node_uuid]
        while parent is not None:
            if not isinstance(parent, str) or parent not in parent_by_node:
                raise _CompositeFailure(
                    "composite_boundary_mapping_invalid",
                    "/child/nodes/parent_uuid",
                )
            if parent in seen:
                raise _CompositeFailure(
                    "composite_recursive_reference",
                    "/child/nodes/parent_uuid",
                )
            seen.add(parent)
            parent = parent_by_node[parent]


def _composite_ancestor(
    node_uuid: str,
    *,
    parent_by_node: Mapping[str, Any],
    composite_nodes: set[str],
) -> str | None:
    parent = parent_by_node[node_uuid]
    while isinstance(parent, str):
        if parent in composite_nodes:
            return parent
        parent = parent_by_node[parent]
    return None


def _node_param(node: Mapping[str, Any]) -> dict[str, Any]:
    value = node.get("param", {})
    if not isinstance(value, Mapping):
        raise _CompositeFailure(
            "composite_boundary_mapping_invalid",
            "/child/nodes/param",
        )
    return _plain(value)


def _node_keyword_arguments(
    node: Mapping[str, Any],
    template: Mapping[str, Any],
    catalog: TemplateCatalogSnapshot,
) -> dict[str, Any]:
    """恢复 nested invocation 的 literal 与真实 I1 input binding。"""

    arguments = _node_param(node)
    template_uuid = template.get("uuid")
    if not isinstance(template_uuid, str):
        raise _CompositeFailure(
            "composite_catalog_mismatch",
            "/catalog/node_templates/uuid",
        )
    targets = {
        str(handle["uuid"]): _plain(handle)
        for handle in catalog.handle_templates
        if handle.get("workflow_node_template_uuid") == template_uuid
        and handle.get("io_type") == "target"
        and _structural_role(handle) is None
    }
    meta_data = node.get("meta_data", {})
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    raw_bindings = (
        unilab.get("input_bindings", {}) if isinstance(unilab, Mapping) else {}
    )
    if not isinstance(raw_bindings, Mapping):
        raise _CompositeFailure(
            "composite_boundary_mapping_invalid",
            "/child/nodes/meta_data/unilab/input_bindings",
        )
    for handle_uuid, raw_binding in raw_bindings.items():
        handle = targets.get(handle_uuid) if isinstance(handle_uuid, str) else None
        if (
            handle is None
            or not isinstance(raw_binding, Mapping)
            or set(raw_binding) != {"parameter"}
            or not isinstance(raw_binding.get("parameter"), str)
            or not isinstance(handle.get("data_key"), str)
        ):
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                "/child/nodes/meta_data/unilab/input_bindings",
            )
        name = str(handle["data_key"])
        if name in arguments:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                f"/child/nodes/param/{name}",
            )
        arguments[name] = {
            "kind": "workflow_input",
            "parameter": str(raw_binding["parameter"]),
        }
    return arguments


def _edge_meta_data(edge: Mapping[str, Any]) -> dict[str, Any]:
    value = edge.get("meta_data", {})
    if not isinstance(value, Mapping):
        raise _CompositeFailure(
            "composite_boundary_mapping_invalid",
            "/child/edges/meta_data",
        )
    return _plain(value)


def _copy_node(
    node: Mapping[str, Any],
    *,
    uuid: str,
    parent_uuid: str,
) -> dict[str, Any]:
    base = {
        key: _plain(value)
        for key, value in node.items()
        if key in WorkflowNodeWrite.model_fields
    }
    base.update({"uuid": uuid, "parent_uuid": parent_uuid})
    try:
        result = WorkflowNodeWrite.model_validate(base).model_dump(exclude_none=True)
        result["parent_uuid"] = parent_uuid
        return result
    except (TypeError, ValueError):
        raise _CompositeFailure(
            "composite_boundary_mapping_invalid",
            "/child/nodes",
        ) from None


def _assert_pinned_nested(
    node: Mapping[str, Any],
    current_pin: Mapping[str, Any],
) -> None:
    try:
        stored = node["meta_data"]["unilab"]["composite"]
    except (KeyError, TypeError):
        raise _CompositeFailure(
            "composite_boundary_mapping_invalid",
            "/child/nodes/composite",
        ) from None
    if not isinstance(stored, Mapping):
        raise _CompositeFailure(
            "composite_boundary_mapping_invalid",
            "/child/nodes/composite",
        )
    if any(stored.get(key) != value for key, value in current_pin.items()):
        raise _CompositeFailure(
            "composite_contract_stale",
            "/child/nodes/composite/pin",
        )


def _derive_path(namespace_uuid: str, path: Sequence[str]) -> str:
    result = namespace_uuid
    for child_uuid in path:
        result = expanded_node_uuid(result, child_uuid)
    return result


def _unique_edges(edges: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    by_uuid: dict[str, dict[str, Any]] = {}
    for edge in edges:
        edge_uuid = str(edge["uuid"])
        existing = by_uuid.get(edge_uuid)
        if existing is not None and existing != edge:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                "/child/edges/uuid",
            )
        by_uuid[edge_uuid] = edge
    return [by_uuid[key] for key in sorted(by_uuid)]


def _assert_acyclic(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
) -> None:
    node_uuids = {str(node["uuid"]) for node in nodes}
    incoming = {node_uuid: 0 for node_uuid in node_uuids}
    outgoing = {node_uuid: [] for node_uuid in node_uuids}
    for edge in edges:
        source = str(edge["source_node_uuid"])
        target = str(edge["target_node_uuid"])
        if source not in node_uuids or target not in node_uuids:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                "/child/edges/node",
            )
        outgoing[source].append(target)
        incoming[target] += 1
    ready = sorted(node_uuid for node_uuid, degree in incoming.items() if degree == 0)
    visited = 0
    while ready:
        current = ready.pop(0)
        visited += 1
        for target in sorted(outgoing[current]):
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
                ready.sort()
    if visited != len(node_uuids):
        raise _CompositeFailure(
            "composite_recursive_reference",
            "/child/edges/cycle",
        )


def _business_handle(
    handles: Sequence[Mapping[str, Any]],
    *,
    name: str,
    io_type: str,
) -> dict[str, Any]:
    matches = []
    for handle in handles:
        meta_data = handle.get("meta_data")
        unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
        if (
            handle.get("io_type") == io_type
            and handle.get("data_key") == name
            and isinstance(unilab, Mapping)
            and unilab.get("structural_role") is None
        ):
            matches.append(_plain(handle))
    if len(matches) != 1:
        raise _CompositeFailure(
            "composite_boundary_mapping_invalid",
            "/composite/boundary_handle",
        )
    return matches[0]


def _target_mappings(
    input_contract: Mapping[str, Any],
    input_bindings: Mapping[str, Mapping[str, Mapping[str, str]]],
    boundary_handles: Sequence[Mapping[str, Any]],
    endpoint_aliases: Mapping[str, str],
) -> dict[str, list[dict[str, str]]]:
    parameters = input_contract.get("parameters")
    if not isinstance(parameters, list):
        raise _CompositeFailure(
            "composite_boundary_mapping_invalid",
            "/child/input_contract",
        )
    result: dict[str, list[dict[str, str]]] = {}
    for raw_parameter in parameters:
        parameter = _mapping(raw_parameter, "/child/input_contract/parameters")
        name = parameter.get("name")
        if not isinstance(name, str):
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                "/child/input_contract/parameters/name",
            )
        boundary = _business_handle(
            boundary_handles,
            name=name,
            io_type="target",
        )
        entries: list[dict[str, str]] = []
        for original_node_uuid, bindings in input_bindings.items():
            mapped_node_uuid = endpoint_aliases.get(original_node_uuid)
            for target_handle_uuid, binding in bindings.items():
                if binding.get("parameter") != name:
                    continue
                if mapped_node_uuid is None:
                    raise _CompositeFailure(
                        "composite_boundary_mapping_invalid",
                        "/child/input_bindings/node",
                    )
                entries.append(
                    {
                        "workflow_node_uuid": mapped_node_uuid,
                        "target_handle_uuid": target_handle_uuid,
                    }
                )
        if not entries and (
            parameter.get("required") is not False or "default" not in parameter
        ):
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                f"/child/input_bindings/{name}",
            )
        result[str(boundary["uuid"])] = sorted(
            entries,
            key=lambda item: (
                item["workflow_node_uuid"],
                item["target_handle_uuid"],
            ),
        )
    return {key: result[key] for key in sorted(result)}


def _source_mappings(
    output_contract: Mapping[str, Any],
    output_bindings: Mapping[str, Mapping[str, str]],
    boundary_handles: Sequence[Mapping[str, Any]],
    endpoint_aliases: Mapping[str, str],
) -> dict[str, dict[str, str]]:
    outputs = output_contract.get("outputs")
    if not isinstance(outputs, list):
        raise _CompositeFailure(
            "composite_boundary_mapping_invalid",
            "/child/output_contract",
        )
    result: dict[str, dict[str, str]] = {}
    for raw_output in outputs:
        output = _mapping(raw_output, "/child/output_contract/outputs")
        name = output.get("name")
        if not isinstance(name, str):
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                "/child/output_contract/outputs/name",
            )
        boundary = _business_handle(
            boundary_handles,
            name=name,
            io_type="source",
        )
        binding = output_bindings.get(name)
        if not isinstance(binding, Mapping):
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                f"/child/output_bindings/{name}",
            )
        if binding.get("kind") == "workflow_input":
            parameter = binding.get("parameter")
            if not isinstance(parameter, str):
                raise _CompositeFailure(
                    "composite_boundary_mapping_invalid",
                    f"/child/output_bindings/{name}",
                )
            normalized = {
                "kind": "workflow_input",
                "parameter": parameter,
            }
        elif binding.get("kind") == "node_output":
            original_node_uuid = binding.get("workflow_node_uuid")
            mapped_node_uuid = (
                endpoint_aliases.get(original_node_uuid)
                if isinstance(original_node_uuid, str)
                else None
            )
            source_handle_uuid = binding.get("source_handle_uuid")
            if mapped_node_uuid is None or not isinstance(source_handle_uuid, str):
                raise _CompositeFailure(
                    "composite_boundary_mapping_invalid",
                    f"/child/output_bindings/{name}",
                )
            normalized = {
                "kind": "node_output",
                "workflow_node_uuid": mapped_node_uuid,
                "source_handle_uuid": source_handle_uuid,
            }
        else:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                f"/child/output_bindings/{name}",
            )
        result[str(boundary["uuid"])] = normalized
    return {key: result[key] for key in sorted(result)}


def _structural_mappings(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    catalog: TemplateCatalogSnapshot,
) -> dict[str, list[dict[str, str]]]:
    node_by_uuid = {
        str(node["uuid"]): node
        for node in nodes
        if not _is_presentation_group(catalog, node)
    }
    incoming = {node_uuid: 0 for node_uuid in node_by_uuid}
    outgoing = {node_uuid: 0 for node_uuid in node_by_uuid}
    for edge in edges:
        source = str(edge["source_node_uuid"])
        target = str(edge["target_node_uuid"])
        if source not in outgoing or target not in incoming:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                "/child/edges/presentation_group",
            )
        outgoing[source] += 1
        incoming[target] += 1
    entry_targets = [
        {
            "workflow_node_uuid": node_uuid,
            "target_handle_uuid": _ready_handle(
                catalog,
                node_by_uuid[node_uuid],
                "target",
            ),
        }
        for node_uuid in sorted(node_by_uuid)
        if incoming[node_uuid] == 0
    ]
    completion_sources = [
        {
            "workflow_node_uuid": node_uuid,
            "source_handle_uuid": _ready_handle(
                catalog,
                node_by_uuid[node_uuid],
                "source",
            ),
        }
        for node_uuid in sorted(node_by_uuid)
        if outgoing[node_uuid] == 0
    ]
    return {
        "entry_targets": entry_targets,
        "completion_sources": completion_sources,
    }


def _is_presentation_group(
    catalog: TemplateCatalogSnapshot,
    node: Mapping[str, Any],
) -> bool:
    template = _node_template(catalog, node)
    return template.get("type") == "group" and template.get("node_type") == "group"


def _ready_handle(
    catalog: TemplateCatalogSnapshot,
    node: Mapping[str, Any],
    io_type: str,
) -> str:
    template_uuid = node.get("workflow_node_template_uuid")
    matches = []
    for handle in catalog.handle_templates:
        meta_data = handle.get("meta_data")
        unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
        if (
            handle.get("workflow_node_template_uuid") == template_uuid
            and handle.get("io_type") == io_type
            and isinstance(unilab, Mapping)
            and unilab.get("structural_role") == "ready"
        ):
            matches.append(handle)
    if len(matches) != 1:
        raise _CompositeFailure(
            "composite_boundary_mapping_invalid",
            "/catalog/ready_handle",
        )
    return str(matches[0]["uuid"])


def _invocation_node(
    *,
    source: PublishedWorkflowSource,
    template_uuid: str,
    invocation_uuid: str,
    parent_uuid: str | None,
    keyword_arguments: Mapping[str, object],
    boundary_handles: Sequence[Mapping[str, Any]],
    base_node: Mapping[str, Any] | None,
    pin: Mapping[str, Any],
    contract_compatibility: Mapping[str, Any],
    target_mappings: Mapping[str, Any],
    source_mappings: Mapping[str, Any],
    structural_mappings: Mapping[str, Any],
) -> dict[str, Any]:
    value_targets = {
        str(handle.get("data_key")): handle
        for handle in boundary_handles
        if handle.get("io_type") == "target"
        and _structural_role(handle) is None
        and isinstance(handle.get("data_key"), str)
    }
    if any(name not in value_targets for name in keyword_arguments):
        raise _CompositeFailure(
            "composite_boundary_mapping_invalid",
            "/keyword_arguments/name",
        )
    existing_meta = (
        _plain(base_node.get("meta_data", {})) if base_node is not None else {}
    )
    if not isinstance(existing_meta, dict):
        existing_meta = {}
    unilab = existing_meta.get("unilab")
    unilab = dict(unilab) if isinstance(unilab, Mapping) else {}
    existing_bindings = unilab.get("input_bindings")
    input_bindings = (
        _plain(existing_bindings) if isinstance(existing_bindings, Mapping) else {}
    )
    param: dict[str, Any] = {}
    for name, value in keyword_arguments.items():
        handle = value_targets[name]
        handle_uuid = str(handle["uuid"])
        if isinstance(value, Mapping) and value.get("kind") == "workflow_input":
            if set(value) != {"kind", "parameter"} or not isinstance(
                value.get("parameter"), str
            ):
                raise _CompositeFailure(
                    "composite_boundary_mapping_invalid",
                    f"/keyword_arguments/{name}",
                )
            input_bindings[handle_uuid] = {"parameter": value["parameter"]}
        else:
            param[name] = _plain(value)
            input_bindings.pop(handle_uuid, None)
    for name, handle in value_targets.items():
        if (
            handle.get("required") is True
            and name not in keyword_arguments
            and str(handle["uuid"]) not in input_bindings
        ):
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                f"/keyword_arguments/{name}",
            )
    composite = {
        "version": 1,
        **_plain(pin),
        "contract_compatibility": _plain(contract_compatibility),
        "target_mappings": _plain(target_mappings),
        "source_mappings": _plain(source_mappings),
        "structural_mappings": _plain(structural_mappings),
    }
    unilab["input_bindings"] = input_bindings
    unilab["composite"] = composite
    existing_meta["unilab"] = unilab
    base = (
        {
            key: _plain(value)
            for key, value in base_node.items()
            if key in WorkflowNodeWrite.model_fields
        }
        if base_node is not None
        else {}
    )
    base.update(
        {
            "uuid": invocation_uuid,
            "workflow_node_template_uuid": template_uuid,
            "parent_uuid": parent_uuid,
            "name": str(base.get("name") or source.symbol),
            "status": str(base.get("status") or "idle"),
            "type": "workflow",
            "pose": _plain(base.get("pose") or {}),
            "param": param,
            "execution_policy": _plain(base.get("execution_policy") or {}),
            "disabled": bool(base.get("disabled", False)),
            "minimized": bool(base.get("minimized", False)),
            "meta_data": existing_meta,
        }
    )
    try:
        result = WorkflowNodeWrite.model_validate(base).model_dump(exclude_none=True)
        result["parent_uuid"] = parent_uuid
        return result
    except (TypeError, ValueError):
        raise _CompositeFailure(
            "composite_boundary_mapping_invalid",
            "/invocation_node",
        ) from None


def _structural_role(handle: Mapping[str, Any]) -> Any:
    meta_data = handle.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    return unilab.get("structural_role") if isinstance(unilab, Mapping) else None


def _parent_input_contract(parent_graph: Mapping[str, Any]) -> dict[str, Any]:
    workflow = parent_graph.get("workflow")
    if not isinstance(workflow, Mapping):
        raise TypeError("缺少 parent Workflow")
    meta_data = workflow.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    raw = (
        unilab.get("input_contract", {"version": 1, "parameters": []})
        if isinstance(unilab, Mapping)
        else {"version": 1, "parameters": []}
    )
    return parse_input_contract(raw).to_dict()


def _effective_parent_input_contract(
    parent_input_contract: Mapping[str, Any],
    child_input_contract: Mapping[str, Any],
    keyword_arguments: Mapping[str, object],
) -> dict[str, Any]:
    effective = _plain(parent_input_contract)
    parameters = effective.get("parameters")
    if not isinstance(parameters, list):
        raise _CompositeFailure(
            "composite_boundary_mapping_invalid",
            "/parent/input_contract",
        )
    parent_by_name = {
        parameter.get("name"): parameter
        for parameter in parameters
        if isinstance(parameter, dict) and isinstance(parameter.get("name"), str)
    }
    child_parameters = child_input_contract.get("parameters")
    if not isinstance(child_parameters, list):
        raise _CompositeFailure(
            "composite_boundary_mapping_invalid",
            "/child/input_contract",
        )
    child_by_name = {
        parameter.get("name"): parameter
        for parameter in child_parameters
        if isinstance(parameter, Mapping) and isinstance(parameter.get("name"), str)
    }
    for child_name, provider in keyword_arguments.items():
        if (
            not isinstance(provider, Mapping)
            or provider.get("kind") != "workflow_input"
        ):
            continue
        parameter_name = provider.get("parameter")
        parent_parameter = (
            parent_by_name.get(parameter_name)
            if isinstance(parameter_name, str)
            else None
        )
        child_parameter = child_by_name.get(child_name)
        if parent_parameter is None or child_parameter is None:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                f"/keyword_arguments/{child_name}",
            )
        parent_schema = parent_parameter.get("schema")
        child_schema = child_parameter.get("schema")
        if not isinstance(parent_schema, Mapping) or not isinstance(
            child_schema, Mapping
        ):
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                f"/keyword_arguments/{child_name}/schema",
            )
        parent_slot = resource_slot_schema(parent_schema)
        child_slot = resource_slot_schema(child_schema)
        if parent_slot is None and child_slot is None:
            continue
        if parent_slot is None or child_slot is None:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                f"/keyword_arguments/{child_name}/schema",
            )
        if not schema_is_assignable(
            _replace_slot_allowlist(parent_schema, None),
            _replace_slot_allowlist(child_schema, None),
        ):
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                f"/keyword_arguments/{child_name}/schema",
            )
        parent_allowed = _slot_allowlist(parent_slot)
        child_allowed = _slot_allowlist(child_slot)
        if parent_allowed is None:
            intersection = child_allowed
        elif child_allowed is None:
            intersection = parent_allowed
        else:
            intersection = sorted(set(parent_allowed) & set(child_allowed))
            if not intersection:
                raise _CompositeFailure(
                    "composite_resource_constraint_empty",
                    f"/keyword_arguments/{child_name}/schema",
                )
        parent_parameter["schema"] = _replace_slot_allowlist(
            parent_schema,
            intersection,
        )
    return effective


def _slot_allowlist(slot_schema: Mapping[str, Any]) -> list[str] | None:
    raw = slot_schema.get("allowed_resource_template_uuids")
    if raw is None:
        return None
    if (
        not isinstance(raw, list)
        or not raw
        or any(not isinstance(item, str) for item in raw)
    ):
        raise _CompositeFailure(
            "composite_catalog_mismatch",
            "/catalog/handle/allowed_resource_template_uuids",
        )
    values = [
        _canonical_uuid(
            item,
            "composite_catalog_mismatch",
            "/catalog/handle/allowed_resource_template_uuids",
        )
        for item in raw
    ]
    if len(set(values)) != len(values):
        raise _CompositeFailure(
            "composite_catalog_mismatch",
            "/catalog/handle/allowed_resource_template_uuids",
        )
    return sorted(values)


def _replace_slot_allowlist(
    schema: Mapping[str, Any],
    allowlist: list[str] | None,
) -> dict[str, Any]:
    result = _plain(schema)
    if result.get("$slot") == "ResourceSlot":
        if allowlist is None:
            result.pop("allowed_resource_template_uuids", None)
        else:
            result["allowed_resource_template_uuids"] = list(allowlist)
        return result
    items = result.get("items")
    if isinstance(items, Mapping) and resource_slot_schema(items) is not None:
        result["items"] = _replace_slot_allowlist(items, allowlist)
        return result
    members = result.get("anyOf")
    if isinstance(members, list):
        result["anyOf"] = [
            _replace_slot_allowlist(member, allowlist)
            if isinstance(member, Mapping) and resource_slot_schema(member) is not None
            else _plain(member)
            for member in members
        ]
    return result


def _reject_private_providers(
    keyword_arguments: Mapping[str, object],
    expanded: _ExpandedChild,
) -> None:
    private_nodes = {str(node["uuid"]) for node in expanded.nodes}
    for name, provider in keyword_arguments.items():
        if not isinstance(provider, Mapping) or provider.get("kind") != "node_output":
            continue
        node_uuid = provider.get("workflow_node_uuid")
        if node_uuid in private_nodes:
            raise _CompositeFailure(
                "composite_external_private_edge",
                f"/keyword_arguments/{name}",
            )
        if set(provider) != {
            "kind",
            "workflow_node_uuid",
            "source_handle_uuid",
        }:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                f"/keyword_arguments/{name}",
            )


def _referenced_templates(
    catalog: TemplateCatalogSnapshot,
    invocation_node: Mapping[str, Any],
    nodes: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    template_uuids = {
        str(node["workflow_node_template_uuid"])
        for node in (invocation_node, *nodes)
        if isinstance(node.get("workflow_node_template_uuid"), str)
    }
    node_templates = [
        _plain(template)
        for template in catalog.node_templates
        if template.get("uuid") in template_uuids
    ]
    if {str(item["uuid"]) for item in node_templates} != template_uuids:
        raise _CompositeFailure(
            "composite_catalog_mismatch",
            "/catalog/node_templates",
        )
    handle_templates = [
        _plain(handle)
        for handle in catalog.handle_templates
        if handle.get("workflow_node_template_uuid") in template_uuids
    ]
    return (
        sorted(node_templates, key=lambda item: str(item["uuid"])),
        sorted(handle_templates, key=lambda item: str(item["uuid"])),
    )


def _group_template(
    host_node_resource_template_uuid: str,
    *,
    authority_id: str,
) -> NodeTemplateImport:
    """C1 使用 host_node renderer owner 发布唯一 presentation group template。"""

    owner_uuid = _host_owner_uuid(host_node_resource_template_uuid)
    return NodeTemplateImport(
        template={
            "resource_template_uuid": owner_uuid,
            "name": "group",
            "display_name": "Group",
            "description": "Presentation group for Workflow authoring",
            "class": "unilabos.workflow.authoring:group",
            "goal": {},
            "goal_default": {},
            "feedback": {},
            "result": {},
            "schema": None,
            "type": "group",
            "node_type": "group",
            "meta_data": {
                "unilab": {
                    "authority_id": authority_id,
                    "framework_owner_only": True,
                    "source_fqid": "unilabos.workflow.authoring:group",
                }
            },
        },
        handles=(),
    )


def _validate_composite_graph_io(
    graph: Mapping[str, Any],
    *,
    catalog: TemplateCatalogSnapshot | None = None,
    host_resource_template_uuid: str | None = None,
) -> ValidatedWorkflowIO:
    """验证 I1，仅把 Composite ResourceSlot 收窄延后到 compile。

    普通 I1 assignability 仍保持严格。D-064 要求 Published Workflow boundary
    在完整 Composite chain 上求 allowlist 交集，因此 publication 不能提前拒绝
    合法收窄。这里只在私有校验副本中擦除权威 Published Workflow target Handle
    的 consumer allowlist；type、wrapper、nullability 与其他 I/O 规则均不放宽。
    """

    relaxed = _plain(graph)
    raw_templates = relaxed.get("node_templates")
    raw_handles = relaxed.get("handle_templates")
    if not isinstance(raw_templates, list) or not isinstance(raw_handles, list):
        return validate_workflow_graph_io(relaxed)
    catalog_templates = (
        {
            str(template.get("uuid")): template
            for template in catalog.node_templates
            if isinstance(template.get("uuid"), str)
        }
        if catalog is not None
        else {}
    )
    composite_template_uuids: set[str] = set()
    for template in raw_templates:
        if not isinstance(template, Mapping) or not isinstance(
            template.get("uuid"), str
        ):
            continue
        template_uuid = str(template["uuid"])
        if catalog is not None:
            catalog_template = catalog_templates.get(template_uuid)
            catalog_is_composite = catalog_template is not None and (
                _is_framework_published_workflow_template(
                    catalog_template,
                    catalog.handle_templates,
                )
            )
            if catalog_is_composite:
                if not _published_workflow_aggregate_matches(
                    template,
                    raw_handles,
                    catalog_template,
                    catalog.handle_templates,
                ):
                    raise CompositeCatalogMismatch(
                        "/catalog/published_workflow/aggregate"
                    )
                composite_template_uuids.add(template_uuid)
                continue
            if _is_published_workflow_template(template):
                raise CompositeCatalogMismatch("/catalog/published_workflow/aggregate")
            continue
        if not _is_published_workflow_template(template):
            continue
        if not _is_framework_published_workflow_template(
            template,
            raw_handles,
            host_resource_template_uuid=host_resource_template_uuid,
        ):
            continue
        composite_template_uuids.add(template_uuid)
    for handle in raw_handles:
        if (
            not isinstance(handle, dict)
            or handle.get("workflow_node_template_uuid") not in composite_template_uuids
            or handle.get("io_type") != "target"
        ):
            continue
        meta_data = handle.get("meta_data")
        unilab = meta_data.get("unilab") if isinstance(meta_data, dict) else None
        value_schema = unilab.get("value_schema") if isinstance(unilab, dict) else None
        if (
            not isinstance(value_schema, Mapping)
            or resource_slot_schema(value_schema) is None
        ):
            continue
        unilab["value_schema"] = _replace_slot_allowlist(value_schema, None)
        unilab["allowed_resource_template_uuids"] = None

    # Expanded child nodes retain the child's private input parameter names in
    # their frozen metadata.  At the parent boundary those bindings are owned by
    # the Published Workflow invocation (and its target_mappings), not by the
    # parent Workflow input contract.  Ignore only descendant bindings while
    # preserving bindings declared on the invocation itself.
    raw_nodes = relaxed.get("nodes")
    if isinstance(raw_nodes, list) and composite_template_uuids:
        parent_by_uuid = {
            str(node.get("uuid")): node.get("parent_uuid")
            for node in raw_nodes
            if isinstance(node, Mapping) and isinstance(node.get("uuid"), str)
        }
        composite_node_uuids = {
            str(node["uuid"])
            for node in raw_nodes
            if isinstance(node, Mapping)
            and isinstance(node.get("uuid"), str)
            and node.get("workflow_node_template_uuid") in composite_template_uuids
        }
        for node in raw_nodes:
            if not isinstance(node, dict) or not isinstance(node.get("uuid"), str):
                continue
            parent_uuid = parent_by_uuid.get(str(node["uuid"]))
            visited: set[str] = set()
            is_composite_descendant = False
            while isinstance(parent_uuid, str) and parent_uuid not in visited:
                if parent_uuid in composite_node_uuids:
                    is_composite_descendant = True
                    break
                visited.add(parent_uuid)
                parent_uuid = parent_by_uuid.get(parent_uuid)
            if not is_composite_descendant:
                continue
            meta_data = node.get("meta_data")
            unilab = (
                meta_data.get("unilab") if isinstance(meta_data, dict) else None
            )
            if isinstance(unilab, dict):
                unilab["input_bindings"] = {}
    return validate_workflow_graph_io(relaxed)


def _published_workflow_aggregate_matches(
    graph_template: Mapping[str, Any],
    graph_handles: Sequence[Mapping[str, Any]],
    catalog_template: Mapping[str, Any],
    catalog_handles: Sequence[Mapping[str, Any]],
) -> bool:
    if _read_entity(graph_template) != _read_entity(catalog_template):
        return False
    template_uuid = graph_template.get("uuid")
    graph_owned = {
        str(handle.get("uuid")): _read_entity(handle)
        for handle in graph_handles
        if handle.get("workflow_node_template_uuid") == template_uuid
        and isinstance(handle.get("uuid"), str)
    }
    catalog_owned = {
        str(handle.get("uuid")): _read_entity(handle)
        for handle in catalog_handles
        if handle.get("workflow_node_template_uuid") == template_uuid
        and isinstance(handle.get("uuid"), str)
    }
    return (
        len(graph_owned)
        == len(
            [
                handle
                for handle in graph_handles
                if handle.get("workflow_node_template_uuid") == template_uuid
            ]
        )
        and graph_owned == catalog_owned
    )


def _read_entity(value: Mapping[str, Any]) -> dict[str, Any]:
    """统一 Catalog read DTO 与 graph snapshot 的 nullable 外形。"""

    return {str(key): _plain(item) for key, item in value.items() if item is not None}


def project_published_workflow_contract(
    *,
    source: PublishedWorkflowSource,
    applied_snapshot: Mapping[str, Any],
    host_node_resource_template_uuid: str | None,
) -> NodeTemplateImport | None:
    """把一个 coherent Applied Workflow snapshot 投影为现有 Catalog aggregate。

    Unapplied/stale child 不是错误：它不进入 Published Catalog。Package source、Applied
    graph 或 host renderer owner 不自洽则 fail closed，绝不合成替代 identity。
    """

    if not isinstance(source, PublishedWorkflowSource):
        raise TypeError("source 必须是 PublishedWorkflowSource")
    workflow = applied_snapshot.get("workflow")
    applied_source = applied_snapshot.get("applied_source")
    if not isinstance(workflow, Mapping):
        raise CompositeCatalogMismatch("/published_workflow/workflow")
    workflow_uuid = _uuid(workflow.get("uuid"), "/published_workflow/workflow/uuid")
    if workflow_uuid != source.workflow_uuid:
        raise CompositeCatalogMismatch("/published_workflow/source/workflow_uuid")
    revision = workflow.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise CompositeCatalogMismatch("/published_workflow/workflow/revision")
    if not isinstance(applied_source, Mapping):
        return None
    if applied_source.get("workflow_revision") != revision:
        return None
    applied_source_hash = _sha256(
        applied_source.get("source_hash"),
        "/published_workflow/applied_source/source_hash",
    )
    host_uuid = _host_owner_uuid(host_node_resource_template_uuid)

    graph = {
        "workflow": _plain(workflow),
        "nodes": _sequence(applied_snapshot.get("nodes"), "/published_workflow/nodes"),
        "edges": _sequence(applied_snapshot.get("edges"), "/published_workflow/edges"),
        "node_templates": _sequence(
            applied_snapshot.get("node_templates"),
            "/published_workflow/node_templates",
        ),
        "handle_templates": _sequence(
            applied_snapshot.get("handle_templates"),
            "/published_workflow/handle_templates",
        ),
    }
    try:
        workflow_io = _validate_composite_graph_io(
            graph,
            host_resource_template_uuid=host_uuid,
        )
    except (
        WorkflowIOValidationError,
        WorkflowSchemaError,
        TypeError,
        ValueError,
    ) as error:
        raise CompositeCatalogMismatch("/published_workflow/io_contract") from error

    input_contract = workflow_io.input_contract.to_dict()
    output_contract = workflow_io.output_contract.to_dict()
    inputs = [_plain(item) for item in input_contract["parameters"]]
    outputs = [_plain(item) for item in output_contract["outputs"]]
    mode = _composition_mode(workflow)
    contract_digest = _contract_digest(
        inputs=inputs,
        outputs=outputs,
        composition_allow_transparent=mode,
    )
    schema = _workflow_schema(
        inputs=inputs,
        outputs=outputs,
        workflow_uuid=workflow_uuid,
        workflow_revision=revision,
        applied_source_hash=applied_source_hash,
        contract_digest=contract_digest,
        composition_allow_transparent=mode,
    )
    handles = tuple(
        [_value_handle(item, io_type="target") for item in input_contract["parameters"]]
        + [_value_handle(item, io_type="source") for item in output_contract["outputs"]]
        + [structural_ready_handle("target"), structural_ready_handle("source")]
    )
    return NodeTemplateImport(
        template={
            "resource_template_uuid": host_uuid,
            "name": f"workflow:{workflow_uuid}",
            "display_name": str(workflow.get("name") or source.symbol),
            "description": str(workflow.get("description") or ""),
            "class": f"{source.module}:{source.symbol}",
            "type": "workflow",
            "node_type": "workflow",
            "goal": {
                str(item["name"]): str(item["name"])
                for item in input_contract["parameters"]
            },
            "goal_default": {
                str(item["name"]): _plain(item["default"])
                for item in input_contract["parameters"]
                if "default" in item
            },
            "feedback": {},
            "result": {
                str(item["name"]): str(item["name"])
                for item in output_contract["outputs"]
            },
            "schema": schema,
            "meta_data": {
                "unilab": {
                    "framework_owner_only": True,
                    "workflow_source": {
                        "kind": "package",
                        "definition_fqid": source.definition_fqid,
                        "module": source.module,
                        "symbol": source.symbol,
                        "package_catalog_digest": source.package_catalog_digest,
                        "definition_content_hash": source.definition_content_hash,
                    },
                }
            },
        },
        handles=handles,
    )


def _workflow_schema(
    *,
    inputs: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    workflow_uuid: str,
    workflow_revision: int,
    applied_source_hash: str,
    contract_digest: str,
    composition_allow_transparent: bool,
) -> dict[str, Any]:
    goal_properties: dict[str, Any] = {}
    for item in inputs:
        property_schema = _plain(item["schema"])
        if "default" in item:
            property_schema["default"] = _plain(item["default"])
        goal_properties[str(item["name"])] = property_schema
    result_properties = {str(item["name"]): _plain(item["schema"]) for item in outputs}
    required = [str(item["name"]) for item in inputs if item.get("required") is True]
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "goal": {
                "type": "object",
                "additionalProperties": False,
                "properties": goal_properties,
                "required": required,
            },
            "result": {
                "type": "object",
                "additionalProperties": False,
                "properties": result_properties,
                "required": [str(item["name"]) for item in outputs],
            },
        },
        "required": ["goal", "result"],
        "x-unilabos-workflow-contract": {
            "version": 1,
            "compatibility_version": 1,
            "workflow_uuid": workflow_uuid,
            "workflow_revision": workflow_revision,
            "applied_source_hash": applied_source_hash,
            "contract_digest": contract_digest,
            "composition_allow_transparent": composition_allow_transparent,
            "input_order": [str(item["name"]) for item in inputs],
            "output_order": [str(item["name"]) for item in outputs],
        },
    }


def _semantic_descriptor(raw: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        str(key): _plain(value)
        for key, value in raw.items()
        if key not in {"title", "description"}
    }
    return result


def classify_published_workflow_compatibility(
    *,
    previous_template: Mapping[str, Any],
    previous_handles: Sequence[Mapping[str, Any]],
    current_template: Mapping[str, Any],
    current_handles: Sequence[Mapping[str, Any]],
) -> str:
    """按 C1 compatibility_version=1 分类 Published Workflow 合同演化。"""

    try:
        previous = _compatibility_projection(
            previous_template,
            previous_handles,
        )
        current = _compatibility_projection(current_template, current_handles)
    except (KeyError, TypeError, ValueError):
        return "breaking"
    return classify_published_workflow_compatibility_projections(previous, current)


def published_workflow_compatibility_projection(
    template: Mapping[str, Any],
    handles: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """生成可随 Applied invocation 固定的最小兼容性投影。"""

    return _compatibility_projection(template, handles)


def published_workflow_projection_is_canonical(
    template: Mapping[str, Any],
    handles: Sequence[Mapping[str, Any]],
) -> bool:
    """认证旧 Canvas 中完整的 Published Workflow template/Handle aggregate。"""

    return _is_framework_published_workflow_template(template, handles)


def classify_published_workflow_compatibility_projections(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> str:
    """比较随 Applied invocation 固定的最小兼容性投影。"""

    required = {
        "template_uuid",
        "workflow_uuid",
        "mode",
        "digest",
        "inputs",
        "outputs",
    }
    if set(previous) != required or set(current) != required:
        return "breaking"
    if (
        previous["workflow_uuid"] != current["workflow_uuid"]
        or previous["template_uuid"] != current["template_uuid"]
        or previous["mode"] != current["mode"]
    ):
        return "breaking"
    previous_inputs = previous["inputs"]
    current_inputs = current["inputs"]
    previous_outputs = previous["outputs"]
    current_outputs = current["outputs"]
    if previous["digest"] == current["digest"]:
        return (
            "exact"
            if previous_inputs == current_inputs and previous_outputs == current_outputs
            else "breaking"
        )
    if (
        current_inputs[: len(previous_inputs)] != previous_inputs
        or current_outputs[: len(previous_outputs)] != previous_outputs
    ):
        return "breaking"
    if any(
        item["required"] is not False or item["has_default"] is not True
        for item in current_inputs[len(previous_inputs) :]
    ):
        return "breaking"
    return "additive"


def _compatibility_projection(
    template: Mapping[str, Any],
    handles: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    schema = template["schema"]
    extension = schema["x-unilabos-workflow-contract"]
    if (
        not isinstance(schema, Mapping)
        or not isinstance(extension, Mapping)
        or extension.get("version") != 1
        or extension.get("compatibility_version") != 1
    ):
        raise ValueError("Published Workflow contract 无效")
    properties = schema["properties"]
    goal = properties["goal"]
    result = properties["result"]
    goal_properties = goal["properties"]
    result_properties = result["properties"]
    required_inputs = set(goal.get("required", []))
    goal_default = template.get("goal_default", {})
    if not all(
        isinstance(value, Mapping)
        for value in (properties, goal, result, goal_properties, result_properties)
    ) or not isinstance(goal_default, Mapping):
        raise ValueError("Published Workflow schema 无效")
    input_order = extension["input_order"]
    output_order = extension["output_order"]
    if (
        not isinstance(input_order, list)
        or not isinstance(output_order, list)
        or any(not isinstance(name, str) for name in [*input_order, *output_order])
    ):
        raise ValueError("Published Workflow order 无效")
    handle_by_key = {
        (str(handle.get("handle_key")), str(handle.get("io_type"))): handle
        for handle in handles
    }

    def handle_identity(name: str, io_type: str) -> str:
        handle = handle_by_key[(name, io_type)]
        value = handle.get("uuid")
        if not isinstance(value, str):
            raise TypeError("Published Workflow Handle UUID 无效")
        return value

    inputs = [
        {
            "name": name,
            "schema": _plain(goal_properties[name]),
            "required": name in required_inputs,
            "has_default": name in goal_default,
            **({"default": _plain(goal_default[name])} if name in goal_default else {}),
            "handle_uuid": handle_identity(name, "target"),
        }
        for name in input_order
    ]
    outputs = []
    for name in output_order:
        handle = handle_by_key[(name, "source")]
        meta_data = handle.get("meta_data")
        unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
        if not isinstance(unilab, Mapping):
            raise TypeError("Published Workflow Handle metadata 无效")
        outputs.append(
            {
                "name": name,
                "schema": _plain(result_properties[name]),
                "implicit": bool(unilab.get("implicit_passthrough", False)),
                "handle_uuid": handle_identity(name, "source"),
            }
        )
    return {
        "template_uuid": str(template["uuid"]),
        "workflow_uuid": str(extension["workflow_uuid"]),
        "mode": extension["composition_allow_transparent"],
        "digest": str(extension["contract_digest"]),
        "inputs": inputs,
        "outputs": outputs,
    }


def _contract_digest(
    *,
    inputs: Sequence[Mapping[str, Any]],
    outputs: Sequence[Mapping[str, Any]],
    composition_allow_transparent: bool,
) -> str:
    payload = {
        "version": 1,
        "composition_allow_transparent": composition_allow_transparent,
        "inputs": [_semantic_descriptor(item) for item in inputs],
        "outputs": [_semantic_descriptor(item) for item in outputs],
    }
    return "sha256:" + hashlib.sha256(rfc8785.dumps(payload)).hexdigest()


def _value_handle(
    descriptor: Mapping[str, Any],
    *,
    io_type: str,
) -> dict[str, Any]:
    name = str(descriptor["name"])
    schema = _plain(descriptor["schema"])
    slot_schema = resource_slot_schema(schema)
    allowed = (
        _plain(slot_schema.get("allowed_resource_template_uuids"))
        if slot_schema is not None
        else None
    )
    implicit = bool(descriptor.get("implicit", False)) if io_type == "source" else False
    return {
        "handle_key": name,
        "io_type": io_type,
        "display_name": str(descriptor.get("title") or name),
        "description": str(descriptor.get("description") or ""),
        "type": workflow_handle_type(schema),
        "required": bool(descriptor.get("required", False))
        if io_type == "target"
        else False,
        "data_source": "goal" if io_type == "target" else "result",
        "data_key": name,
        "meta_data": {
            "unilab": {
                "value_schema": schema,
                "editor_control": (
                    "material_port" if slot_schema is not None else "variable_selector"
                ),
                "allowed_resource_template_uuids": allowed,
                "implicit_passthrough": implicit,
            }
        },
    }


def _composition_mode(workflow: Mapping[str, Any]) -> bool:
    meta_data = workflow.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    raw = (
        unilab.get("composition_allow_transparent", False)
        if isinstance(unilab, Mapping)
        else False
    )
    if not isinstance(raw, bool):
        raise CompositeCatalogMismatch(
            "/published_workflow/composition_allow_transparent"
        )
    return raw


def _host_owner_uuid(value: Any) -> str:
    if value is None:
        raise CompositeCatalogMismatch("/host_node/resource_template_uuid")
    return _uuid(value, "/host_node/resource_template_uuid")


def _uuid(value: Any, path: str) -> str:
    try:
        canonical = validate_uuid(value)
    except (TypeError, ValueError):
        raise CompositeCatalogMismatch(path) from None
    if canonical != value:
        raise CompositeCatalogMismatch(path)
    return canonical


def _sha256(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise CompositeCatalogMismatch(path)
    digest = value.removeprefix("sha256:")
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise CompositeCatalogMismatch(path)
    return value


def _sequence(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise CompositeCatalogMismatch(path)
    return _plain(value)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


__all__ = [
    "CompositeAuthoring",
    "CompositeCatalogMismatch",
    "CompositeExpansion",
    "PublishedWorkflowCatalogPublisher",
    "PublishedWorkflowResolver",
    "PublishedWorkflowSource",
    "classify_published_workflow_compatibility",
    "classify_published_workflow_compatibility_projections",
    "project_published_workflow_contract",
    "published_workflow_compatibility_projection",
    "published_workflow_projection_is_canonical",
]
