"""QG01 宿主节点（HostNode）物料转运动作的可信创作合同。"""

from __future__ import annotations

from pathlib import Path

import pytest

from unilabos.registry.registry import Registry
from unilabos.registry.template_projection import RegistryTemplateProjection
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "e7c53119-9fde-5250-9bf5-264f23d157a8"
TRANSFER_NODE_UUID = "8d8bfc18-03db-5ff3-a681-edf1c15294b7"
HOST_RESOURCE_TEMPLATE_UUID = "90000000-0000-4000-8000-000000000001"


def _empty_applied_graph() -> dict[str, object]:
    """构造首次编译 SZLab 物料转运工作流（Material-transfer Workflow）的空应用图。

    参数：无。返回：保留稳定工作流（Workflow）身份且不含节点、边和模板的
    Backend-shaped 应用图。异常：无；图内容是独立于实现的已知输入。
    """

    return {
        "workflow": {
            "uuid": WORKFLOW_UUID,
            "name": "SZLab 标准物料转运",
            "description": "",
            "tags": [],
            "meta_data": {},
            "revision": 1,
        },
        "nodes": [],
        "edges": [],
        "node_templates": [],
        "handle_templates": [],
    }


def _material_transfer_source() -> str:
    """返回只调用宿主转运记账动作的最小可信工作流源码（Workflow Source）。

    参数：无。返回：保留 SZLab 公共动作名、参数名、物料占位符
    （ResourceSlot）传递和结果字段的静态 Python 源码。异常：无；源码不会被
    导入执行，只由工作流创作编译器（Authoring Compiler）进行静态分析。
    """

    return f'''from typing import TypedDict

from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.ros.nodes.presets.host_node import HostNode
from unilabos.workflow.authoring import device, workflow


class TransferResult(TypedDict):
    site: str


host_node: HostNode = device("host_node")


@workflow(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="SZLab 标准物料转运",
)
def material_transfer(
    *,
    resource: ResourceSlot,
    target_device: str,
    target_warehouse: ResourceSlot,
    target_site: str,
) -> TransferResult:
    # unilab:node_uuid={TRANSFER_NODE_UUID}
    committed = host_node.transfer_resource(
        resource=resource,
        target_device=target_device,
        mount_resource=target_warehouse,
        site=target_site,
    )
    return {{"site": committed.site}}
'''


def _host_resource_template_identity(_registry_identity: str) -> str:
    """把本测试唯一宿主注册表身份解析为稳定资源模板（ResourceTemplate）UUID。

    参数：``_registry_identity`` 是真实注册表投影请求的宿主业务身份；当前夹具
    只有 ``host_node``，故该值不参与分支选择。返回：预先固定的宿主资源模板
    UUID。异常：无；测试意在验证动作合同而非库存同步。
    """

    return HOST_RESOURCE_TEMPLATE_UUID


def test_builtin_host_transfer_is_projected_and_compiles_szlab_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内置转运必须发布类型化动作（Typed Action）并通过真实 SZLab 编译路径。

    参数：``tmp_path`` 隔离注册表模板投影（Registry Template Projection）的
    SQLite；``monkeypatch`` 隔离全局注册表（Registry）单例的既有代际。返回：
    无；断言真实内置扫描、持久模板投影和工作流创作编译器共同识别
    ``host_node.transfer_resource``，且两个物料输入默认声明动作物料锁
    （Action Material Lock）。异常：模板缺失或合同无效时测试保持 RED，并禁止
    通过猜测动作身份继续编译。
    """

    # ``registry`` 是只扫描核心 HostNode 源码的真实注册表代际，不伪造动作 DTO。
    registry = Registry()
    monkeypatch.setattr(registry, "_setup_called", False)
    monkeypatch.setattr(registry, "_startup_executor", None)
    monkeypatch.setattr(registry, "device_type_registry", {})
    monkeypatch.setattr(registry, "resource_type_registry", {})
    registry.setup(external_only=True)

    # ``projection`` 是工作流创作使用的持久模板权威；宿主资源模板 UUID 模拟已由
    # 库存权威（Inventory Authority）稳定解析，测试不创建第二套身份。
    projection = RegistryTemplateProjection(
        WorkflowStore(tmp_path / "workflow_history.db"),
        authority_id="qg01-host-transfer",
        resource_template_identity_resolver=_host_resource_template_identity,
    )
    try:
        catalog = projection.refresh(registry)
        transfer = catalog.require_action(
            "unilabos.ros.nodes.presets.host_node:HostNode",
            "transfer_resource",
        )
        transfer_template = transfer.detached_template()
        result = WorkflowAuthoringEngine(catalog=catalog).compile(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=1,
            python_source=_material_transfer_source(),
            source_uri=(
                "package://szlab_poly_studio/workflows/material_transfer.py"
            ),
            applied_graph=_empty_applied_graph(),
        )
    finally:
        projection.close()

    assert transfer_template["class"] == (
        "unilabos.ros.nodes.presets.host_node:HostNode"
    )
    assert transfer_template["meta_data"]["unilab"]["contract_kind"] == "typed"
    goal_properties = transfer_template["meta_data"]["unilab"][
        "action_contract_schema"
    ]["properties"]["goal"]["properties"]
    assert goal_properties["resource"]["x-unilabos-material-lock"] is True
    assert goal_properties["mount_resource"]["x-unilabos-material-lock"] is True
    assert result.valid, result.diagnostics
    assert result.graph is not None
    transfer_node = next(
        node for node in result.graph["nodes"] if node["uuid"] == TRANSFER_NODE_UUID
    )
    assert transfer_node["workflow_node_template_uuid"] == transfer_template["uuid"]
