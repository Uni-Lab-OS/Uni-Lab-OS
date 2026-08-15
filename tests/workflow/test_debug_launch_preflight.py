"""调试启动缺失输入预检（Debug Launch Preflight）的公开深模块合同。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from unilabos.workflow.debug_launch import DebugLaunchPreflight


WORKFLOW_UUID = "71000000-0000-4000-8000-000000000001"
PRODUCER_UUID = "71000000-0000-4000-8000-000000000002"
CONSUMER_UUID = "71000000-0000-4000-8000-000000000003"
UNRELATED_UUID = "71000000-0000-4000-8000-000000000004"
PRODUCER_TEMPLATE_UUID = "72000000-0000-4000-8000-000000000001"
CONSUMER_TEMPLATE_UUID = "72000000-0000-4000-8000-000000000002"
SOURCE_HANDLE_UUID = "73000000-0000-4000-8000-000000000001"
TARGET_HANDLE_UUID = "73000000-0000-4000-8000-000000000002"
MIDDLE_TARGET_HANDLE_UUID = "73000000-0000-4000-8000-000000000004"
EDGE_UUID = "74000000-0000-4000-8000-000000000001"
MATERIAL_UUID = "75000000-0000-4000-8000-000000000001"
OTHER_MATERIAL_UUID = "75000000-0000-4000-8000-000000000002"
MATERIAL_TEMPLATE_UUID = "76000000-0000-4000-8000-000000000001"
OTHER_TEMPLATE_UUID = "76000000-0000-4000-8000-000000000002"


def _handle(
    *,
    uuid: str,
    template_uuid: str,
    io_type: str,
    schema: dict[str, Any],
    required: bool,
) -> dict[str, Any]:
    """构造一个有规范值 Schema 的模板连接点。"""

    return {
        "uuid": uuid,
        "workflow_node_template_uuid": template_uuid,
        "handle_key": "value",
        "io_type": io_type,
        "display_name": "样品" if "$slot" in schema else "计数",
        "description": "",
        "type": "ResourceSlot" if "$slot" in schema else "integer",
        "required": required,
        "data_source": "result" if io_type == "source" else "executor",
        "data_key": "value",
        "meta_data": {"unilab": {"value_schema": schema}},
    }


def _node(
    *,
    uuid: str,
    template_uuid: str,
    name: str,
    disabled: bool = False,
    param: dict[str, Any] | None = None,
    meta_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造预检使用的可执行手工节点。"""

    return {
        "uuid": uuid,
        "workflow_node_template_uuid": template_uuid,
        "name": name,
        "type": "manual_confirm",
        "pose": {},
        "param": param or {},
        "execution_policy": {},
        "disabled": disabled,
        "minimized": False,
        "meta_data": meta_data or {},
    }


def _graph(*, disabled_producer: bool = False) -> dict[str, Any]:
    """构造生产者到消费者以及一条无关支路的冻结应用图。"""

    schema = {"type": "integer"}
    return {
        "workflow": {
            "uuid": WORKFLOW_UUID,
            "revision": 7,
            "name": "debug preflight",
            "tags": [],
            "meta_data": {
                "unilab": {
                    "input_contract": {"version": 1, "parameters": []},
                    "output_contract": {"version": 1, "outputs": []},
                    "output_bindings": {},
                }
            },
        },
        "nodes": [
            _node(
                uuid=PRODUCER_UUID,
                template_uuid=PRODUCER_TEMPLATE_UUID,
                name="生产计数",
                disabled=disabled_producer,
            ),
            _node(
                uuid=CONSUMER_UUID,
                template_uuid=CONSUMER_TEMPLATE_UUID,
                name="消费计数",
            ),
            _node(
                uuid=UNRELATED_UUID,
                template_uuid=PRODUCER_TEMPLATE_UUID,
                name="无关支路",
            ),
        ],
        "edges": [
            {
                "uuid": EDGE_UUID,
                "source_node_uuid": PRODUCER_UUID,
                "target_node_uuid": CONSUMER_UUID,
                "source_handle_uuid": SOURCE_HANDLE_UUID,
                "target_handle_uuid": TARGET_HANDLE_UUID,
            }
        ],
        "node_templates": [
            {
                "uuid": PRODUCER_TEMPLATE_UUID,
                "node_type": "manual_confirm",
                "type": "manual_confirm",
            },
            {
                "uuid": CONSUMER_TEMPLATE_UUID,
                "node_type": "manual_confirm",
                "type": "manual_confirm",
            },
        ],
        "handle_templates": [
            _handle(
                uuid=SOURCE_HANDLE_UUID,
                template_uuid=PRODUCER_TEMPLATE_UUID,
                io_type="source",
                schema=schema,
                required=False,
            ),
            _handle(
                uuid=TARGET_HANDLE_UUID,
                template_uuid=CONSUMER_TEMPLATE_UUID,
                io_type="target",
                schema=schema,
                required=True,
            ),
        ],
    }


