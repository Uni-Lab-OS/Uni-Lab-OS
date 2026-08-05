"""同步本地资源模板（ResourceTemplate）身份并发布单代只读解析器。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from unilabos.app.scheduler.inventory.backend_contract import (
    BackendContractError,
    BackendResourceService,
)
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.registry.action_template_projection import (
    ActionTemplateProjectionError,
    compile_action_template_handles,
)
from unilabos.registry.template_projection import RegistryTemplateProjectionError
from unilabos.registry.template_snapshot import RegistryTemplateSnapshot
from unilabos.workflow.models import validate_uuid
from unilabos.workflow.source_identity import (
    PythonSourceIdentityError,
    canonical_python_source_identity,
)

# ``_ACTION_CONTRACT_PREFLIGHT_UUID`` 仅为无副作用动作合同（Action Contract）
# 编译提供格式有效的占位 UUID，不是持久资源模板（ResourceTemplate）身份。
_ACTION_CONTRACT_PREFLIGHT_UUID = "00000000-0000-4000-8000-000000000001"


def synchronize_local_template_identities(
    *,
    inventory_store: InventoryStore,
    registry_snapshot: RegistryTemplateSnapshot,
) -> Callable[[str], str]:
    """原子同步单代本地模板身份并返回关闭式解析器。

    参数说明：``inventory_store`` 是本地库存资源模板写权威；
    ``registry_snapshot`` 是组合根冻结的不可变注册表快照（Registry Snapshot）。
    返回：解析本代注册表（Registry）业务 ID、``source_fqid`` 或
    ``class.module`` 别名的只读函数，未知身份返回空串供投影层关闭式失败。
    异常：业务 ID、源码别名、同步回执或 UUID 不完整、不唯一、不可解析时抛出
    ``RegistryTemplateProjectionError``；所有快照预校验均发生在库存写事务之前。
    """

    # ``template_definitions`` 是本代设备与物料资源模板的完整分离定义。
    template_definitions = registry_snapshot.detached_definitions()
    # ``template_name_by_alias`` 在任何库存写入前证明业务 ID 和源码别名一一归属。
    template_name_by_alias = _prevalidate_template_aliases(template_definitions)
    _prevalidate_action_resource_template_aliases(
        registry_snapshot.detached_devices(),
        template_name_by_alias=template_name_by_alias,
    )
    try:
        # ``synchronization`` 是活动资源模板身份同步事务的规范回执。
        synchronization = (
            BackendResourceService(inventory_store).sync_resource_templates(
                template_definitions
            )
            if template_definitions
            else {"templates": []}
        )
    except BackendContractError as error:
        raise RegistryTemplateProjectionError(
            f"本地资源模板同步失败: {error.message}"
        ) from error

    # ``template_uuid_by_name`` 是经回执验证的活动业务 ID 到稳定 UUID 的全集。
    template_uuid_by_name = _validated_receipt_identities(
        synchronization,
        expected_names=set(template_name_by_alias.values()),
    )
    # ``template_uuid_by_alias`` 只在当前快照代际内存活，不建立第二持久身份权威。
    template_uuid_by_alias = {
        alias: template_uuid_by_name[template_name]
        for alias, template_name in template_name_by_alias.items()
    }

    def resolve_resource_template_identity(resource_identity: str) -> str:
        """解析当前代资源模板业务 ID 或源码别名。

        参数说明：``resource_identity`` 来自同一注册表快照（Registry Snapshot）。
        返回：对应活动资源模板（ResourceTemplate）的稳定 UUID；身份不在本代时
        返回空串，不猜测历史 UUID，也不产生新的库存写入。
        """

        return template_uuid_by_alias.get(resource_identity, "")

    return resolve_resource_template_identity


def _prevalidate_template_aliases(
    template_definitions: list[dict[str, Any]],
) -> dict[str, str]:
    """在库存写入前验证本代模板业务 ID 与全部源码别名。

    参数说明：``template_definitions`` 是同一冻结代际的完整模板定义。返回：每个
    业务 ID、``source_fqid`` 和 ``class.module`` 规范别名到唯一业务 ID 的映射。
    异常：空业务 ID、跨设备/物料重复业务 ID、缺少源码身份、非法 Python 源码
    身份或跨模板别名冲突时抛出 ``RegistryTemplateProjectionError``。
    """

    # ``template_name_by_alias`` 先登记业务 ID，再登记源码别名以检测跨种类冲突。
    template_name_by_alias: dict[str, str] = {}
    for definition in template_definitions:
        # ``template_name`` 是当前定义声明的注册表（Registry）业务 ID。
        template_name = definition.get("id")
        if not isinstance(template_name, str) or not template_name:
            raise RegistryTemplateProjectionError("注册表（Registry）模板缺少业务 ID")
        if template_name in template_name_by_alias:
            raise RegistryTemplateProjectionError(
                f"注册表（Registry）模板业务 ID 重复: {template_name}"
            )
        template_name_by_alias[template_name] = template_name

    for definition in template_definitions:
        # ``template_name`` 是已经通过全集唯一性验证的注册表（Registry）业务 ID。
        template_name = str(definition["id"])
        # ``source_aliases`` 是当前业务 ID 声明且可安全解析的全部源码别名。
        source_aliases = _canonical_source_aliases(definition, template_name)
        for source_alias in source_aliases:
            # ``previous_name`` 是同一别名已登记的业务 ID，用于拒绝跨模板歧义。
            previous_name = template_name_by_alias.get(source_alias)
            if previous_name is not None and previous_name != template_name:
                raise RegistryTemplateProjectionError(
                    "资源模板源码身份不得绑定多个注册表（Registry）业务 ID: "
                    f"{source_alias}"
                )
            template_name_by_alias[source_alias] = template_name
    return template_name_by_alias


def _prevalidate_action_resource_template_aliases(
    device_definitions: Sequence[Mapping[str, Any]],
    *,
    template_name_by_alias: Mapping[str, str],
) -> None:
    """在库存写入前关闭式验证全部动作资源模板源码别名。

    参数说明：``device_definitions`` 是冻结注册表快照（Registry Snapshot）的完整
    设备定义；``template_name_by_alias`` 是已证明唯一的业务 ID/源码别名到资源
    模板（ResourceTemplate）业务 ID 映射。返回：无。异常：设备动作集合、动作
    合同（Action Contract）或任一 ``resource_template_symbols`` 别名无法通过现有
    动作模板编译器解析时，抛出 ``RegistryTemplateProjectionError``；调用者不得
    进入后端（Backend）形态资源模板同步事务。
    """

    def resolve_prevalidated_template_alias(source_alias: str) -> str:
        """把已知源码别名映射为仅供预检使用的有效 UUID。

        参数说明：``source_alias`` 是动作合同声明的资源模板源码身份。返回：别名
        存在于当前注册表（Registry）代际时返回固定有效 UUID，否则返回空串；该
        UUID 只驱动无副作用合同校验，绝不作为持久资源模板身份或同步回执。
        """

        return (
            _ACTION_CONTRACT_PREFLIGHT_UUID
            if source_alias in template_name_by_alias
            else ""
        )

    for device_definition in device_definitions:
        # ``device_name`` 只为错误定位标识当前注册表（Registry）设备业务 ID。
        device_name = str(device_definition.get("id") or "")
        # ``class_definition`` 持有设备实现身份及其完整动作（Action）映射。
        class_definition = device_definition.get("class")
        if not isinstance(class_definition, Mapping):
            raise RegistryTemplateProjectionError(f"设备定义缺少类合同: {device_name}")
        # ``action_mappings`` 是本设备冻结代际内的完整动作合同集合。
        action_mappings = class_definition.get("action_value_mappings") or {}
        if not isinstance(action_mappings, Mapping):
            raise RegistryTemplateProjectionError(
                f"设备动作映射必须是对象: {device_name}"
            )
        for action_name, action_definition in action_mappings.items():
            # ``action_name`` 是错误定位使用的动作业务名；自动动作不进入可信投影。
            if not isinstance(action_definition, Mapping):
                raise RegistryTemplateProjectionError(
                    f"设备动作定义必须是对象: {device_name}.{action_name}"
                )
            if action_definition.get("contract_kind") != "typed":
                continue
            # ``action_schema`` 是资源模板源码别名所在的第 2 版动作合同根模式。
            action_schema = action_definition.get("schema")
            if not isinstance(action_schema, Mapping):
                raise RegistryTemplateProjectionError(
                    f"动作合同缺少 Schema: {device_name}.{action_name}"
                )
            try:
                compile_action_template_handles(
                    action_schema,
                    node_business_key=(device_name, str(action_name)),
                    resource_template_identity_resolver=(
                        resolve_prevalidated_template_alias
                    ),
                )
            except ActionTemplateProjectionError as error:
                raise RegistryTemplateProjectionError(
                    f"动作资源模板源码身份预检失败: "
                    f"{device_name}.{action_name}: {error!s}"
                ) from error


def _canonical_source_aliases(
    definition: Mapping[str, Any],
    template_name: str,
) -> set[str]:
    """校验一个模板的 ``source_fqid`` 与 ``class.module`` 源码别名。

    参数说明：``definition`` 是单个冻结模板定义；``template_name`` 是已验证的
    注册表（Registry）业务 ID。返回：去重后的规范 Python 源码身份集合。
    异常：两个字段均缺失或任一已声明字段不可安全解析时抛出
    ``RegistryTemplateProjectionError``，不得进入库存写事务。
    """

    # ``class_definition`` 是资源模板实现合同，可能提供类模块源码别名。
    class_definition = definition.get("class")
    # ``class_module`` 是 ``class.module`` 声明的候选 Python 源码身份。
    class_module = (
        class_definition.get("module")
        if isinstance(class_definition, Mapping)
        else None
    )
    # ``raw_aliases`` 保留两个合同字段；任一已声明别名都必须独立通过校验。
    raw_aliases = [definition.get("source_fqid"), class_module]
    # ``declared_aliases`` 只保留作者实际声明、因此必须逐个通过校验的候选别名。
    declared_aliases = [alias for alias in raw_aliases if alias not in (None, "")]
    if not declared_aliases:
        raise RegistryTemplateProjectionError(f"资源模板缺少源码身份: {template_name}")
    # ``canonical_aliases`` 是当前模板经过可信 Python 身份校验的去重别名集合。
    canonical_aliases: set[str] = set()
    for raw_alias in declared_aliases:
        # ``raw_alias`` 是尚未规范化的 ``source_fqid`` 或类模块源码身份。
        try:
            canonical_aliases.add(canonical_python_source_identity(raw_alias))
        except PythonSourceIdentityError:
            raise RegistryTemplateProjectionError(
                f"资源模板源码身份不能安全解析: {template_name}"
            ) from None
    return canonical_aliases


def _validated_receipt_identities(
    synchronization: Any,
    *,
    expected_names: set[str],
) -> dict[str, str]:
    """验证同步回执并返回完整活动模板身份映射。

    参数说明：``synchronization`` 是本地后端（Backend）形态资源服务回执；
    ``expected_names`` 是预校验快照中的完整注册表（Registry）业务 ID 集合。
    返回：业务 ID 到规范 UUID 的映射。异常：回执形状、成员、UUID、唯一性或
    集合完整性不符时抛出 ``RegistryTemplateProjectionError``。
    """

    # ``synchronized_templates`` 是同步事务回执中的资源模板身份成员。
    synchronized_templates = (
        synchronization.get("templates")
        if isinstance(synchronization, Mapping)
        else None
    )
    if not isinstance(synchronized_templates, list):
        raise RegistryTemplateProjectionError("本地资源模板同步回执缺少模板身份列表")
    # ``template_uuid_by_name`` 汇总已验证回执业务 ID 到活动稳定 UUID 的全集。
    template_uuid_by_name: dict[str, str] = {}
    for identity in synchronized_templates:
        # ``identity`` 是同步回执（Receipt）中的单个资源模板身份成员。
        if not isinstance(identity, Mapping):
            raise RegistryTemplateProjectionError("本地资源模板同步回执成员必须是对象")
        # ``template_name`` 是回执成员声明的注册表（Registry）业务 ID。
        template_name = identity.get("name")
        if not isinstance(template_name, str) or not template_name:
            raise RegistryTemplateProjectionError("本地资源模板同步回执缺少业务 ID")
        try:
            # ``template_uuid`` 是资源服务提交的活动资源模板稳定身份。
            template_uuid = validate_uuid(identity.get("uuid"))
        except (TypeError, ValueError):
            raise RegistryTemplateProjectionError(
                f"本地资源模板同步回执 UUID 无效: {template_name}"
            ) from None
        if template_name in template_uuid_by_name:
            raise RegistryTemplateProjectionError("本地资源模板同步回执业务 ID 重复")
        template_uuid_by_name[template_name] = template_uuid
    if set(template_uuid_by_name) != expected_names:
        raise RegistryTemplateProjectionError(
            "本地资源模板同步回执与注册表（Registry）快照不一致"
        )
    return template_uuid_by_name


__all__ = ["synchronize_local_template_identities"]
