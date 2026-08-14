"""HTTP acceptance tests for the stable Workspace Backend authority gate."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.workspace_authority_gate import install_workspace_authority_gate


def _configuration(path: Path, mode: str) -> None:
    path.write_text(
        json.dumps({"schemaVersion": 1, "domainMode": mode}),
        encoding="utf-8",
    )


def test_workspace_backend_authority_gate_switches_without_rebuilding_app(
    tmp_path: Path,
) -> None:
    configuration = tmp_path / "environment.local.json"
    _configuration(configuration, "local")
    app = FastAPI()
    install_workspace_authority_gate(app, configuration)

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/workspace/package-mounts")
    def package_mounts() -> dict[str, object]:
        return {"data": {"mounts": []}}

    @app.get("/api/v1/workflows")
    def workflows() -> dict[str, object]:
        return {"data": {"items": []}}

    @app.post("/api/v1/workflow-tasks")
    def run_workflow() -> dict[str, str]:
        return {"status": "running"}

    client = TestClient(app)
    assert client.get("/api/v1/workflows").status_code == 200
    assert client.post("/api/v1/workflow-tasks").status_code == 200

    # Atomic replacement is the runtime switch signal; the FastAPI process,
    # application and route graph stay untouched.
    replacement = configuration.with_suffix(".next")
    _configuration(replacement, "backend")
    replacement.replace(configuration)

    health_response = client.get("/api/v1/health")
    mounts = client.get("/api/v1/workspace/package-mounts")
    workflows_response = client.get("/api/v1/workflows")
    run = client.post("/api/v1/workflow-tasks")
    assert health_response.status_code == 200
    assert mounts.status_code == 200
    assert workflows_response.status_code == 409
    assert workflows_response.json()["error"]["code"] == "authority_inactive"
    assert run.status_code == 409

    _configuration(replacement, "local")
    replacement.replace(configuration)
    assert client.get("/api/v1/workflows").status_code == 200
    assert client.post("/api/v1/workflow-tasks").status_code == 200
