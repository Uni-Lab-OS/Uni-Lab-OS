"""F05.4-C0b 固定执行器（Fixed Executor）双投影合同测试。"""

from __future__ import annotations

from typing import Any

import pytest

from unilabos.workflow.authoring_ast import parse_authoring_source
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_graph import build_candidate_graph
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.models import CandidateCompilation

from .test_authoring_engine import WORKFLOW_UUID, _applied_graph, _template

# ``DEVICE_MATERIAL_UUID`` 是可信工作流（Workflow）源码固定选择的实际设备
# 物料（Material）稳定身份。
DEVICE_MATERIAL_UUID = "51000000-0000-4000-8000-000000000001"
# ``ACTION_NODE_UUID`` 是固定执行器（Fixed Executor）动作节点的稳定身份。
ACTION_NODE_UUID = "52000000-0000-4000-8000-000000000001"
# ``ACTION_TEMPLATE_UUID`` 只标识动作模板，绝不能冒充实际设备物料
# （Material）身份。
ACTION_TEMPLATE_UUID = "53000000-0000-4000-8000-000000000001"


def _catalog() -> AuthoringCatalogSnapshot:
    """构造带完整动作合同（Action Contract）的 ``ILab`` 目录快照。

    参数说明：无。返回：仅含一个设备动作模板（Action Template）、没有连接点
    （Handle）的不可变目录快照（Catalog Snapshot）。异常：夹具实体不满足目录
    约束时让构造异常直接暴露。
    """

    # ``action_template`` 是可信工作流（Workflow）编译冻结的唯一设备动作模板
    # （Action Template）。
    # ``handles`` 是该无参数动作的空连接点（Handle）合同集合。
    action_template, handles = _template(
        ACTION_TEMPLATE_UUID,
        name="prepare",
        handles=[],
    )
    action_template["node_type"] = "ILab"
    action_template["schema"] = {"type": "object", "properties": {}}
    action_template["meta_data"] = {
        "unilab": {
            "action_contract_schema": {
                "type": "object",
                "properties": {
                    "goal": {"type": "object", "properties": {}},
                },
                "required": ["goal"],
            }
        }
    }
    return AuthoringCatalogSnapshot.from_entities([action_template], handles)


def _source(device_identity: str | None) -> str:
    """构造只包含固定或动态设备选择的可信工作流（Workflow）源码。

    参数说明：``device_identity`` 为 ``None`` 时生成动态 ``device()``，否则把
    该字面量写入固定设备选择。返回：可由公共可信工作流编译器（Authoring
    Compiler）解析的 Python 源码。异常：无；非法身份由公共编译接缝给出
    结构化诊断。
    """

    # ``device_argument`` 是可信工作流（Workflow）源码中的静态设备选择表达式，
    # 不代表模板身份。
    device_argument = "" if device_identity is None else repr(device_identity)
    return f'''from lab.devices import Reactor
from unilabos.workflow.authoring import device, workflow, workflow_output


reactor: Reactor = device({device_argument})


@workflow(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="Fixed executor projection",
)
def fixed_executor_projection():
    # unilab:node_uuid={ACTION_NODE_UUID}
    prepared = reactor.prepare()
    return workflow_output()
'''


def _build_graph(device_identity: str | None) -> dict[str, Any]:
    """经公共构建接缝生成尚未进入候选包（Candidate Bundle）校验的候选图。

    参数说明：``device_identity`` 控制固定或动态执行器绑定（ExecutorBinding）。
    返回：``build_candidate_graph`` 生成的完整五集合候选图（Candidate Graph）。
    异常：可信工作流（Workflow）源码或目录映射非法时保留公共解析/候选构建
    异常。
    """

    # ``program`` 是不执行源码得到的可信工作流（Workflow）静态中间表示。
    program = parse_authoring_source(
        python_source=_source(device_identity),
        expected_workflow_uuid=WORKFLOW_UUID,
    )
    # ``graph`` 是待公共校验的完整候选图（Candidate Graph）；``_changeset`` 是
    # 同次构建产生、但本接缝测试不消费的变更集（Changeset）。
    graph, _changeset = build_candidate_graph(
        program=program,
        catalog=_catalog(),
        applied_graph=_applied_graph(),
    )
    return graph


def _compile(device_identity: str | None) -> CandidateCompilation:
    """经可信工作流编译器（Authoring Compiler）编译设备选择源码。

    参数说明：``device_identity`` 控制固定或动态执行器绑定（ExecutorBinding）。
    返回：公共编译接口产生的候选编译结果（CandidateCompilation）。异常：可信
    工作流编译器（Authoring Compiler）把领域失败收敛为诊断，不应向测试泄漏
    预期异常。
    """

    return WorkflowAuthoringEngine(catalog=_catalog()).compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=_source(device_identity),
        source_uri="package://lab/workflows/fixed_executor_projection.py",
        applied_graph=_applied_graph(),
    )


