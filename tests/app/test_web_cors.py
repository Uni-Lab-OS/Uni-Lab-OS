"""Workbench browser CORS contract for the production FastAPI application."""

from fastapi.testclient import TestClient

from unilabos.app.web.server import app


def test_workbench_cross_origin_get_reflects_origin_with_credentials() -> None:
    """Credentialed Workbench requests cannot combine wildcard origin and credentials."""

    origin = "http://127.0.0.1:3310"
    response = TestClient(app).get(
        "/api/openapi.json",
        headers={"Origin": origin},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"
    assert response.headers["vary"] == "Origin"
