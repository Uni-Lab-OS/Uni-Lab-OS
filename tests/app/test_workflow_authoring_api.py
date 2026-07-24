"""RED contracts for non-executing local authoring HTTP routes."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from unilabos.app.local_bridge.local_api import LocalApiState, create_app
from unilabos.app.local_bridge.schedule_ws import ScheduleSession
from unilabos.workflow.canonical import WorkflowRevision

from tests.workflow.authoring_test_support import (
    BASE_REVISION_ID,
    GOLDEN_SOURCE,
    GOLDEN_SOURCE_URI,
    authoring_request,
    golden_action_catalog,
)


class RecordingTransport:
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []

    async def send(self, message: dict[str, Any]) -> None:
        self.received.append(message)


class ForbiddenPersistence:
    def __getattr__(self, name: str) -> Any:
        def forbidden(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError(f"authoring route touched persistence seam: {name}")

        return forbidden


def _client() -> tuple[TestClient, LocalApiState, RecordingTransport]:
    transport = RecordingTransport()
    schedule = ScheduleSession(transport.send)
    state = LocalApiState(
        schedule,
        action_catalog=golden_action_catalog(),
        journal=ForbiddenPersistence(),  # type: ignore[arg-type]
    )
    return TestClient(create_app(lambda: state)), state, transport


def _assert_no_runtime_change(
    state: LocalApiState,
    transport: RecordingTransport,
    before: dict[str, Any],
) -> None:
    assert state.runtime_workflow() == before
    assert transport.received == []
    assert state._runs == {}


def test_all_authoring_routes_share_candidates_without_running_or_persisting() -> None:
    client, state, transport = _client()
    before = copy.deepcopy(state.runtime_workflow())

    compiled_response = client.post(
        "/api/v1/authoring/compile",
        json=authoring_request(),
    )
    assert compiled_response.status_code == 200
    compiled = compiled_response.json()
    assert compiled["base_revision_id"] == BASE_REVISION_ID
    assert compiled["candidate"]["python_source"] == GOLDEN_SOURCE
    assert compiled["diagnostics"] == []
    _assert_no_runtime_change(state, transport, before)

    generated_response = client.post(
        "/api/v1/authoring/generate-python",
        json={
            "base_revision_id": BASE_REVISION_ID,
            "canonical_ir": compiled["candidate"]["canonical_ir"],
            "source_uri": GOLDEN_SOURCE_URI,
        },
    )
    assert generated_response.status_code == 200
    generated = generated_response.json()
    assert (
        generated["candidate"]["content_hash"] == compiled["candidate"]["content_hash"]
    )
    assert generated["candidate"]["python_source"].strip()
    _assert_no_runtime_change(state, transport, before)

    validated_response = client.post(
        "/api/v1/authoring/validate",
        json={
            "base_revision_id": BASE_REVISION_ID,
            "candidate": generated["candidate"],
        },
    )
    assert validated_response.status_code == 200
    assert (
        validated_response.json()["candidate"]["revision_id"]
        == generated["candidate"]["revision_id"]
    )
    _assert_no_runtime_change(state, transport, before)


def test_compile_errors_are_diagnostics_and_do_not_replace_runtime_revision() -> None:
    client, state, transport = _client()
    before = copy.deepcopy(state.runtime_workflow())
    response = client.post(
        "/api/v1/authoring/compile",
        json=authoring_request(
            source=(
                '@workflow_definition(workflow_id="bad", revision="v1")\n'
                "def bad():\n"
                "    while True:\n"
                "        develop.init(target_tank=1)\n"
            ),
        ),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["candidate"] is None
    assert payload["diagnostics"][0]["severity"] == "error"
    assert payload["diagnostics"][0]["start_line"] >= 1
    _assert_no_runtime_change(state, transport, before)


def test_validate_route_fails_closed_on_unknown_action_without_side_effects() -> None:
    client, state, transport = _client()
    before = copy.deepcopy(state.runtime_workflow())
    compiled_response = client.post(
        "/api/v1/authoring/compile",
        json=authoring_request(),
    )
    assert compiled_response.status_code == 200
    candidate = compiled_response.json()["candidate"]
    candidate["canonical_ir"]["invocations"][0]["action_ref"] = (
        "uninstalled_device.dispatch"
    )
    candidate["content_hash"] = WorkflowRevision.model_validate(
        candidate["canonical_ir"]
    ).content_hash

    response = client.post(
        "/api/v1/authoring/validate",
        json={
            "base_revision_id": BASE_REVISION_ID,
            "candidate": candidate,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["candidate"] is None
    assert any(
        diagnostic["severity"] == "error" and diagnostic["code"] == "UNKNOWN_ACTION"
        for diagnostic in payload["diagnostics"]
    )
    _assert_no_runtime_change(state, transport, before)


@pytest.mark.parametrize(
    ("path", "method", "origin"),
    [
        (
            "/api/v1/authoring/compile",
            "POST",
            "http://localhost:32234",
        ),
        (
            "/api/runtime/local/workflow",
            "GET",
            "http://127.0.0.1:41123",
        ),
    ],
)
def test_loopback_frontends_can_preflight_authoring_and_runtime_routes(
    path: str,
    method: str,
    origin: str,
) -> None:
    client, _, _ = _client()

    response = client.options(
        path,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert method in response.headers["access-control-allow-methods"]


@pytest.mark.parametrize(
    "origin",
    [
        "https://cloud.example.test",
        "http://localhost.evil.example:32234",
        "http://192.168.1.20:32234",
    ],
)
def test_non_loopback_origins_are_rejected_by_cors(origin: str) -> None:
    client, _, _ = _client()

    response = client.options(
        "/api/v1/authoring/compile",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code in {400, 403}
    assert "access-control-allow-origin" not in response.headers


def test_runtime_workflow_without_identity_keeps_active_projection_compatibility() -> (
    None
):
    client, state, _ = _client()

    response = client.get("/api/runtime/local/workflow")

    assert response.status_code == 200
    assert response.json() == state.runtime_workflow()


def test_runtime_workflow_query_enforces_active_workflow_identity() -> None:
    client, state, _ = _client()
    active = state.runtime_workflow()
    active_workflow_id = active["definition"]["id"]

    matched = client.get(
        "/api/runtime/local/workflow",
        params={"workflow_id": active_workflow_id},
    )
    mismatched = client.get(
        "/api/runtime/local/workflow",
        params={"workflow_id": f"{active_workflow_id}-different"},
    )

    assert matched.status_code == 200
    assert matched.json() == active
    assert mismatched.status_code == 404
    assert "WORKFLOW_NOT_FOUND" in str(mismatched.json().get("detail"))


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/v1/authoring/compile", None),
        (
            "/api/v1/authoring/compile",
            {
                **authoring_request(),
                "execution_request": {"mode": "real"},
            },
        ),
        (
            "/api/v1/authoring/compile",
            {
                **authoring_request(),
                "device_token": "secret",
            },
        ),
        (
            "/api/v1/authoring/compile",
            {
                **authoring_request(),
                "database_credentials": {"password": "secret"},
            },
        ),
        (
            "/api/v1/authoring/compile",
            {
                **authoring_request(
                    source=(
                        '@workflow_definition(workflow_id="evil", revision="v1")\n'
                        "def evil():\n"
                        "    attacker.dispatch(command='run')\n"
                    ),
                ),
                "action_catalog": {"attacker.dispatch": {"inputs": {}, "outputs": {}}},
            },
        ),
        ("/api/v1/authoring/generate-python", {"base_revision_id": "only"}),
        ("/api/v1/authoring/validate", {"candidate": {}}),
    ],
)
def test_malformed_or_unsafe_envelopes_fail_with_no_dispatch(
    path: str,
    payload: dict[str, Any] | None,
) -> None:
    client, state, transport = _client()
    before = copy.deepcopy(state.runtime_workflow())

    response = client.post(path, json=payload)
    repeated = client.post(path, json=payload)

    assert response.status_code in {400, 422}
    assert repeated.status_code == response.status_code
    assert response.json().get("detail")
    assert repeated.json().get("detail") == response.json().get("detail")
    _assert_no_runtime_change(state, transport, before)
