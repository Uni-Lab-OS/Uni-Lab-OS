"""F05.2 物料流线性（MaterialFlowLinearity）公共合同。"""

from __future__ import annotations

from typing import Any

import pytest

from unilabos.workflow.service import WorkflowError

from .f05_material_graph_fixtures import (
    PASSTHROUGH_NODE_UUID,
    PASSTHROUGH_SOURCE_UUID,
    compile_material_source_graph,
    fan_out_candidate,
    fan_out_source,
    material_graph_engine,
    passthrough_chain_source,
    single_chain_source,
)
from .test_authoring_engine import WORKFLOW_UUID
from .test_f05_material_source_direct_graph import (
    _DirectGraphContext,
    direct_graph_context,
)


def _diagnostic_codes(result: Any) -> list[str]:
    """提取候选编译结果中的稳定机器诊断码。

    参数说明：``result`` 是公共工作流创作编译结果。返回：保持诊断顺序的代码
    列表；该局部投影让三个公共接缝使用同一断言。
    """

    return [item["code"] for item in result.diagnostics]


def test_single_material_source_chain_is_valid() -> None:
    """单一物料来源到单一消费者应满足物料流线性。"""

    result = compile_material_source_graph(
        material_graph_engine(),
        single_chain_source(),
    )

    assert result.valid, result.diagnostics
    assert result.graph is not None
    assert len(result.graph["edges"]) == 1


def test_compile_rejects_one_material_output_consumed_twice() -> None:
    """源码编译必须拒绝同一物料输出的两条物理消费边。"""

    result = compile_material_source_graph(
        material_graph_engine(),
        fan_out_source(),
    )

    assert not result.valid
    assert result.graph is None
    assert result.normalized_python_source is None
    assert _diagnostic_codes(result) == ["material_flow_fan_out"]


@pytest.mark.parametrize("public_seam", ("generate_python", "validate"))
def test_graph_public_seams_reject_material_fan_out(
    public_seam: str,
) -> None:
    """两个图公共接缝必须返回同一物料分叉诊断。

    参数说明：``public_seam`` 选择确定性源码生成或图/源码共同验证。返回：无；
    两种入口都不得泄漏图或规范源码。
    """

    engine = material_graph_engine()
    compiled = compile_material_source_graph(engine, single_chain_source())
    assert compiled.valid and compiled.graph is not None
    graph = fan_out_candidate(compiled.graph)
    if public_seam == "generate_python":
        result = engine.generate_python(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=7,
            graph=graph,
            source_uri="package://lab/workflows/f05_material_graph.py",
        )
    else:
        result = engine.validate(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=7,
            graph=graph,
            python_source=single_chain_source(),
            source_uri="package://lab/workflows/f05_material_graph.py",
        )

    assert not result.valid
    assert result.graph is None
    assert result.normalized_python_source is None
    assert _diagnostic_codes(result) == ["material_flow_fan_out"]


def test_direct_save_rejects_material_fan_out_without_writes(
    direct_graph_context: _DirectGraphContext,
) -> None:
    """直接保存非法物料分叉必须回滚完整图和修订。

    参数说明：``direct_graph_context`` 提供独占 SQLite 工作流写模型。返回：无；
    ``before`` 是写入前权威投影，失败后必须逐字段相同。
    """

    graph = fan_out_candidate(direct_graph_context.candidate)
    before = direct_graph_context.service.get_graph(WORKFLOW_UUID)

    with pytest.raises(WorkflowError) as caught:
        direct_graph_context.service.save_graph(
            WORKFLOW_UUID,
            revision=1,
            nodes=graph["nodes"],
            edges=graph["edges"],
        )

    assert caught.value.code == "material_flow_fan_out"
    assert direct_graph_context.service.get_graph(WORKFLOW_UUID) == before
    assert before == direct_graph_context.applied_graph
    assert before["workflow"]["revision"] == 1


def test_implicit_passthrough_is_an_ordered_python_graph_fixed_point() -> None:
    """同名隐式物料透传必须形成单链并达到 Python↔图固定点。"""

    engine = material_graph_engine(include_passthrough=True)
    compiled = compile_material_source_graph(engine, passthrough_chain_source())
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    outgoing = [
        edge
        for edge in compiled.graph["edges"]
        if edge["source_node_uuid"] == PASSTHROUGH_NODE_UUID
        and edge["source_handle_uuid"] == PASSTHROUGH_SOURCE_UUID
    ]
    assert len(outgoing) == 1

    generated = engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=compiled.graph,
        source_uri="package://lab/workflows/f05_material_graph.py",
    )
    assert generated.valid and generated.normalized_python_source is not None
    assert "reactor.prepare(sample=passed.sample)" in generated.normalized_python_source
    repeated = compile_material_source_graph(
        engine,
        generated.normalized_python_source,
    )
    assert repeated.valid, repeated.diagnostics
    assert repeated.graph == compiled.graph


__all__ = ["direct_graph_context"]
