"""Legacy UI transports must enter execution through the one RuntimeService."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from unilabos.app.local_bridge.local_api import LocalApiState
from unilabos.app.local_bridge.schedule_ws import ScheduleSession
from unilabos.app.local_bridge.workflow_ws import RUN_WORKFLOW, WorkflowSession


ACTION_CATALOG: dict[str, dict[str, Any]] = {
    "pump_1.dose": {"inputs": {"volume": {"type": "number"}}, "outputs": {}},
    "mixer_1.mix": {"inputs": {"seconds": {"type": "integer"}}, "outputs": {}},
}


class RecordingTransport:
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []

    async def send(self, message: dict[str, Any]) -> None:
        self.received.append(message)


class RecordingRuntimeService:
    def __init__(self, *, run_id: str) -> None:
        self.run_id = run_id
        self.start_calls: list[dict[str, Any]] = []

    async def start_run(self, body: dict[str, Any]) -> dict[str, str]:
        self.start_calls.append(body)
        return {"id": self.run_id, "status": "pending"}

    def get_workflow(self) -> dict[str, Any]:
        return {
            "revision": {
                "canonical": {
                    "schema_version": "2",
                    "revision_id": "legacy-ws-revision",
                    "workflow_id": "legacy-ws",
                    "invocations": [
                        {"node_id": "n1", "action_ref": "pump_1.dose"},
                        {"node_id": "n2", "action_ref": "mixer_1.mix"},
                    ],
                    "control_edges": [
                        {
                            "edge_id": "n1-to-n2",
                            "source": "n1",
                            "target": "n2",
                            "branch": None,
                        }
                    ],
                }
            }
        }


def _legacy_workflow() -> dict[str, Any]:
    return {
        "name": "legacy-two-step",
        "nodes": [
            {
                "id": "dose",
                "data": {
                    "device_id": "pump_1",
                    "method": "dose",
                    "params": {"volume": 5.0},
                },
            },
            {
                "id": "mix",
                "data": {
                    "device_id": "mixer_1",
                    "method": "mix",
                    "params": {"seconds": 10},
                },
            },
        ],
        "edges": [{"id": "dose-to-mix", "source": "dose", "target": "mix"}],
    }


def _assert_canonical_source(call: dict[str, Any]) -> dict[str, Any]:
    assert "workflow" not in call
    assert call["source"]["format"] == "canonical_workflow_v2"
    payload = call["source"]["payload"]
    assert payload["schema_version"] == "2"
    assert payload["invocations"]
    return payload


def test_legacy_local_api_run_adapts_then_delegates_to_runtime_service() -> None:
    os_side = RecordingTransport()
    schedule = ScheduleSession(os_side.send)
    runtime = RecordingRuntimeService(run_id="runtime-local-1")
    state = LocalApiState(
        schedule,
        action_catalog=ACTION_CATALOG,
        runtime_service=runtime,
    )

    response = asyncio.run(
        state.start_run({"workflow": _legacy_workflow(), "timeout": 300})
    )

    assert len(runtime.start_calls) == 1
    payload = _assert_canonical_source(runtime.start_calls[0])
    assert [node["node_id"] for node in payload["invocations"]] == [
        "dose",
        "mix",
    ]
    assert payload["invocations"][0]["input_bindings"] == {
        "volume": {"kind": "literal", "value": 5.0}
    }
    assert payload["control_edges"] == [
        {
            "edge_id": "dose-to-mix",
            "source": "dose",
            "target": "mix",
            "branch": None,
        }
    ]
    assert response["run_id"] == "runtime-local-1"
    assert response["status"] == "pending"
    assert os_side.received == [], "legacy HTTP bridge must not dispatch TaskDag itself"


def test_legacy_workflow_session_run_delegates_to_runtime_service() -> None:
    async def scenario() -> None:
        os_side = RecordingTransport()
        panel_side = RecordingTransport()
        schedule = ScheduleSession(os_side.send)
        runtime = RecordingRuntimeService(run_id="runtime-ws-1")
        try:
            workflow = WorkflowSession(
                panel_side.send,
                schedule,
                uuid="panel-1",
                runtime_service=runtime,
            )
        except TypeError as exc:
            pytest.fail(
                "WorkflowSession must accept the shared RuntimeService dependency: "
                f"{exc}",
                pytrace=False,
            )

        await workflow.handle_incoming({"action": RUN_WORKFLOW})

        assert len(runtime.start_calls) == 1
        payload = _assert_canonical_source(runtime.start_calls[0])
        assert [node["node_id"] for node in payload["invocations"]] == ["n1", "n2"]
        assert workflow._task_id == "runtime-ws-1"  # noqa: SLF001
        assert panel_side.received[-1] == {
            "code": 0,
            "data": {"action": RUN_WORKFLOW, "data": "runtime-ws-1"},
        }
        assert os_side.received == [], "legacy WS bridge must not dispatch TaskDag itself"

    asyncio.run(scenario())
