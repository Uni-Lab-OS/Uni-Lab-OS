"""F05.2 物料图校验（Material Graph Validation）的纯图测试夹具。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_identity import authoring_edge_uuid
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
    MATERIAL_SOURCE_HANDLE_UUID,
    MATERIAL_SOURCE_NODE_UUID,
    PLATE_SOURCE_SYMBOL,
    PLATE_TEMPLATE_UUID,
    PREPARE_NODE_UUID,
    _material_source_template,
    _source,
)

SECOND_CONSUMER_NODE_UUID = "20000000-0000-4000-8000-000000000012"
PASSTHROUGH_NODE_UUID = "20000000-0000-4000-8000-000000000013"
PASSTHROUGH_TEMPLATE_UUID = "30000000-0000-4000-8000-000000000013"
PASSTHROUGH_TARGET_UUID = "40000000-0000-4000-8000-000000000013"
PASSTHROUGH_SOURCE_UUID = "40000000-0000-4000-8000-000000000014"


def material_graph_engine(
    *,
    include_passthrough: bool = False,
) -> WorkflowAuthoringEngine:
    """构造 F05.2 所需的纯工作流创作编译器（Authoring Compiler）。

    参数说明：``include_passthrough`` 决定是否加入带同名输入/输出的普通动作
    （Action）；函数中的模板与连接点（Handle）局部变量组成不可变目录快照。
    返回：不访问数据库或库存（Inventory）的编译器。
    """

    source_template, source_handle = _material_source_template()
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
    templates = [source_template, prepare_template]
    handles = [source_handle, *prepare_handles]
    if include_passthrough:
        passthrough_template, passthrough_handles = _template(
            PASSTHROUGH_TEMPLATE_UUID,
            name="pass_material",
            handles=[
                _handle(
                    PASSTHROUGH_TARGET_UUID,
                    node_template_uuid=PASSTHROUGH_TEMPLATE_UUID,
                    key="sample",
                    io_type="target",
                    value_type="ResourceSlot",
                    required=True,
                ),
                _handle(
                    PASSTHROUGH_SOURCE_UUID,
                    node_template_uuid=PASSTHROUGH_TEMPLATE_UUID,
                    key="sample",
                    io_type="source",
                    value_type="ResourceSlot",
                ),
            ],
        )
        passthrough_handles[1]["meta_data"]["unilab"]["implicit_passthrough"] = True
        templates.append(passthrough_template)
        handles.extend(passthrough_handles)
    return WorkflowAuthoringEngine(
        catalog=AuthoringCatalogSnapshot.from_entities(
            templates,
            handles,
            resource_template_symbols={PLATE_SOURCE_SYMBOL: PLATE_TEMPLATE_UUID},
        )
    )


def compile_material_source_graph(
    engine: WorkflowAuthoringEngine,
    source: str,
):
    """通过公共接缝编译一份物料来源（MaterialSource）作者源码。

    参数说明：``engine`` 是纯编译器，``source`` 是待验证 Python 文本。
    返回：候选编译结果（CandidateCompilation）；函数不读取实时库存。
    """

    return engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=source,
        source_uri="package://lab/workflows/f05_material_graph.py",
        applied_graph=_applied_graph(),
    )


def single_chain_source() -> str:
    """返回物料来源到单一消费动作的合法线性源码。"""

    return _source()


def fan_out_source() -> str:
    """返回同一物料占位符（ResourceSlot）被两个动作消费的非法源码。"""

    return _source().replace(
        "    prepared = reactor.prepare(sample=assay_plate)",
        (
            "    prepared = reactor.prepare(sample=assay_plate)\n"
            f"    # unilab:node_uuid={SECOND_CONSUMER_NODE_UUID}\n"
            "    duplicate = reactor.prepare(sample=assay_plate)"
        ),
    )


def passthrough_chain_source() -> str:
    """返回同名物料输入/输出顺序透传到最终动作的合法源码。"""

    return _source().replace(
        (
            f"    # unilab:node_uuid={PREPARE_NODE_UUID}\n"
            "    prepared = reactor.prepare(sample=assay_plate)"
        ),
        (
            f"    # unilab:node_uuid={PASSTHROUGH_NODE_UUID}\n"
            "    passed = reactor.pass_material(sample=assay_plate)\n"
            f"    # unilab:node_uuid={PREPARE_NODE_UUID}\n"
            "    prepared = reactor.prepare(sample=passed.sample)"
        ),
    )


def fan_out_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """从合法单链候选构造同一物料输出的双消费反例。

    参数说明：``candidate`` 是合法后端（Backend）形状图；``duplicate_node``
    与 ``duplicate_edge`` 是保持模板身份、只改变节点与边身份的第二消费者。
    返回：不修改输入的非法完整图。
    """

    graph = deepcopy(candidate)
    original_node = next(
        node for node in graph["nodes"] if node["uuid"] == PREPARE_NODE_UUID
    )
    duplicate_node = deepcopy(original_node)
    duplicate_node["uuid"] = SECOND_CONSUMER_NODE_UUID
    duplicate_node["name"] = "Duplicate prepare"
    duplicate_node["meta_data"]["unilab"]["authoring_result_name"] = "duplicate"
    graph["nodes"].append(duplicate_node)
    original_edge = graph["edges"][0]
    duplicate_edge = deepcopy(original_edge)
    duplicate_edge.update(
        {
            "uuid": authoring_edge_uuid(
                workflow_uuid=WORKFLOW_UUID,
                source_node_uuid=MATERIAL_SOURCE_NODE_UUID,
                source_handle_uuid=MATERIAL_SOURCE_HANDLE_UUID,
                target_node_uuid=SECOND_CONSUMER_NODE_UUID,
                target_handle_uuid=PREPARE_SAMPLE_TARGET,
            ),
            "target_node_uuid": SECOND_CONSUMER_NODE_UUID,
        }
    )
    graph["edges"].append(duplicate_edge)
    graph["edges"].sort(key=lambda edge: edge["uuid"])
    return graph
