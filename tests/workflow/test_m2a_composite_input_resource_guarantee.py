"""M2A 组合工作流输入物料模板保证的公开编译回归测试。"""

from __future__ import annotations

from pathlib import Path

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.catalog import NodeTemplateImport
from unilabos.workflow.composite import CompositeAuthoring
from unilabos.workflow.models import CandidateCompilation

from .c1_r2_static_expansion_fixture import (
    AUTHORITY,
    MATERIAL_TEMPLATE_A_UUID,
    MATERIAL_TEMPLATE_B_UUID,
    PARENT_WORKFLOW_UUID,
    SECOND_ACTION_READY_SOURCE_UUID,
    SECOND_ACTION_READY_TARGET_UUID,
    SECOND_ACTION_RESOURCE_TEMPLATE_UUID,
    SECOND_ACTION_TEMPLATE_UUID,
    SECOND_ACTION_VALUE_SOURCE_UUID,
    SECOND_ACTION_VALUE_TARGET_UUID,
    action_import,
    make_direct_world,
    resource_slot_schema,
)

CONSUMER_CLASS = "tests.c1_r2:FixtureConsumer"
CONSUMER_NODE_UUID = "12111111-1111-4111-8111-111111111111"
INVOCATION_NODE_UUID = "11111111-1111-4111-8111-111111111111"
RESOURCE_SYMBOL = "tests.c1_r3.resources:material_b"


class _ResourceTemplateIdentityIndex:
    """为父工作流输入解析唯一的资源模板身份。"""

    def resolve_symbol(self, qualified_name: str) -> str:
        """把测试资源符号解析为物料模板 UUID。

        参数：
            qualified_name: 工作流源码中的绝对资源符号。

        返回：
            与该符号对应的资源模板 UUID。

        异常：
            LookupError: 符号不属于本测试固定目录。
        """

        if qualified_name != RESOURCE_SYMBOL:
            raise LookupError(qualified_name)
        return MATERIAL_TEMPLATE_B_UUID

    def identify_uuid(self, resource_template_uuid: str) -> str:
        """把物料模板 UUID 反向映射为规范资源符号。

        参数：
            resource_template_uuid: 待识别的资源模板 UUID。

        返回：
            规范化 Python 应使用的绝对资源符号。

        异常：
            LookupError: UUID 不属于本测试固定目录。
        """

        if resource_template_uuid != MATERIAL_TEMPLATE_B_UUID:
            raise LookupError(resource_template_uuid)
        return RESOURCE_SYMBOL


def _consumer_import(*, allowed_resource_template_uuid: str) -> NodeTemplateImport:
    """构造限制单一物料模板的下游动作模板。

    参数：
        allowed_resource_template_uuid: 下游物料占位符允许的资源模板 UUID。

    返回：
        可发布到测试模板目录的下游动作模板。
    """

    imported = action_import(
        template_uuid=SECOND_ACTION_TEMPLATE_UUID,
        resource_template_uuid=SECOND_ACTION_RESOURCE_TEMPLATE_UUID,
        target_handle_uuid=SECOND_ACTION_VALUE_TARGET_UUID,
        source_handle_uuid=SECOND_ACTION_VALUE_SOURCE_UUID,
        ready_target_uuid=SECOND_ACTION_READY_TARGET_UUID,
        ready_source_uuid=SECOND_ACTION_READY_SOURCE_UUID,
        value_schema=resource_slot_schema(allowed_resource_template_uuid),
    )
    imported.template["class"] = CONSUMER_CLASS
    imported.template["name"] = "consume"
    return imported


def _source() -> str:
    """生成父输入穿过组合工作流边界后供给下游动作的源码。

    返回：
        包含受限父物料输入、组合调用和下游消费动作的规范工作流源码。
    """

    return f'''from typing import Annotated

from tests.c1_r2 import FixtureConsumer
from tests.c1_r2.child import child
from tests.c1_r3.resources import material_b
from unilabos.registry.annotations import AllowedResourceTemplates
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, workflow_definition


consumer: FixtureConsumer = device()


@workflow_definition(
    workflow_uuid="{PARENT_WORKFLOW_UUID}",
    displayname="组合工作流输入物料透传",
)
def parent(
    *,
    value: Annotated[ResourceSlot, AllowedResourceTemplates(material_b)],
) -> None:
    # unilab:node_uuid={INVOCATION_NODE_UUID}
    forwarded = child(value=value)
    # unilab:node_uuid={CONSUMER_NODE_UUID}
    consumed = consumer.consume(value=forwarded.value)
'''


def _compile(
    tmp_path: Path,
    *,
    consumer_resource_template_uuid: str,
) -> CandidateCompilation:
    """通过真实创作编译 seam 编译组合物料透传案例。

    参数：
        tmp_path: 隔离工作流数据库所在的临时目录。
        consumer_resource_template_uuid: 下游物料占位符允许的资源模板 UUID。

    返回：
        工作流创作引擎给出的候选编译结果。
    """

    world = make_direct_world(
        tmp_path,
        input_schema=resource_slot_schema(),
        output_schema=resource_slot_schema(),
    )
    try:
        # 组合工作流调用（CompositeWorkflowInvocation）的公开边界输出必须保持
        # “无模板限制的隐式透传”，使保证只能来自父工作流输入而非输出句柄自身。
        published_child = world.imports[world.child.template_uuid]
        child_boundary_source = next(
            handle
            for handle in published_child.handles
            if handle["io_type"] == "source" and handle["handle_key"] == "value"
        )
        boundary_contract = child_boundary_source["meta_data"]["unilab"]
        assert boundary_contract["implicit_passthrough"] is True
        assert boundary_contract["allowed_resource_template_uuids"] is None

        world.imports[SECOND_ACTION_TEMPLATE_UUID] = _consumer_import(
            allowed_resource_template_uuid=consumer_resource_template_uuid
        )
        world.publish()
        engine = WorkflowAuthoringEngine(
            catalog=world.catalog,
            authority=AUTHORITY,
            resource_template_identity_index=_ResourceTemplateIdentityIndex(),
            composite_authoring=CompositeAuthoring(
                store=world.store,
                catalog=world.catalog,
                authority=AUTHORITY,
                resolver=world.resolver,
            ),
        )
        return engine.compile(
            workflow_uuid=PARENT_WORKFLOW_UUID,
            workflow_revision=1,
            python_source=_source(),
            source_uri="package://tests/m2a/workflows/composite_input.py",
            applied_graph=world.store.get_graph(PARENT_WORKFLOW_UUID),
        )
    finally:
        world.close()


def test_matching_parent_workflow_input_guarantee_reaches_composite_output(
    tmp_path: Path,
) -> None:
    """证明同模板父输入可穿过隐式组合输出并供给受限下游物料占位符。"""

    compiled = _compile(
        tmp_path,
        consumer_resource_template_uuid=MATERIAL_TEMPLATE_B_UUID,
    )

    assert compiled.valid, compiled.diagnostics
    assert compiled.graph is not None


def test_incompatible_parent_workflow_input_guarantee_remains_rejected(
    tmp_path: Path,
) -> None:
    """证明组合边界不能把不兼容父输入伪装成下游可接受的物料模板。"""

    compiled = _compile(
        tmp_path,
        consumer_resource_template_uuid=MATERIAL_TEMPLATE_A_UUID,
    )

    assert not compiled.valid
    assert compiled.graph is None
    assert [item["code"] for item in compiled.diagnostics] == ["candidate_invalid"]