def test_start_frontier_requires_only_the_active_branch_boundary_value() -> None:
    """从支路内部启动只要求该活动支路被裁掉的边界值。"""

    decision = DebugLaunchPreflight().evaluate(
        graph=_graph(),
        raw_input={},
        start_node_uuid=CONSUMER_UUID,
        breakpoint_node_uuids=[CONSUMER_UUID],
        launch_overrides=[],
    )

    assert decision.status == "needs_input"
    assert len(decision.requirements) == 1
    requirement = decision.requirements[0]
    assert requirement["kind"] == "value"
    assert requirement["reason"] == "start_scope"
    assert requirement["target"] == {
        "node_uuid": CONSUMER_UUID,
        "node_name": "消费计数",
        "handle_uuid": TARGET_HANDLE_UUID,
        "data_key": "value",
        "display_name": "计数",
    }
    assert requirement["schema"] == {"type": "integer"}
    assert requirement["upstream_nodes"] == [
        {"node_uuid": PRODUCER_UUID, "node_name": "生产计数", "disabled": False}
    ]
    assert all(
        item["target"]["node_uuid"] != UNRELATED_UUID for item in decision.requirements
    )


def test_confirmed_value_override_is_frozen_into_plan_and_job() -> None:
    """已确认普通值覆盖须形成可创建的不可变计划，不写回工作流。"""

    graph = _graph()
    first = DebugLaunchPreflight().evaluate(
        graph=graph,
        raw_input={},
        start_node_uuid=CONSUMER_UUID,
        breakpoint_node_uuids=[],
        launch_overrides=[],
    )
    ready = DebugLaunchPreflight().evaluate(
        graph=graph,
        raw_input={},
        start_node_uuid=CONSUMER_UUID,
        breakpoint_node_uuids=[],
        launch_overrides=[{"requirement_id": first.requirements[0]["id"], "value": 7}],
    )

    assert ready.status == "ready"
    assert ready.requirements == []
    assert ready.prepared is not None
    assert ready.prepared.execution_plan["nodes"][0]["param"] == {"value": 7}
    assert ready.prepared.jobs[0]["param"] == {"value": 7}
    assert ready.prepared.execution_plan["debug_launch_overrides"] == [
        {
            "requirement_id": first.requirements[0]["id"],
            "target_node_uuid": CONSUMER_UUID,
            "target_handle_uuid": TARGET_HANDLE_UUID,
            "value": 7,
            "confirmed": False,
        }
    ]
    assert graph == _graph()


def test_disabled_producer_has_a_distinct_fail_closed_reason() -> None:
    """静态禁用生产者不能被依赖旁路伪装为一个值提供者。"""

    decision = DebugLaunchPreflight().evaluate(
        graph=_graph(disabled_producer=True),
        raw_input={},
        start_node_uuid=CONSUMER_UUID,
        breakpoint_node_uuids=[],
        launch_overrides=[],
    )

    assert decision.status == "needs_input"
    assert decision.requirements[0]["reason"] == "disabled_node"
    assert decision.requirements[0]["upstream_nodes"][0]["disabled"] is True


