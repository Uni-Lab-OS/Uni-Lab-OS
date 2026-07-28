from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from unilabos.app.local_bridge.local_api import create_app
from unilabos.app.local_bridge.resource_template_api import (
    ResourceTemplateProxy,
)


def test_public_catalog_is_independent_of_schedule_session_and_revalidates() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers.get("If-None-Match") == '"revision-1"':
            return httpx.Response(
                304,
                headers={"ETag": '"revision-1"'},
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "revision": "revision-1",
                "stale": False,
                "items": [_summary()],
            },
            headers={"ETag": '"revision-1"'},
            request=request,
        )

    proxy = ResourceTemplateProxy(
        "http://127.0.0.1:8002",
        ttl_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    client = TestClient(create_app(lambda: None, proxy))

    first = client.get("/api/v1/resource-templates")
    second = client.get("/api/v1/resource-templates?refresh=true")

    assert first.status_code == 200
    assert first.json()["data"]["items"][0]["key"] == "plate-96"
    assert second.status_code == 200
    assert second.json()["data"]["stale"] is False
    assert requests[1].headers["If-None-Match"] == '"revision-1"'


def test_public_catalog_returns_stale_cache_and_disables_creation() -> None:
    available = True

    def handler(request: httpx.Request) -> httpx.Response:
        if not available:
            raise httpx.ConnectError("OS stopped", request=request)
        return httpx.Response(
            200,
            json={
                "revision": "revision-1",
                "stale": False,
                "items": [_summary(creation_available=True)],
            },
            headers={"ETag": '"revision-1"'},
            request=request,
        )

    proxy = ResourceTemplateProxy(
        "http://127.0.0.1:8002",
        ttl_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    client = TestClient(create_app(lambda: None, proxy))
    assert client.get("/api/v1/resource-templates").status_code == 200

    available = False
    stale = client.get(
        "/api/v1/resource-templates",
        headers={"If-None-Match": '"revision-1"'},
    )

    assert stale.status_code == 200
    assert stale.json()["data"]["stale"] is True
    creation = stale.json()["data"]["items"][0]["creation"]
    assert creation["available"] is False
    assert "缓存目录" in creation["reason"]
    assert stale.headers["warning"].startswith("299 Uni-Lab")


def test_public_catalog_cold_failure_is_structured_503() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("OS stopped", request=request)

    proxy = ResourceTemplateProxy(
        "http://127.0.0.1:8002",
        transport=httpx.MockTransport(handler),
    )
    response = TestClient(create_app(lambda: None, proxy)).get(
        "/api/v1/resource-templates"
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CATALOG_UNAVAILABLE"
    assert response.json()["error"]["retryable"] is True


def test_public_catalog_forwards_internal_token_and_asset_range() -> None:
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(
            206,
            content=b"mesh",
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Range": "bytes 0-3/4",
                "Accept-Ranges": "bytes",
            },
            request=request,
        )

    proxy = ResourceTemplateProxy(
        "http://127.0.0.1:8002",
        internal_token="internal-secret",
        transport=httpx.MockTransport(handler),
    )
    response = TestClient(create_app(lambda: None, proxy)).get(
        "/api/v1/resource-templates/template-1/assets/model",
        headers={"Range": "bytes=0-3"},
    )

    assert response.status_code == 206
    assert response.content == b"mesh"
    assert seen_headers["authorization"] == "Bearer internal-secret"
    assert seen_headers["range"] == "bytes=0-3"


def _summary(*, creation_available: bool = False) -> dict:
    return {
        "uuid": "template-1",
        "key": "plate-96",
        "source_namespace": "unilabos",
        "kind": "resource",
        "display_name": "96 孔板",
        "description": None,
        "category_path": ["plates"],
        "tags": [],
        "icon": "resource",
        "status": "ready",
        "content_hash": "content-1",
        "creation": {
            "mode": "resource-tree",
            "available": creation_available,
            "reason": None,
        },
    }
