"""安装包工作流的精确任意 DAG sidecar 激活深模块。"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from typing import Any

from unilabos.workflow.models import WorkflowEdgeWrite, WorkflowNodeWrite
from unilabos.workflow.source_workspace import (
    SourceWorkspaceError,
    read_declared_exact_graph,
)


class ExactGraphSidecarError(RuntimeError):
    """sidecar 来源、语义映射或公开持久化合同失败。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def load_declared_exact_graph(registration: object) -> dict[str, Any] | None:
    """从冻结注册读取并验证可选 sidecar，且关闭式检查内容哈希。"""

    relative_path = getattr(registration, "exact_graph_relative_path", None)
    expected_hash = getattr(registration, "exact_graph_content_hash", None)
    if relative_path is None and expected_hash is None:
        return None
    if not isinstance(relative_path, str) or not isinstance(expected_hash, str):
        raise ExactGraphSidecarError("exact_graph_declaration_invalid")
    package_root = getattr(registration, "package_root", None)
    package_root_identity = getattr(registration, "package_root_identity", None)
    if (
        not isinstance(package_root_identity, tuple)
        or len(package_root_identity) != 2
        or not all(isinstance(item, int) for item in package_root_identity)
    ):
        raise ExactGraphSidecarError("exact_graph_declaration_invalid")
    try:
        payload = read_declared_exact_graph(
            package_root=package_root,
            package_root_identity=package_root_identity,
            relative_path=relative_path,
        )
    except (AttributeError, OSError, SourceWorkspaceError):
        raise ExactGraphSidecarError("exact_graph_source_invalid") from None
    if f"sha256:{hashlib.sha256(payload).hexdigest()}" != expected_hash:
        raise ExactGraphSidecarError("exact_graph_content_changed")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise ExactGraphSidecarError("exact_graph_json_invalid") from None
    _validate_five_set(document)
    return document


def build_exact_graph_from_live(
    *,
    workflow_uuid: str,
    sidecar: Mapping[str, object],
    live_graph: Mapping[str, object],
) -> dict[str, Any]:
    """用 live 模板 handle 重建边，并合并可信 sidecar 的节点展示字段。"""

    _validate_five_set(sidecar)
    _validate_five_set(live_graph)
    side_workflow = _mapping(sidecar["workflow"])
    live_workflow = dict(_mapping(live_graph["workflow"]))
    if side_workflow.get("uuid") != workflow_uuid or live_workflow.get(
        "uuid"
    ) != workflow_uuid:
        raise ExactGraphSidecarError("exact_graph_workflow_mismatch")

    side_nodes = _uuid_map(sidecar["nodes"], "node")
    live_nodes = _uuid_map(live_graph["nodes"], "node")
    if set(side_nodes) != set(live_nodes):
        raise ExactGraphSidecarError("exact_graph_node_set_mismatch")
    for node_uuid, side_node in side_nodes.items():
        live_node = live_nodes[node_uuid]
        for field in ("type", "action_name", "material_uuid"):
            if side_node.get(field) != live_node.get(field):
                raise ExactGraphSidecarError("exact_graph_node_semantics_mismatch")
        if _semantic_param(side_node) != _semantic_param(live_node):
            raise ExactGraphSidecarError("exact_graph_node_semantics_mismatch")
        if _executor_binding(side_node) != _executor_binding(live_node):
            raise ExactGraphSidecarError("exact_graph_executor_mismatch")

    merged_nodes: list[dict[str, Any]] = []
    for node_uuid, live_node in live_nodes.items():
        side_node = side_nodes[node_uuid]
        merged_node = copy.deepcopy(dict(live_node))
        merged_node["description"] = copy.deepcopy(side_node.get("description"))
        side_metadata = _mapping(side_node.get("meta_data", {}))
        live_metadata = _mapping(live_node.get("meta_data", {}))
        merged_metadata = {
            str(key): copy.deepcopy(value)
            for key, value in side_metadata.items()
            if key != "unilab"
        }
        if "unilab" in live_metadata:
            merged_metadata["unilab"] = copy.deepcopy(live_metadata["unilab"])
        merged_node["meta_data"] = merged_metadata
        merged_nodes.append(merged_node)

    side_handles = _uuid_map(sidecar["handle_templates"], "handle")
    live_handles = [_mapping(item) for item in _array(live_graph["handle_templates"])]

    def remap(edge: Mapping[str, object], endpoint: str) -> str:
        node_uuid = str(edge[f"{endpoint}_node_uuid"])
        try:
            side_handle = side_handles[str(edge[f"{endpoint}_handle_uuid"])]
            live_node = live_nodes[node_uuid]
        except KeyError:
            raise ExactGraphSidecarError("exact_graph_endpoint_invalid") from None
        descriptor = _handle_descriptor(side_handle)
        matches = [
            str(handle["uuid"])
            for handle in live_handles
            if handle.get("workflow_node_template_uuid")
            == live_node.get("workflow_node_template_uuid")
            and _handle_descriptor(handle) == descriptor
        ]
        if len(matches) != 1:
            raise ExactGraphSidecarError("exact_graph_handle_ambiguous")
        return matches[0]

    edges: list[dict[str, Any]] = []
    for item in _array(sidecar["edges"]):
        edge = dict(_mapping(item))
        edge["source_handle_uuid"] = remap(edge, "source")
        edge["target_handle_uuid"] = remap(edge, "target")
        edges.append(edge)
    if len({str(edge.get("uuid")) for edge in edges}) != len(edges):
        raise ExactGraphSidecarError("exact_graph_edge_identity_duplicate")

    live_metadata = dict(_mapping(live_workflow.get("meta_data", {})))
    side_metadata = _mapping(side_workflow.get("meta_data", {}))
    for key, value in side_metadata.items():
        if key != "unilab":
            live_metadata[str(key)] = copy.deepcopy(value)
    live_workflow["meta_data"] = live_metadata
    return {
        "workflow": live_workflow,
        "nodes": merged_nodes,
        "edges": edges,
        "node_templates": copy.deepcopy(_array(live_graph["node_templates"])),
        "handle_templates": copy.deepcopy(_array(live_graph["handle_templates"])),
    }


