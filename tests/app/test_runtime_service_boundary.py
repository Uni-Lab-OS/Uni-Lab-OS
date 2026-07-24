"""Runtime API ownership, persistence, cancel, and reconcile RED contracts."""

from __future__ import annotations

import ast
import asyncio
import importlib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi.testclient import TestClient

from unilabos.app.local_bridge.local_api import LocalApiState, create_app
from unilabos.app.local_bridge.schedule_ws import ScheduleSession
from unilabos.runtime.event_store import SQLiteEventJournal
from unilabos.runtime.profile_loader import LoadedProfile
from unilabos.scheduler.resource_lock import ResourceLockManager
from unilabos.workflow.canonical import WorkflowRevision


ACTION_CATALOG: dict[str, dict[str, Any]] = {
    "pump_1.dose": {
        "inputs": {"volume": {"type": "number"}},
        "outputs": {"receipt": {"type": "string"}},
        "resource_claims": [],
        "effects": [],
    }
}


class FakeOS:
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []

    async def send(self, message: dict[str, Any]) -> None:
        self.received.append(message)


def _runtime_api() -> ModuleType:
    try:
        return importlib.import_module("unilabos.runtime.service")
    except ModuleNotFoundError as exc:
        if exc.name != "unilabos.runtime.service":
            raise
        pytest.fail("OS RuntimeService capability is missing", pytrace=False)


def _schedule() -> tuple[ScheduleSession, FakeOS]:
    os_side = FakeOS()
    return ScheduleSession(os_side.send), os_side


def _canonical_request() -> dict[str, Any]:
    return {
        "profile_ref": "profiles/generic.yaml",
        "source": {
            "format": "canonical_workflow_v2",
            "payload": {
                "schema_version": "2",
                "revision_id": "revision-1",
                "workflow_id": "quick-debug",
                "invocations": [
                    {
                        "node_id": "dose",
                        "action_ref": "pump_1.dose",
                        "input_bindings": {
                            "volume": {"kind": "literal", "value": 5.0}
                        },
                    }
                ],
                "control_edges": [],
            },
        },
        "parameters": {"operator": "test"},
    }


def _legacy_profile() -> LoadedProfile:
    return LoadedProfile(
        profile_id="generic_profile",
        action_catalog=ACTION_CATALOG,
        driver_binding={},
        driver_config={},
        resources={},
        legacy_stage_map={"dose_stage": "pump_1.dose"},
    )


def _legacy_request() -> dict[str, Any]:
    return {
        "profile_ref": "generic_profile",
        "source": {
            "format": "legacy_recipe",
            "payload": {
                "name": "legacy-quick-debug",
                "stages": [
                    {
                        "name": "dose_stage",
                        "enabled": True,
                        "params": {"volume": 7.5},
                    }
                ],
            },
        },
        "parameters": {},
    }


def _service(
    schedule: ScheduleSession,
    journal: SQLiteEventJournal,
    *,
    lock_manager: ResourceLockManager | None = None,
) -> Any:
    api = _runtime_api()
    return api.RuntimeService(
        schedule,
        journal=journal,
        action_catalog=ACTION_CATALOG,
        profiles={"generic_profile": _legacy_profile()},
        resource_lock_manager=lock_manager,
    )


def _runtime_client(
    schedule: ScheduleSession,
    service: Any,
) -> TestClient:
    state = LocalApiState(
        schedule,
        action_catalog=ACTION_CATALOG,
        runtime_service=service,
    )
    return TestClient(create_app(lambda: state))


def _emit_job_status(
    schedule: ScheduleSession,
    *,
    run_id: str,
    status: str,
) -> None:
    asyncio.run(
        schedule.handle_incoming(
            {
                "action": "job_status",
                "data": {
                    "job_id": "dose",
                    "task_id": run_id,
                    "device_id": "pump_1",
                    "notebook_id": "",
                    "action_name": "dose",
                    "status": status,
                    "feedback_data": {},
                    "return_info": {
                        "physical_state": "confirmed_safe",
                        "reconcile_required": False,
                    },
                    "timestamp": 1.0,
                },
            }
        )
    )


def test_local_api_does_not_import_or_call_canonical_compiler() -> None:
    local_api_path = (
        Path(__file__).resolve().parents[2]
        / "unilabos"
        / "app"
        / "local_bridge"
        / "local_api.py"
    )
    tree = ast.parse(local_api_path.read_text(encoding="utf-8"))
    violations: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == (
            "unilabos.workflow.dag_compile"
        ):
            if any(alias.name == "compile_workflow_revision" for alias in node.names):
                violations.append(node.lineno)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "compile_workflow_revision"
        ):
            violations.append(node.lineno)

    assert violations == [], (
        "LocalApiState must delegate Runtime source compilation exclusively to "
        f"RuntimeService; direct compiler references found at {violations}"
    )


