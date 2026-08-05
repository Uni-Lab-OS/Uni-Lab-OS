"""已发布工作流（PublishedWorkflow）的生产目录代际构造。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from unilabos.workflow.catalog import PublishedSourceCatalog
from unilabos.workflow.composite import (
    PublishedWorkflowContractError,
    PublishedWorkflowSnapshotProvider,
    project_published_workflow_contract,
)


class PublishedWorkflowGenerationError(RuntimeError):
    """活动来源不能安全构成一个封闭的工作流模板目录代际。"""


@dataclass(frozen=True, slots=True)
class PublishedWorkflowGeneration:
    """同一来源目录摘要下的工作流模板与连接点（Handle）全集。"""

    source_catalog: PublishedSourceCatalog
    node_templates: tuple[dict[str, Any], ...]
    handle_templates: tuple[dict[str, Any], ...]


def build_published_workflow_generation(
    *,
    registrations: Sequence[Mapping[str, Any]],
    snapshot_provider: PublishedWorkflowSnapshotProvider,
    base_node_templates: Sequence[Mapping[str, Any]],
) -> PublishedWorkflowGeneration:
    """从活动授权与同修订应用快照构造完整发布扩展代际。

    参数：``registrations`` 是本次进程活动可编辑包来源；``snapshot_provider``
    只读工作流图和应用源码；``base_node_templates`` 是同次设备目录编译结果，用于
    定位唯一宿主节点（Host Node）所有者。返回：一个来源目录和可追加到同事务
    替换的模板/连接点全集。异常：来源、宿主、应用快照或发布合同不一致时抛出
    ``PublishedWorkflowGenerationError``，不返回部分代际。
    """

    if not isinstance(registrations, Sequence) or isinstance(
        registrations,
        (str, bytes),
    ):
        raise PublishedWorkflowGenerationError("活动工作流来源必须是数组")
    host_summary = _host_summary(base_node_templates)
    snapshots: dict[str, Mapping[str, Any]] = {}
    records: list[dict[str, str]] = []
    for index, registration in enumerate(registrations):
        try:
            workflow_uuid = str(registration["workflow_uuid"])
            package_id = str(registration["package_id"])
            relative_path = str(registration["relative_path"])
            source_uri = str(registration["source_uri"])
        except (KeyError, TypeError):
            raise PublishedWorkflowGenerationError(
                f"活动工作流来源 {index} 字段不完整"
            ) from None
        try:
            snapshot = snapshot_provider.get_published_workflow_snapshot(
                workflow_uuid
            )
        except LookupError:
            # 活动来源指向缺失/软删除定义时不能被猜成发布合同；启动目录保持关闭。
            continue
        if not _eligible(snapshot):
            continue
        workflow = snapshot["workflow"]
        applied_source = snapshot["applied_source"]
        symbol = _authoring_symbol(workflow)
        module = _source_module(package_id, relative_path)
        records.append(
            {
                "workflow_uuid": workflow_uuid,
                "definition_fqid": f"{module}.{symbol}",
                "module": module,
                "symbol": symbol,
                "source_uri": source_uri,
                "definition_content_hash": str(applied_source["source_hash"]),
            }
        )
        snapshots[workflow_uuid] = snapshot
    try:
        source_catalog = PublishedSourceCatalog.from_records(records)
    except (TypeError, ValueError) as error:
        raise PublishedWorkflowGenerationError(str(error)) from error
    nodes: list[dict[str, Any]] = []
    handles: list[dict[str, Any]] = []
    for source in source_catalog.sources:
        try:
            projected = project_published_workflow_contract(
                source=source,
                applied_snapshot=snapshots[source.workflow_uuid],
                host_node_resource_template=host_summary,
            )
        except PublishedWorkflowContractError as error:
            raise PublishedWorkflowGenerationError(error.code) from error
        if projected is None:
            raise PublishedWorkflowGenerationError(
                "发布资格在同一目录构造期间发生漂移"
            )
        nodes.append(projected.template)
        handles.extend(projected.handles)
    return PublishedWorkflowGeneration(
        source_catalog=source_catalog,
        node_templates=tuple(nodes),
        handle_templates=tuple(handles),
    )


def _eligible(snapshot: Mapping[str, Any]) -> bool:
    """判断快照是否具有同修订应用源码且哈希可用于静态发布。

    参数：``snapshot`` 是只读存储回执。返回：工作流、正修订和同修订应用哈希
    完整时为 ``True``。异常：无；不完整值不进入来源目录。
    """

    workflow = snapshot.get("workflow") if isinstance(snapshot, Mapping) else None
    applied = snapshot.get("applied_source") if isinstance(snapshot, Mapping) else None
    if not isinstance(workflow, Mapping) or not isinstance(applied, Mapping):
        return False
    revision = workflow.get("revision")
    source_hash = applied.get("source_hash")
    return (
        isinstance(revision, int)
        and not isinstance(revision, bool)
        and revision >= 1
        and applied.get("workflow_revision") == revision
        and isinstance(source_hash, str)
    )


def _authoring_symbol(workflow: Mapping[str, Any]) -> str:
    """读取已应用工作流唯一作者函数符号。

    参数：``workflow`` 是工作流读投影。返回：Python 标识符。异常：保留元数据
    缺失或符号非法时抛出 ``PublishedWorkflowGenerationError``。
    """

    meta_data = workflow.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    symbol = unilab.get("authoring_function_name") if isinstance(unilab, Mapping) else None
    if not isinstance(symbol, str) or not symbol.isidentifier():
        raise PublishedWorkflowGenerationError("已应用工作流缺少作者函数符号")
    return symbol


def _source_module(package_id: str, relative_path: str) -> str:
    """把已授权包身份和规范相对路径转换为绝对 Python 模块。

    参数：包 ID 与 ``workflows/*.py`` 相对路径来自来源注册。返回：不导入模块的
    静态点分身份。异常：任一分段不是 Python 标识符时抛出发布代际错误。
    """

    path = PurePosixPath(relative_path)
    parts = (package_id, *path.with_suffix("").parts)
    if any(not part.isidentifier() for part in parts):
        raise PublishedWorkflowGenerationError("工作流来源不能转换为绝对模块")
    return ".".join(parts)


def _host_summary(
    node_templates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """从本代框架模板取得唯一宿主资源模板摘要。

    参数：``node_templates`` 是尚未持久化的设备/框架模板候选。返回：含 UUID、
    名称和展示名的分离摘要。异常：缺失或多个宿主摘要时抛出发布代际错误。
    """

    matches: list[dict[str, Any]] = []
    for template in node_templates:
        meta_data = template.get("meta_data")
        unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
        summary = (
            unilab.get("resource_template")
            if isinstance(unilab, Mapping)
            else None
        )
        if isinstance(summary, Mapping) and summary.get("name") == "host_node":
            candidate = {
                "uuid": summary.get("uuid"),
                "name": summary.get("name"),
                "display_name": summary.get("display_name"),
            }
            if candidate not in matches:
                matches.append(candidate)
    if len(matches) != 1:
        raise PublishedWorkflowGenerationError("目录缺少唯一宿主节点所有者")
    return matches[0]


__all__ = [
    "PublishedWorkflowGeneration",
    "PublishedWorkflowGenerationError",
    "build_published_workflow_generation",
]
