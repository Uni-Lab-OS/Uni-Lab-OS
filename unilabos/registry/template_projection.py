"""把完成构建的设备注册表（Registry）发布为工作流创作模板投影。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from unilabos.registry.action_template_projection import (
    ActionTemplateProjectionError,
    compile_action_template_handles,
    goal_parameter_schema,
)
from unilabos.registry.template_identity_projection import (
    ResourceTemplateIdentityProjectionError,
    embed_resource_template_identities,
    extract_resource_template_identities,
)
from unilabos.registry.template_snapshot import (
    RegistryTemplateSnapshot,
    RegistryTemplateSnapshotError,
)
from unilabos.workflow.authoring_kernel import (
    AuthoringCatalogError,
    AuthoringCatalogSnapshot,
)
from unilabos.workflow.models import validate_uuid
from unilabos.workflow.source_identity import (
    PythonSourceIdentityError,
    canonical_python_source_identity,
)
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
        try:
            resource_template_symbols = extract_resource_template_identities(nodes)
            self._snapshot = AuthoringCatalogSnapshot.from_entities(
                nodes,
                handles,
                resource_template_symbols=resource_template_symbols,
            )
        except (
            AuthoringCatalogError,
            ResourceTemplateIdentityProjectionError,
        ) as error:
            raise RegistryTemplateProjectionError(str(error)) from error

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
        resource_definitions = registry_snapshot.detached_resources()
        nodes, handles = self._compile(device_definitions)
        resource_template_symbols = self._compile_resource_template_identities(
            resource_definitions
        )
        try:
            nodes = embed_resource_template_identities(
                nodes,
                resource_template_symbols,
            )
        except ResourceTemplateIdentityProjectionError as error:
            raise RegistryTemplateProjectionError(str(error)) from error
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
        try:
            next_snapshot = AuthoringCatalogSnapshot.from_entities(
                persisted_nodes,
                persisted_handles,
                resource_template_symbols=resource_template_symbols,
            )
        except AuthoringCatalogError as error:
            raise RegistryTemplateProjectionError(str(error)) from error
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

    def _compile_resource_template_identities(
        self,
        resource_definitions: Sequence[Mapping[str, Any]],
    ) -> dict[str, str]:
        """冻结源码资源符号到既有资源模板 UUID 的一一映射。

        参数说明：``resource_definitions`` 是同代 Registry 资源模板全集。
        返回：以源码符号为键、本地模板 UUID 为值的新字典；业务唯一名缺失、
        身份解析失败或 UUID 被多个符号复用时关闭式失败。
        """

        # ``template_uuid_by_symbol`` 供工作流源码（Workflow Source）编译使用；
        # ``symbol_by_template_uuid`` 防止两个源码符号静默绑定同一模板身份。
        template_uuid_by_symbol: dict[str, str] = {}
        symbol_by_template_uuid: dict[str, str] = {}
        for definition in sorted(
            resource_definitions,
            key=lambda item: str(item.get("id", "")),
        ):
            resource_name = definition.get("id")
            class_definition = definition.get("class")
            source_symbol = definition.get("source_fqid")
            if not source_symbol and isinstance(class_definition, Mapping):
                source_symbol = class_definition.get("module")
            if not isinstance(resource_name, str) or not resource_name:
                raise RegistryTemplateProjectionError("资源模板缺少业务唯一名称")
            if not isinstance(source_symbol, str) or not source_symbol:
                raise RegistryTemplateProjectionError("资源模板缺少源码身份")
            try:
                source_symbol = canonical_python_source_identity(source_symbol)
            except PythonSourceIdentityError as error:
                raise RegistryTemplateProjectionError(
                    "资源模板源码身份不能安全生成 Python import"
                ) from error
            try:
                template_uuid = validate_uuid(
                    self._resource_template_identity_resolver(resource_name)
                )
            except (KeyError, TypeError, ValueError):
                raise RegistryTemplateProjectionError(
                    f"资源模板身份解析失败: {resource_name}"
                ) from None
            if source_symbol in template_uuid_by_symbol:
                raise RegistryTemplateProjectionError("资源模板源码身份重复")
            previous_symbol = symbol_by_template_uuid.get(template_uuid)
            if previous_symbol is not None and previous_symbol != source_symbol:
                raise RegistryTemplateProjectionError(
                    "资源模板 UUID 不得绑定多个源码身份"
                )
            template_uuid_by_symbol[source_symbol] = template_uuid
            symbol_by_template_uuid[template_uuid] = source_symbol
        return template_uuid_by_symbol

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
        if resource_name == "host_node":
            framework_node, framework_handle = self._compile_material_source(
                resource_template_uuid=resource_template_uuid,
                resource_name=resource_name,
                resource_display_name=resource_display_name,
            )
            nodes.append(framework_node)
            handles.append(framework_handle)
        return nodes, handles

    @staticmethod
    def _compile_material_source(
        *,
        resource_template_uuid: str,
        resource_name: str,
        resource_display_name: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """编译 Host 唯一拥有的物料来源（MaterialSource）框架模板。

        参数说明：``resource_template_uuid`` 是本地 Host 模板稳定身份，名称字段
        用于 HTTP 资源摘要。返回：一个框架节点和一个 source 物料占位符
        （ResourceSlot）；稳定 UUID 由持久投影按业务唯一键复用或首次分配。
        """

        node_business_key = (resource_template_uuid, "material_source")
        node = {
            "resource_template_uuid": resource_template_uuid,
            "name": "material_source",
            "display_name": "Material Source",
            "description": "声明工作流进入边界的物料来源。",
            "class": "unilabos.workflow.authoring:material_source",
            "goal": {},
            "goal_default": {},
            "feedback": {},
            "result": {},
            "schema": None,
            "type": "material_source",
            "node_type": "material_source",
            "meta_data": {
                "unilab": {
                    "framework_owner_only": True,
                    "resource_template": {
                        "uuid": resource_template_uuid,
                        "name": resource_name,
                        "display_name": resource_display_name,
                    },
                }
            },
        }
        handle = {
            "node_business_key": node_business_key,
            "handle_key": "material",
            "io_type": "source",
            "display_name": "Material",
            "description": "向下游节点传递具体物料实例的物料占位符。",
            "type": "ResourceSlot",
            "required": False,
            "data_source": "executor",
            "data_key": "material",
            "meta_data": {
                "unilab": {
                    "value_schema": {"$slot": "ResourceSlot"},
                }
            },
        }
        return node, handle

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
