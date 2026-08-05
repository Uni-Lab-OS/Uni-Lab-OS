"""组合工作流调用（CompositeWorkflowInvocation）的只读静态展开算法。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from unilabos.workflow.authoring_identity import (
    authoring_edge_uuid,
    expanded_node_uuid,
)
from unilabos.workflow.authoring_kernel import (
    AuthoringCatalogAction,
    AuthoringCatalogError,
    AuthoringCatalogSnapshot,
)
from unilabos.workflow.catalog import (
    PublishedSourceCatalogError,
    PublishedWorkflowSource,
)
from unilabos.workflow.models import validate_uuid
from unilabos.workflow.workflow_io import (
    WorkflowIOValidationError,
    validate_workflow_graph_io,
)


class PublishedWorkflowSnapshotProvider(Protocol):
    """按稳定工作流身份读取同视图已应用快照的窄端口。"""

    def get_published_workflow_snapshot(
        self,
        workflow_uuid: str,
    ) -> Mapping[str, Any]:
        """返回已应用工作流快照；不存在时抛出 ``LookupError``。"""


class PublishedWorkflowResolver(Protocol):
    """按绝对模块与静态符号解析已发布工作流来源的窄端口。"""

    def resolve(self, module: str, symbol: str) -> PublishedWorkflowSource:
        """返回唯一冻结来源；不存在或歧义时抛出目录错误。"""


@dataclass(frozen=True, slots=True)
class CompositeExpansion:
    """一次组合调用的完整服务端所有创作投影。"""

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
    """把内部失败收敛成稳定诊断而不泄漏快照内容。"""

    def __init__(self, code: str, path: str) -> None:
        """保存公共错误码和 JSON Pointer 路径。"""

        self.code = code
        self.path = path
        super().__init__(code)


class CompositeAuthoring:
    """通过一个只读入口把已发布子工作流展开为父候选图。"""

    def __init__(
        self,
        *,
        snapshot_provider: PublishedWorkflowSnapshotProvider,
        catalog: AuthoringCatalogSnapshot,
        resolver: PublishedWorkflowResolver,
    ) -> None:
        """绑定只读快照、不可变模板目录和静态来源解析器。

        参数：三个依赖分别提供图事实、模板代际和 package 来源身份。返回：无。
        异常：接口形状不合法时抛出 ``TypeError``；构造时不读取或写入外部状态。
        """

        if not callable(
            getattr(snapshot_provider, "get_published_workflow_snapshot", None)
        ):
            raise TypeError("snapshot_provider 必须实现已发布快照读取端口")
        if not isinstance(catalog, AuthoringCatalogSnapshot):
            raise TypeError("catalog 必须是 AuthoringCatalogSnapshot")
        if not callable(getattr(resolver, "resolve", None)):
            raise TypeError("resolver 必须实现已发布来源解析端口")
        self._snapshot_provider = snapshot_provider
        self._catalog = catalog
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
        """只读编译一个组合工作流调用并把失败转换为零写诊断。

        参数：父工作流/调用 UUID 决定身份；模块和符号选择已发布子工作流；关键字
        参数绑定其输入边界；``parent_input_contract`` 预留给 R3 有效约束传播。
        返回：成功时包含调用节点、平面内部图、映射和 pin，失败时只含一个稳定
        诊断。异常：非领域编程错误不吞并；该接口没有写端口。
        """

        del parent_input_contract
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
            if not isinstance(keyword_arguments, Mapping) or any(
                not isinstance(key, str) for key in keyword_arguments
            ):
                raise _CompositeFailure(
                    "composite_boundary_mapping_invalid",
                    "/keyword_arguments",
                )
            try:
                source = self._resolver.resolve(module, symbol)
            except (LookupError, PublishedSourceCatalogError):
                raise _CompositeFailure("composite_child_not_found", "/source") from None
            if not isinstance(source, PublishedWorkflowSource):
                raise _CompositeFailure("composite_catalog_mismatch", "/source")
            if source.workflow_uuid == parent_uuid:
                raise _CompositeFailure(
                    "composite_recursive_reference",
                    "/composite/child_workflow_uuid",
                )
            try:
                snapshot = self._snapshot_provider.get_published_workflow_snapshot(
                    source.workflow_uuid
                )
            except LookupError:
                raise _CompositeFailure(
                    "composite_child_not_found",
                    "/child/workflow_uuid",
                ) from None
            return self._compile_snapshot(
                parent_workflow_uuid=parent_uuid,
                invocation_uuid=invocation,
                source=source,
                keyword_arguments=dict(keyword_arguments),
                snapshot=snapshot,
            )
        except _CompositeFailure as error:
            return _failed_expansion(error.code, error.path)

    def _compile_snapshot(
        self,
        *,
        parent_workflow_uuid: str,
        invocation_uuid: str,
        source: PublishedWorkflowSource,
        keyword_arguments: dict[str, object],
        snapshot: Mapping[str, Any],
    ) -> CompositeExpansion:
        """验证一个快照并构造直接子工作流的平面展开结果。"""

        workflow = _mapping(snapshot.get("workflow"), "/child/workflow")
        if workflow.get("uuid") != source.workflow_uuid:
            raise _CompositeFailure("composite_catalog_mismatch", "/child/workflow/uuid")
        revision = workflow.get("revision")
        applied_source = snapshot.get("applied_source")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or not isinstance(applied_source, Mapping)
            or applied_source.get("workflow_revision") != revision
        ):
            raise _CompositeFailure("composite_child_unapplied", "/child/applied_source")
        template_action, extension = _published_template(
            self._catalog,
            source,
            revision=revision,
            applied_source_hash=applied_source.get("source_hash"),
        )
        graph = {
            "workflow": _plain(workflow),
            "nodes": _sequence(snapshot.get("nodes"), "/child/nodes"),
            "edges": _sequence(snapshot.get("edges"), "/child/edges"),
            "node_templates": _sequence(
                snapshot.get("node_templates"),
                "/child/node_templates",
            ),
            "handle_templates": _sequence(
                snapshot.get("handle_templates"),
                "/child/handle_templates",
            ),
        }
        try:
            workflow_io = validate_workflow_graph_io(graph)
        except (WorkflowIOValidationError, KeyError, TypeError, ValueError):
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                "/child/io_contract",
            ) from None
        input_contract = workflow_io.input_contract.to_dict()
        output_contract = workflow_io.output_contract.to_dict()
        normalized_arguments = _normalize_arguments(
            input_contract,
            keyword_arguments,
        )
        nodes, node_uuid_map = _expand_nodes(
            graph["nodes"],
            invocation_uuid=invocation_uuid,
            catalog=self._catalog,
        )
        edges = _expand_edges(
            graph["edges"],
            node_uuid_map=node_uuid_map,
            parent_workflow_uuid=parent_workflow_uuid,
        )
        boundary_handles = template_action.handles
        target_mappings = _target_mappings(
            input_contract,
            workflow_io.input_bindings,
            boundary_handles,
            node_uuid_map,
        )
        source_mappings = _source_mappings(
            output_contract,
            workflow_io.output_bindings,
            boundary_handles,
            node_uuid_map,
        )
        structural = _structural_mappings(
            nodes,
            edges,
            catalog=self._catalog,
        )
        contract_pin = {
            "child_workflow_uuid": source.workflow_uuid,
            "child_workflow_revision": revision,
            "child_applied_source_hash": str(applied_source["source_hash"]),
            "contract_digest": str(extension["contract_digest"]),
            "composition_allow_transparent": bool(
                extension["composition_allow_transparent"]
            ),
        }
        invocation_node = _invocation_node(
            parent_workflow_uuid=parent_workflow_uuid,
            invocation_uuid=invocation_uuid,
            template_uuid=str(template_action.template["uuid"]),
            symbol=source.symbol,
            keyword_arguments=normalized_arguments,
            contract_pin=contract_pin,
            target_mappings=target_mappings,
            source_mappings=source_mappings,
            structural_mappings=structural,
        )
        referenced_nodes, referenced_handles = _referenced_templates(
            self._catalog,
            template_action=template_action,
            nodes=nodes,
        )
        return CompositeExpansion(
            invocation_node=invocation_node,
            nodes=tuple(nodes),
            edges=tuple(edges),
            target_mappings={key: tuple(value) for key, value in target_mappings.items()},
            source_mappings=source_mappings,
            structural_mappings={key: tuple(value) for key, value in structural.items()},
            node_templates=tuple(referenced_nodes),
            handle_templates=tuple(referenced_handles),
            contract_pin=contract_pin,
            effective_parent_input_contract={},
            diagnostics=(),
        )


def _published_template(
    catalog: AuthoringCatalogSnapshot,
    source: PublishedWorkflowSource,
    *,
    revision: int,
    applied_source_hash: Any,
) -> tuple[AuthoringCatalogAction, dict[str, Any]]:
    """取得并鉴别与来源、修订和应用哈希一致的工作流模板。"""

    matches = [
        action
        for action in catalog.actions
        if action.template.get("type") == "workflow"
        and action.template.get("node_type") == "workflow"
        and action.template.get("class") == f"{source.module}:{source.symbol}"
    ]
    if len(matches) != 1:
        raise _CompositeFailure("composite_catalog_mismatch", "/catalog/workflow")
    action = matches[0]
    template = action.template
    meta_data = template.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    provenance = (
        unilab.get("workflow_source") if isinstance(unilab, Mapping) else None
    )
    expected_provenance = {
        "kind": "package",
        "definition_fqid": source.definition_fqid,
        "module": source.module,
        "symbol": source.symbol,
        "package_catalog_digest": source.package_catalog_digest,
        "definition_content_hash": source.definition_content_hash,
    }
    if not isinstance(provenance, Mapping) or _plain(provenance) != expected_provenance:
        raise _CompositeFailure("composite_catalog_mismatch", "/catalog/provenance")
    schema = _schema_object(template.get("schema"))
    extension = schema.get("x-unilabos-workflow-contract")
    if (
        not isinstance(extension, Mapping)
        or extension.get("version") != 1
        or extension.get("workflow_uuid") != source.workflow_uuid
        or extension.get("workflow_revision") != revision
        or extension.get("applied_source_hash") != applied_source_hash
    ):
        raise _CompositeFailure("composite_catalog_mismatch", "/catalog/contract")
    return action, _plain(extension)


def _normalize_arguments(
    input_contract: Mapping[str, Any],
    keyword_arguments: Mapping[str, object],
) -> dict[str, object]:
    """核对关键字边界覆盖并补入合同默认值。"""

    parameters = input_contract.get("parameters")
    if not isinstance(parameters, list):
        raise _CompositeFailure(
            "composite_boundary_mapping_invalid",
            "/child/input_contract",
        )
    descriptors = {str(item["name"]): item for item in parameters}
    if set(keyword_arguments) - set(descriptors):
        raise _CompositeFailure(
            "composite_boundary_mapping_invalid",
            "/keyword_arguments",
        )
    normalized = dict(keyword_arguments)
    for name, descriptor in descriptors.items():
        if name in normalized:
            continue
        if "default" in descriptor:
            normalized[name] = _plain(descriptor["default"])
        elif descriptor.get("required") is True:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                f"/keyword_arguments/{name}",
            )
    return normalized


def _expand_nodes(
    raw_nodes: Sequence[Any],
    *,
    invocation_uuid: str,
    catalog: AuthoringCatalogSnapshot,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """复制直接内部节点并派生调用局部 UUID 与父关系。"""

    nodes = [_mapping(item, "/child/nodes") for item in raw_nodes]
    by_uuid: dict[str, dict[str, Any]] = {}
    for node in nodes:
        node_uuid = _canonical_uuid(
            node.get("uuid"),
            "composite_boundary_mapping_invalid",
            "/child/nodes/uuid",
        )
        if node_uuid in by_uuid:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                "/child/nodes/uuid",
            )
        try:
            template = catalog.require_template(str(node["workflow_node_template_uuid"]))
        except (AuthoringCatalogError, KeyError):
            raise _CompositeFailure(
                "composite_catalog_mismatch",
                "/child/nodes/template",
            ) from None
        if template.template.get("node_type") == "workflow":
            raise _CompositeFailure(
                "composite_nested_requires_recursive_expansion",
                "/child/nodes/template",
            )
        by_uuid[node_uuid] = node
    node_uuid_map = {
        node_uuid: expanded_node_uuid(invocation_uuid, node_uuid)
        for node_uuid in by_uuid
    }
    result: list[dict[str, Any]] = []
    for node_uuid in sorted(by_uuid):
        node = _plain(by_uuid[node_uuid])
        raw_parent = node.get("parent_uuid")
        if raw_parent is None:
            parent_uuid = invocation_uuid
        else:
            parent_uuid = node_uuid_map.get(str(raw_parent))
            if parent_uuid is None:
                raise _CompositeFailure(
                    "composite_boundary_mapping_invalid",
                    "/child/nodes/parent_uuid",
                )
        node["uuid"] = node_uuid_map[node_uuid]
        node["parent_uuid"] = parent_uuid
        result.append(node)
    return result, node_uuid_map


def _expand_edges(
    raw_edges: Sequence[Any],
    *,
    node_uuid_map: Mapping[str, str],
    parent_workflow_uuid: str,
) -> list[dict[str, Any]]:
    """复制内部边并按父工作流创作边规则重算身份。"""

    edges: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_edges:
        edge = _mapping(raw, "/child/edges")
        source_node = node_uuid_map.get(str(edge.get("source_node_uuid")))
        target_node = node_uuid_map.get(str(edge.get("target_node_uuid")))
        if source_node is None or target_node is None:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                "/child/edges/node",
            )
        source_handle = _canonical_uuid(
            edge.get("source_handle_uuid"),
            "composite_boundary_mapping_invalid",
            "/child/edges/source_handle_uuid",
        )
        target_handle = _canonical_uuid(
            edge.get("target_handle_uuid"),
            "composite_boundary_mapping_invalid",
            "/child/edges/target_handle_uuid",
        )
        edge_uuid = authoring_edge_uuid(
            workflow_uuid=parent_workflow_uuid,
            source_node_uuid=source_node,
            source_handle_uuid=source_handle,
            target_node_uuid=target_node,
            target_handle_uuid=target_handle,
        )
        if edge_uuid in seen:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                "/child/edges",
            )
        seen.add(edge_uuid)
        edges.append(
            {
                "uuid": edge_uuid,
                "source_node_uuid": source_node,
                "target_node_uuid": target_node,
                "source_handle_uuid": source_handle,
                "target_handle_uuid": target_handle,
                "description": edge.get("description"),
                "meta_data": _plain(edge.get("meta_data") or {}),
            }
        )
    return sorted(edges, key=lambda item: item["uuid"])


def _target_mappings(
    input_contract: Mapping[str, Any],
    input_bindings: Mapping[str, Mapping[str, Mapping[str, str]]],
    boundary_handles: Sequence[Mapping[str, Any]],
    node_uuid_map: Mapping[str, str],
) -> dict[str, list[dict[str, str]]]:
    """把工作流输入绑定投影到真实展开节点目标连接点。"""

    result: dict[str, list[dict[str, str]]] = {}
    for descriptor in input_contract["parameters"]:
        name = str(descriptor["name"])
        boundary_uuid = _boundary_handle_uuid(boundary_handles, name, "target")
        targets: list[dict[str, str]] = []
        for child_node_uuid, bindings in input_bindings.items():
            for handle_uuid, binding in bindings.items():
                if binding.get("parameter") == name:
                    targets.append(
                        {
                            "workflow_node_uuid": node_uuid_map[child_node_uuid],
                            "target_handle_uuid": handle_uuid,
                        }
                    )
        if not targets:
            raise _CompositeFailure(
                "composite_boundary_mapping_invalid",
                f"/target_mappings/{name}",
            )
        result[boundary_uuid] = sorted(
            targets,
            key=lambda item: (
                item["workflow_node_uuid"],
                item["target_handle_uuid"],
            ),
        )
    return dict(sorted(result.items()))


def _source_mappings(
    output_contract: Mapping[str, Any],
    output_bindings: Mapping[str, Mapping[str, str]],
    boundary_handles: Sequence[Mapping[str, Any]],
    node_uuid_map: Mapping[str, str],
) -> dict[str, dict[str, str]]:
    """把工作流输出绑定投影到展开节点输出或父输入。"""

    result: dict[str, dict[str, str]] = {}
    for descriptor in output_contract["outputs"]:
        name = str(descriptor["name"])
        boundary_uuid = _boundary_handle_uuid(boundary_handles, name, "source")
        binding = dict(output_bindings[name])
        if binding.get("kind") == "node_output":
            child_node_uuid = binding.get("workflow_node_uuid")
            mapped_node = node_uuid_map.get(str(child_node_uuid))
            if mapped_node is None:
                raise _CompositeFailure(
                    "composite_boundary_mapping_invalid",
                    f"/source_mappings/{name}",
                )
            binding["workflow_node_uuid"] = mapped_node
        result[boundary_uuid] = {str(key): str(value) for key, value in binding.items()}
    return dict(sorted(result.items()))


def _structural_mappings(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    *,
    catalog: AuthoringCatalogSnapshot,
) -> dict[str, list[dict[str, str]]]:
    """从平面有向无环图根/终点投影 ready 结构映射。"""

    node_ids = {str(node["uuid"]) for node in nodes}
    incoming = {str(edge["target_node_uuid"]) for edge in edges}
    outgoing = {str(edge["source_node_uuid"]) for edge in edges}
    by_uuid = {str(node["uuid"]): node for node in nodes}
    entries = [
        {
            "workflow_node_uuid": node_uuid,
            "target_handle_uuid": _ready_handle_uuid(
                catalog,
                by_uuid[node_uuid],
                "target",
            ),
        }
        for node_uuid in sorted(node_ids - incoming)
    ]
    completions = [
        {
            "workflow_node_uuid": node_uuid,
            "source_handle_uuid": _ready_handle_uuid(
                catalog,
                by_uuid[node_uuid],
                "source",
            ),
        }
        for node_uuid in sorted(node_ids - outgoing)
    ]
    return {"entry_targets": entries, "completion_sources": completions}


def _ready_handle_uuid(
    catalog: AuthoringCatalogSnapshot,
    node: Mapping[str, Any],
    io_type: str,
) -> str:
    """取得一个内部节点模板唯一 ready 连接点身份。"""

    try:
        action = catalog.require_template(str(node["workflow_node_template_uuid"]))
    except (AuthoringCatalogError, KeyError):
        raise _CompositeFailure("composite_catalog_mismatch", "/catalog/ready") from None
    matches = [
        handle
        for handle in action.handles
        if handle.get("handle_key") == "ready" and handle.get("io_type") == io_type
    ]
    if len(matches) != 1:
        raise _CompositeFailure("composite_catalog_mismatch", "/catalog/ready")
    return str(matches[0]["uuid"])


def _boundary_handle_uuid(
    handles: Sequence[Mapping[str, Any]],
    name: str,
    io_type: str,
) -> str:
    """取得发布工作流边界唯一业务连接点身份。"""

    matches = [
        handle
        for handle in handles
        if handle.get("handle_key") == name and handle.get("io_type") == io_type
    ]
    if len(matches) != 1:
        raise _CompositeFailure(
            "composite_boundary_mapping_invalid",
            f"/boundary/{name}",
        )
    return str(matches[0]["uuid"])


def _invocation_node(
    *,
    parent_workflow_uuid: str,
    invocation_uuid: str,
    template_uuid: str,
    symbol: str,
    keyword_arguments: Mapping[str, object],
    contract_pin: Mapping[str, Any],
    target_mappings: Mapping[str, Sequence[Mapping[str, str]]],
    source_mappings: Mapping[str, Mapping[str, str]],
    structural_mappings: Mapping[str, Sequence[Mapping[str, str]]],
) -> dict[str, Any]:
    """构造父图中真实存在但不拥有运行时任务权威的调用节点。"""

    composite = {
        "version": 1,
        **_plain(contract_pin),
        "target_mappings": _plain(target_mappings),
        "source_mappings": _plain(source_mappings),
        "structural_mappings": _plain(structural_mappings),
    }
    return {
        "uuid": invocation_uuid,
        "workflow_uuid": parent_workflow_uuid,
        "workflow_node_template_uuid": template_uuid,
        "parent_uuid": None,
        "name": symbol,
        "status": "idle",
        "type": "workflow",
        "pose": {},
        "param": _plain(keyword_arguments),
        "execution_policy": {},
        "disabled": False,
        "minimized": False,
        "meta_data": {"unilab": {"composite": composite}},
    }


def _referenced_templates(
    catalog: AuthoringCatalogSnapshot,
    *,
    template_action: AuthoringCatalogAction,
    nodes: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回调用节点和内部节点实际引用的去重模板/连接点全集。"""

    actions = {str(template_action.template["uuid"]): template_action}
    for node in nodes:
        try:
            action = catalog.require_template(str(node["workflow_node_template_uuid"]))
        except (AuthoringCatalogError, KeyError):
            raise _CompositeFailure("composite_catalog_mismatch", "/catalog/templates")
        actions[str(action.template["uuid"])] = action
    node_templates = [
        actions[key].detached_template() for key in sorted(actions)
    ]
    handles = [
        handle
        for key in sorted(actions)
        for handle in actions[key].detached_handles()
    ]
    return node_templates, handles


