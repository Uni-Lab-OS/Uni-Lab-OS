"""Legacy recipe compilation through a loaded profile and generic Runtime API."""

from __future__ import annotations

import importlib
import inspect
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from unilabos.app.local_bridge.local_api import LocalApiState, create_app
from unilabos.app.local_bridge.schedule_ws import ScheduleSession


FIXTURE = Path(__file__).parent / "fixtures" / "single_sample_golden.json"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROFILE_PATH = (
    PROJECT_ROOT.parent
    / "Uni-Lab-Templates"
    / "packages"
    / "ptlc_station"
    / "package.yaml"
)
GENERIC_PRODUCTION_ROOTS = (
    PROJECT_ROOT / "unilabos" / "app",
    PROJECT_ROOT / "unilabos" / "scheduler",
    PROJECT_ROOT / "unilabos" / "runtime",
    PROJECT_ROOT / "unilabos" / "workflow",
    PROJECT_ROOT / "unilabos" / "registry",
)


class FakeTransport:
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []

    async def send(self, message: dict[str, Any]) -> None:
        self.received.append(message)


def _loaded_profile():
    driver_api = importlib.import_module("unilabos.devices.generic_plc_macro")
    profile_api = importlib.import_module("unilabos.runtime.profile_loader")
    return profile_api.ProfileLoader(
        driver_catalog={
            "generic_plc_macro": driver_api.DeclarativePLCMacroDriver,
        }
    ).load(PROFILE_PATH)


def _source_request(
    golden: dict[str, Any],
    *,
    sample_id: str | None = None,
) -> dict[str, Any]:
    return {
        "profile_ref": "ptlc_station",
        "source": {
            "format": "legacy_recipe",
            "payload": {
                "name": "single-sample",
                "stages": [
                    {
                        "name": step["macro"],
                        "enabled": True,
                        "params": step["params"],
                    }
                    for step in golden["recipe"]
                ],
            },
        },
        "parameters": {"sample_id": sample_id or golden["sample_id"]},
    }


def test_legacy_recipe_compiles_once_via_profiled_runtime_api() -> None:
    golden = json.loads(FIXTURE.read_text(encoding="utf-8"))
    profile = _loaded_profile()
    os_side = FakeTransport()
    schedule = ScheduleSession(os_side.send)
    state = LocalApiState(
        schedule,
        profiles={"ptlc_station": profile},
    )
    client = TestClient(create_app(lambda: state))
    first_request = _source_request(golden, sample_id="sample-001")
    second_request = _source_request(golden, sample_id="sample-002")

    first_response = client.post("/api/runtime/local/runs", json=first_request)
    second_response = client.post("/api/runtime/local/runs", json=second_request)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert len(os_side.received) == 2
    submitted = os_side.received[0]
    assert submitted["action"] == "task_dag"
    dag = submitted["data"]
    second_dag = os_side.received[1]["data"]
    assert len(dag["workflow_revision_hash"]) == 64
    assert dag["workflow_revision_hash"] == second_dag["workflow_revision_hash"]
    assert dag["runtime_parameters"] == {"sample_id": "sample-001"}
    assert second_dag["runtime_parameters"] == {"sample_id": "sample-002"}
    assert [(node["device_id"], node["action"]) for node in dag["nodes"]] == [
        ("ptlc_station", step["macro"])
        for step in golden["recipe"]
    ]
    assert [
        (edge["source_node_uuid"], edge["target_node_uuid"])
        for edge in dag["edges"]
    ] == [
        (dag["nodes"][index]["node_id"], dag["nodes"][index + 1]["node_id"])
        for index in range(len(dag["nodes"]) - 1)
    ]
    assert all(len(node["idempotency_key"]) == 64 for node in dag["nodes"])
    assert len({node["idempotency_key"] for node in dag["nodes"]}) == len(
        dag["nodes"]
    )
    assert all(node["resource_claims"] for node in dag["nodes"])
    assert all("output_schema" in node for node in dag["nodes"])
    assert all("sample_id" not in node["input_bindings"] for node in dag["nodes"])
    assert [node["idempotency_key"] for node in dag["nodes"]] == [
        node["idempotency_key"] for node in second_dag["nodes"]
    ]
    assert "workflow" not in first_request
    assert "nodes" not in first_request["source"]["payload"]


def test_generic_runtime_and_macro_driver_have_no_device_family_dependency() -> None:
    offenders: list[str] = []
    for root in GENERIC_PRODUCTION_ROOTS:
        for path in root.rglob("*.py"):
            if "ptlc" in path.read_text(encoding="utf-8").lower():
                offenders.append(str(path.relative_to(PROJECT_ROOT)))

    driver_api = importlib.import_module("unilabos.devices.generic_plc_macro")
    assert offenders == []
    assert "ptlc" not in driver_api.__name__.lower()
    assert "ptlc" not in inspect.getsource(driver_api).lower()
    assert not (
        PROJECT_ROOT / "unilabos" / "devices" / "ptlc" / "recipe_runtime.py"
    ).exists()