def test_local_api_runtime_methods_are_thin_runtime_service_delegates() -> None:
    schedule, _os_side = _schedule()

    class RecordingRuntimeService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def get_workflow(self) -> dict[str, str]:
            self.calls.append(("get_workflow", ""))
            return {"kind": "workflow"}

        async def start_run(self, body: dict[str, Any]) -> dict[str, str]:
            self.calls.append(("start_run", body))
            return {"id": "run-1", "status": "pending"}

        def get_run(self, run_id: str) -> dict[str, str]:
            self.calls.append(("get_run", run_id))
            return {"id": run_id, "status": "running"}

        def get_events(self, run_id: str) -> list[dict[str, str]]:
            self.calls.append(("get_events", run_id))
            return [{"type": "run_started"}]

        def get_timeline(self, run_id: str) -> dict[str, str]:
            self.calls.append(("get_timeline", run_id))
            return {"runId": run_id}

        async def cancel_run(self, run_id: str) -> dict[str, str]:
            self.calls.append(("cancel_run", run_id))
            return {"id": run_id, "status": "cancel_requested"}

        async def reconcile_run(
            self,
            run_id: str,
            body: dict[str, Any],
        ) -> dict[str, str]:
            self.calls.append(("reconcile_run", (run_id, body)))
            return {"id": run_id, "status": "reconciled"}

    service = RecordingRuntimeService()
    state = LocalApiState(schedule, runtime_service=service)
    reconcile_body = {
        "lease_id": "lease-1",
        "resolution": "confirmed_safe",
    }

    assert state.runtime_workflow() == {"kind": "workflow"}
    assert asyncio.run(state.start_runtime_run({"source": {}})) == {
        "id": "run-1",
        "status": "pending",
    }
    assert state.get_runtime_run("run-1") == {
        "id": "run-1",
        "status": "running",
    }
    assert state.runtime_events("run-1") == [{"type": "run_started"}]
    assert state.runtime_timeline("run-1") == {"runId": "run-1"}
    assert asyncio.run(state.cancel_runtime_run("run-1"))["status"] == (
        "cancel_requested"
    )
    assert asyncio.run(
        state.reconcile_runtime_run("run-1", reconcile_body)
    )["status"] == "reconciled"
    assert service.calls == [
        ("get_workflow", ""),
        ("start_run", {"source": {}}),
        ("get_run", "run-1"),
        ("get_events", "run-1"),
        ("get_timeline", "run-1"),
        ("cancel_run", "run-1"),
        ("reconcile_run", ("run-1", reconcile_body)),
    ]


@pytest.mark.parametrize("request_factory", [_canonical_request, _legacy_request])
def test_runtime_service_is_the_source_compilation_and_dispatch_entry(
    tmp_path: Path,
    request_factory: Any,
) -> None:
    schedule, os_side = _schedule()
    journal = SQLiteEventJournal(
        tmp_path / "compile-entry.sqlite",
        runtime_epoch="epoch-1",
    )
    service = _service(schedule, journal)

    accepted = asyncio.run(service.start_run(request_factory()))

    assert accepted["status"] == "pending"
    assert len(os_side.received) == 1
    dispatched = os_side.received[0]
    assert dispatched["action"] == "task_dag"
    assert dispatched["data"]["task_id"] == accepted["id"]
    assert dispatched["data"]["nodes"][0]["device_id"] == "pump_1"
    assert dispatched["data"]["nodes"][0]["action"] == "dose"


