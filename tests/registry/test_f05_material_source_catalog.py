"""F05.1 物料来源（MaterialSource）框架模板投影合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from unilabos.registry.template_projection import RegistryTemplateProjection
from unilabos.workflow.store import WorkflowStore

HOST_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000001"
PLATE_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000002"
PLATE_SOURCE_IDENTITY = "lab.resources:plate_96"


class _Registry:
    """提供 Host 设备与一个物料资源模板（ResourceTemplate）的冻结快照。"""

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """返回框架物料来源（MaterialSource）的唯一 Host 所有者。

        参数：无。返回：只含 ``host_node`` 的完整设备定义集。
        """

        return [
            {
                "id": "host_node",
                "display_name": "Host Node",
                "registry_type": "device",
                "class": {
                    "module": "unilabos.ros.nodes.presets.host_node:HostNode",
                    "type": "python",
                    "action_value_mappings": {},
                },
                "handles": [],
                "category": [],
            }
        ]

    def obtain_registry_resource_info(self) -> list[dict[str, Any]]:
        """返回可供创作编译器解析的物料资源模板定义。

        参数：无。返回：含稳定源码身份的资源定义集。
        """

        return [
            {
                "id": "plate_96",
                "source_fqid": PLATE_SOURCE_IDENTITY,
                "display_name": "96 孔板",
                "registry_type": "resource",
                "class": {"module": PLATE_SOURCE_IDENTITY, "type": "python"},
                "handles": [],
                "category": [],
            }
        ]


def _identity(source_identity: str) -> str:
    """把 Registry 唯一名称解析为本地稳定资源模板 UUID。

    参数说明：``source_identity`` 是 Host 或物料资源的业务唯一
    名称。返回：已存储的资源模板 UUID。异常：未知身份抛出
    ``KeyError``，投影层必须关闭失败。
    """

    # ``identities`` 是测试代表的已有模板数据库身份映射。
    identities = {
        "host_node": HOST_TEMPLATE_UUID,
        "plate_96": PLATE_TEMPLATE_UUID,
    }
    return identities[source_identity]


def _projection(database_path: Path) -> RegistryTemplateProjection:
    """打开使用固定身份解析器的模板投影。

    参数说明：``database_path`` 是工作流模板 SQLite 路径。
    返回：可刷新和读取的注册表模板投影。
    """

    return RegistryTemplateProjection(
        WorkflowStore(database_path),
        authority_id="local",
        resource_template_identity_resolver=_identity,
    )


def test_registry_projects_one_stable_material_source_framework_template(
    tmp_path: Path,
) -> None:
    """Host 应发布单一物料来源（MaterialSource）模板及 source 物料占位符。

    参数说明：``tmp_path`` 提供跨刷新和重启的隔离 SQLite 路径。
    返回：无；断言节点、连接点（Handle）和 UUID 生命周期。
    """

    # ``database_path`` 保留本地节点模板业务唯一键到 UUID 的身份映射。
    database_path = tmp_path / "workflow_history.db"
    projection = _projection(database_path)
    first = projection.refresh(_Registry()).require_material_source()
    # ``framework_uuid`` 是首次发布时分配或复用的框架节点模板身份。
    framework_uuid = str(first.template["uuid"])

    assert {
        "resource_template_uuid": first.template["resource_template_uuid"],
        "name": first.template["name"],
        "class": first.template["class"],
        "type": first.template["type"],
        "node_type": first.template["node_type"],
    } == {
        "resource_template_uuid": HOST_TEMPLATE_UUID,
        "name": "material_source",
        "class": "unilabos.workflow.authoring:material_source",
        "type": "material_source",
        "node_type": "material_source",
    }
    assert len(first.handles) == 1
    assert {
        "handle_key": first.handles[0]["handle_key"],
        "io_type": first.handles[0]["io_type"],
        "type": first.handles[0]["type"],
        "required": first.handles[0]["required"],
    } == {
        "handle_key": "material",
        "io_type": "source",
        "type": "ResourceSlot",
        "required": False,
    }
    assert projection.refresh(_Registry()).require_material_source().template[
        "uuid"
    ] == framework_uuid
    projection.close()

    restarted = _projection(database_path)
    assert restarted.snapshot().require_material_source().template["uuid"] == (
        framework_uuid
    )
    restarted.close()


def test_catalog_snapshot_freezes_bidirectional_resource_template_identity(
    tmp_path: Path,
) -> None:
    """创作快照应以冻结 Registry 投影解析资源模板符号，不查实时库存。

    参数说明：``tmp_path`` 提供隔离存储。返回：无；断言源码身份
    与资源模板 UUID 双向一一对应。
    """

    projection = _projection(tmp_path / "workflow_history.db")
    snapshot = projection.refresh(_Registry())

    assert snapshot.require_resource_template_uuid(PLATE_SOURCE_IDENTITY) == (
        PLATE_TEMPLATE_UUID
    )
    assert snapshot.require_resource_template_symbol(PLATE_TEMPLATE_UUID) == (
        PLATE_SOURCE_IDENTITY
    )
    projection.close()
