"""F05.1 物料来源（MaterialSource）生产组合根合同。"""

from __future__ import annotations

from pathlib import Path

from tests.registry.test_f05_material_source_catalog import (
    PLATE_SOURCE_IDENTITY,
    _Registry,
)
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.workflow.composition import (
    compose_local_workflow_template_runtime,
    reset_workflow_service_for_test,
)


def test_local_composition_shares_frozen_resource_template_projection(
    tmp_path: Path,
) -> None:
    """生产组合根应让创作编译器和模板查询共用同一冻结代际。

    参数说明：``tmp_path`` 是本地工作流/调度存储根目录。
    返回：无；断言组合后的稳定身份和模板共享。
    """

    reset_workflow_service_for_test()
    # ``inventory_store`` 是真实后端（Backend）形态库存模板写权威，启动前为空。
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        # ``service`` 与 ``projection`` 必须共享同一注册表（Registry）模板代际。
        service, projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_Registry(),
        )
        # ``template_rows`` 是库存权威提交的活动资源模板业务 ID/UUID 映射。
        template_rows = {
            str(row["name"]): str(row["uuid"])
            for row in inventory_store.query_all(
                """
                SELECT uuid, name
                FROM resource_template
                WHERE deleted_at IS NULL
                """
            )
        }

        assert service.compiler is not None
        assert service.compiler.template_catalog_fingerprint == (
            projection.snapshot().fingerprint
        )
        assert (
            projection.snapshot().require_resource_template_uuid(PLATE_SOURCE_IDENTITY)
            == template_rows["plate_96"]
        )
        assert (
            projection.snapshot()
            .require_material_source()
            .template["resource_template_uuid"]
            == template_rows["host_node"]
        )
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_local_composition_creates_missing_material_template_identity(
    tmp_path: Path,
) -> None:
    """物料资源模板身份缺失时组合必须在库存权威中创建稳定身份。

    参数说明：``tmp_path`` 提供隔离存储。返回：无；断言工作流模板投影引用
    同一事务同步后 inventory.db 中的物料资源模板（ResourceTemplate）UUID。
    """

    reset_workflow_service_for_test()
    # ``inventory_store`` 是缺失模板身份首次创建的真实库存权威。
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        # ``projection`` 必须引用本次同步事务新建的反应板模板 UUID。
        _service, projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_Registry(),
        )
        # ``plate_row`` 是库存权威中唯一活动反应板资源模板事实。
        plate_row = inventory_store.query_one(
            """
            SELECT uuid
            FROM resource_template
            WHERE name = ? AND deleted_at IS NULL
            """,
            ("plate_96",),
        )

        assert plate_row is not None
        assert projection.snapshot().require_resource_template_uuid(
            PLATE_SOURCE_IDENTITY
        ) == str(plate_row["uuid"])
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()
