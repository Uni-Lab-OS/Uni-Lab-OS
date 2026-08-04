"""F05.1 物料来源（MaterialSource）生产组合根合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unilabos.registry.template_projection import RegistryTemplateProjectionError
from unilabos.workflow.composition import (
    compose_local_workflow_template_runtime,
    get_workflow_service,
    reset_workflow_service_for_test,
)

from tests.registry.test_f05_material_source_catalog import (
    HOST_TEMPLATE_UUID,
    PLATE_SOURCE_IDENTITY,
    PLATE_TEMPLATE_UUID,
    _Registry,
)


class _InventoryIdentityStore:
    """提供组合根所需的只读资源模板身份映射。"""

    def __init__(self, *, include_plate: bool = True) -> None:
        """配置是否存在物料资源模板身份。

        参数说明：``include_plate`` 为假时模拟生产身份缺失。
        返回：无；实例仅作为读取边界。
        """

        self.include_plate = include_plate

    def query_one(self, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        """按 Registry 业务唯一名称返回已有资源模板行。

        参数说明：``sql`` 必须查询资源模板表，``params`` 只含业务
        唯一名称。返回：活动身份摘要或 ``None``。
        """

        assert "FROM resource_template" in sql
        source_identity = str(params[0])
        # ``rows`` 是模拟已存在的本地模板数据库业务唯一索引。
        rows = {
            "host_node": {
                "uuid": HOST_TEMPLATE_UUID,
                "name": "host_node",
                "display_name": "Host Node",
            },
            "plate_96": {
                "uuid": PLATE_TEMPLATE_UUID,
                "name": "plate_96",
                "display_name": "96 孔板",
            },
        }
        if not self.include_plate and source_identity == "plate_96":
            return None
        return rows.get(source_identity)


def test_local_composition_shares_frozen_resource_template_projection(
    tmp_path: Path,
) -> None:
    """生产组合根应让创作编译器和模板查询共用同一冻结代际。

    参数说明：``tmp_path`` 是本地工作流/调度存储根目录。
    返回：无；断言组合后的稳定身份和模板共享。
    """

    reset_workflow_service_for_test()
    try:
        service, projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=_InventoryIdentityStore(),
            registry=_Registry(),
        )

        assert service.compiler is not None
        assert service.compiler.template_catalog_fingerprint == (
            projection.snapshot().fingerprint
        )
        assert projection.snapshot().require_resource_template_uuid(
            PLATE_SOURCE_IDENTITY
        ) == PLATE_TEMPLATE_UUID
        assert projection.snapshot().require_material_source().template[
            "resource_template_uuid"
        ] == HOST_TEMPLATE_UUID
    finally:
        reset_workflow_service_for_test()


def test_local_composition_fails_closed_when_material_template_identity_is_missing(
    tmp_path: Path,
) -> None:
    """物料资源模板身份缺失时组合必须失败关闭且不发布半成品服务。

    参数说明：``tmp_path`` 提供隔离存储。返回：无；断言预期领域
    错误和空进程级工作流服务。
    异常：缺失身份必须对外抛出 ``RegistryTemplateProjectionError``。
    """

    reset_workflow_service_for_test()
    try:
        with pytest.raises(RegistryTemplateProjectionError, match="身份"):
            compose_local_workflow_template_runtime(
                tmp_path,
                inventory_store=_InventoryIdentityStore(include_plate=False),
                registry=_Registry(),
            )
        assert get_workflow_service() is None
    finally:
        reset_workflow_service_for_test()
