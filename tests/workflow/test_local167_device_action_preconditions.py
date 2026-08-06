"""LOCAL-167 单动作调试（D1A）物理派发前置条件回归。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import tests.app.test_d1a_device_action_task_contract as contract
from unilabos.app.scheduler.device_state import DeviceStateStore
from unilabos.app.workflow_api import create_workflow_app
from unilabos.registry.action_preconditions import normalize_action_preconditions
from unilabos.workflow.catalog import TemplateCatalog
from unilabos.workflow.device_action_preconditions import (
    DeviceActionPreconditionFailure,
    evaluate_device_action_preconditions,
)
from unilabos.workflow.device_action_task import DeviceActionTaskService
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

NOW_MS = 1_786_000_000_000
PRECONDITIONS = normalize_action_preconditions(
    [
        {
            "id": "material_present",
            "parameter": "duration_seconds",
            "properties": {"1": "material_present_position_1"},
            "sensors": {"1": "传感器状态_上位机[2].NO[10]"},
            "expected": True,
            "policy": "fail_fast",
            "max_age_seconds": 2,
            "message": "位置 1 无物料，无法开始磁搅",
        }
    ]
)


def _evaluate(
    state: DeviceStateStore | None,
    *,
    now_ms: int = NOW_MS,
) -> dict[str, Any] | None:
    try:
        evaluate_device_action_preconditions(
            state=state,
            device_id="robot",
            input_value={"duration_seconds": 1},
            conditions=PRECONDITIONS,
            now_ms=now_ms,
        )
    except DeviceActionPreconditionFailure as error:
        return error.details
    return None


def test_false_observation_fails_with_complete_details() -> None:
    state = DeviceStateStore()
    try:
        state.set("robot", "material_present_position_1", False, now_ms=NOW_MS)
        details = _evaluate(state)
        assert details is not None
        assert details["reason"] == "not_met"
        assert details["sensor"] == "传感器状态_上位机[2].NO[10]"
        assert details["expected"] is True
        assert details["actual"] is False
        assert details["checked_at_ms"] == NOW_MS
        assert details["timeout_policy"] == {
            "mode": "fail_fast",
            "timeout_seconds": 0,
        }
    finally:
        state.close()


def test_true_observation_passes() -> None:
    state = DeviceStateStore()
    try:
        state.set("robot", "material_present_position_1", True, now_ms=NOW_MS)
        assert _evaluate(state) is None
    finally:
        state.close()


@pytest.mark.parametrize(
    ("state_value", "observed_at", "reason"),
    [
        (None, None, "unknown"),
        (True, NOW_MS - 2_001, "stale"),
    ],
)
def test_unknown_and_stale_observations_fail_closed(
    state_value: bool | None,
    observed_at: int | None,
    reason: str,
) -> None:
    state = DeviceStateStore()
    try:
        if state_value is not None and observed_at is not None:
            state.set(
                "robot",
                "material_present_position_1",
                state_value,
                now_ms=observed_at,
            )
        details = _evaluate(state)
        assert details is not None
        assert details["reason"] == reason
        assert details["actual"] is state_value
    finally:
        state.close()


def test_unavailable_state_authority_fails_closed() -> None:
    details = _evaluate(None)
    assert details is not None
    assert details["reason"] == "unknown"


def _runtime(tmp_path: Path) -> tuple[
    WorkflowStore,
    DeviceStateStore,
    contract.RecordingAdmission,
    TestClient,
    contract.Harness,
]:
    database_path = tmp_path / "workflow.db"
    store = WorkflowStore(database_path)
    catalog = TemplateCatalog(store)
    base = contract._template_import(
        name="move",
        display_name="移动",
        resource_template_uuid=contract.RESOURCE_TEMPLATE_UUID,
        schema=contract.SIMPLE_SCHEMA,
    )
    template = dict(base.template)
    template["meta_data"] = {
        "unilab": {"action_preconditions": PRECONDITIONS}
    }
    snapshot = catalog.replace(
        contract.AUTHORITY,
        [replace(base, template=template)],
    )
    node_template = snapshot.node_templates[0]
    live = contract.MutableLiveCatalog()
    live.devices["robot"]["actions"]["move"]["preconditions"] = PRECONDITIONS
    admission = contract.RecordingAdmission(database_path)
    state = DeviceStateStore()
    service = DeviceActionTaskService(
        store=store,
        template_catalog=catalog,
        authority=contract.AUTHORITY,
        live_catalog=live,
        admission=admission,
        precondition_state=state,
    )
    client = TestClient(
        create_workflow_app(WorkflowService(store), device_action_tasks=service),
        raise_server_exceptions=False,
    )
    harness = contract.Harness(
        database_path,
        store,
        client,
        catalog,
        snapshot.fingerprint,
        str(node_template["uuid"]),
        "",
        live,
        admission,
    )
    return store, state, admission, client, harness


def test_http_fast_failure_creates_no_task_and_never_wakes_dispatch(
    tmp_path: Path,
) -> None:
    store, state, admission, client, harness = _runtime(tmp_path)
    try:
        state.set("robot", "material_present_position_1", False)
        response = client.post(
            "/api/v1/device-action-tasks",
            json=contract._request(
                harness,
                input_value={
                    "duration_seconds": 1,
                    "note": None,
                    "axes": [],
                    "options": {},
                },
            ),
        )
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "precondition_not_met"
        assert error["message"] == "位置 1 无物料，无法开始磁搅"
        assert error["details"]["reason"] == "not_met"
        assert admission.wakes == []
        with store.transaction() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM workflow_task"
            ).fetchone()[0] == 0
    finally:
        client.close()
        state.close()
        store.close()


def test_http_true_observation_admits_task(tmp_path: Path) -> None:
    store, state, admission, client, harness = _runtime(tmp_path)
    try:
        state.set("robot", "material_present_position_1", True)
        response = client.post(
            "/api/v1/device-action-tasks",
            json=contract._request(
                harness,
                input_value={
                    "duration_seconds": 1,
                    "note": None,
                    "axes": [],
                    "options": {},
                },
            ),
        )
        assert response.status_code == 201
        assert len(admission.wakes) == 1
    finally:
        client.close()
        state.close()
        store.close()
