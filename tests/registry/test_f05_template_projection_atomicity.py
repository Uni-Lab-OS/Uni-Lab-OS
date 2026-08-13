"""F05.1 设备注册表模板投影的候选校验与事务原子性合同。"""

from __future__ import annotations

import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from tests.registry.test_template_projection import FakeRegistry
from unilabos.registry.template_projection import RegistryTemplateProjection
from unilabos.workflow.authoring_kernel import AuthoringCatalogError
from unilabos.workflow.store import WorkflowStore

PRIMARY_RESOURCE_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000001"
SECONDARY_RESOURCE_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000003"


class _DuplicateActionBusinessIdentityRegistry(FakeRegistry):
    """发布两个资源身份不同、动作业务身份相同的设备定义。"""

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """构造会污染工作流创作目录（Authoring Catalog）的重复动作。

        参数：无。返回：两个资源模板（ResourceTemplate）身份不同，但设备类与
        动作业务名完全相同的设备注册表（Registry）定义；目录应完整发布两套
        UUID 模板，同时把仅凭源码业务键的查询标记为歧义。
        """

        devices = super().obtain_registry_device_info()
        secondary = deepcopy(devices[0])
        secondary["id"] = "backup_pump"
        secondary["displayname"] = "备用注射泵"
        return [devices[0], secondary]


class _DistinctFactorySourceRegistry(FakeRegistry):
    """发布共用返回类、但激活工厂源码身份不同的两个设备定义。"""

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """构造两个合法复用同一合同类的工厂设备定义。

        参数：无。返回：两个资源模板共享 ``class.module`` 和动作合同，但分别
        声明唯一 ``source_fqid``；工作流创作目录必须按实际工厂入口消歧。
        """

        devices = super().obtain_registry_device_info()
        primary = devices[0]
        primary["source_fqid"] = "lab.devices:make_primary_pump"
        secondary = deepcopy(primary)
        secondary["id"] = "backup_pump"
        secondary["displayname"] = "备用注射泵"
        secondary["source_fqid"] = "lab.devices:make_backup_pump"
        return [primary, secondary]


def _projection(database_path: Path) -> RegistryTemplateProjection:
    """装配覆盖两个设备资源身份的本地模板投影。

    参数说明：``database_path`` 是跨失败刷新与重启复用的 SQLite 路径。返回：
    使用确定资源模板（ResourceTemplate）UUID 的设备注册表模板投影；未知业务
    身份返回空串，由投影边界关闭式失败。
    异常：数据库或模板投影初始化失败时传播原异常。
    """

    identities = {
        "pump": PRIMARY_RESOURCE_TEMPLATE_UUID,
        "backup_pump": SECONDARY_RESOURCE_TEMPLATE_UUID,
    }

    def resolve_resource_template_identity(resource_name: str) -> str:
        """按资源业务名读取本用例固定的资源模板身份。

        参数：``resource_name`` 是设备动作合同引用的资源业务名。
        返回：已声明名称的稳定 UUID；未知名称返回空串并由投影边界拒绝。
        异常：无。
        """

        return identities.get(resource_name, "")

    return RegistryTemplateProjection(
        WorkflowStore(database_path),
        authority_id="local",
        resource_template_identity_resolver=resolve_resource_template_identity,
    )


def _persisted_projection_state(database_path: Path) -> dict[str, Any]:
    """读取投影原子性测试需要比较的完整持久事实。

    参数说明：``database_path`` 是投影正在使用或已关闭的 SQLite 文件。返回：
    当前投影代际、资源模板（ResourceTemplate）源码身份 JSON、全部节点模板和
    全部连接点（Handle）模板的确定元组；读取异常原样传播，确保测试不会把缺表
    或损坏状态误判为成功回滚。
    """

    connection = sqlite3.connect(database_path)
    try:
        generation = connection.execute(
            """
            SELECT authority_id, generation, resource_template_symbols
            FROM registry_template_projection_generation
            ORDER BY authority_id
            """
        ).fetchall()
        nodes = connection.execute(
            """
            SELECT uuid, create_time, update_time, deleted_at, authority_id,
                   resource_template_uuid, name, class, meta_data
            FROM workflow_node_template
            ORDER BY uuid
            """
        ).fetchall()
        handles = connection.execute(
            """
            SELECT uuid, create_time, update_time, deleted_at, authority_id,
                   workflow_node_template_uuid, handle_key, io_type, meta_data
            FROM workflow_handle_template
            ORDER BY uuid
            """
        ).fetchall()
        manifest = connection.execute(
            """
            SELECT authority_id, projection_kind, source_definition_key,
                   business_key, target_uuid, semantic_hash, generation
            FROM registry_template_projection_member
            ORDER BY projection_kind, business_key
            """
        ).fetchall()
    finally:
        connection.close()
    return {
        "generation": [tuple(row) for row in generation],
        "nodes": [tuple(row) for row in nodes],
        "handles": [tuple(row) for row in handles],
        "manifest": [tuple(row) for row in manifest],
    }


def test_shared_action_identity_publishes_uuid_addressable_templates(
    tmp_path: Path,
) -> None:
    """共享实现的多设备动作必须完整发布且不被任意源码解析。

    参数说明：``tmp_path`` 提供隔离数据库目录。返回：无；断言共享类与动作名的
    两个资源模板均持久发布、可按模板 UUID 区分，且源码业务键查询明确报告歧义；
    重启后保持同一目录事实。
    """

    database_path = tmp_path / "workflow_history.db"
    projection = _projection(database_path)
    snapshot = projection.refresh(_DuplicateActionBusinessIdentityRegistry())
    matching_actions = tuple(
        action
        for action in snapshot.actions
        if action.template.get("class") == "lab.devices:Pump"
        and action.template.get("name") == "transfer"
    )
    assert {
        action.template["resource_template_uuid"] for action in matching_actions
    } == {PRIMARY_RESOURCE_TEMPLATE_UUID, SECONDARY_RESOURCE_TEMPLATE_UUID}
    for action in matching_actions:
        assert snapshot.require_template(str(action.template["uuid"])) is action
    with pytest.raises(AuthoringCatalogError, match="动作身份不唯一"):
        snapshot.require_action("lab.devices:Pump", "transfer")
    persisted_state = _persisted_projection_state(database_path)
    projection.close()

    restarted = _projection(database_path)
    assert restarted.snapshot().fingerprint == snapshot.fingerprint
    assert _persisted_projection_state(database_path) == persisted_state
    with pytest.raises(AuthoringCatalogError, match="动作身份不唯一"):
        restarted.snapshot().require_action("lab.devices:Pump", "transfer")
    restarted.close()


def test_factory_source_identity_disambiguates_shared_return_class(
    tmp_path: Path,
) -> None:
    """工厂设备应按实际激活入口建立动作业务身份。

    参数说明：``tmp_path`` 隔离 SQLite。返回：无；断言两个工厂即使共用同一
    返回类和动作名也可同时发布，并可分别通过工厂源码身份精确取得。
    """

    projection = _projection(tmp_path / "workflow_history.db")
    snapshot = projection.refresh(_DistinctFactorySourceRegistry())

    assert snapshot.require_action(
        "lab.devices:make_primary_pump",
        "transfer",
    ).template["resource_template_uuid"] == PRIMARY_RESOURCE_TEMPLATE_UUID
    assert snapshot.require_action(
        "lab.devices:make_backup_pump",
        "transfer",
    ).template["resource_template_uuid"] == SECONDARY_RESOURCE_TEMPLATE_UUID
    projection.close()