def test_disabled_middle_node_keeps_downstream_branch_and_requests_its_value() -> None:
    """禁用中间节点须旁路执行顺序，但其值输出只能通过显式覆盖补齐。"""

    graph = _graph(disabled_producer=True)
    graph["handle_templates"].append(
        _handle(
            uuid=MIDDLE_TARGET_HANDLE_UUID,
            template_uuid=PRODUCER_TEMPLATE_UUID,
            io_type="target",
            schema={"type": "integer"},
            required=False,
        )
    )
    graph["edges"].append(
        {
            "uuid": "74000000-0000-4000-8000-000000000002",
            "source_node_uuid": UNRELATED_UUID,
            "target_node_uuid": PRODUCER_UUID,
            "source_handle_uuid": SOURCE_HANDLE_UUID,
            "target_handle_uuid": MIDDLE_TARGET_HANDLE_UUID,
        }
    )

    decision = DebugLaunchPreflight().evaluate(
        graph=graph,
        raw_input={},
        start_node_uuid=UNRELATED_UUID,
        breakpoint_node_uuids=[],
        launch_overrides=[],
    )

    assert decision.status == "needs_input"
    assert decision.requirements[0]["target"]["node_uuid"] == CONSUMER_UUID
    assert decision.requirements[0]["reason"] == "disabled_node"


def _material_graph() -> dict[str, Any]:
    """构造带显式同一物料透传合同的两节点图。"""

    graph = _graph()
    schema = {
        "$slot": "ResourceSlot",
        "allowed_resource_template_uuids": [MATERIAL_TEMPLATE_UUID],
    }
    graph["nodes"] = [
        _node(
            uuid=PRODUCER_UUID,
            template_uuid=PRODUCER_TEMPLATE_UUID,
            name="跳过的物料搬运",
            param={"value": {"uuid": MATERIAL_UUID}},
            meta_data={
                "unilab": {
                    "material_passthrough_handles": {
                        SOURCE_HANDLE_UUID: TARGET_HANDLE_UUID
                    },
                    "output_schema_overrides": {SOURCE_HANDLE_UUID: schema},
                }
            },
        ),
        _node(
            uuid=CONSUMER_UUID,
            template_uuid=CONSUMER_TEMPLATE_UUID,
            name="消费物料",
        ),
    ]
    graph["handle_templates"] = [
        _handle(
            uuid=TARGET_HANDLE_UUID,
            template_uuid=PRODUCER_TEMPLATE_UUID,
            io_type="target",
            schema=schema,
            required=True,
        ),
        _handle(
            uuid=SOURCE_HANDLE_UUID,
            template_uuid=PRODUCER_TEMPLATE_UUID,
            io_type="source",
            schema=schema,
            required=False,
        ),
        _handle(
            uuid="73000000-0000-4000-8000-000000000003",
            template_uuid=CONSUMER_TEMPLATE_UUID,
            io_type="target",
            schema=schema,
            required=True,
        ),
    ]
    graph["edges"][0]["target_handle_uuid"] = graph["handle_templates"][2]["uuid"]
    return graph