def test_runtime_submission_survives_service_and_local_api_rebuild(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime-restart.sqlite"
    first_schedule, _first_os = _schedule()
    first_journal = SQLiteEventJournal(database_path, runtime_epoch="epoch-1")
    first_service = _service(first_schedule, first_journal)
    first_client = _runtime_client(first_schedule, first_service)
    request = _canonical_request()

    response = first_client.post("/api/runtime/local/runs", json=request)

    assert response.status_code == 200
    run_id = str(response.json()["id"])
    submission = first_journal.load_run_submission(run_id)
    assert submission is not None
    assert submission.source == request["source"]
    assert submission.profile_ref == request["profile_ref"]
    assert submission.compiled_dag["task_id"] == run_id
    assert submission.status == "pending"
    first_journal.close()

    second_schedule, _second_os = _schedule()
    second_journal = SQLiteEventJournal(database_path, runtime_epoch="epoch-2")
    second_service = _service(second_schedule, second_journal)
    second_client = _runtime_client(second_schedule, second_service)

    run_response = second_client.get(f"/api/runtime/local/runs/{run_id}")
    events_response = second_client.get(
        f"/api/runtime/local/runs/{run_id}/events"
    )
    timeline_response = second_client.get(
        f"/api/runtime/local/runs/{run_id}/timeline"
    )

    assert run_response.status_code == 200
    assert run_response.json() == {"id": run_id, "status": "pending"}
    assert events_response.status_code == 200
    assert any(
        event["type"] == "run_submitted" for event in events_response.json()
    )
    assert timeline_response.status_code == 200
    assert timeline_response.json()["estimated"]["workflowRevisionHash"]


def test_runtime_cancel_route_waits_for_os_terminal(
    tmp_path: Path,
) -> None:
    schedule, os_side = _schedule()
    journal = SQLiteEventJournal(
        tmp_path / "runtime-cancel.sqlite",
        runtime_epoch="epoch-1",
    )
    service = _service(schedule, journal)
    client = _runtime_client(schedule, service)
    run_id = client.post(
        "/api/runtime/local/runs",
        json=_canonical_request(),
    ).json()["id"]
    os_side.received.clear()

    cancel_response = client.post(f"/api/runtime/local/runs/{run_id}/cancel")

    assert cancel_response.status_code == 200
    assert cancel_response.json() == {
        "id": run_id,
        "status": "cancel_requested",
    }
    assert os_side.received == [
        {"action": "cancel_task", "data": {"task_id": run_id}}
    ]
    handle = schedule.get_run(run_id)
    assert handle is not None
    assert not handle.finished
    assert client.get(f"/api/runtime/local/runs/{run_id}").json()["status"] == (
        "cancel_requested"
    )

    _emit_job_status(schedule, run_id=run_id, status="cancelled")

    # A transport-level node terminal is not the executor-owned run terminal.
    assert client.get(f"/api/runtime/local/runs/{run_id}").json()["status"] == (
        "running"
    )
    journal.record_run_terminal(run_id=run_id, terminal="cancelled")

    terminal = client.get(f"/api/runtime/local/runs/{run_id}")
    assert terminal.status_code == 200
    assert terminal.json()["status"] == "cancelled"


def test_runtime_workflow_preserves_device_and_action_for_quick_debug() -> None:
    schedule, os_side = _schedule()
    state = LocalApiState(schedule, action_catalog=ACTION_CATALOG)
    state.build_graph(
        {
            "name": "projection-test",
            "nodes": [
                {
                    "id": "nested",
                    "data": {
                        "device_id": "pump_1",
                        "method": "dose",
                        "params": {"volume": 1.0},
                    },
                },
                {
                    "id": "flat",
                    "device_id": "pump_1",
                    "action": "dose",
                    "action_args": {"volume": 2.0},
                },
            ],
            "edges": [],
        }
    )

    nodes = state.runtime_workflow()["revision"]["nodes"]

    assert nodes == [
        {
            "id": "nested",
            "label": "dose",
            "deviceId": "pump_1",
            "action": "dose",
        },
        {
            "id": "flat",
            "label": "dose",
            "deviceId": "pump_1",
            "action": "dose",
        },
    ]


def test_runtime_workflow_content_hash_uses_canonical_execution_content(
    tmp_path: Path,
) -> None:
    schedule, os_side = _schedule()
    journal = SQLiteEventJournal(
        tmp_path / "runtime-content-hash.sqlite",
        runtime_epoch="epoch-1",
    )
    service = _service(schedule, journal)
    first_request = _canonical_request()
    first_payload = first_request["source"]["payload"]
    first_payload["layout"] = {"dose": {"x": 10, "y": 20}}
    first_payload["source_map"] = {
        "entries": [{"node_id": "dose", "line": 3, "column": 4}]
    }
    canonical = WorkflowRevision.model_validate(first_payload)

    asyncio.run(service.start_run(first_request))
    first_hash = service.get_workflow()["revision"]["contentHash"]
    first_dispatched_hash = os_side.received[-1]["data"][
        "workflow_revision_hash"
    ]

    second_request = _canonical_request()
    second_request["source"]["payload"]["layout"] = {
        "dose": {"x": 900, "y": 700}
    }
    second_request["source"]["payload"]["source_map"] = {
        "entries": [{"node_id": "dose", "line": 99, "column": 1}]
    }
    asyncio.run(service.start_run(second_request))
    second_hash = service.get_workflow()["revision"]["contentHash"]
    second_dispatched_hash = os_side.received[-1]["data"][
        "workflow_revision_hash"
    ]

    assert canonical.content_hash
    assert first_hash == first_dispatched_hash
    assert second_hash == second_dispatched_hash
    assert second_hash == first_hash
