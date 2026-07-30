"""T04 local_api 实现 B（SZLab local_ui）UI 面 HTTP 服务器 hermetic 单测。

覆盖 AC-4（端点集/方法/响应结构对照 unilabos_local_ui/src/main.tsx）：
- GET  /api/preset               → PresetPayload（id/title/default_config/actions）
- GET  /api/stack-status         → StackStatusPayload（success/stacks）
- POST /api/workflow/build-graph → WorkflowJson（{name,nodes,edges}）；含环 → 400 detail
- POST /api/run                  → RunStatus（run_id + node_statuses 全 idle + OS 面收 F002 task_dag）
- GET  /api/run/{id}             → RunStatus（node_statuses 随 job_status 推进；终态 completed/failed）
- POST /api/run/{id}/cancel      → RunStatus（OS 面收 cancel_task，等待 OS 确认终态）
- 未知 run → 404；OS 未连入 → 503

用 FastAPI TestClient（同步）驱动 HTTP；用内存 ScheduleSession（send→内存 OS 面）顶替真实
WS 传输。请求之间以 asyncio.run(schedule.handle_incoming(...)) 喂回 job_status——纯内存态更新
+ 同步回调累积日志，无 time.sleep、无真实 OS、无网络。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from unilabos.app.local_bridge.local_api import (
    LocalApiState,
    build_demo_preset,
    create_app,
    node_statuses_of,
    overall_status_of,
)
from unilabos.app.local_bridge.offline_os import OfflineOS
from unilabos.app.local_bridge.schedule_ws import ScheduleSession
from unilabos.scheduler.dag_model import NodeState


ACTION_CATALOG: dict[str, dict[str, Any]] = {
    "pump_1.pump_liquid": {
        "inputs": {"volume": {"type": "number"}},
        "outputs": {},
    },
    "stirrer_1.stir": {
        "inputs": {"seconds": {"type": "integer"}},
        "outputs": {},
    },
}


class FakeTransport:
    """内存版收信端：把桥→OS 下行报文塞入 received。"""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []

    async def send(self, msg: dict[str, Any]) -> None:
        self.received.append(msg)


def _make_client() -> tuple[TestClient, LocalApiState, FakeTransport]:
    """接线：ScheduleSession(→OS 面) + LocalApiState + FastAPI app（TestClient）。"""
    os_side = FakeTransport()
    schedule = ScheduleSession(os_side.send)
    state = LocalApiState(schedule, action_catalog=ACTION_CATALOG)
    app = create_app(lambda: state)
    return TestClient(app), state, os_side


def _demo_request() -> dict[str, Any]:
    """createWorkflowRequest 形状（{name, nodes:[{id,position,data:{...}}], edges:[{id,source,target}]}）。"""
    return {
        "name": "wf_b",
        "nodes": [
            {
                "id": "n1",
                "position": {"x": 0, "y": 0},
                "data": {"method": "pump_liquid", "device_id": "pump_1", "params": {"volume": 5.0}},
            },
            {
                "id": "n2",
                "position": {"x": 200, "y": 0},
                "data": {"method": "stir", "device_id": "stirrer_1", "params": {"seconds": 10}},
            },
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n2"}],
    }


def _emit_job_status(schedule: ScheduleSession, task_id: str, job_id: str, status: str, **extra: Any) -> None:
    """模拟 OS publish_job_status：以 F002 job_status 报文喂回 schedule（纯内存态更新）。"""
    data = {
        "job_id": job_id,
        "task_id": task_id,
        "device_id": extra.get("device_id", ""),
        "notebook_id": "",
        "action_name": extra.get("action_name", ""),
        "status": status,
        "feedback_data": extra.get("feedback_data", {}),
        "return_info": extra.get("return_info"),
        "timestamp": 0.0,
    }
    asyncio.run(schedule.handle_incoming({"action": "job_status", "data": data}))


def test_preset_shape() -> None:
    client, _state, _os = _make_client()
    resp = client.get("/api/preset")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["id"] == "local_bridge_demo"
    assert "title" in payload
    assert "default_config" in payload
    methods = {action["method"] for action in payload["actions"]}
    assert methods == {"pump_liquid", "stir"}
    # ActionSpec 必备字段
    for action in payload["actions"]:
        assert "label" in action
        assert "needs_position" in action
        assert "device_id" in action


def test_stack_status_shape() -> None:
    client, _state, _os = _make_client()
    resp = client.get("/api/stack-status")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["success"] is True
    assert payload["stacks"] == {}


def test_runtime_actions_projects_generic_catalog_in_stable_order() -> None:
    os_side = FakeTransport()
    schedule = ScheduleSession(os_side.send)
    state = LocalApiState(
        schedule,
        action_catalog={
            "zeta_station.finish": {
                "inputs": {"sample": {"required": True, "type": "string"}},
                "label": "Finish sample",
                "outputs": {},
            },
            "alpha_camera.capture": {
                "inputs": {
                    "exposure_ms": {
                        "default": 125,
                        "type": "number",
                    }
                },
                "label": "Capture image",
                "outputs": {"image": {"type": "string"}},
            },
        },
    )
    client = TestClient(create_app(lambda: state))

    response = client.get("/api/runtime/local/actions")

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "runtime/v1",
        "available": True,
        "revision": None,
        "error": None,
        "actions": [
            {
                "action_ref": "alpha_camera.capture",
                "input_schema": {
                    "exposure_ms": {
                        "default": 125,
                        "type": "number",
                    }
                },
                "label": "Capture image",
                "output_schema": {"image": {"type": "string"}},
            },
            {
                "action_ref": "zeta_station.finish",
                "input_schema": {
                    "sample": {
                        "required": True,
                        "type": "string",
                    }
                },
                "label": "Finish sample",
                "output_schema": {},
            },
        ],
    }


def test_edge_device_catalog_populates_ui_and_single_action_runtime() -> None:
    edge_catalog = {
        "schema": "unilab/device-catalog-v1",
        "timestamp": 1,
        "machine_name": "Edge A",
        "devices": [
            {
                "device_id": "robot",
                "device_key": "/cell/robot",
                "namespace": "/cell",
                "machine_name": "六轴机械臂",
                "is_online": True,
                "actions": [
                    {
                        "action_name": "home",
                        "action_ref": "robot.home",
                        "label": "回零",
                        "type_name": "UniLabJsonCommand",
                        "input_schema": {
                            "speed": {
                                "type": "integer",
                                "default": 20,
                            }
                        },
                        "output_schema": {},
                        "contract": {},
                        "is_busy": False,
                    }
                ],
            }
        ],
    }
    offline = OfflineOS(device_catalog=edge_catalog)
    schedule = ScheduleSession(offline.receive)
    offline.bind(schedule)
    state = LocalApiState(
        schedule,
        action_catalog={
            "robot.home": {
                "label": "回零",
                "inputs": {
                    "speed": {
                        "type": "integer",
                        "default": 20,
                    }
                },
                "outputs": {},
            }
        },
    )
    client = TestClient(create_app(lambda: state))

    response = client.get("/api/v1/devices")

    assert response.status_code == 200
    assert response.json()["items"] == [
        {
            "id": "robot",
            "deviceKey": "/cell/robot",
            "namespace": "/cell",
            "name": "六轴机械臂",
            "online": True,
            "actions": [
                {
                    "id": "home",
                    "actionRef": "robot.home",
                    "name": "回零",
                    "typeName": "UniLabJsonCommand",
                    "inputSchema": {
                        "speed": {
                            "type": "integer",
                            "default": 20,
                        }
                    },
                    "outputSchema": {},
                    "busy": False,
                }
            ],
        }
    ]

    run = client.post(
        "/api/v1/runtime/runs",
        json={
            "source": {
                "format": "workflow_revision_v2",
                "revision": {
                    "schema_version": "2",
                    "revision_id": "device-debug-rev",
                    "workflow_id": "device-debug",
                    "invocations": [
                        {
                            "node_id": "action",
                            "action_ref": "robot.home",
                            "name": "回零",
                            "input_bindings": {
                                "speed": {
                                    "kind": "literal",
                                    "value": 25,
                                }
                            },
                        }
                    ],
                    "control_edges": [],
                },
            }
        },
    )

    assert run.status_code == 200
    task_dag = next(
        message
        for message in offline.received
        if message["action"] == "task_dag"
    )
    assert task_dag["data"]["nodes"][0]["device_id"] == "robot"
    assert task_dag["data"]["nodes"][0]["action"] == "home"


def test_build_graph_returns_workflow_json() -> None:
    client, _state, _os = _make_client()
    resp = client.post("/api/workflow/build-graph", json=_demo_request())
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["name"] == "wf_b"
    assert {n["id"] for n in payload["nodes"]} == {"n1", "n2"}
    assert payload["edges"][0]["source"] == "n1"


def test_build_graph_rejects_cycle_with_400_detail() -> None:
    client, _state, _os = _make_client()
    req = _demo_request()
    # 造环：n2→n1，与 n1→n2 成环
    req["edges"].append({"id": "e2", "source": "n2", "target": "n1"})
    resp = client.post("/api/workflow/build-graph", json=req)
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "含环" in detail or "cycle" in detail.lower()


def test_run_submits_task_dag_and_returns_idle_statuses() -> None:
    client, _state, os_side = _make_client()
    built = client.post("/api/workflow/build-graph", json=_demo_request()).json()
    resp = client.post("/api/run", json={"workflow": built, "timeout": 300})
    assert resp.status_code == 200
    payload = resp.json()
    run_id = payload["run_id"]
    assert run_id
    assert payload["status"] == "pending"
    assert payload["node_statuses"] == {"n1": "idle", "n2": "idle"}
    # OS 面收到 F002 task_dag（task_id==run_id，逐节点字段）
    assert len(os_side.received) == 1
    dag_msg = os_side.received[0]
    assert dag_msg["action"] == "task_dag"
    assert dag_msg["data"]["task_id"] == run_id
    assert {n["node_id"] for n in dag_msg["data"]["nodes"]} == {"n1", "n2"}
    assert {n["device_id"] for n in dag_msg["data"]["nodes"]} == {"pump_1", "stirrer_1"}


def test_run_poll_reflects_job_status_progress() -> None:
    client, state, _os = _make_client()
    built = client.post("/api/workflow/build-graph", json=_demo_request()).json()
    run_id = client.post("/api/run", json={"workflow": built}).json()["run_id"]

    _emit_job_status(state._schedule, run_id, "n1", "running", action_name="加液")
    status = client.get(f"/api/run/{run_id}").json()
    assert status["status"] == "running"
    assert status["node_statuses"]["n1"] == "running"
    assert status["node_statuses"]["n2"] == "idle"
    # 日志累积：下发 workflow + n1 运行中
    assert any(event["node_id"] == "n1" for event in status["log_events"])

    _emit_job_status(state._schedule, run_id, "n1", "success")
    _emit_job_status(state._schedule, run_id, "n2", "success")
    done = client.get(f"/api/run/{run_id}").json()
    assert done["status"] == "completed"
    assert done["node_statuses"] == {"n1": "success", "n2": "success"}


def test_run_failed_reaches_failed_status() -> None:
    client, state, _os = _make_client()
    built = client.post("/api/workflow/build-graph", json=_demo_request()).json()
    run_id = client.post("/api/run", json={"workflow": built}).json()["run_id"]
    _emit_job_status(state._schedule, run_id, "n1", "success")
    _emit_job_status(state._schedule, run_id, "n2", "failed")
    status = client.get(f"/api/run/{run_id}").json()
    assert status["status"] == "failed"
    assert status["node_statuses"]["n2"] == "failed"
    assert status["error"]


def test_cancel_sends_request_without_claiming_terminal() -> None:
    client, _state, os_side = _make_client()
    built = client.post("/api/workflow/build-graph", json=_demo_request()).json()
    run_id = client.post("/api/run", json={"workflow": built}).json()["run_id"]
    os_side.received.clear()
    resp = client.post(f"/api/run/{run_id}/cancel")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "cancel_requested"
    assert payload["node_statuses"] == {
        "n1": "cancel_requested",
        "n2": "cancel_requested",
    }
    handle = _state._schedule.get_run(run_id)
    assert handle is not None
    assert not handle.finished
    # OS 面收到 cancel_task
    assert os_side.received[0]["action"] == "cancel_task"
    assert os_side.received[0]["data"] == {"task_id": run_id}


def test_cancel_converges_after_all_os_nodes_confirm_safe() -> None:
    client, state, _os = _make_client()
    built = client.post("/api/workflow/build-graph", json=_demo_request()).json()
    run_id = client.post("/api/run", json={"workflow": built}).json()["run_id"]
    client.post(f"/api/run/{run_id}/cancel")
    confirmed_safe = {
        "physical_state": "confirmed_safe",
        "reconcile_required": False,
    }

    _emit_job_status(
        state._schedule,
        run_id,
        "n1",
        "cancelled",
        return_info=confirmed_safe,
    )
    pending = client.get(f"/api/run/{run_id}").json()
    assert pending["status"] == "cancel_requested"
    assert pending["node_statuses"] == {
        "n1": "cancelled",
        "n2": "cancel_requested",
    }

    _emit_job_status(
        state._schedule,
        run_id,
        "n2",
        "cancelled",
        return_info=confirmed_safe,
    )
    done = client.get(f"/api/run/{run_id}").json()
    assert done["status"] == "cancelled"
    assert done["node_statuses"] == {"n1": "cancelled", "n2": "cancelled"}
    handle = state._schedule.get_run(run_id)
    assert handle is not None
    assert handle.finished


@pytest.mark.parametrize("terminal_status", ["cancelled", "failed"])
def test_cancel_with_unknown_physical_state_is_not_safe_completion(
    terminal_status: str,
) -> None:
    client, state, _os = _make_client()
    built = client.post("/api/workflow/build-graph", json=_demo_request()).json()
    run_id = client.post("/api/run", json={"workflow": built}).json()["run_id"]
    _emit_job_status(state._schedule, run_id, "n1", "success")
    client.post(f"/api/run/{run_id}/cancel")

    _emit_job_status(
        state._schedule,
        run_id,
        "n2",
        terminal_status,
        return_info={
            "error": "transport disconnected",
            "physical_state": "unknown",
            "reconcile_required": True,
        },
    )

    payload = client.get(f"/api/run/{run_id}").json()
    assert payload["status"] == "reconciling"
    assert payload["node_statuses"] == {
        "n1": "success",
        "n2": "reconciling",
    }
    assert payload["status"] not in {"completed", "cancelled"}
    handle = state._schedule.get_run(run_id)
    assert handle is not None
    assert not handle.finished


def test_unknown_run_returns_404() -> None:
    client, _state, _os = _make_client()
    assert client.get("/api/run/nope").status_code == 404
    assert client.post("/api/run/nope/cancel").status_code == 404


def test_endpoints_503_when_os_not_connected() -> None:
    app = create_app(lambda: None)
    client = TestClient(app)
    # 无 OS 时读端点（preset/stack-status）仍可用，但涉及调度的端点 503
    assert client.get("/api/preset").status_code == 200
    assert client.post("/api/workflow/build-graph", json=_demo_request()).status_code == 503
    assert client.post("/api/run", json={"workflow": {}}).status_code == 503


def test_status_mapping_pure_helpers() -> None:
    from unilabos.app.local_bridge.schedule_ws import RunHandle
    from unilabos.app.local_bridge.workflow_to_dag import workflow_to_task_dag

    dag = workflow_to_task_dag(_demo_request()["nodes"], _demo_request()["edges"], task_id="t")
    run = RunHandle(dag)
    assert node_statuses_of(run) == {"n1": "idle", "n2": "idle"}
    assert overall_status_of(run) == "pending"
    run.node_states["n1"] = NodeState.RUNNING
    assert overall_status_of(run) == "running"
    run.node_states["n1"] = NodeState.SUCCESS
    run.node_states["n2"] = NodeState.SUCCESS
    run.done.set()
    assert overall_status_of(run) == "completed"


def test_build_demo_preset_actions_align_with_demo_devices() -> None:
    preset = build_demo_preset()
    devices = {action["device_id"] for action in preset["actions"]}
    assert devices == {"pump_1", "stirrer_1"}
