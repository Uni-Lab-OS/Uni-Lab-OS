from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.web.runtime_actions import create_runtime_action_router
from unilabos.registry.action_catalog import (
    action_catalog_from_runtime_mappings,
)


def _catalog() -> dict:
    actions = action_catalog_from_runtime_mappings(
        {
            "host_node": {
                "test_latency": {
                    "schema": {
                        "properties": {
                            "goal": {
                                "properties": {},
                            },
                            "result": {
                                "properties": {
                                    "avg_rtt_ms": {"type": "number"},
                                    "test_count": {"type": "integer"},
                                    "status": {"type": "string"},
                                },
                                "required": ["status"],
                            },
                        }
                    },
                    "contract": {
                        "timing": {"estimated_duration_s": 1},
                    },
                },
                "_execute_driver_command": {
                    "schema": {
                        "properties": {
                            "goal": {"properties": {}},
                            "result": {"properties": {}},
                        }
                    }
                },
            }
        }
    )
    return {
        "schema_version": "runtime-actions/v1",
        "revision": "runtime-revision-1",
        "actions": actions,
    }


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(
        create_runtime_action_router(_catalog),
        prefix="/internal/v1",
    )
    return TestClient(app)


def test_internal_runtime_catalog_projects_current_instance_contracts() -> None:
    response = _client().get("/internal/v1/runtime-actions")

    assert response.status_code == 200
    assert response.headers["etag"] == '"runtime-revision-1"'
    actions = response.json()["actions"]
    assert set(actions) == {"host_node.test_latency"}
    assert actions["host_node.test_latency"]["inputs"] == {}
    assert actions["host_node.test_latency"]["outputs"]["status"] == {
        "type": "string",
        "required": True,
    }
    assert actions["host_node.test_latency"]["timing"] == {"estimated_duration_s": 1}


def test_internal_runtime_catalog_supports_etag() -> None:
    response = _client().get(
        "/internal/v1/runtime-actions",
        headers={"If-None-Match": '"runtime-revision-1"'},
    )

    assert response.status_code == 304
    assert response.content == b""


def test_internal_runtime_catalog_requires_configured_token(
    monkeypatch,
) -> None:
    monkeypatch.setenv("UNILABOS_INTERNAL_API_TOKEN", "edge-secret")
    client = _client()

    denied = client.get("/internal/v1/runtime-actions")
    allowed = client.get(
        "/internal/v1/runtime-actions",
        headers={"Authorization": "Bearer edge-secret"},
    )

    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "INTERNAL_API_UNAUTHORIZED"
    assert allowed.status_code == 200
