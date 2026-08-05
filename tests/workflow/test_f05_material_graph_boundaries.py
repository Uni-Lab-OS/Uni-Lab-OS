"""F05.2-C 工作流边界物料图（Material Graph）不变量的 RED 合同。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

import pytest

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_identity import authoring_edge_uuid
from unilabos.workflow.models import (
    CandidateCompilation,
    WorkflowEdgeWrite,
    WorkflowNodeWrite,
)
from unilabos.workflow.store import StoreAuthoringConflict

from .f05_material_graph_fixtures import (
    INCOMPATIBLE_TEMPLATE_UUID,
    PASSTHROUGH_NODE_UUID,
    PASSTHROUGH_SOURCE_UUID,
    PLATE_TEMPLATE_UUID,
    SECOND_CONSUMER_NODE_UUID,
    compile_material_source_graph,
    fan_out_candidate,
    material_graph_engine,
    opened_material_graph_store,
    passthrough_chain_source,
    single_chain_source,
)
from .test_authoring_engine import (
    PREPARE_SAMPLE_TARGET,
    WORKFLOW_UUID,
    _applied_graph,
)
from .test_f05_material_source_authoring import PREPARE_NODE_UUID

_SOURCE_URI = "package://lab/workflows/f05_material_graph_boundaries.py"


def _diagnostic_codes(result: CandidateCompilation) -> list[str]:
    """提取公共创作结果的稳定机器诊断码。

    参数说明：``result`` 是编译、源码生成或共同验证返回的候选结果。
    返回：保持公共接口顺序的诊断码列表；不读取内部异常或实现状态。
    """

    return [diagnostic["code"] for diagnostic in result.diagnostics]


def _workflow_input_source(*, fan_out: bool) -> str:
    """构造工作流输入（Workflow Input）物料占位符作者源码。

    参数说明：``fan_out`` 为真时让同一 ``sample`` 输入绑定到两个动作（Action），
    否则只绑定首个动作。返回：可由公共静态编译器解析的确定性 Python 文本；
    文本只声明工作流创作（Workflow Authoring）事实，不访问物料权威。
    """

    # ``second_consumer`` 是非法第二物理消费者的完整静态声明；关闭时不留下节点。
    second_consumer = (
        (
            f"    # unilab:node_uuid={SECOND_CONSUMER_NODE_UUID}\n"
            "    duplicate = reactor.prepare(sample=sample)\n"
        )
        if fan_out
        else ""
    )
    return f'''from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, workflow, workflow_output


reactor: Reactor = device()


@workflow(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="Workflow input material boundary",
)
def workflow_input_material_boundary(*, sample: ResourceSlot):
    # unilab:node_uuid={PREPARE_NODE_UUID}
    primary = reactor.prepare(sample=sample)
{second_consumer}    return workflow_output(sample=sample)
'''


def _compile_workflow_input(
    engine: WorkflowAuthoringEngine,
    *,
    fan_out: bool,
) -> CandidateCompilation:
    """通过公共编译接缝解析工作流输入（Workflow Input）边界源码。

    参数说明：``engine`` 是冻结目录的工作流创作编译器，``fan_out`` 选择单链
    或双物理消费者。返回：公共候选编译结果；编译不执行作者源码或查询库存。
    """

    return engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=_workflow_input_source(fan_out=fan_out),
        source_uri=_SOURCE_URI,
        applied_graph=_applied_graph(),
    )


def _workflow_input_fan_out_candidate(
    engine: WorkflowAuthoringEngine,
) -> dict[str, Any]:
    """从合法单消费者候选构造工作流输入双绑定反例。

    参数说明：``engine`` 通过公共编译接缝产生合法基线。局部 ``duplicate_node``
    保留同一输入绑定，只获得不同节点 UUID。返回：不修改基线结果的非法完整图；
    若合法基线本身不能编译，测试准备立即以断言失败。
    """

    compiled = _compile_workflow_input(engine, fan_out=False)
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    graph = deepcopy(compiled.graph)
    original_node = next(
        node for node in graph["nodes"] if node["uuid"] == PREPARE_NODE_UUID
    )
    duplicate_node = deepcopy(original_node)
    duplicate_node["uuid"] = SECOND_CONSUMER_NODE_UUID
    duplicate_node["meta_data"]["unilab"]["authoring_result_name"] = "duplicate"
    graph["nodes"].append(duplicate_node)
    graph["nodes"].sort(key=lambda node: node["uuid"])
    return graph


def _list_resource_slot_schema(
    allowlist: tuple[str, ...],
) -> dict[str, Any]:
    """构造数组物料占位符（list[ResourceSlot]）值 Schema。

    参数说明：``allowlist`` 是每个数组成员允许的资源模板（ResourceTemplate）
    UUID 闭集。返回：规范数组 Schema；空集合不在本轮测试输入中。
    """

    return {
        "type": "array",
        "items": {
            "$slot": "ResourceSlot",
            "allowed_resource_template_uuids": list(allowlist),
        },
    }


def _replace_handle_with_list_schema(
    graph: dict[str, Any],
    *,
    handle_uuid: str,
    allowlist: tuple[str, ...],
) -> None:
    """把候选连接点（Handle）改为数组物料占位符 Schema。

    参数说明：``graph`` 是本测试独占可变候选，``handle_uuid`` 是目标连接点稳定
    身份，``allowlist`` 是数组成员资源模板闭集。返回：无；只原地修改候选目录
    投影，不修改编译器目录或持久权威；目标缺失时抛出 ``StopIteration``。
    """

    # ``handle`` 是候选五集合中的目录投影，不是运行时物料实例或库存事实。
    handle = next(
        item for item in graph["handle_templates"] if item["uuid"] == handle_uuid
    )
    unilab = handle["meta_data"]["unilab"]
    unilab["value_schema"] = _list_resource_slot_schema(allowlist)
    unilab["allowed_resource_template_uuids"] = list(allowlist)
    handle["type"] = "array"


def _duplicate_edge_consumer(
    graph: dict[str, Any],
    *,
    source_node_uuid: str,
    source_handle_uuid: str,
    disabled: bool = False,
) -> None:
    """为一个物料输出加入第二物理消费者。

    参数说明：``graph`` 是本测试独占候选；两个来源 UUID 标识待分叉输出；
    ``disabled`` 决定第二消费者是否禁用。返回：无；原地加入一个新节点和一条
    确定性边，保持第一个消费者不变；来源边缺失时抛出 ``StopIteration``。
    """

    original_node = next(
        node for node in graph["nodes"] if node["uuid"] == PREPARE_NODE_UUID
    )
    duplicate_node = deepcopy(original_node)
    duplicate_node["uuid"] = SECOND_CONSUMER_NODE_UUID
    duplicate_node["disabled"] = disabled
    duplicate_node["meta_data"]["unilab"]["authoring_result_name"] = "duplicate"
    graph["nodes"].append(duplicate_node)
    graph["nodes"].sort(key=lambda node: node["uuid"])

    original_edge = next(
        edge
        for edge in graph["edges"]
        if edge["source_node_uuid"] == source_node_uuid
        and edge["source_handle_uuid"] == source_handle_uuid
        and edge["target_node_uuid"] == PREPARE_NODE_UUID
    )
    duplicate_edge = deepcopy(original_edge)
    duplicate_edge.update(
        {
            "uuid": authoring_edge_uuid(
                workflow_uuid=WORKFLOW_UUID,
                source_node_uuid=source_node_uuid,
                source_handle_uuid=source_handle_uuid,
                target_node_uuid=SECOND_CONSUMER_NODE_UUID,
                target_handle_uuid=str(original_edge["target_handle_uuid"]),
            ),
            "target_node_uuid": SECOND_CONSUMER_NODE_UUID,
        }
    )
    graph["edges"].append(duplicate_edge)
    graph["edges"].sort(key=lambda edge: edge["uuid"])


def _list_boundary_candidate(
    *,
    consumer_allowlist: tuple[str, ...],
    fan_out: bool,
) -> tuple[WorkflowAuthoringEngine, dict[str, Any]]:
    """构造显式动作输出的数组物料边界候选。

    参数说明：``consumer_allowlist`` 是最终消费者接受的资源模板闭集，
    ``fan_out`` 决定是否复制最终消费者。返回：公共编译器与其合法单链候选的
    独占数组 Schema 变体；基线编译失败时测试准备以断言失败。
    """

    engine = material_graph_engine(
        include_passthrough=True,
        prepare_allowlist=(PLATE_TEMPLATE_UUID,),
        passthrough_input_allowlist=(PLATE_TEMPLATE_UUID,),
        passthrough_output_allowlist=(PLATE_TEMPLATE_UUID,),
        passthrough_implicit=False,
    )
    compiled = compile_material_source_graph(engine, passthrough_chain_source())
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    graph = deepcopy(compiled.graph)
    _replace_handle_with_list_schema(
        graph,
        handle_uuid=PASSTHROUGH_SOURCE_UUID,
        allowlist=(PLATE_TEMPLATE_UUID,),
    )
    _replace_handle_with_list_schema(
        graph,
        handle_uuid=PREPARE_SAMPLE_TARGET,
        allowlist=consumer_allowlist,
    )
    if fan_out:
        _duplicate_edge_consumer(
            graph,
            source_node_uuid=PASSTHROUGH_NODE_UUID,
            source_handle_uuid=PASSTHROUGH_SOURCE_UUID,
        )
    return engine, graph


def test_compile_rejects_workflow_input_material_bound_to_two_actions() -> None:
    """编译必须拒绝同一工作流输入物料被两个动作消费。

    参数：无。返回：无；断言关闭失败且只产生 ``material_flow_fan_out``，不得
    泄漏候选图或规范源码。异常：公共编译接缝不得泄漏内部异常。
    """

    result = _compile_workflow_input(material_graph_engine(), fan_out=True)

    assert not result.valid
    assert result.graph is None
    assert result.normalized_python_source is None
    assert _diagnostic_codes(result) == ["material_flow_fan_out"]


@pytest.mark.parametrize("public_seam", ("generate_python", "validate"))
def test_graph_public_seams_reject_workflow_input_material_fan_out(
    public_seam: Literal["generate_python", "validate"],
) -> None:
    """图公共接缝必须统一拒绝工作流输入物料双绑定。

    参数说明：``public_seam`` 选择确定性源码生成或图/源码共同验证。返回：无；
    两种入口均只返回 ``material_flow_fan_out``，不得泄漏候选或规范源码。
    """

    engine = material_graph_engine()
    # ``graph`` 是从合法单消费者候选复制出的双绑定反例，不依赖未来 RED 实现。
    graph = _workflow_input_fan_out_candidate(engine)
    if public_seam == "generate_python":
        result = engine.generate_python(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=7,
            graph=graph,
            source_uri=_SOURCE_URI,
        )
    else:
        result = engine.validate(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=7,
            graph=graph,
            python_source=_workflow_input_source(fan_out=True),
            source_uri=_SOURCE_URI,
        )

    assert not result.valid
    assert result.graph is None
    assert result.normalized_python_source is None
    assert _diagnostic_codes(result) == ["material_flow_fan_out"]


def test_direct_save_rejects_workflow_input_material_fan_out_without_writes(
    tmp_path: Path,
) -> None:
    """直接保存工作流输入物料双绑定必须保持图和修订零写入。

    参数说明：``tmp_path`` 提供隔离 SQLite 文件。返回：无；``before`` 是保存前
    工作流权威投影，拒绝后必须逐字段相同。异常：公共保存接缝必须抛出错误码为
    ``material_flow_fan_out`` 的 ``StoreAuthoringConflict``。
    """

    engine = material_graph_engine()
    graph = _workflow_input_fan_out_candidate(engine)
    with opened_material_graph_store(
        tmp_path / "workflow.db",
        prepare_allowlist=None,
        workflow_meta_data=graph["workflow"]["meta_data"],
    ) as context:
        before = context.service.get_graph(WORKFLOW_UUID)
        with pytest.raises(StoreAuthoringConflict) as caught:
            context.store.save_graph(
                WORKFLOW_UUID,
                revision=1,
                nodes=[
                    WorkflowNodeWrite.model_validate(node) for node in graph["nodes"]
                ],
                edges=[
                    WorkflowEdgeWrite.model_validate(edge) for edge in graph["edges"]
                ],
                protect_reserved_metadata=False,
                validate_workflow_io_contract=True,
            )

        assert caught.value.code == "material_flow_fan_out"
        assert context.service.get_graph(WORKFLOW_UUID) == before
        assert before == context.applied_graph
        assert before["workflow"]["revision"] == 1


def test_list_resource_slot_output_fan_out_is_rejected() -> None:
    """数组物料占位符输出仍必须满足物料流线性。

    参数：无。返回：无；公共源码生成接缝必须以 ``material_flow_fan_out`` 拒绝
    同一 list[ResourceSlot] 输出的两个消费者，不得把数组当成普通数据边。
    """

    engine, graph = _list_boundary_candidate(
        consumer_allowlist=(PLATE_TEMPLATE_UUID,),
        fan_out=True,
    )
    result = engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=graph,
        source_uri=_SOURCE_URI,
    )

    assert not result.valid
    assert result.graph is None
    assert result.normalized_python_source is None
    assert _diagnostic_codes(result) == ["material_flow_fan_out"]


def test_list_resource_slot_producer_must_satisfy_consumer_template() -> None:
    """数组物料生产保证必须满足消费者资源模板约束。

    参数：无。返回：无；公共源码生成接缝必须以
    ``material_template_mismatch`` 拒绝成员模板集合互斥的 list[ResourceSlot]，
    不得跳过嵌套 Schema。异常：公共接口不得泄漏内部 Schema 异常。
    """

    engine, graph = _list_boundary_candidate(
        consumer_allowlist=(INCOMPATIBLE_TEMPLATE_UUID,),
        fan_out=False,
    )
    result = engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=graph,
        source_uri=_SOURCE_URI,
    )

    assert not result.valid
    assert result.graph is None
    assert result.normalized_python_source is None
    assert _diagnostic_codes(result) == ["material_template_mismatch"]


def test_disabled_second_material_consumer_still_counts_as_fan_out() -> None:
    """禁用节点不得把非法物料分叉隐藏在持久候选图中。

    参数：无。返回：无；公共源码生成接缝必须以 ``material_flow_fan_out`` 拒绝
    含禁用第二消费者的完整图，禁用状态不改变物料占位符身份或安全不变量。
    """

    engine = material_graph_engine()
    compiled = compile_material_source_graph(engine, single_chain_source())
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    graph = fan_out_candidate(compiled.graph)
    second_consumer = next(
        node for node in graph["nodes"] if node["uuid"] == SECOND_CONSUMER_NODE_UUID
    )
    second_consumer["disabled"] = True

    result = engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=graph,
        source_uri=_SOURCE_URI,
    )

    assert not result.valid
    assert result.graph is None
    assert result.normalized_python_source is None
    assert _diagnostic_codes(result) == ["material_flow_fan_out"]
