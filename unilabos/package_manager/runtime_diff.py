"""工作区运行代差异分类与稳定指纹。"""

from __future__ import annotations

import hashlib
from typing import Any, Protocol

import rfc8785

from .runtime_activation import WorkspaceRegistryRuntime


class WorkspaceGenerationIdentity(Protocol):
    """差异分类所需的最小工作区输入代接口。"""

    identity: str
    dependency_revision: str


def candidate_fingerprint(
    candidate: Any,
    generation: WorkspaceGenerationIdentity,
) -> str:
    """读取候选代稳定指纹并纳入显式依赖文件修订。

    参数：``candidate`` 是完整准备结果；``generation`` 提供输入代身份，以及
    监视器可选提交的依赖修订。
    返回：覆盖注册表快照（Registry Snapshot）、物理图（Graph）和显式依赖文件
    原始字节修订的稳定摘要；通用测试候选退回输入代身份。
    异常：无；正式候选缺少内建依赖修订时兼容使用输入代修订。
    """

    if isinstance(candidate, WorkspaceRegistryRuntime):
        # ``generation_payload`` 覆盖候选真正编译观察到的完整稳定输入。
        generation_payload = {
            "dependency_revision": _dependency_revision(candidate, generation),
            "graph": candidate.graph_copy(),
            "registry_snapshot": candidate.registry_snapshot.fingerprint,
        }
        return "sha256:" + hashlib.sha256(rfc8785.dumps(generation_payload)).hexdigest()
    return f"{generation.identity}:{generation.dependency_revision}"


def restart_reasons(
    *,
    previous: Any,
    candidate: Any,
    previous_input: WorkspaceGenerationIdentity,
    candidate_input: WorkspaceGenerationIdentity,
) -> tuple[str, ...]:
    """判断候选变化是否越过可热发布安全边界。

    参数：``previous`` 与 ``candidate`` 是完整旧/新候选；``previous_input`` 与
    ``candidate_input`` 是其输入代身份兼容后备。
    返回：稳定排序且去重的关闭重启原因集合；空集合表示可原子热发布。
    异常：无；未知候选类型保守返回驱动实现变化，禁止猜测热发布。
    """

    if not isinstance(previous, WorkspaceRegistryRuntime) or not isinstance(
        candidate,
        WorkspaceRegistryRuntime,
    ):
        return ("active_driver_implementation_changed",)
    reasons: set[str] = set()
    if previous.graph_copy() != candidate.graph_copy():
        reasons.add("graph_changed")
    if _dependency_revision(previous, previous_input) != _dependency_revision(
        candidate,
        candidate_input,
    ):
        reasons.add("binary_dependencies_changed")

    previous_devices = {
        definition.fqid: definition
        for definition in previous.registry_snapshot.devices
    }
    candidate_devices = {
        definition.fqid: definition
        for definition in candidate.registry_snapshot.devices
    }
    # ``active_device_fqids`` 来自旧活跃物理图，未选定义变化不触碰设备。
    active_device_fqids = {item.fqid for item in previous.activation_plan.devices}
    for fqid in active_device_fqids:
        old_definition = previous_devices.get(fqid)
        new_definition = candidate_devices.get(fqid)
        if old_definition is None or new_definition is None:
            reasons.add("active_driver_implementation_changed")
            continue
        if _action_contract(old_definition) != _action_contract(new_definition):
            reasons.add("active_action_contract_changed")
        elif _implementation_identity(old_definition) != _implementation_identity(
            new_definition
        ):
            reasons.add("active_driver_implementation_changed")

    previous_resources = {
        definition.fqid: definition
        for definition in previous.registry_snapshot.resources
    }
    candidate_resources = {
        definition.fqid: definition
        for definition in candidate.registry_snapshot.resources
    }
    # ``active_resource_fqids`` 是已参与资源树及库位（Site）物化的资源定义。
    active_resource_fqids = {item.fqid for item in previous.activation_plan.resources}
    for fqid in active_resource_fqids:
        old_definition = previous_resources.get(fqid)
        new_definition = candidate_resources.get(fqid)
        if (
            old_definition is None
            or new_definition is None
            or _implementation_identity(old_definition)
            != _implementation_identity(new_definition)
            or old_definition.details != new_definition.details
        ):
            reasons.add("resource_tree_or_site_structure_changed")
    return tuple(sorted(reasons))


def _dependency_revision(
    candidate: WorkspaceRegistryRuntime,
    generation: WorkspaceGenerationIdentity,
) -> str:
    """选择候选实际观察到的依赖文件修订。

    参数：``candidate`` 是正式工作区运行代；``generation`` 是监视器输入代。
    返回：优先使用候选准备阶段读取的依赖声明和锁摘要；旧候选为空时使用监视器
    修订，保持现有 Adapter 与测试兼容。
    异常：无。
    """

    return candidate.dependency_revision or generation.dependency_revision


def _implementation_identity(definition: Any) -> tuple[str, str, str]:
    """读取一个静态定义的作者实现身份。

    参数：``definition`` 是包目录（PackageCatalog）定义。
    返回：模块、符号和声明文件内容摘要三元组。
    异常：无；正式候选已完成目录字段验证。
    """

    return (definition.module, definition.symbol, definition.content_hash)


def _action_contract(definition: Any) -> Any:
    """读取设备定义的规范动作合同（Action Contract）投影。

    参数：``definition`` 是包目录（PackageCatalog）中的设备定义。
    返回：规范 ``action_value_mappings`` 冻结值；缺失时返回空元组。
    异常：无；目录已保证详情只含不可变 JSON 值。
    """

    registry_entry = definition.details.get("registry_entry")
    if not isinstance(registry_entry, dict) and not hasattr(registry_entry, "get"):
        return ()
    class_entry = registry_entry.get("class")
    if not isinstance(class_entry, dict) and not hasattr(class_entry, "get"):
        return ()
    return class_entry.get("action_value_mappings", ())


__all__ = ["candidate_fingerprint", "restart_reasons"]