def test_material_passthrough_suggests_current_fact_but_requires_confirmation() -> None:
    """确定性同一物料透传可预填建议，但不能静默成为库存事实。"""

    materials = [
        {
            "uuid": MATERIAL_UUID,
            "name": "实际样品",
            "resource_template_uuid": MATERIAL_TEMPLATE_UUID,
            "current_site": {"uuid": "site-actual", "name": "当前库位"},
            "inventory_status": "available",
        },
        {
            "uuid": OTHER_MATERIAL_UUID,
            "name": "错误模板",
            "resource_template_uuid": OTHER_TEMPLATE_UUID,
            "current_site": None,
            "inventory_status": "available",
        },
    ]
    preflight = DebugLaunchPreflight(
        material_resolver=lambda uuid: next(
            (item for item in materials if item["uuid"] == uuid), None
        ),
        material_candidates=lambda: materials,
    )
    decision = preflight.evaluate(
        graph=_material_graph(),
        raw_input={},
        start_node_uuid=CONSUMER_UUID,
        breakpoint_node_uuids=[],
        launch_overrides=[],
    )

    assert decision.status == "needs_input"
    requirement = decision.requirements[0]
    assert requirement["kind"] == "material"
    assert requirement["allowed_resource_template_uuids"] == [MATERIAL_TEMPLATE_UUID]
    assert [item["material_uuid"] for item in requirement["suggestions"]] == [
        MATERIAL_UUID
    ]
    suggestion = requirement["suggestions"][0]
    assert suggestion["recommended"] is True
    assert suggestion["requires_confirmation"] is True
    assert suggestion["actual"] == {
        "site": {"uuid": "site-actual", "name": "当前库位"},
        "status": "available",
    }
    assert suggestion["inferred_target"] == {
        "kind": "same_material_passthrough",
        "through_node_uuids": [PRODUCER_UUID],
        "site": None,
        "status": None,
    }

    rejected = preflight.evaluate(
        graph=_material_graph(),
        raw_input={},
        start_node_uuid=CONSUMER_UUID,
        breakpoint_node_uuids=[],
        launch_overrides=[
            {"requirement_id": requirement["id"], "value": {"uuid": MATERIAL_UUID}}
        ],
    )
    assert rejected.status == "needs_input"
    assert rejected.diagnostics[0]["code"] == "material_confirmation_required"

    ready = preflight.evaluate(
        graph=_material_graph(),
        raw_input={},
        start_node_uuid=CONSUMER_UUID,
        breakpoint_node_uuids=[],
        launch_overrides=[
            {
                "requirement_id": requirement["id"],
                "value": {"uuid": MATERIAL_UUID},
                "confirmed": True,
            }
        ],
    )
    assert ready.status == "ready"
    assert ready.prepared is not None
    assert ready.prepared.jobs[0]["param"] == {"value": {"uuid": MATERIAL_UUID}}


def test_material_operation_without_passthrough_never_prefills_inferred_state() -> None:
    """移动、转化、创建或销毁语义未声明 passthrough 时只能人工选择现有事实。"""

    graph = _material_graph()
    graph["nodes"][0]["meta_data"] = {
        "unilab": {"operation_kind": "material_transform"}
    }
    material = {
        "uuid": MATERIAL_UUID,
        "name": "转化前样品",
        "resource_template_uuid": MATERIAL_TEMPLATE_UUID,
        "current_site": {"uuid": "site-actual", "name": "当前库位"},
        "inventory_status": "available",
    }

    decision = DebugLaunchPreflight(
        material_resolver=lambda _uuid: material,
        material_candidates=lambda: [material],
    ).evaluate(
        graph=graph,
        raw_input={},
        start_node_uuid=CONSUMER_UUID,
        breakpoint_node_uuids=[],
        launch_overrides=[],
    )

    suggestion = decision.requirements[0]["suggestions"][0]
    assert suggestion["recommended"] is False
    assert suggestion["inferred_target"] == {
        "kind": "selected_inventory_candidate",
        "through_node_uuids": [],
        "site": None,
        "status": None,
    }


def test_breakpoints_do_not_change_preflight_requirements() -> None:
    """断点只改变暂停位置，不能凭空产生输入要求。"""

    preflight = DebugLaunchPreflight()
    without = preflight.evaluate(
        graph=_graph(),
        raw_input={},
        start_node_uuid=CONSUMER_UUID,
        breakpoint_node_uuids=[],
        launch_overrides=[],
    )
    with_breakpoint = preflight.evaluate(
        graph=_graph(),
        raw_input={},
        start_node_uuid=CONSUMER_UUID,
        breakpoint_node_uuids=[CONSUMER_UUID],
        launch_overrides=[],
    )

    assert deepcopy(without.requirements) == with_breakpoint.requirements