def _failed_expansion(code: str, path: str) -> CompositeExpansion:
    """构造不含任何候选事实的单诊断失败结果。"""

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
                "message": "组合工作流创作合同校验失败",
            },
        ),
    )


def _canonical_uuid(value: Any, code: str, path: str) -> str:
    """校验规范 UUID 并映射为稳定组合诊断。"""

    try:
        identity = validate_uuid(value)
    except (TypeError, ValueError):
        raise _CompositeFailure(code, path) from None
    if identity != value:
        raise _CompositeFailure(code, path)
    return identity


def _mapping(value: Any, path: str) -> dict[str, Any]:
    """复制必填 JSON 对象并在形状非法时关闭失败。"""

    if not isinstance(value, Mapping):
        raise _CompositeFailure("composite_boundary_mapping_invalid", path)
    return _plain(value)


def _sequence(value: Any, path: str) -> list[Any]:
    """复制必填 JSON 数组并在形状非法时关闭失败。"""

    if not isinstance(value, list):
        raise _CompositeFailure("composite_boundary_mapping_invalid", path)
    return _plain(value)


def _schema_object(value: Any) -> dict[str, Any]:
    """把目录中对象或 JSON 文本 Schema 规范为分离字典。"""

    if isinstance(value, Mapping):
        return _plain(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            decoded = None
        if isinstance(decoded, dict):
            return decoded
    raise _CompositeFailure("composite_catalog_mismatch", "/catalog/schema")


def _plain(value: Any) -> Any:
    """递归复制冻结映射/元组为普通 JSON 容器。"""

    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


__all__ = [
    "CompositeAuthoring",
    "CompositeExpansion",
    "PublishedWorkflowResolver",
    "PublishedWorkflowSnapshotProvider",
]
