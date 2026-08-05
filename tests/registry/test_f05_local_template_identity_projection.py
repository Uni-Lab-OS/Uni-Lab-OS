"""F05.4-C0b2 本地资源模板（ResourceTemplate）身份同步合同。"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from unilabos.app.scheduler.inventory.backend_contract import (
    TEMPLATE_DATA_CONFLICT,
    BackendContractError,
    BackendResourceService,
)
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.registry.ast_registry_scanner import _parse_file
from unilabos.registry.registry import Registry
from unilabos.registry.template_projection import RegistryTemplateProjectionError
from unilabos.registry.template_snapshot import RegistryTemplateSnapshot
from unilabos.workflow.composition import (
    compose_local_workflow_template_runtime,
    get_workflow_service,
    reset_workflow_service_for_test,
)


class _BuiltRegistry:
    """暴露由真实 AST 扫描器和注册表构建器生成的模板定义。"""

    def __init__(
        self,
        *,
        devices: list[dict[str, Any]],
        resources: list[dict[str, Any]],
    ) -> None:
        """保存一次测试注册表（Registry）定义代际。

        参数说明：``devices`` 是设备模板定义；``resources`` 是资源模板
        （ResourceTemplate）定义。返回：无；调用者只通过标准读取接口交给冻结
        快照，测试替身不直接创建前端模板或工作流数据库身份。
        """

        self._devices = devices
        self._resources = resources

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """返回真实构建器生成的完整设备模板定义。

        参数：无。返回：本次测试注册表中的设备定义列表；冻结快照负责后续分离。
        """

        return self._devices

    def obtain_registry_resource_info(self) -> list[dict[str, Any]]:
        """返回真实构建器生成的完整资源模板定义。

        参数：无。返回：本次测试注册表中的资源定义列表；不注入库存 UUID。
        """

        return self._resources


class _FailingInventoryStore(InventoryStore):
    """模拟库存模板同步事务明确拒绝写入。"""

    @contextmanager
    def transaction(self) -> Any:
        """在模板同步开始时返回稳定后端（Backend）合同错误。

        参数：无。返回：本生成器不会产生事务连接。异常：始终抛出模板数据冲突，
        用于证明组合根不能在库存写权威拒绝后继续发布工作流模板投影。
        """

        raise BackendContractError(TEMPLATE_DATA_CONFLICT, "测试模板身份冲突")
        yield  # pragma: no cover - contextmanager 语法所需，不可到达


def _build_registry(tmp_path: Path) -> _BuiltRegistry:
    """从 Python 声明构建含设备动作和物料模板的真实注册表输入。

    参数说明：``tmp_path`` 是隔离的 Python 包根目录。返回：通过产品 AST 扫描器
    与注册表（Registry）构建器产生的测试注册表。异常：源码合同无法扫描或构建时原样
    抛出，让测试不能退化为手写前端模板夹具。
    """

    # ``module_path`` 是静态扫描证据文件；产品扫描器不会导入或执行该源码。
    module_path = tmp_path / "local_templates.py"
    module_path.write_text(
        '''
from typing import TypedDict
from unilabos.registry.decorators import action, device, resource
from unilabos.registry.placeholder_type import ResourceSlot

@resource(
    id="plate_96",
    category=["plate"],
    displayname="96 孔板",
    description="测试反应板物料模板。",
)
def plate_96():
    """构造测试反应板。"""
    raise NotImplementedError

class TransferResult(TypedDict):
    material: ResourceSlot

@device(
    id="pump",
    category=["pump"],
    displayname="测试泵",
    description="用于模板身份同步测试的设备。",
)
class Pump:
    @action(description="转移反应板")
    def transfer(
        self,
        plate: ResourceSlot,
    ) -> TransferResult:
        """转移需要稳定模板身份的反应板。"""
        raise NotImplementedError
''',
        encoding="utf-8",
    )
    scanned_devices, scanned_resources = _parse_file(module_path, tmp_path)
    # ``registry_builder`` 复用产品注册表构建规则，不自行拼装动作 Schema。
    registry_builder = Registry()
    built_devices = [
        {
            "id": str(definition["device_id"]),
            **registry_builder._build_device_entry_from_ast(
                str(definition["device_id"]),
                definition,
            ),
        }
        for definition in scanned_devices
    ]
    built_resources = [
        {
            "id": str(definition["resource_id"]),
            **registry_builder._build_resource_entry_from_ast(
                str(definition["resource_id"]),
                definition,
            ),
        }
        for definition in scanned_resources
    ]
    return _BuiltRegistry(devices=built_devices, resources=built_resources)


def _active_template_identities(store: InventoryStore) -> dict[str, str]:
    """读取库存权威中的活动资源模板业务名与 UUID。

    参数说明：``store`` 是本地库存存储。返回：业务唯一名到稳定 UUID 的映射；
    仅用于验证公开组合操作产生的权威事实，不参与产品身份解析。
    """

    # ``template_rows`` 是 inventory.db 当前全部活动资源模板事实。
    template_rows = store.query_all(
        """
        SELECT uuid, name
        FROM resource_template
        WHERE deleted_at IS NULL
        ORDER BY name
        """
    )
    return {str(row["name"]): str(row["uuid"]) for row in template_rows}


def _template_storage_facts(
    store: InventoryStore,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """读取完整模板事实与聚合版本，用于证明失败前后零新增、零更新。

    参数说明：``store`` 是真实本地库存存储。返回：按稳定身份排序的资源模板
    （ResourceTemplate）行和模板库存聚合行；调用者只比较公开组合操作前后的
    权威事实，不把数据库旁路用作产品身份解析。
    """

    # 两组事实同时覆盖模板字段更新和聚合版本递增，避免只检查活动行数漏报更新。
    template_rows = [
        dict(row)
        for row in store.query_all("SELECT * FROM resource_template ORDER BY uuid")
    ]
    inventory_rows = [
        dict(row)
        for row in store.query_all(
            "SELECT * FROM resource_template_inventory ORDER BY resource_template_uuid"
        )
    ]
    return template_rows, inventory_rows


def test_local_composition_creates_missing_inventory_template_identities(
    tmp_path: Path,
) -> None:
    """本地组合必须先创建缺失身份，再发布可查询的工作流模板投影。

    参数说明：``tmp_path`` 隔离库存与工作流数据库。返回：无；断言设备和物料
    资源模板（ResourceTemplate）均进入 inventory.db，动作所有者与物料占位符
    （ResourceSlot）允许集都引用同一批稳定 UUID。
    """

    reset_workflow_service_for_test()
    # ``inventory_store`` 是本地库存模板身份权威；启动前没有任何模板行。
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        registry = _build_registry(tmp_path)
        _service, projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=registry,
        )

        template_identities = _active_template_identities(inventory_store)
        action = projection.snapshot().require_action(
            "local_templates:Pump",
            "transfer",
        )

        assert set(template_identities) == {"plate_96", "pump"}
        assert action.template["resource_template_uuid"] == template_identities["pump"]
        assert (
            projection.snapshot().require_resource_template_uuid(
                "local_templates:plate_96"
            )
            == template_identities["plate_96"]
        )
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_local_composition_reuses_existing_business_identity_uuid(
    tmp_path: Path,
) -> None:
    """已有活动业务唯一名必须复用 UUID，而不能产生第二模板身份。

    参数说明：``tmp_path`` 隔离数据库。返回：无；断言既有同步结果经本地组合
    再次同步后完全不变，并被模板投影（Template Projection）直接引用。
    """

    reset_workflow_service_for_test()
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        registry = _build_registry(tmp_path)
        # ``frozen_registry`` 是预置和组合必须共同遵守的同一规范定义代际。
        frozen_registry = RegistryTemplateSnapshot.from_registry(registry)
        first_result = BackendResourceService(inventory_store).sync_resource_templates(
            frozen_registry.detached_definitions()
        )
        expected_identities = {
            str(item["name"]): str(item["uuid"]) for item in first_result["templates"]
        }

        _service, projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=frozen_registry,
        )
        action = projection.snapshot().require_action(
            "local_templates:Pump",
            "transfer",
        )

        assert _active_template_identities(inventory_store) == expected_identities
        assert action.template["resource_template_uuid"] == expected_identities["pump"]
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_local_composition_restart_keeps_template_identity_stable(
    tmp_path: Path,
) -> None:
    """重复组合与进程重启不得让库存模板 UUID 漂移。

    参数说明：``tmp_path`` 保留同一 inventory.db 与 workflow_history.db。返回：
    无；断言关闭并重新打开两个本地存储后，业务名映射和动作所有者身份均稳定。
    """

    reset_workflow_service_for_test()
    inventory_path = tmp_path / "inventory.db"
    first_store = InventoryStore(str(inventory_path))
    registry = _build_registry(tmp_path)
    try:
        _first_service, first_projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=first_store,
            registry=registry,
        )
        first_identities = _active_template_identities(first_store)
        first_owner_uuid = (
            first_projection.snapshot()
            .require_action(
                "local_templates:Pump",
                "transfer",
            )
            .template["resource_template_uuid"]
        )
    finally:
        reset_workflow_service_for_test()
        first_store.close()

    restarted_store = InventoryStore(str(inventory_path))
    try:
        _second_service, second_projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=restarted_store,
            registry=registry,
        )

        assert _active_template_identities(restarted_store) == first_identities
        assert (
            second_projection.snapshot()
            .require_action(
                "local_templates:Pump",
                "transfer",
            )
            .template["resource_template_uuid"]
            == first_owner_uuid
        )
    finally:
        reset_workflow_service_for_test()
        restarted_store.close()


def test_local_composition_rejects_conflicting_resource_source_aliases(
    tmp_path: Path,
) -> None:
    """同一源码别名指向多个资源模板 UUID 时必须关闭式失败。

    参数说明：``tmp_path`` 隔离数据库。返回：无；断言冲突不能选择任一物料模板
    （ResourceTemplate），也不能发布半成品工作流权威（Workflow Authority）。
    """

    reset_workflow_service_for_test()
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        registry = _build_registry(tmp_path)
        # ``stable_snapshot`` 先建立合法同代事实；冲突组合不得更新既有模板版本。
        stable_snapshot = RegistryTemplateSnapshot.from_registry(registry)
        BackendResourceService(inventory_store).sync_resource_templates(
            stable_snapshot.detached_definitions()
        )
        facts_before_conflict = _template_storage_facts(inventory_store)
        original_resource = registry.obtain_registry_resource_info()[0]
        conflicting_resource = {
            **original_resource,
            "id": "plate_384",
            "displayname": "384 孔板",
        }
        # 两个不同业务模板故意声明同一源码身份，不能静默选择先出现者。
        original_resource["source_fqid"] = "lab.resources:shared_plate"
        conflicting_resource["source_fqid"] = "lab.resources:shared_plate"
        registry._resources = [original_resource, conflicting_resource]

        with pytest.raises(RegistryTemplateProjectionError, match="源码身份"):
            compose_local_workflow_template_runtime(
                tmp_path,
                inventory_store=inventory_store,
                registry=registry,
            )
        assert get_workflow_service() is None
        assert _template_storage_facts(inventory_store) == facts_before_conflict
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_local_composition_rejects_unresolvable_alias_before_inventory_write(
    tmp_path: Path,
) -> None:
    """不可解析源码别名必须在任何库存模板写事务之前关闭式失败。

    参数说明：``tmp_path`` 隔离真实库存数据库。返回：无；断言非法
    ``source_fqid`` 和类模块别名不能留下资源模板（ResourceTemplate）或模板库存
    聚合事实，也不能发布半成品工作流权威（Workflow Authority）。
    """

    reset_workflow_service_for_test()
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        # ``facts_before_invalid_alias`` 包含迁移生成的软删除兼容占位事实；失败后也不得更新。
        facts_before_invalid_alias = _template_storage_facts(inventory_store)
        registry = _build_registry(tmp_path)
        invalid_resource = registry.obtain_registry_resource_info()[0]
        # 两个字段共同构成无效 Python 源码别名，不能延迟到库存提交后才校验。
        invalid_resource["source_fqid"] = "not a python source identity"
        invalid_resource["class"]["module"] = "not a python source identity"
        registry._resources = [invalid_resource]

        with pytest.raises(RegistryTemplateProjectionError, match="源码身份"):
            compose_local_workflow_template_runtime(
                tmp_path,
                inventory_store=inventory_store,
                registry=registry,
            )

        assert get_workflow_service() is None
        assert _template_storage_facts(inventory_store) == facts_before_invalid_alias
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_local_composition_fails_closed_when_inventory_sync_is_rejected(
    tmp_path: Path,
) -> None:
    """本地库存拒绝资源模板身份同步时，模板运行时必须关闭式失败。

    参数说明：``tmp_path`` 隔离数据库。返回：无；断言同步错误被转换为模板投影
    （Template Projection）领域错误，且不发布半装配工作流权威（Workflow
    Authority）。
    """

    reset_workflow_service_for_test()
    inventory_store = _FailingInventoryStore(str(tmp_path / "inventory.db"))
    try:
        with pytest.raises(RegistryTemplateProjectionError, match="同步失败"):
            compose_local_workflow_template_runtime(
                tmp_path,
                inventory_store=inventory_store,
                registry=_build_registry(tmp_path),
            )
        assert get_workflow_service() is None
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()
