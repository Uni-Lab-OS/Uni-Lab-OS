"""F05.3-D 设备单动作运行（DeviceActionRun）共享调度接缝测试。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from tests.registry.test_template_projection import (
    DEVICE_MATERIAL_UUID,
    FakeInventoryStore,
    FakeRegistry,
)
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.composition import (
    compose_local_workflow_template_runtime,
    reset_workflow_service_for_test,
)
from unilabos.workflow.workflow_spec_compiler import WorkflowSpecCompiler


def test_device_action_run_freezes_compiler_valid_execution_plan(
    tmp_path: Path,
) -> None:
    """设备单动作运行必须先冻结可由公共编译器读取的完整执行计划。

    参数：``tmp_path`` 隔离工作流数据库。返回无；断言持久执行计划
    （ExecutionPlan）携带版本、连接点、固定执行器和冻结动作合同，且公共编译器
    无需再读取注册表或物料解析器即可保留标准任务/作业身份。
    """

    reset_workflow_service_for_test()
    try:
        # ``workflow_service`` 是本地工作流任务（WorkflowTask）写权威；
        # ``projection`` 提供创建命令使用的已发布动作模板目录代际。
        workflow_service, projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=FakeInventoryStore(),
            registry=FakeRegistry(),
        )
        # ``action_template`` 是设备单动作运行冻结进执行计划（ExecutionPlan）的
        # 已发布动作合同来源，测试据此比较完整 Schema，而非手写局部字段。
        action_template = (
            projection.snapshot()
            .require_action(
                "lab.devices:Pump",
                "transfer",
            )
            .template
        )
        # ``created`` 是已经持久化的设备单动作任务及唯一作业聚合。
        created = workflow_service.create_device_action_run(
            material_uuid=DEVICE_MATERIAL_UUID,
            workflow_node_template_uuid=action_template["uuid"],
            param={"volume": 2.0},
            execution_policy={},
            idempotency_key="d1a-frozen-plan",
            description=None,
            meta_data={},
        )

        # ``plan`` 是创建事务返回的冻结执行计划（ExecutionPlan），不得依赖后续
        # 注册表或物料解析器补齐静态执行事实。
        plan = created["task"]["execution_plan"]
        assert plan["version"] == 1
        assert plan["handles"] == []
        assert plan["nodes"][0]["device_id"] == "pump-01"
        assert plan["nodes"][0]["action_name"] == "transfer"
        assert plan["nodes"][0]["action_type"] == "UniLabJsonCommand"
        assert (
            plan["nodes"][0]["param_schema"]
            == action_template["meta_data"]["unilab"]["action_contract_schema"]
        )

        # ``compiled_spec`` 证明执行只依赖冻结计划和既有作业，不再调度期补事实。
        compiled_spec = WorkflowSpecCompiler().compile(
            created["task"],
            [created["job"]],
        )
        assert compiled_spec.task_id == created["task"]["uuid"]
        assert compiled_spec.nodes[0].job_id == created["job"]["uuid"]
    finally:
        reset_workflow_service_for_test()


def test_local_composition_uses_one_scheduler_listener_pair_for_d1a(
    tmp_path: Path,
) -> None:
    """普通任务与设备单动作运行必须共享唯一任务调度桥及监听器。

    参数：``tmp_path`` 隔离工作流数据库。返回无；断言生产组合根只注册一对
    调度器（Scheduler）生命周期监听器，避免第二套状态机重复推进相同作业。
    """

    reset_workflow_service_for_test()
    # ``scheduler`` 是普通工作流任务与设备单动作运行共同装配的唯一调度器
    # （Scheduler）实例，监听器注册次数反映是否误建了第二座桥。
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    try:
        # 两个观察器只包裹调度器（Scheduler）的公共监听器注册接缝，不读取其
        # 内部监听器容器；调用次数直接刻画生产组合根装配出的桥数量。
        with (
            patch.object(
                scheduler,
                "add_job_pre_dispatch_listener",
                wraps=scheduler.add_job_pre_dispatch_listener,
            ) as add_pre_dispatch_listener,
            patch.object(
                scheduler,
                "add_job_finished_listener",
                wraps=scheduler.add_job_finished_listener,
            ) as add_finished_listener,
        ):
            compose_local_workflow_template_runtime(
                tmp_path,
                inventory_store=FakeInventoryStore(),
                registry=FakeRegistry(),
                scheduler=scheduler,
            )

        assert add_pre_dispatch_listener.call_count == 1
        assert add_finished_listener.call_count == 1
    finally:
        reset_workflow_service_for_test()
