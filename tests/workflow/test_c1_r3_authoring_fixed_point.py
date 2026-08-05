"""F06 R3 已发布工作流调用的创作固定点 RED。"""

from __future__ import annotations

import sys
from copy import deepcopy
from typing import Any

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.models import CandidateCompilation

from .test_c1_r2_static_expansion_contract import (
    ACTION_RESOURCE_TEMPLATE_UUID,
    CHILD_WORKFLOW_UUID,
    INVOCATION_UUID,
    PARENT_WORKFLOW_UUID,
    _world_components,
)

CHILD_MODULE = "c1_published_lab.workflows.child"
CHILD_SYMBOL = "prepare_sample"
PRECEDING_ACTION_UUID = "11111111-1111-4111-8111-111111111121"
FOLLOWING_ACTION_UUID = "11111111-1111-4111-8111-111111111122"


def _applied_parent_graph() -> dict[str, Any]:
    """构造首次编译使用的空父工作流应用图。

    参数：无。返回：修订为一的空父图。异常：无。
    """

    return {
        "workflow": {
            "uuid": PARENT_WORKFLOW_UUID,
            "revision": 1,
            "name": "Persisted parent",
            "description": None,
            "tags": [],
            "meta_data": {},
        },
        "nodes": [],
        "edges": [],
        "node_templates": [],
        "handle_templates": [],
    }


def _source() -> str:
    """返回一个通过绝对导入调用已发布子工作流的规范作者源码。

    参数：无。返回：包含固定调用身份的 Python 源码。异常：无。
    """

    return f'''from typing import TypedDict

from {CHILD_MODULE} import {CHILD_SYMBOL}
from unilabos.workflow.authoring import workflow


class ParentResult(TypedDict):
    result: float


@workflow(
    workflow_uuid="{PARENT_WORKFLOW_UUID}",
    displayname="Composite parent",
)
def composite_parent(*, value: float) -> ParentResult:
    # unilab:node_uuid={INVOCATION_UUID}
    result = {CHILD_SYMBOL}(value=value)
    return {{"result": result.result}}
'''


def _source_with_surrounding_actions() -> str:
    """返回把已发布工作流调用放在两个普通动作之间的作者源码。

    参数：无。返回：包含两个结构性 ready 依赖的 Python 源码。异常：无。
    """

    return f'''from typing import TypedDict

from c1_published_lab.devices import Measure
from {CHILD_MODULE} import {CHILD_SYMBOL}
from unilabos.workflow.authoring import device, workflow


class ParentResult(TypedDict):
    result: float


measure: Measure = device("{ACTION_RESOURCE_TEMPLATE_UUID}")


@workflow(
    workflow_uuid="{PARENT_WORKFLOW_UUID}",
    displayname="Composite parent",
)
def composite_parent(*, value: float) -> ParentResult:
    # unilab:node_uuid={PRECEDING_ACTION_UUID}
    prepared = measure.measure(value=value)
    # unilab:node_uuid={INVOCATION_UUID}
    child = {CHILD_SYMBOL}(value=value)
    # unilab:node_uuid={FOLLOWING_ACTION_UUID}
    finalized = measure.measure(value=value)
    return {{"result": finalized.result}}
'''


def _engine() -> WorkflowAuthoringEngine:
    """装配固定目录与只读组合创作端口的工作流创作编译器。

    参数：无。返回：绑定冻结目录和组合端口的编译器。异常：夹具目录无效时
    由构造器抛出。
    """

    authoring, _provider, catalog, _resolver = _world_components()
    return WorkflowAuthoringEngine(
        catalog=catalog,
        composite_authoring=authoring,
    )


def _compile(
    engine: WorkflowAuthoringEngine,
    source: str,
    graph: dict[str, Any],
) -> CandidateCompilation:
    """经公共编译接口生成父工作流候选结果。

    参数：``engine`` 是编译器，``source`` 是作者源码，``graph`` 是应用基线。
    返回：结构化候选编译结果。异常：公共编译接口未收敛的异常原样传播。
    """

    return engine.compile(
        workflow_uuid=PARENT_WORKFLOW_UUID,
        workflow_revision=1,
        python_source=source,
        source_uri="package://c1_published_lab/workflows/parent.py",
        applied_graph=graph,
    )


def test_absolute_published_workflow_call_is_a_canonical_fixed_point() -> None:
    """绝对调用静态展开后，生成源码和再次编译保持完整语义固定点。

    参数：无。返回：无；断言图、源码和来源映射固定。异常：编译或断言失败时
    由 pytest 报告。
    """

    engine = _engine()
    assert CHILD_MODULE not in sys.modules

    compiled = _compile(engine, _source(), _applied_parent_graph())

    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    normalized = compiled.normalized_python_source
    assert normalized is not None
    assert f"from {CHILD_MODULE} import {CHILD_SYMBOL}" in normalized
    assert f"result = {CHILD_SYMBOL}(value=value)" in normalized
    assert CHILD_MODULE not in sys.modules
    assert [entry["workflow_node_uuid"] for entry in compiled.source_map] == [
        INVOCATION_UUID
    ]
    invocation = next(
        node for node in compiled.graph["nodes"] if node["uuid"] == INVOCATION_UUID
    )
    internal = [
        node for node in compiled.graph["nodes"] if node["uuid"] != INVOCATION_UUID
    ]
    assert internal and all(
        node["parent_uuid"] == INVOCATION_UUID for node in internal
    )
    assert invocation["meta_data"]["unilab"]["composite"][
        "child_workflow_uuid"
    ] == CHILD_WORKFLOW_UUID

    repeated = _compile(engine, normalized, compiled.graph)

    assert repeated.valid and repeated.graph == compiled.graph, repeated.diagnostics
    assert repeated.normalized_python_source == normalized
    assert repeated.source_map == compiled.source_map
    assert CHILD_MODULE not in sys.modules


def test_breaking_child_pin_fails_closed_at_compile_seam() -> None:
    """已应用调用节点的合同摘要被篡改时不得静默重写候选图。

    参数：无。返回：无；断言篡改 pin 只产生稳定诊断。异常：编译或断言失败时
    由 pytest 报告。
    """

    engine = _engine()
    compiled = _compile(engine, _source(), _applied_parent_graph())
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    stale = deepcopy(compiled.graph)
    invocation = next(
        node for node in stale["nodes"] if node["uuid"] == INVOCATION_UUID
    )
    invocation["meta_data"]["unilab"]["composite"]["contract_digest"] = (
        "sha256:" + "f" * 64
    )

    rejected = _compile(engine, _source(), stale)

    assert not rejected.valid
    assert rejected.graph is None
    assert [item["code"] for item in rejected.diagnostics] == [
        "composite_contract_stale"
    ]


def test_composite_between_actions_keeps_structural_ready_out_of_arguments() -> None:
    """组合工作流调用夹在普通动作间时仍保持可回编译固定点。

    参数：无。返回：无；断言结构性 ready 连接点（Handle）只形成边，不进入已
    发布工作流业务实参。异常：编译或断言失败时由 pytest 报告。
    """

    engine = _engine()
    compiled = _compile(
        engine,
        _source_with_surrounding_actions(),
        _applied_parent_graph(),
    )

    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    normalized = compiled.normalized_python_source
    assert normalized is not None
    assert "ready=" not in normalized
    repeated = _compile(engine, normalized, compiled.graph)
    assert repeated.valid and repeated.graph == compiled.graph, repeated.diagnostics
