"""把完成构建的设备注册表（Registry）发布为工作流创作模板投影。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from unilabos.registry.action_template_projection import (
    ActionTemplateProjectionError,
    compile_action_template_handles,
    goal_parameter_schema,
)
from unilabos.registry.template_snapshot import (
    RegistryTemplateSnapshot,
    RegistryTemplateSnapshotError,
)
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.models import validate_uuid
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.template_projection_store import (
    RegistryTemplateProjectionStore,
    TemplateProjectionIdentityConflict,
)


class RegistryTemplateProjectionError(ValueError):
    """设备注册表不能安全投影为规范模板。"""


class RegistryTemplateProjection:
    """发布并提供单代不可变设备动作模板快照的深模块。"""

    def __init__(
        self,
        workflow_store: WorkflowStore,
        *,
        authority_id: str,
        resource_template_identity_resolver: Callable[[str], str],
    ) -> None:
        """装配模板投影及资源模板身份解析器。

        参数说明：``workflow_store`` 保存现有工作流模板表；``authority_id`` 标识
        本地投影来源；``resource_template_identity_resolver`` 把设备注册身份映射为
        稳定资源模板 UUID，解析失败时必须关闭式失败（Fail-closed）。
        """

        self._store = RegistryTemplateProjectionStore(workflow_store)
        self._authority_id = authority_id
        self._resource_template_identity_resolver = (
            resource_template_identity_resolver
        )
        nodes, handles = self._store.load(authority_id=authority_id)
        self._snapshot = AuthoringCatalogSnapshot.from_entities(nodes, handles)

    def refresh(self, registry: Any) -> AuthoringCatalogSnapshot:
        """从一次完整设备注册表快照原子发布新模板代际。

        参数说明：``registry`` 必须提供 ``obtain_registry_device_info``；返回值是
        新的不可变目录快照。规范化或事务失败时，旧内存快照和旧持久投影保持不变。
        """

        try:
            registry_snapshot = (
                registry
                if isinstance(registry, RegistryTemplateSnapshot)
                else RegistryTemplateSnapshot.from_registry(registry)
            )
        except RegistryTemplateSnapshotError as error:
            raise RegistryTemplateProjectionError(str(error)) from error
        device_definitions = registry_snapshot.detached_devices()
        nodes, handles = self._compile(device_definitions)
        try:
            persisted_nodes, persisted_handles = self._store.replace(
                authority_id=self._authority_id,
                node_templates=nodes,
                handle_templates=handles,
            )
        except TemplateProjectionIdentityConflict as error:
            raise RegistryTemplateProjectionError(
                f"模板身份冲突: {error!s}"
            ) from error
        next_snapshot = AuthoringCatalogSnapshot.from_entities(
            persisted_nodes,
            persisted_handles,
        )
        self._snapshot = next_snapshot
        return next_snapshot

    def snapshot(self) -> AuthoringCatalogSnapshot:
        """返回最近一次已提交的不可变模板快照。

        返回值不触发设备注册表扫描、网络读取或数据库重新导入。
        """

        return self._snapshot

    def close(self) -> None:
        """关闭投影持有的本地工作流存储连接。"""

        self._store.close()

    def _compile(
        self,
        device_definitions: Sequence[Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """把完整设备定义规范化为节点和句柄模板候选。

        参数说明：``device_definitions`` 是设备注册表已完成构建的只读快照；返回
        两个完整候选集合，只接收第 2 版强类型动作合同（Action Contract）。
        """

        if not isinstance(device_definitions, Sequence) or isinstance(
            device_definitions, (str, bytes)
        ):
            raise RegistryTemplateProjectionError("设备注册表快照必须是数组")
        nodes: list[dict[str, Any]] = []
        handles: list[dict[str, Any]] = []
        for device in sorted(
            device_definitions,
            key=lambda item: str(item.get("id", "")),
        ):
            node_candidates, handle_candidates = self._compile_device(device)
            nodes.extend(node_candidates)
            handles.extend(handle_candidates)
        return nodes, handles

    def _compile_device(
        self,
        device: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """规范化一个设备定义中的全部强类型动作。

        参数说明：``device`` 是单个 Registry 设备条目；返回该设备的节点和句柄
        候选，旧式或自动生成动作不进入可信工作流创作投影。
        """

        if not isinstance(device, Mapping):
            raise RegistryTemplateProjectionError("设备注册表条目必须是对象")
        resource_identity = device.get("source_fqid") or device.get("id")
        if not isinstance(resource_identity, str) or not resource_identity:
            raise RegistryTemplateProjectionError("设备定义缺少稳定资源身份")
        resource_template_uuid = self._resource_template_identity_resolver(
            resource_identity
        )
        try:
            resource_template_uuid = validate_uuid(resource_template_uuid)
        except (TypeError, ValueError):
            raise RegistryTemplateProjectionError("设备资源模板身份解析失败")

        class_definition = device.get("class")
        if not isinstance(class_definition, Mapping):
            raise RegistryTemplateProjectionError("设备定义缺少类合同")
        class_identity = class_definition.get("module")
        if not isinstance(class_identity, str) or not class_identity:
            raise RegistryTemplateProjectionError("设备定义缺少类身份")
        action_mappings = class_definition.get("action_value_mappings") or {}
        if not isinstance(action_mappings, Mapping):
            raise RegistryTemplateProjectionError("设备动作映射必须是对象")
        resource_name = str(device.get("id") or resource_identity)
        resource_display_name = str(
            device.get("display_name")
            or device.get("displayname")
            or resource_name
        )

        nodes: list[dict[str, Any]] = []
        handles: list[dict[str, Any]] = []
        for action_name in sorted(action_mappings):
            action = action_mappings[action_name]
            if not isinstance(action, Mapping):
                raise RegistryTemplateProjectionError("设备动作定义必须是对象")
            contract_kind = action.get("contract_kind")
            if contract_kind == "invalid_typed":
                diagnostic = action.get("contract_diagnostic")
                diagnostic_message = (
                    diagnostic.get("message")
                    if isinstance(diagnostic, Mapping)
                    else None
                )
                raise RegistryTemplateProjectionError(
                    f"强类型动作合同 {action_name} 无效"
                    + (f": {diagnostic_message}" if diagnostic_message else "")
                )
            if contract_kind != "typed":
                continue
            try:
                node, action_handles = self._compile_action(
                    action_name=str(action_name),
                    action=action,
                    class_identity=class_identity,
                    resource_template_uuid=resource_template_uuid,
                    resource_name=resource_name,
                    resource_display_name=resource_display_name,
                    resource_template_identity_resolver=(
                        self._resource_template_identity_resolver
                    ),
                )
            except ActionTemplateProjectionError as error:
                raise RegistryTemplateProjectionError(str(error)) from error
            nodes.append(node)
            handles.extend(action_handles)
        return nodes, handles

    @staticmethod
    def _compile_action(
        *,
        action_name: str,
        action: Mapping[str, Any],
        class_identity: str,
        resource_template_uuid: str,
        resource_name: str,
        resource_display_name: str,
        resource_template_identity_resolver: Callable[[str], str],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """把一个第 2 版强类型动作合同编译为模板候选。

        参数说明：``action_name`` 与 ``resource_template_uuid`` 构成持久业务身份；
        ``action`` 提供 Schema 和展示字段；``class_identity`` 供 F02 工作流创作编译
        按 Python 设备类解析动作；``resource_name`` 和 ``resource_display_name``
        固化 HTTP 摘要；``resource_template_identity_resolver`` 把动作中的源码资源
        约束解析为本地模板 UUID。返回一个节点模板及其完整控制/数据连接点。
        """

        schema = action.get("schema")
        if not isinstance(schema, Mapping):
            raise RegistryTemplateProjectionError("强类型动作缺少 JSON Schema")
        contract = schema.get("x-unilabos-action-contract")
        if not isinstance(contract, Mapping) or contract.get("version") != 2:
            raise RegistryTemplateProjectionError("只接受第 2 版动作合同")
        node_business_key = (resource_template_uuid, action_name)
        node = {
            "resource_template_uuid": resource_template_uuid,
            "name": action_name,
            "display_name": (
                action.get("display_name") or action.get("displayname") or action_name
            ),
            "description": action.get("description") or schema.get("description"),
            "class": class_identity,
            "goal": dict(action.get("goal") or {}),
            "goal_default": dict(action.get("goal_default") or {}),
            "feedback": dict(action.get("feedback") or {}),
            "result": dict(action.get("result") or {}),
            "schema": goal_parameter_schema(schema),
            "type": action.get("type") or "UniLabJsonCommand",
            "node_type": "ILab",
            "meta_data": {
                "unilab": {
                    "contract_kind": "typed",
                    "action_contract_schema": dict(schema),
                    "resource_template": {
                        "uuid": resource_template_uuid,
                        "name": resource_name,
                        "display_name": resource_display_name,
                    },
                }
            },
        }
        if action.get("uuid") is not None:
            try:
                node["uuid"] = validate_uuid(action["uuid"])
            except (TypeError, ValueError):
                raise RegistryTemplateProjectionError(
                    "节点模板显式 UUID 非法"
                ) from None

        handles = compile_action_template_handles(
            schema,
            node_business_key=node_business_key,
            resource_template_identity_resolver=(
                resource_template_identity_resolver
            ),
        )
        return node, handles


__all__ = [
    "RegistryTemplateProjection",
    "RegistryTemplateProjectionError",
]
