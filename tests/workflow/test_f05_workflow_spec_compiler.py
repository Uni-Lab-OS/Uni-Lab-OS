"""F05.3-A 工作流规格编译器（WorkflowSpecCompiler）的失败关闭合同。"""

from __future__ import annotations

from importlib import import_module
from typing import Any

import pytest

WORKFLOW_UUID = "10000000-0000-4000-8000-000000000001"
TASK_UUID = "20000000-0000-4000-8000-000000000001"
SOURCE_NODE_UUID = "30000000-0000-4000-8000-000000000001"
FIRST_NODE_UUID = "30000000-0000-4000-8000-000000000002"
SECOND_NODE_UUID = "30000000-0000-4000-8000-000000000003"
SOURCE_JOB_UUID = "40000000-0000-4000-8000-000000000001"
FIRST_JOB_UUID = "40000000-0000-4000-8000-000000000002"
SECOND_JOB_UUID = "40000000-0000-4000-8000-000000000003"
MATERIAL_UUID = "50000000-0000-4000-8000-000000000001"
DEVICE_UUID = "60000000-0000-4000-8000-000000000001"


def _compiler_contract() -> tuple[type[Any], type[BaseException]]:
    """取得待实现编译器及其稳定错误类型。

    参数：无。返回：工作流规格编译器（WorkflowSpecCompiler）类型与编译错误
    类型。异常：生产模块尚未实现时保留 ``ModuleNotFoundError``，使每个行为
    测试独立呈现 RED，而不是在测试收集阶段中止。
    """

    module = import_module("unilabos.workflow.workflow_spec_compiler")
    return module.WorkflowSpecCompiler, module.WorkflowSpecCompilationError


