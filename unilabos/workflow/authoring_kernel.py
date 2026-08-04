"""可信工作流创作编译内核（Authoring Kernel）的窄公共接口。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from unilabos.workflow.models import CandidateCompilation, validate_uuid


class AuthoringCatalogError(ValueError):
    """工作流创作目录（Authoring Catalog）不完整或不一致。"""


@dataclass(frozen=True, slots=True)
class AuthoringCatalogAction:
    """一个动作节点模板及其不可变连接点（Handle）集合。"""

    template: Mapping[str, Any]
    handles: tuple[Mapping[str, Any], ...]

    def detached_template(self) -> dict[str, Any]:
        """返回不共享容器的节点模板投影。

        返回值：可安全写入候选图的普通 JSON 字典；修改它不会污染目录快照。
        """

        return _detach(self.template)

    def detached_handles(self) -> list[dict[str, Any]]:
        """返回不共享容器的连接点（Handle）模板投影列表。"""

        return [_detach(handle) for handle in self.handles]


@dataclass(frozen=True, slots=True)
class AuthoringCatalogSnapshot:
    """编译期间使用的不可变、带指纹目录快照（Catalog Snapshot）。"""

    fingerprint: str
    actions: tuple[AuthoringCatalogAction, ...]
    _by_business_key: Mapping[tuple[str, str], AuthoringCatalogAction]
    _by_template_uuid: Mapping[str, AuthoringCatalogAction]

    @classmethod
    def from_entities(
        cls,
        node_templates: Sequence[Mapping[str, Any]],
        handle_templates: Sequence[Mapping[str, Any]],
    ) -> AuthoringCatalogSnapshot:
        """从后端形状目录实体建立不可变快照。

        参数说明：``node_templates`` 是完整节点模板集合，``handle_templates``
        是完整连接点集合。返回值按稳定 JSON 计算 SHA-256 指纹；重复业务身份、
        重复 UUID 或孤儿连接点会抛出 ``AuthoringCatalogError``。
        """

        nodes = [_json_mapping(item, "节点模板") for item in node_templates]
        handles = [_json_mapping(item, "连接点模板") for item in handle_templates]
        node_ids = [_required_uuid(item, "uuid") for item in nodes]
        handle_ids = [_required_uuid(item, "uuid") for item in handles]
        if len(set(node_ids)) != len(node_ids) or len(set(handle_ids)) != len(handle_ids):
            raise AuthoringCatalogError("工作流创作目录包含重复 UUID")

        handles_by_parent: dict[str, list[dict[str, Any]]] = {
            node_uuid: [] for node_uuid in node_ids
        }
        for handle in handles:
            parent_uuid = _required_uuid(handle, "workflow_node_template_uuid")
            if parent_uuid not in handles_by_parent:
                raise AuthoringCatalogError("连接点模板引用未知节点模板")
            handles_by_parent[parent_uuid].append(handle)

        actions: list[AuthoringCatalogAction] = []
        by_business_key: dict[tuple[str, str], AuthoringCatalogAction] = {}
        by_template_uuid: dict[str, AuthoringCatalogAction] = {}
        for node in sorted(nodes, key=lambda item: str(item["uuid"])):
            class_identity = node.get("class")
            action_name = node.get("name")
            if not isinstance(class_identity, str) or not class_identity.strip():
                raise AuthoringCatalogError("节点模板缺少设备类身份")
            if not isinstance(action_name, str) or not action_name.strip():
                raise AuthoringCatalogError("节点模板缺少动作业务名")
            node_uuid = str(node["uuid"])
            action = AuthoringCatalogAction(
                template=_freeze(node),
                handles=tuple(
                    _freeze(handle)
                    for handle in sorted(
                        handles_by_parent[node_uuid],
                        key=lambda item: str(item["uuid"]),
                    )
                ),
            )
            business_key = (class_identity, action_name)
            if business_key in by_business_key:
                raise AuthoringCatalogError("工作流创作目录动作业务身份重复")
            by_business_key[business_key] = action
            by_template_uuid[node_uuid] = action
            actions.append(action)

        payload = {
            "node_templates": sorted(nodes, key=lambda item: str(item["uuid"])),
            "handle_templates": sorted(handles, key=lambda item: str(item["uuid"])),
        }
        fingerprint = "sha256:" + hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return cls(
            fingerprint=fingerprint,
            actions=tuple(actions),
            _by_business_key=MappingProxyType(by_business_key),
            _by_template_uuid=MappingProxyType(by_template_uuid),
        )

    def require_action(
        self,
        class_identity: str,
        action_name: str,
    ) -> AuthoringCatalogAction:
        """按设备类和动作业务名取得唯一目录动作。

        参数说明：两个字符串来自纯 AST（抽象语法树）静态解析；返回不可变动作
        aggregate，缺失时抛出 ``AuthoringCatalogError``，不进行模糊匹配。
        """

        try:
            return self._by_business_key[(class_identity, action_name)]
        except (KeyError, TypeError):
            raise AuthoringCatalogError("工作流创作目录缺少动作身份") from None

    def require_template(self, template_uuid: str) -> AuthoringCatalogAction:
        """按节点模板 UUID 取得唯一目录动作。

        参数说明：``template_uuid`` 来自候选图；返回不可变目录动作，未知或非法
        UUID 抛出 ``AuthoringCatalogError``。
        """

        try:
            return self._by_template_uuid[validate_uuid(template_uuid)]
        except (KeyError, TypeError, ValueError):
            raise AuthoringCatalogError("工作流创作目录缺少模板 UUID") from None


class AuthoringKernel(Protocol):
    """工作流服务（WorkflowService）可依赖的纯创作编译接口。"""

    compiler_version: str
    template_catalog_fingerprint: str

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> CandidateCompilation:
        """把作者源码静态编译为候选结果（CandidateCompilation）。"""

    def generate_python(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        graph: dict[str, Any],
        source_uri: str,
    ) -> CandidateCompilation:
        """把候选图确定性生成规范 Python 源码。"""

    def validate(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        graph: dict[str, Any],
        python_source: str,
        source_uri: str,
    ) -> CandidateCompilation:
        """共同验证候选图和源码是否语义等价。"""


def _required_uuid(entity: Mapping[str, Any], field: str) -> str:
    """读取目录实体中的必填 UUID。

    参数说明：``entity`` 是目录实体，``field`` 是字段名；返回规范 UUID，缺失
    或非法时抛出 ``AuthoringCatalogError``。
    """

    try:
        return validate_uuid(entity[field])
    except (KeyError, TypeError, ValueError):
        raise AuthoringCatalogError(f"目录字段 {field} 不是有效 UUID") from None


def _json_mapping(entity: Mapping[str, Any], label: str) -> dict[str, Any]:
    """把调用方目录实体复制为普通 JSON 字典。

    参数说明：``entity`` 是待复制映射，``label`` 用于错误说明；返回完全分离的
    字典，不可 JSON 编码时抛出 ``AuthoringCatalogError``。
    """

    if not isinstance(entity, Mapping):
        raise AuthoringCatalogError(f"{label}必须是对象")
    try:
        return json.loads(
            json.dumps(
                dict(entity),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
            )
        )
    except (TypeError, ValueError):
        raise AuthoringCatalogError(f"{label}必须是 JSON 对象") from None


def _freeze(value: Any) -> Any:
    """递归冻结 JSON 值。

    参数说明：``value`` 是已验证 JSON 值；对象变为只读映射、数组变为元组，
    标量保持不变，返回值不与调用方共享可变容器。
    """

    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def _detach(value: Any) -> Any:
    """把冻结 JSON 值递归还原为普通容器。

    参数说明：``value`` 来自目录快照；返回可写字典/列表副本，供候选图持有。
    """

    if isinstance(value, Mapping):
        return {key: _detach(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_detach(child) for child in value]
    return value


__all__ = [
    "AuthoringCatalogAction",
    "AuthoringCatalogError",
    "AuthoringCatalogSnapshot",
    "AuthoringKernel",
]