def apply_declared_exact_graph(*, service: object, registration: object) -> dict[str, Any]:
    """在源码成功激活后 CAS 应用、GET 验证 sidecar 与公开 metadata。"""

    sidecar = load_declared_exact_graph(registration)
    if sidecar is None:
        return {"status": "not_declared"}
    workflow_uuid = getattr(registration, "workflow_uuid", None)
    if not isinstance(workflow_uuid, str):
        raise ExactGraphSidecarError("exact_graph_declaration_invalid")
    live = service.get_graph(workflow_uuid)
    exact = build_exact_graph_from_live(
        workflow_uuid=workflow_uuid,
        sidecar=sidecar,
        live_graph=live,
    )
    changed = not _write_graph_equal(live, exact)
    if changed:
        revision = _mapping(live["workflow"]).get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool):
            raise ExactGraphSidecarError("exact_graph_revision_invalid")
        service.save_graph(
            workflow_uuid,
            revision=revision,
            nodes=exact["nodes"],
            edges=exact["edges"],
        )
    roundtrip = service.get_graph(workflow_uuid)
    if not _write_graph_equal(roundtrip, exact):
        raise ExactGraphSidecarError("exact_graph_roundtrip_mismatch")

    workflow = service.get_workflow(workflow_uuid)
    desired_metadata = _mapping(exact["workflow"]).get("meta_data", {})
    metadata_changed = workflow.get("meta_data") != desired_metadata
    if metadata_changed:
        service.update_workflow(
            workflow_uuid,
            name=workflow["name"],
            tags=workflow.get("tags", []),
            description=workflow.get("description"),
            meta_data=desired_metadata,
        )
    final = service.get_graph(workflow_uuid)
    if not _write_graph_equal(final, exact):
        raise ExactGraphSidecarError("exact_graph_roundtrip_mismatch")
    if service.get_workflow(workflow_uuid).get("meta_data") != desired_metadata:
        raise ExactGraphSidecarError("exact_graph_metadata_roundtrip_mismatch")
    return {
        "status": "applied" if changed or metadata_changed else "unchanged",
        "node_count": len(exact["nodes"]),
        "edge_count": len(exact["edges"]),
    }


def _validate_five_set(value: object) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "workflow",
        "nodes",
        "edges",
        "node_templates",
        "handle_templates",
    }:
        raise ExactGraphSidecarError("exact_graph_shape_invalid")
    _mapping(value["workflow"])
    for key in ("nodes", "edges", "node_templates", "handle_templates"):
        _array(value[key])


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExactGraphSidecarError("exact_graph_shape_invalid")
    return value


def _array(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ExactGraphSidecarError("exact_graph_shape_invalid")
    return value


def _uuid_map(value: object, kind: str) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for item in _array(value):
        row = _mapping(item)
        identity = row.get("uuid")
        if not isinstance(identity, str) or identity in result:
            raise ExactGraphSidecarError(f"exact_graph_{kind}_identity_invalid")
        result[identity] = row
    return result


def _handle_descriptor(handle: Mapping[str, object]) -> tuple[object, ...]:
    return tuple(handle.get(key) for key in ("handle_key", "io_type", "type", "data_key"))


def _semantic_param(node: Mapping[str, object]) -> object:
    value = copy.deepcopy(node.get("param"))
    if node.get("type") == "material_source" and isinstance(value, dict):
        value.pop("resource_template_uuid", None)
    return value


def _executor_binding(node: Mapping[str, object]) -> object:
    metadata = node.get("meta_data")
    unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
    return unilab.get("executor_binding") if isinstance(unilab, Mapping) else None


def _write_graph_equal(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    def normalized(graph: Mapping[str, object], key: str) -> list[dict[str, Any]]:
        model = WorkflowNodeWrite if key == "nodes" else WorkflowEdgeWrite
        return sorted(
            (model.model_validate(item).model_dump(mode="json") for item in _array(graph[key])),
            key=lambda item: str(item["uuid"]),
        )

    return normalized(left, "nodes") == normalized(right, "nodes") and normalized(
        left, "edges"
    ) == normalized(right, "edges")


__all__ = [
    "ExactGraphSidecarError",
    "apply_declared_exact_graph",
    "build_exact_graph_from_live",
    "load_declared_exact_graph",
]