def _task_snapshot(
    *,
    mode: str = "existing",
    material_uuid: str | None = MATERIAL_UUID,
    disable_first_consumer: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """构造冻结任务快照与工作流节点作业（WorkflowNodeJob）列表。

    参数：``mode`` 是物料来源模式（MaterialSourceMode）；``material_uuid`` 是
    任务提交前已固定的具体物料身份；``disable_first_consumer`` 控制物料链首个
    物理消费者是否禁用。返回：纯字典任务快照与按节点稳定绑定的作业列表。
    异常：无；非法组合由待测编译边界失败关闭。
    """

    # ``source_selector`` 是冻结的物料来源（MaterialSource）选择器，不允许
    # 编译器在运行时查询库存（Inventory）或补写具体物料身份。
    source_selector = {
        "mode": mode,
        "resource_template_uuid": "70000000-0000-4000-8000-000000000001",
        "mount": {"uuid": "70000000-0000-4000-8000-000000000002"},
        "material_uuid": material_uuid,
        "site": None,
        "slot_range": None,
        "flow_role": "primary_sample",
    }
    # ``snapshot_nodes`` 是已应用工作流图的不可变节点顺序；物料来源节点只提供
    # 任务物料绑定（TaskMaterialBinding），两个 ILab 节点才是可派发动作。
    snapshot_nodes = [
        {
            "uuid": SOURCE_NODE_UUID,
            "workflow_node_template_uuid": "71000000-0000-4000-8000-000000000001",
            "name": "固定孔板来源",
            "type": "material_source",
            "param": source_selector,
            "disabled": False,
        },
        {
            "uuid": FIRST_NODE_UUID,
            "workflow_node_template_uuid": "71000000-0000-4000-8000-000000000002",
            "material_uuid": DEVICE_UUID,
            "name": "加入预混液",
            "type": "ILab",
            "action_name": "distribute",
            "action_type": "UniLabJsonCommand",
            "param": {"plate": {"uuid": MATERIAL_UUID}},
            "disabled": disable_first_consumer,
        },
        {
            "uuid": SECOND_NODE_UUID,
            "workflow_node_template_uuid": "71000000-0000-4000-8000-000000000003",
            "material_uuid": DEVICE_UUID,
            "name": "读取孔板",
            "type": "ILab",
            "action_name": "read_plate",
            "action_type": "UniLabJsonCommand",
            "param": {"plate": {"uuid": MATERIAL_UUID}},
            "disabled": False,
        },
    ]
    # ``snapshot_edges`` 冻结同一物料占位符（ResourceSlot）的线性消费顺序。
    snapshot_edges = [
        {
            "uuid": "72000000-0000-4000-8000-000000000001",
            "source_node_uuid": SOURCE_NODE_UUID,
            "source_handle_uuid": "73000000-0000-4000-8000-000000000001",
            "target_node_uuid": FIRST_NODE_UUID,
            "target_handle_uuid": "73000000-0000-4000-8000-000000000002",
        },
        {
            "uuid": "72000000-0000-4000-8000-000000000002",
            "source_node_uuid": FIRST_NODE_UUID,
            "source_handle_uuid": "73000000-0000-4000-8000-000000000003",
            "target_node_uuid": SECOND_NODE_UUID,
            "target_handle_uuid": "73000000-0000-4000-8000-000000000004",
        },
    ]
    # ``handle_templates`` 冻结每条边是否传递物料占位符，而不让编译器从参数
    # 名称猜测物料语义。
    handle_templates = [
        {
            "uuid": "73000000-0000-4000-8000-000000000001",
            "workflow_node_template_uuid": "71000000-0000-4000-8000-000000000001",
            "handle_key": "material",
            "io_type": "source",
            "type": "ResourceSlot",
        },
        {
            "uuid": "73000000-0000-4000-8000-000000000002",
            "workflow_node_template_uuid": "71000000-0000-4000-8000-000000000002",
            "handle_key": "plate",
            "io_type": "target",
            "type": "ResourceSlot",
        },
        {
            "uuid": "73000000-0000-4000-8000-000000000003",
            "workflow_node_template_uuid": "71000000-0000-4000-8000-000000000002",
            "handle_key": "plate",
            "io_type": "source",
            "type": "ResourceSlot",
        },
        {
            "uuid": "73000000-0000-4000-8000-000000000004",
            "workflow_node_template_uuid": "71000000-0000-4000-8000-000000000003",
            "handle_key": "plate",
            "io_type": "target",
            "type": "ResourceSlot",
        },
    ]
    task_snapshot = {
        "uuid": TASK_UUID,
        "workflow_uuid": WORKFLOW_UUID,
        "workflow_snapshot": {
            "nodes": snapshot_nodes,
            "edges": snapshot_edges,
            "handle_templates": handle_templates,
        },
    }
    # ``jobs`` 是任务创建事务已经分配的稳定作业身份；编译不得重新生成 UUID。
    jobs = [
        {"uuid": SOURCE_JOB_UUID, "workflow_node_uuid": SOURCE_NODE_UUID},
        {"uuid": FIRST_JOB_UUID, "workflow_node_uuid": FIRST_NODE_UUID},
        {"uuid": SECOND_JOB_UUID, "workflow_node_uuid": SECOND_NODE_UUID},
    ]
    return task_snapshot, jobs


def test_fixed_existing_source_compiles_only_physical_consumers() -> None:
    """固定既有物料来源只为首个物理消费者建立一次物料需求。

    参数：无。返回：无；断言物料来源（MaterialSource）不派发，且首个动作获得
    唯一 ``MaterialRequirement(instance_uuid=...)``。异常：编译失败即测试失败。
    """

    compiler_type, _error_type = _compiler_contract()
    task_snapshot, jobs = _task_snapshot()

    spec = compiler_type().compile(task_snapshot, jobs)

    assert [node.id for node in spec.nodes] == [FIRST_NODE_UUID, SECOND_NODE_UUID]
    assert SOURCE_NODE_UUID not in {node.id for node in spec.nodes}
    assert [
        requirement.instance_uuid for requirement in spec.nodes[0].material_requirements
    ] == [MATERIAL_UUID]


def test_material_requirement_is_not_duplicated_on_downstream_consumer() -> None:
    """同一线性物料链的后续消费者不得重复申请任务物料预留。

    参数：无。返回：无；断言只有首个启用物理消费者携带短期整图物料预留输入，
    后续动作不重复；该兼容输入将在 K11 被正式任务物料预留
    （TaskMaterialReservation）替换。异常：编译失败即测试失败。
    """

    compiler_type, _error_type = _compiler_contract()
    task_snapshot, jobs = _task_snapshot()

    spec = compiler_type().compile(task_snapshot, jobs)

    requirements_by_node = spec.material_requirements_by_node()
    assert list(requirements_by_node) == [FIRST_NODE_UUID]
    assert spec.nodes[1].material_requirements == []


def test_disabled_consumer_moves_requirement_to_first_enabled_node() -> None:
    """禁用消费者不派发，物料需求应归属首个启用物理消费者。

    参数：无。返回：无；断言禁用节点及其作业身份都不会进入调度规格，后续
    启用动作获得唯一物料需求。异常：编译失败即测试失败。
    """

    compiler_type, _error_type = _compiler_contract()
    task_snapshot, jobs = _task_snapshot(disable_first_consumer=True)

    spec = compiler_type().compile(task_snapshot, jobs)

    assert [node.id for node in spec.nodes] == [SECOND_NODE_UUID]
    assert spec.nodes[0].job_id == SECOND_JOB_UUID
    assert [
        requirement.instance_uuid for requirement in spec.nodes[0].material_requirements
    ] == [MATERIAL_UUID]


def test_compiler_preserves_task_node_and_job_stable_identities() -> None:
    """编译必须保留任务、节点与作业的稳定 UUID，禁止创建第二套身份。

    参数：无。返回：无；断言工作流规格（WorkflowSpec）的任务身份以及每个
    工作流节点（WorkflowNode）/作业身份与持久快照完全一致。异常：编译失败即
    测试失败。
    """

    compiler_type, _error_type = _compiler_contract()
    task_snapshot, jobs = _task_snapshot()

    spec = compiler_type().compile(task_snapshot, jobs)

    assert spec.workflow_id == TASK_UUID
    assert spec.task_id == TASK_UUID
    assert [(node.id, node.job_id) for node in spec.nodes] == [
        (FIRST_NODE_UUID, FIRST_JOB_UUID),
        (SECOND_NODE_UUID, SECOND_JOB_UUID),
    ]


def test_create_new_material_source_fails_closed_before_scheduling() -> None:
    """短期桥不支持新建物料来源时必须在调度前失败关闭。

    参数：无。返回：无；断言 ``create_new`` 不会绕过库存权威（Inventory
    Authority）或生成可派发节点。异常：预期稳定编译错误码。
    """

    compiler_type, error_type = _compiler_contract()
    task_snapshot, jobs = _task_snapshot(mode="create_new", material_uuid=None)

    with pytest.raises(error_type) as caught:
        compiler_type().compile(task_snapshot, jobs)

    assert caught.value.code == "unsupported_material_source_mode"


def test_unresolved_existing_material_source_fails_closed_before_scheduling() -> None:
    """尚未固定物料 UUID 的既有来源必须等待权威准入而不能临时选择。

    参数：无。返回：无；断言自动分配既有物料的选择器不会在编译期查询库存
    （Inventory）或产生派发。异常：预期稳定物料解析错误码。
    """

    compiler_type, error_type = _compiler_contract()
    task_snapshot, jobs = _task_snapshot(material_uuid=None)

    with pytest.raises(error_type) as caught:
        compiler_type().compile(task_snapshot, jobs)

    assert caught.value.code == "material_source_resolution_required"
