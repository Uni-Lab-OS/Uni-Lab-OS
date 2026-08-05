"""QG01 SZLab ``resource_ref`` 真实物料身份解析合同。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot

from .test_authoring_engine import (
    PREPARE_SAMPLE_TARGET,
    PREPARE_TEMPLATE_UUID,
    WORKFLOW_UUID,
    _applied_graph,
    _handle,
    _template,
)
from .test_f05_material_source_authoring import (
    MATERIAL_SOURCE_NODE_UUID,
    MATERIAL_SOURCE_TEMPLATE_UUID,
    PLATE_SOURCE_SYMBOL,
    PLATE_TEMPLATE_UUID,
    _material_source_template,
)

# ``S3_WAREHOUSE_UUID`` 是库存权威（Inventory Authority）分配给 SZLab
# ``s3_unused_beaker`` 启动资源的稳定物料（Material）UUID。
S3_WAREHOUSE_UUID = "61000000-0000-4000-8000-000000000001"
# ``S3_WAREHOUSE_TEMPLATE_UUID`` 是该挂载资源对应的资源模板（ResourceTemplate）身份。
S3_WAREHOUSE_TEMPLATE_UUID = "62000000-0000-4000-8000-000000000001"
# ``ACTION_NODE_UUID`` 是直接消费 SZLab 启动资源的动作节点稳定身份。
ACTION_NODE_UUID = "63000000-0000-4000-8000-000000000001"


class _ResourceReferenceResolver:
    """模拟只读库存权威（Inventory Authority）的启动资源解析端口。"""

    def __init__(self, resources: Mapping[str, Mapping[str, str]]) -> None:
        """保存业务资源 ID 到实际物料身份的隔离映射。

        参数：``resources`` 的键是部署业务资源 ID，值包含物料 UUID 与资源模板
        UUID。返回：无。异常：无；非法回执由被测编译边界关闭式拒绝。
        """

        # ``_resources`` 是测试代际冻结的只读资源身份，不代表第二库存权威。
        self._resources = {
            resource_id: dict(resource) for resource_id, resource in resources.items()
        }

    def __call__(self, resource_id: str) -> dict[str, str] | None:
        """按部署业务资源 ID 返回实际物料身份。

        参数：``resource_id`` 是 ``resource_ref`` 的静态字符串。返回：命中时返回
        分离字典，未知身份返回 ``None``。异常：无。
        """

        # ``resolved`` 是本次编译读取的库存身份摘要，修改它不能污染夹具。
        resolved = self._resources.get(resource_id)
        return dict(resolved) if resolved is not None else None


def _catalog() -> AuthoringCatalogSnapshot:
    """构造物料来源（MaterialSource）及其消费动作的目录快照。

    参数：无。返回：带真实物料占位符（ResourceSlot）目标连接点和资源模板
    双向身份的不可变目录快照。异常：夹具违反目录合同时直接传播。
    """

    # ``source_template`` 与 ``source_handle`` 是物料来源（MaterialSource）框架合同。
    source_template, source_handle = _material_source_template()
    # ``prepare_template`` 与 ``prepare_handles`` 是普通动作（Action）合同，目标
    # 物料占位符（ResourceSlot）只接受 S3 仓库资源模板。
    prepare_template, prepare_handles = _template(
        PREPARE_TEMPLATE_UUID,
        name="prepare",
        handles=[
            _handle(
                PREPARE_SAMPLE_TARGET,
                node_template_uuid=PREPARE_TEMPLATE_UUID,
                key="sample",
                io_type="target",
                value_type="ResourceSlot",
                required=True,
            )
        ],
    )
    prepare_handles[0]["meta_data"]["unilab"][
        "value_schema"
    ] = {
        "$slot": "ResourceSlot",
        "allowed_resource_template_uuids": [S3_WAREHOUSE_TEMPLATE_UUID],
    }
    return AuthoringCatalogSnapshot.from_entities(
        [source_template, prepare_template],
        [source_handle, *prepare_handles],
        resource_template_symbols={PLATE_SOURCE_SYMBOL: PLATE_TEMPLATE_UUID},
    )


def _resolver() -> _ResourceReferenceResolver:
    """返回能解析真实 SZLab S3 空烧杯仓的库存端口替身。

    参数：无。返回：业务 ID 唯一映射到实际物料 UUID 与模板 UUID 的解析器。
    异常：无。
    """

    return _ResourceReferenceResolver(
        {
            "s3_unused_beaker": {
                "uuid": S3_WAREHOUSE_UUID,
                "resource_template_uuid": S3_WAREHOUSE_TEMPLATE_UUID,
            }
        }
    )


def _material_source_code() -> str:
    """生成使用真实 SZLab 业务资源 ID 的物料来源作者源码。

    参数：无。返回：``mount`` 使用 ``resource_ref('s3_unused_beaker')`` 的可信
    Python 源码。异常：无；源码只用于静态编译，不创建工作流任务
    （WorkflowTask）或执行动作。
    """

    return f'''from lab.resources import plate_96
from unilabos.workflow.authoring import (
    MaterialFlowRole,
    material_source,
    resource_ref,
    workflow,
    workflow_output,
)


@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="S07 material source")
def s07_material_source():
    # unilab:node_uuid={MATERIAL_SOURCE_NODE_UUID}
    beaker = material_source(
        resource_template=plate_96,
        mode="existing",
        mount=resource_ref("s3_unused_beaker"),
        material_uuid=None,
        site=None,
        slot_range=None,
        flow_role=MaterialFlowRole.PRIMARY_SAMPLE,
    )
    return workflow_output()
'''


def test_szlab_material_source_resource_id_resolves_to_actual_material_uuid() -> None:
    """SZLab 物料来源（MaterialSource）挂载业务 ID 必须解析为实际物料 UUID。

    参数：无。返回：无。断言：公共编译结果有效，选择器仅冻结库存权威返回的
    实际物料 UUID，不能把 ``s3_unused_beaker`` 业务 ID 冒充 UUID；本测试不
    创建工作流任务（WorkflowTask）或执行动作。
    """

    # ``engine`` 是注入只读库存身份解析端口的可信工作流创作编译器。
    engine = WorkflowAuthoringEngine(
        catalog=_catalog(),
        resource_reference_resolver=_resolver(),
    )
    # ``compiled`` 是仅含候选工作流图的静态编译回执。
    compiled = engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=_material_source_code(),
        source_uri="package://szlab/workflows/s07_material_source.py",
        applied_graph=_applied_graph(),
    )

    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    # ``source_node`` 是物料来源（MaterialSource）候选节点，不是可执行动作节点。
    source_node = next(
        node
        for node in compiled.graph["nodes"]
        if node["workflow_node_template_uuid"] == MATERIAL_SOURCE_TEMPLATE_UUID
    )
    assert source_node["param"]["mount"] == {"uuid": S3_WAREHOUSE_UUID}
    assert source_node["param"]["mount"]["uuid"] != "s3_unused_beaker"

