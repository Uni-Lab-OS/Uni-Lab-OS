"""工作流创作候选图的稳定比较与变更语义。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from unilabos.workflow.models import CandidateChangeset


class AuthoringGraphError(ValueError):
    """作者程序无法映射到权威目录或候选图。"""

    def __init__(self, code: str, message: str):
        """保存稳定诊断码和中文消息。

        参数说明：``code`` 供接口判断错误类别，``message`` 供用户理解。返回：
        无；构造后的异常保留两项属性，不改变原始异常文本。
        """

        super().__init__(message)
        self.code = code
        self.message = message


def candidate_changeset(
    *,
    graph: Mapping[str, Any],
    applied_graph: Mapping[str, Any],
) -> dict[str, Any]:
    """计算候选图相对已应用图的精确变更集。

    参数说明：两个图均为后端五集合形状；返回经过 ``CandidateChangeset`` 校验
    的规范字典，数组按 UUID 排序，目录投影变化不独立计入生命周期集合。
    """

    candidate = graph_containers(graph)
    applied = graph_containers(applied_graph)
    candidate_nodes = _semantic_entities(candidate["nodes"])
    applied_nodes = _semantic_entities(applied["nodes"])
    candidate_edges = _semantic_entities(candidate["edges"])
    applied_edges = _semantic_entities(applied["edges"])
    expected = {
        "created_node_uuids": sorted(set(candidate_nodes) - set(applied_nodes)),
        "updated_node_uuids": sorted(
            identity
            for identity in set(candidate_nodes) & set(applied_nodes)
            if candidate_nodes[identity] != applied_nodes[identity]
        ),
        "deleted_node_uuids": sorted(set(applied_nodes) - set(candidate_nodes)),
        "created_edge_uuids": sorted(set(candidate_edges) - set(applied_edges)),
        "updated_edge_uuids": sorted(
            identity
            for identity in set(candidate_edges) & set(applied_edges)
            if candidate_edges[identity] != applied_edges[identity]
        ),
        "deleted_edge_uuids": sorted(set(applied_edges) - set(candidate_edges)),
    }
    candidate_unilab = (candidate["workflow"].get("meta_data") or {}).get("unilab")
    applied_unilab = (applied["workflow"].get("meta_data") or {}).get("unilab")
    reserved_changed = canonical_json(candidate_unilab) != canonical_json(
        applied_unilab
    )
    graph_changed = reserved_changed or any(expected.values())
    return CandidateChangeset.model_validate(
        {
            "kind": "graph" if graph_changed else "source_only",
            **expected,
            "reserved_metadata_changed": reserved_changed,
        }
    ).model_dump()


def semantic_graph_equal(left: Any, right: Any) -> bool:
    """比较两个候选图的创作语义而忽略数组顺序和投影时间。

    参数说明：``left`` 和 ``right`` 是待比较对象；结构非法时返回 ``False``，
    合法时比较工作流、节点、边以及目录实体的规范 JSON。
    """

    try:
        return _semantic_graph(left) == _semantic_graph(right)
    except (KeyError, TypeError, ValueError):
        return False


def graph_containers(graph: Mapping[str, Any]) -> dict[str, Any]:
    """复制并验证工作流图五个顶层集合。

    参数说明：``graph`` 必须是映射并含 workflow/nodes/edges/node_templates/
    handle_templates；返回深拷贝，结构非法抛出 ``AuthoringGraphError``。
    """

    required = {"workflow", "nodes", "edges", "node_templates", "handle_templates"}
    if not isinstance(graph, Mapping) or set(graph) != required:
        raise AuthoringGraphError("candidate_invalid", "工作流图必须包含完整五集合")
    copied = deepcopy(dict(graph))
    if not isinstance(copied["workflow"], dict) or any(
        not isinstance(copied[field], list) for field in required - {"workflow"}
    ):
        raise AuthoringGraphError("candidate_invalid", "工作流图集合类型无效")
    return copied


def canonical_json(value: Any) -> str:
    """把 JSON 值编码为稳定比较字符串。

    参数说明：``value`` 是候选语义；返回排序、紧凑且禁止 NaN 的 JSON。
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _semantic_entities(values: list[dict[str, Any]]) -> dict[str, str]:
    """按 UUID 索引实体的稳定创作语义。

    参数说明：``values`` 是节点、边或目录实体数组；返回 UUID 到规范 JSON 的
    映射，忽略数据库时间和所属工作流投影字段。
    """

    result: dict[str, str] = {}
    for value in values:
        identity = str(value["uuid"])
        semantic = {
            key: child
            for key, child in value.items()
            if key not in {"create_time", "update_time", "workflow_uuid"}
        }
        result[identity] = canonical_json(semantic)
    return result


def _semantic_graph(graph: Mapping[str, Any]) -> str:
    """生成忽略投影时间与数组顺序的候选图规范 JSON。

    参数说明：``graph`` 是后端五集合形状；返回稳定 JSON 字符串。
    """

    value = graph_containers(graph)
    workflow = {
        key: child
        for key, child in value["workflow"].items()
        if key not in {"create_time", "update_time"}
    }
    payload = {
        "workflow": workflow,
        "nodes": sorted(_semantic_entities(value["nodes"]).values()),
        "edges": sorted(_semantic_entities(value["edges"]).values()),
        "node_templates": sorted(_semantic_entities(value["node_templates"]).values()),
        "handle_templates": sorted(
            _semantic_entities(value["handle_templates"]).values()
        ),
    }
    return canonical_json(payload)


__all__ = [
    "AuthoringGraphError",
    "candidate_changeset",
    "canonical_json",
    "graph_containers",
    "semantic_graph_equal",
]
