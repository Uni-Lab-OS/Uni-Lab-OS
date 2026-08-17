"""工作流源码（Workflow Source）路径与代码标签的纯合同模块。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

WORKFLOW_TAG_LIMIT = 16
WORKFLOW_TAG_LENGTH_LIMIT = 64


class WorkflowTagError(ValueError):
    """表示路径、代码或候选标签不能证明同一规范并集。"""


def normalize_workflow_tags(raw_tags: object) -> tuple[str, ...]:
    """把有序标签序列规范为首次出现去重的不可变元组。

    参数：``raw_tags`` 是路径或 ``@workflow`` 提供的列表/元组。返回：保持首次
    出现顺序的字符串元组。异常：类型、空白、单项长度或去重后数量违反合同时
    抛出 ``WorkflowTagError``。
    """

    if not isinstance(raw_tags, (list, tuple)):
        raise WorkflowTagError("工作流标签必须是字符串序列")
    if any(
        not isinstance(tag, str)
        or not tag
        or tag != tag.strip()
        or len(tag) > WORKFLOW_TAG_LENGTH_LIMIT
        for tag in raw_tags
    ):
        raise WorkflowTagError("工作流标签必须是规范非空字符串")
    normalized_tags = tuple(dict.fromkeys(raw_tags))
    if len(normalized_tags) > WORKFLOW_TAG_LIMIT:
        raise WorkflowTagError("工作流标签超过上限")
    return normalized_tags


def merge_workflow_tags(*tag_groups: Sequence[str]) -> tuple[str, ...]:
    """按调用方给定优先级合并多组工作流标签。

    参数：``tag_groups`` 是已经有序的路径与代码标签组。返回：跨组首次出现去重的
    标签元组。异常：任一组或最终并集违反标签合同时抛出 ``WorkflowTagError``。
    """

    normalized_groups = tuple(normalize_workflow_tags(group) for group in tag_groups)
    return normalize_workflow_tags(
        tuple(tag for group in normalized_groups for tag in group)
    )


def resolved_source_workflow_tags(
    *,
    unilab_meta: Mapping[str, Any],
    declared_tags: tuple[str, ...],
) -> list[str] | None:
    """解析包来源拥有的路径与代码标签候选。

    参数：``unilab_meta`` 是已应用工作流的来源元数据；``declared_tags`` 是 AST
    读取的代码标签。返回：包来源的路径优先标签并集；非包来源返回 ``None``，
    保留数据库标签权威。异常：来源路径标签失真时抛出 ``WorkflowTagError``。
    """

    source_bootstrap = unilab_meta.get("source_bootstrap")
    if not (
        isinstance(source_bootstrap, Mapping)
        and source_bootstrap.get("kind") == "editable_package_manifest"
    ):
        return None
    path_tags = normalize_workflow_tags(source_bootstrap.get("path_tags"))
    return list(merge_workflow_tags(path_tags, declared_tags))


def workflow_authoring_tags(workflow: Mapping[str, Any]) -> tuple[str, ...]:
    """读取只应写回 ``@workflow`` 的显式代码标签。

    参数：``workflow`` 是后端形态（Backend-shaped）工作流投影。返回：来源编译
    已明确记录的代码标签；缺少新字段时返回空元组，避免把历史数据库或路径标签
    写入源码。异常：字段形状无效时抛出 ``WorkflowTagError``。
    """

    meta_data = workflow.get("meta_data") or {}
    unilab_meta = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    if not isinstance(unilab_meta, Mapping) or "authoring_tags" not in unilab_meta:
        return ()
    return normalize_workflow_tags(unilab_meta["authoring_tags"])


def has_package_source_tag_contract(workflow: Mapping[str, Any]) -> bool:
    """判断工作流投影是否受可编辑包路径标签合同约束。

    参数：``workflow`` 是候选或已应用工作流投影。返回：来源证据声明为
    ``editable_package_manifest`` 时为真；缺失或其他来源类型时为假。异常：无。
    """

    source_bootstrap = _unilab_metadata(workflow).get("source_bootstrap")
    return (
        isinstance(source_bootstrap, Mapping)
        and source_bootstrap.get("kind") == "editable_package_manifest"
    )


def validate_package_source_tag_change(
    *,
    candidate: Mapping[str, Any],
    base: Mapping[str, Any],
) -> None:
    """证明候选标签变化完全来自既有包路径与代码标签。

    参数：``candidate`` 是待签发工作流投影；``base`` 是不可伪造的已应用基线。
    返回：证明成立时无返回值。异常：候选改变来源证据或标签不等于有序并集时
    抛出 ``WorkflowTagError``。
    """

    candidate_unilab = _unilab_metadata(candidate)
    base_unilab = _unilab_metadata(base)
    source_bootstrap = candidate_unilab.get("source_bootstrap")
    if source_bootstrap != base_unilab.get("source_bootstrap"):
        raise WorkflowTagError("候选结果改变了包来源路径标签证据")
    if not (
        isinstance(source_bootstrap, Mapping)
        and source_bootstrap.get("kind") == "editable_package_manifest"
    ):
        raise WorkflowTagError("候选结果缺少包来源标签证据")
    path_tags = normalize_workflow_tags(source_bootstrap.get("path_tags"))
    # 旧编译器没有该元数据时等价于未声明代码标签；只要最终标签仍精确等于路径
    # 标签即可兼容，任何额外最终标签仍会在并集比较处失败关闭。
    authoring_tags = normalize_workflow_tags(
        candidate_unilab.get("authoring_tags", ())
    )
    resolved_tags = list(merge_workflow_tags(path_tags, authoring_tags))
    if candidate.get("tags") != resolved_tags:
        raise WorkflowTagError("候选结果标签不等于包路径与代码标签并集")


def _unilab_metadata(workflow: Mapping[str, Any]) -> Mapping[str, Any]:
    """读取工作流投影中的 Uni-Lab 保留元数据。

    参数：``workflow`` 是候选或已应用工作流投影。返回：存在且为映射时的
    ``meta_data.unilab``，否则返回空映射；本函数不修改调用方容器。
    """

    meta_data = workflow.get("meta_data")
    unilab_meta = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    return unilab_meta if isinstance(unilab_meta, Mapping) else {}


__all__ = [
    "WorkflowTagError",
    "has_package_source_tag_contract",
    "merge_workflow_tags",
    "normalize_workflow_tags",
    "resolved_source_workflow_tags",
    "validate_package_source_tag_change",
    "workflow_authoring_tags",
]
