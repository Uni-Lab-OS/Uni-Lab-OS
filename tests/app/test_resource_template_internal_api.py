from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.web.resource_templates import (
    create_resource_template_router,
)
from unilabos.registry.template_catalog import ResourceTemplateCatalog


class FakeRegistry:
    _setup_called = True
    device_type_registry = {
        "public-device": {
            "catalog": {"visibility": "public"},
            "class": {"module": "example:Device"},
        }
    }
    resource_type_registry = {}

    @staticmethod
    def _module_source_hash(module: str) -> str:
        return module


def _client() -> TestClient:
    app = FastAPI()
    catalog = ResourceTemplateCatalog(FakeRegistry())
    app.include_router(
        create_resource_template_router(lambda: catalog),
        prefix="/internal/v1",
    )
    return TestClient(app)


def test_internal_catalog_supports_etag_and_detail() -> None:
    client = _client()

    response = client.get("/internal/v1/resource-templates")
    assert response.status_code == 200
    etag = response.headers["etag"]
    item = response.json()["items"][0]

    unchanged = client.get(
        "/internal/v1/resource-templates",
        headers={"If-None-Match": etag},
    )
    assert unchanged.status_code == 304
    assert unchanged.content == b""

    detail = client.get(
        f"/internal/v1/resource-templates/{item['uuid']}"
    )
    assert detail.status_code == 200
    assert detail.json()["configuration"]["schema"] == {}


def test_internal_catalog_requires_configured_token(monkeypatch) -> None:
    monkeypatch.setenv("UNILABOS_INTERNAL_API_TOKEN", "edge-secret")
    client = _client()

    denied = client.get("/internal/v1/resource-templates")
    allowed = client.get(
        "/internal/v1/resource-templates",
        headers={"Authorization": "Bearer edge-secret"},
    )

    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "INTERNAL_API_UNAUTHORIZED"
    assert allowed.status_code == 200


def test_internal_catalog_returns_structured_not_found() -> None:
    response = _client().get(
        "/internal/v1/resource-templates/00000000-0000-0000-0000-000000000000"
    )

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "TEMPLATE_NOT_FOUND",
            "message": "00000000-0000-0000-0000-000000000000",
            "retryable": False,
        }
    }