def test_fixed_device_material_identity_projects_to_both_candidate_fields() -> None:
    """固定实际设备物料（Material）身份必须投影到节点顶层和绑定元数据。

    参数说明：无。返回：无。异常/断言：公共候选构建结果的 ``material_uuid``
    或 ``executor_binding.device_id`` 不等于同一个规范 UUID 时测试失败。
    """

    # ``graph`` 是固定执行器（Fixed Executor）源码产生的候选图（Candidate
    # Graph）。
    graph = _build_graph(DEVICE_MATERIAL_UUID)
    # ``action_node`` 是可信工作流（Workflow）源码生成的唯一设备动作工作流节点
    # （WorkflowNode）。
    action_node = graph["nodes"][0]

    assert action_node["material_uuid"] == DEVICE_MATERIAL_UUID
    assert action_node["meta_data"]["unilab"]["executor_binding"] == {
        "mode": "fixed",
        "device_id": DEVICE_MATERIAL_UUID,
    }


def test_trusted_compile_freezes_same_device_identity_into_execution_plan() -> None:
    """可信编译与执行计划（ExecutionPlan）必须保留同一设备物料（Material）身份。

    参数说明：无。返回：无。异常/断言：候选编译无效，或执行计划中的顶层
    ``material_uuid`` 与固定 ``device_id`` 分叉时测试失败。
    """

    # ``compilation`` 是已穿越公共候选校验的可信工作流（Workflow）编译结果。
    compilation = _compile(DEVICE_MATERIAL_UUID)

    assert compilation.valid and compilation.graph is not None, compilation.diagnostics
    # ``execution_plan`` 是从已通过公共候选校验的冻结图派生的静态运行输入。
    # ``_jobs`` 是与执行计划（ExecutionPlan）同步生成、但本合同不检查的首次
    # 工作流节点作业（WorkflowNodeJob）集合。
    execution_plan, _jobs = ExecutionPlanBuilder().build(
        compilation.graph,
        run_mode="normal",
        target_node_uuid=None,
    )
    # ``planned_action`` 是执行计划中冻结同一实际设备物料（Material）身份的动作。
    planned_action = execution_plan["nodes"][0]
    assert planned_action["material_uuid"] == DEVICE_MATERIAL_UUID
    assert planned_action["device_id"] == DEVICE_MATERIAL_UUID


def test_fixed_device_projection_survives_authoring_round_trip() -> None:
    """固定执行器（Fixed Executor）双投影必须通过图与源码往返达到语义固定点。

    参数说明：无。返回：无。异常/断言：源码生成、重复编译失败，或重复候选图
    （Candidate Graph）改变实际设备物料（Material）身份与执行器绑定
    （ExecutorBinding）时测试失败。
    """

    # ``engine`` 是持有同一不可变目录快照的可信工作流编译器（Authoring
    # Compiler）。
    engine = WorkflowAuthoringEngine(catalog=_catalog())
    # ``compilation`` 是首次固定设备物料（Material）双投影编译结果。
    compilation = _compile(DEVICE_MATERIAL_UUID)
    assert compilation.valid and compilation.graph is not None, compilation.diagnostics
    # ``generated`` 是从候选图（Candidate Graph）确定性生成的规范源码结果。
    generated = engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=compilation.graph,
        source_uri="package://lab/workflows/generated_fixed_executor.py",
    )
    assert generated.valid and generated.normalized_python_source is not None
    # ``repeated`` 是规范源码重新编译后的候选结果，用于证明语义固定点。
    repeated = engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=generated.normalized_python_source,
        source_uri="package://lab/workflows/generated_fixed_executor.py",
        applied_graph=compilation.graph,
    )

    assert repeated.valid, repeated.diagnostics
    assert repeated.graph == compilation.graph


def test_dynamic_device_does_not_fabricate_concrete_material_identity() -> None:
    """动态设备选择不得伪造物料（Material）身份并须关闭失败（Fail-closed）。

    参数说明：无。返回：无。异常/断言：公共候选构建器写入具体
    ``material_uuid`` 或 ``executor_binding``，或可信工作流编译器（Authoring
    Compiler）放行当前不可执行的动态 ``ILab`` 节点时测试失败。
    """

    # ``graph`` 是动态设备选择产生、尚未进入候选包校验的候选图（Candidate
    # Graph）。
    graph = _build_graph(None)
    # ``action_node`` 是必须保持空实际设备物料（Material）身份的设备动作节点。
    action_node = graph["nodes"][0]
    # ``compilation`` 是应被公共图校验关闭失败（Fail-closed）的动态绑定编译
    # 结果。
    compilation = _compile(None)

    assert action_node["material_uuid"] is None
    assert "executor_binding" not in action_node["meta_data"]["unilab"]
    assert not compilation.valid
    assert compilation.graph is None
    assert [item["code"] for item in compilation.diagnostics] == ["candidate_invalid"]


@pytest.mark.parametrize(
    ("device_identity", "expected_code"),
    [
        pytest.param("", "invalid_device_selector", id="empty"),
        pytest.param("reactor-a", "candidate_invalid", id="non-uuid"),
    ],
)
def test_invalid_fixed_device_identity_fails_closed(
    device_identity: str,
    expected_code: str,
) -> None:
    """空值或非 UUID 固定身份不得冒充实际设备物料（Material）身份。

    参数说明：``device_identity`` 是不可信固定身份字面量，``expected_code`` 是
    对应公共诊断码。返回：无。异常/断言：可信编译器返回候选图或诊断码漂移时
    测试失败。
    """

    # ``compilation`` 是不可信固定身份经公共编译边界得到的失败结果。
    compilation = _compile(device_identity)

    assert not compilation.valid
    assert compilation.graph is None
    assert [item["code"] for item in compilation.diagnostics] == [expected_code]
