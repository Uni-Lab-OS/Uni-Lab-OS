from __future__ import annotations

import asyncio

import httpx
import pytest

from unilabos.app.local_bridge.runtime_action_api import (
    RuntimeActionCatalogProxy,
    RuntimeActionCatalogProxyError,
)


def _payload() -> dict:
    return {
        "schema_version": "runtime-actions/v1",
        "revision": "runtime-revision-1",
        "actions": {
            "host_node.test_latency": {
                "inputs": {},
                "outputs": {
                    "status": {"type": "string", "required": True},
                },
                "contract": {},
                "resource_claims": [],
                "effects": [],
                "timing": {},
            }
        },
    }


def test_proxy_fetches_revalidates_and_forwards_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers.get("If-None-Match") == '"runtime-revision-1"':
            return httpx.Response(
                304,
                headers={"ETag": '"runtime-revision-1"'},
                request=request,
            )
        return httpx.Response(
            200,
            json=_payload(),
            headers={"ETag": '"runtime-revision-1"'},
            request=request,
        )

    async def scenario() -> None:
        proxy = RuntimeActionCatalogProxy(
            "http://127.0.0.1:8002",
            internal_token="edge-secret",
            transport=httpx.MockTransport(handler),
        )
        first, first_revision = await proxy.fetch(force=True)
        second, second_revision = await proxy.fetch(force=True)

        assert set(first) == {"host_node.test_latency"}
        assert second == first
        assert first_revision == second_revision == "runtime-revision-1"

    asyncio.run(scenario())
    assert requests[0].headers["authorization"] == "Bearer edge-secret"
    assert requests[1].headers["if-none-match"] == '"runtime-revision-1"'


def test_proxy_rejects_invalid_catalog_without_stale_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "schema_version": "runtime-actions/v1",
                "revision": "bad",
                "actions": {
                    "host_node.test_latency": {
                        "inputs": {},
                    }
                },
            },
            request=request,
        )

    async def scenario() -> None:
        proxy = RuntimeActionCatalogProxy(
            "http://127.0.0.1:8002",
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(
            RuntimeActionCatalogProxyError,
            match="输入/输出",
        ):
            await proxy.fetch(force=True)

    asyncio.run(scenario())
