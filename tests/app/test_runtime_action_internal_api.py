from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.web.runtime_actions import (
    create_runtime_action_router,
    runtime_action_mappings_for_host,
)
from unilabos.registry.action_catalog import (
    action_catalog_from_runtime_mappings,
)
from unilabos.registry.registry import lab_registry


def _catalog() -> dict:
    actions = action_catalog_from_runtime_mappings(
        {
            "host_node": {
                "test_latency": {
                    "type": "UniLabJsonCommand",
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
    assert actions["host_node.test_latency"]["action_type"] == "UniLabJsonCommand"


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


def test_configured_graph_actions_survive_driver_initialization_failure(
    monkeypatch,
) -> None:
    configured_action = {
        "type": "UniLabJsonCommand",
        "schema": {
            "properties": {
                "goal": {
                    "properties": {
                        "volume": {"type": "number"},
                    }
                },
                "result": {
                    "properties": {
                        "delivered": {"type": "number"},
                    }
                },
            }
        }
    }
    monkeypatch.setitem(
        lab_registry.device_type_registry,
        "liquid_handler",
        {
            "class": {
                "action_value_mappings": {
                    "transfer": configured_action,
                }
            }
        },
    )
    configured_device = SimpleNamespace(
        res_content=SimpleNamespace(
            id="PLR_STATION",
            klass="liquid_handler",
            type="device",
        )
    )
    host = SimpleNamespace(
        devices_config=SimpleNamespace(root_nodes=[configured_device]),
        # Simulate the real failure mode: HostNode is ready, but only its own
        # live mapping exists because PLR_STATION driver initialization failed.
        _action_value_mappings={
            "host_node": {
                "test_latency": {
                    "schema": {
                        "properties": {
                            "goal": {"properties": {}},
                            "result": {
                                "properties": {
                                    "status": {"type": "string"},
                                }
                            },
                        }
                    }
                }
            }
        },
    )

    mappings = runtime_action_mappings_for_host(host)
    actions = action_catalog_from_runtime_mappings(mappings)

    assert "PLR_STATION.transfer" in actions
    assert actions["PLR_STATION.transfer"]["inputs"] == {
        "volume": {"type": "number"}
    }
    assert actions["PLR_STATION.transfer"]["action_type"] == "UniLabJsonCommand"
    assert "host_node.test_latency" in actions
