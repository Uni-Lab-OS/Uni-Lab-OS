"""F05.1 设备注册表模板投影的候选校验与事务原子性合同。"""

from __future__ import annotations

import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from tests.registry.test_template_projection import FakeRegistry
from unilabos.registry.template_projection import (
    RegistryTemplateProjection,
    RegistryTemplateProjectionError,
)
from unilabos.workflow.store import WorkflowStore

PRIMARY_RESOURCE_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000001"
SECONDARY_RESOURCE_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000003"


class _DuplicateActionBusinessIdentityRegistry(FakeRegistry):
    """发布两个资源身份不同、动作业务身份相同的设备定义。"""

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """构造会污染工作流创作目录（Authoring Catalog）的重复动作。

        参数：无。返回：两个资源模板（ResourceTemplate）身份不同，但设备类与
        动作业务名完全相同的设备注册表（Registry）定义；候选模板持久化层允许
        各自的节点业务键，完整目录校验必须拒绝重复动作业务身份。
        """

        devices = super().obtain_registry_device_info()
        secondary = deepcopy(devices[0])
        secondary["id"] = "backup_pump"
        secondary["displayname"] = "备用注射泵"
        return [devices[0], secondary]


def _projection(database_path: Path) -> RegistryTemplateProjection:
    """装配覆盖两个设备资源身份的本地模板投影。

    参数说明：``database_path`` 是跨失败刷新与重启复用的 SQLite 路径。返回：
    使用确定资源模板（ResourceTemplate）UUID 的设备注册表模板投影；未知业务
    身份返回空串，由投影边界关闭式失败。
    """

    identities = {
        "pump": PRIMARY_RESOURCE_TEMPLATE_UUID,
        "backup_pump": SECONDARY_RESOURCE_TEMPLATE_UUID,
    }
    return RegistryTemplateProjection(
        WorkflowStore(database_path),
        authority_id="local",
        resource_template_identity_resolver=lambda resource_name: identities.get(
            resource_name,
            "",
        ),
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
    finally:
        connection.close()
    return {
        "generation": [tuple(row) for row in generation],
        "nodes": [tuple(row) for row in nodes],
        "handles": [tuple(row) for row in handles],
    }


def test_invalid_catalog_generation_rolls_back_before_durable_publish(
    tmp_path: Path,
) -> None:
    """完整目录校验失败必须回滚同一模板投影事务。

    参数说明：``tmp_path`` 提供隔离数据库目录。返回：无；先发布合法代际，再用
    重复动作业务身份触发工作流创作目录（Authoring Catalog）校验异常，并断言
    内存快照、SQLite 代际、节点、连接点（Handle）、资源身份和重启恢复全部保持
    合法代际；对外只暴露 ``RegistryTemplateProjectionError``。
    """

    database_path = tmp_path / "workflow_history.db"
    projection = _projection(database_path)
    good_snapshot = projection.refresh(FakeRegistry())
    good_state = _persisted_projection_state(database_path)

    with pytest.raises(RegistryTemplateProjectionError, match="动作业务身份重复"):
        projection.refresh(_DuplicateActionBusinessIdentityRegistry())

    assert projection.snapshot().fingerprint == good_snapshot.fingerprint
    assert _persisted_projection_state(database_path) == good_state
    projection.close()

    restarted = _projection(database_path)
    assert restarted.snapshot().fingerprint == good_snapshot.fingerprint
    assert restarted.snapshot().require_action(
        "lab.devices:Pump",
        "transfer",
    ).template["resource_template_uuid"] == PRIMARY_RESOURCE_TEMPLATE_UUID
    restarted.close()
