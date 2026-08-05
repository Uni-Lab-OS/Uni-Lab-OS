"""F05.4-A 物料来源（MaterialSource）公开 HTTP 调度纵向合同。"""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.workflow.test_f05_execution_plan_safety_followup import (
    _real_authoring_graph,
)
from unilabos.app.scheduler.api import create_scheduler_router
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.inventory.backend_api import install_backend_resource_api
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.domain import MaterialRequirement
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge

WORKFLOW_UUID = "11000000-0000-4000-8000-000000000404"
HOLDER_TASK_UUID = "61000000-0000-4000-8000-000000000404"
HOLDER_NODE_UUID = "61000000-0000-4000-8000-000000000405"


@dataclass(slots=True)
class _Runtime:
    """汇总一套真实本地 HTTP 调度运行时及其可观察公共接缝。"""

    client: TestClient
    workflow_service: WorkflowService
    inventory: InventoryService
    scheduler: EdgeScheduler
    dispatcher: RecordingDispatcher


def _create_resource_template(client: TestClient) -> str:
    """通过公开 HTTP 创建物料使用的资源模板（ResourceTemplate）。

    参数：``client`` 是组合应用客户端。返回：服务端生成的稳定模板 UUID。
    异常：公开接口失败由断言终止夹具，禁止绕过合同直接写库存数据库。
    """

    response = client.post(
        "/api/v1/resource-templates",
        json={
            "resources": [
                {
                    "id": "lab.plate",
                    "display_name": "96 孔板",
                    "registry_type": "material",
                    "model": {},
                    "class": {"module": "lab.plate", "type": "python"},
                    "handles": [],
                }
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["code"] == 0
    return str(response.json()["data"]["templates"][0]["uuid"])


def _apply_workflow_graph(
    runtime: _Runtime, *, resource_template_uuid: str, material_uuid: str, mode: str
) -> None:
    """准备已应用图并冻结本轮关注的物料来源执行计划。

    参数：``runtime`` 持有唯一工作流权威；资源模板和物料 UUID 冻结 ``existing``
    选择器，``mode`` 可切换失败关闭反例。返回无。异常：执行计划构建错误在公开
    任务创建入口失败关闭。F05.3 已独立覆盖图到计划，本测试只替换服务的计划
    构建接缝，以聚焦 HTTP、真实库存预留和准入重试（AdmissionRetry）。
    """

    runtime.workflow_service.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="F05 HTTP 物料来源",
        tags=[],
        description=None,
        meta_data={},
    )
    # ``frozen_graph`` 复用已经由 F05.3 证明可编译的真实物料占位符链，仅替换
    # 本场景经 HTTP 创建的稳定物料身份和选择器模式。
    frozen_graph = deepcopy(_real_authoring_graph())
    source_selector = frozen_graph["nodes"][0]["param"]
    source_selector["mode"] = mode
    source_selector["resource_template_uuid"] = resource_template_uuid
    source_selector["material_uuid"] = material_uuid if mode == "existing" else None
    runtime.workflow_service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[],
        edges=[],
    )

    # ``build_execution_plan`` 是本运行时唯一计划构建接缝；任务仍由公开 HTTP
    # 创建并在标准事务中持久化任务/作业身份。
    def build_execution_plan(
        _applied_graph: dict[str, object],
        *,
        run_mode: str,
        target_node_uuid: str | None,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        """从冻结真实图构建执行计划。

        参数：``_applied_graph`` 是服务读取的已应用空图，本测试不重复验证；运行
        模式和目标节点来自 HTTP。返回：正式构建器产出的计划与作业；非法
        ``create_new`` 选择器抛稳定计划错误并使创建事务回滚。
        """

        return ExecutionPlanBuilder().build(
            frozen_graph,
            run_mode=run_mode,
            target_node_uuid=target_node_uuid,
        )

    runtime.workflow_service._build_execution_plan = build_execution_plan


@pytest.fixture()
def runtime(tmp_path: Path) -> Iterator[_Runtime]:
    """装配真实库存、调度桥、工作流服务与组合 FastAPI 应用。

    参数：``tmp_path`` 隔离两类 SQLite 权威。产生：共享同一库存服务和调度器的
    运行时；结束时关闭桥、工作流存储及库存存储。异常：装配失败原样传播。
    """

    # ``inventory_store`` 是本测试唯一库存权威（Inventory Authority）。
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    inventory = InventoryService(inventory_store)
    # ``workflow_store`` 是标准任务/作业的唯一工作流权威。
    workflow_store = WorkflowStore(tmp_path / "workflow_history.db")
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)
    bridge = TaskSchedulerBridge(workflow_store, scheduler=scheduler)
    workflow_service = WorkflowService(
        workflow_store,
        task_scheduler_bridge=bridge,
    )
    app = create_workflow_app(workflow_service)
    install_backend_resource_api(app, BackendResourceService(inventory_store))
    app.include_router(
        create_scheduler_router(
            lambda: scheduler,
            include_execution_shaped_workflow_routes=False,
        )
    )
    try:
        with TestClient(app) as client:
            yield _Runtime(
                client=client,
                workflow_service=workflow_service,
                inventory=inventory,
                scheduler=scheduler,
                dispatcher=dispatcher,
            )
    finally:
        workflow_service.close()
        inventory_store.close()


def test_fixed_existing_waits_then_reschedules_with_same_task_and_job(
    runtime: _Runtime,
) -> None:
    """固定既有物料被占用时等待，释放后以同一身份完成准入重试。

    参数：``runtime`` 是真实组合运行时。返回无；断言公开 HTTP 创建物料和任务，
    外部任务/作业保持 ``pending``，内部为 ``waiting_for_material``；释放夹具占用
    后，``POST /reschedule`` 使同一任务/作业进入运行。异常：任何私有替代接口、
    新任务身份或提前设备派发都会使断言失败。
    """

    resource_template_uuid = _create_resource_template(runtime.client)
    material_response = runtime.client.post(
        "/api/v1/materials",
        json={
            "resource_template_uuid": resource_template_uuid,
            "barcode": "F05-PLATE-001",
            "name": "F05 固定孔板",
        },
    )
    assert material_response.status_code == 201
    # ``material_uuid`` 是 HTTP、冻结选择器和短期遗留预留共享的稳定物料身份。
    material_uuid = str(material_response.json()["data"]["uuid"])
    _apply_workflow_graph(
        runtime,
        resource_template_uuid=resource_template_uuid,
        material_uuid=material_uuid,
        mode="existing",
    )
    runtime.inventory.reserve_workflow(
        HOLDER_TASK_UUID,
        {HOLDER_NODE_UUID: [MaterialRequirement(instance_uuid=material_uuid)]},
    )

    created = runtime.client.post(
        "/api/v1/workflow-tasks",
        json={"workflow_uuid": WORKFLOW_UUID, "run_mode": "normal", "meta_data": {}},
    )
    assert created.status_code == 201
    assert created.json()["code"] == 0
    # ``task_uuid`` 与 ``job_uuid`` 是本次准入重试前后必须保持不变的持久身份。
    task_uuid = str(created.json()["data"]["uuid"])
    jobs_before = runtime.client.get(f"/api/v1/workflow-tasks/{task_uuid}/jobs").json()[
        "data"
    ]

    assert created.json()["data"]["status"] == "pending"
    assert [job["status"] for job in jobs_before] == ["pending"]
    assert runtime.dispatcher.dispatched == []
    assert runtime.client.get("/api/v1/materials/graph").json()["code"] == 0
    assert runtime.scheduler.workflow_snapshot(task_uuid)["state"] == (
        "waiting_for_material"
    )

    runtime.inventory.release_workflow(HOLDER_TASK_UUID, reason="fixture_release")
    rescheduled = runtime.client.post("/api/v1/reschedule")
    jobs_after = runtime.client.get(f"/api/v1/workflow-tasks/{task_uuid}/jobs").json()[
        "data"
    ]
    task_after = runtime.client.get(f"/api/v1/workflow-tasks/{task_uuid}").json()[
        "data"
    ]

    assert rescheduled.status_code == 200
    assert len(rescheduled.json()["dispatched"]) == 1
    assert task_after["uuid"] == task_uuid
    assert task_after["status"] == "running"
    assert [job["uuid"] for job in jobs_after] == [jobs_before[0]["uuid"]]
    assert [job["status"] for job in jobs_after] == ["dispatched"]


def test_create_new_fails_closed_without_task_or_material_graph_change(
    runtime: _Runtime,
) -> None:
    """短期 ``create_new`` 应在任务事务内失败关闭并保持物料图不变。

    参数：``runtime`` 是真实组合运行时。返回无；断言公开任务接口返回 Backend
    业务错误，任务列表零新增，前后公开物料图完全相同且没有设备派发。异常：若
    不支持模式创建了部分任务、物料或作业，任一公共响应断言都会失败。
    """

    resource_template_uuid = _create_resource_template(runtime.client)
    material_response = runtime.client.post(
        "/api/v1/materials",
        json={
            "resource_template_uuid": resource_template_uuid,
            "barcode": "F05-MOUNT-001",
            "name": "F05 新建来源挂载物料",
        },
    )
    assert material_response.status_code == 201
    # ``mount_material_uuid`` 只满足物料来源选择器的挂载引用，不是待新建物料。
    mount_material_uuid = str(material_response.json()["data"]["uuid"])
    _apply_workflow_graph(
        runtime,
        resource_template_uuid=resource_template_uuid,
        material_uuid=mount_material_uuid,
        mode="create_new",
    )
    material_graph_before = runtime.client.get("/api/v1/materials/graph").json()
    tasks_before = runtime.client.get("/api/v1/workflow-tasks").json()["data"]

    failed = runtime.client.post(
        "/api/v1/workflow-tasks",
        json={"workflow_uuid": WORKFLOW_UUID, "run_mode": "normal", "meta_data": {}},
    )

    material_graph_after = runtime.client.get("/api/v1/materials/graph").json()
    tasks_after = runtime.client.get("/api/v1/workflow-tasks").json()["data"]
    assert failed.status_code == 200
    assert failed.json()["code"] == 1000
    assert failed.json()["error"]["msg"]
    assert tasks_after == tasks_before
    assert material_graph_after == material_graph_before
    assert runtime.dispatcher.dispatched == []
