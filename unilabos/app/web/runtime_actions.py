"""当前 Edge 设备实例的 Runtime Action Catalog 内部接口。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from unilabos.app.web.internal_api import (
    authorize_internal,
    etag,
    etag_matches,
    internal_error,
)
from unilabos.registry.action_catalog import (
    action_catalog_from_runtime_mappings,
)


class RuntimeActionCatalogNotReady(RuntimeError):
    """HostNode 尚未完成当前设备动作注册。"""


def current_runtime_action_catalog() -> dict[str, Any]:
    """从当前 HostNode 内存映射生成可供统一 Runtime 使用的动作合同。"""

    from unilabos.ros.nodes.presets.host_node import HostNode

    host_node = HostNode.get_instance(0)
    if host_node is None:
        raise RuntimeActionCatalogNotReady("HostNode 尚未初始化")
    mappings = getattr(host_node, "_action_value_mappings", None)
    if not isinstance(mappings, Mapping):
        raise RuntimeActionCatalogNotReady("HostNode 动作目录尚未就绪")
    actions = action_catalog_from_runtime_mappings(mappings)
    canonical = json.dumps(
        actions,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    revision = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        "schema_version": "runtime-actions/v1",
        "revision": revision,
        "actions": actions,
    }


def create_runtime_action_router(
    get_catalog: Callable[[], dict[str, Any]] = current_runtime_action_catalog,
) -> APIRouter:
    router = APIRouter()

    @router.get("/runtime-actions")
    async def list_runtime_actions(request: Request) -> Response:
        denied = authorize_internal(request, capability="Runtime 动作目录接口")
        if denied is not None:
            return denied
        try:
            result = get_catalog()
        except RuntimeActionCatalogNotReady as exc:
            return internal_error(
                503,
                "ACTION_CATALOG_UNAVAILABLE",
                str(exc),
                retryable=True,
            )
        response_etag = etag(str(result["revision"]))
        if etag_matches(request, response_etag):
            return Response(status_code=304, headers={"ETag": response_etag})
        return JSONResponse(result, headers={"ETag": response_etag})

    return router


runtime_action_router = create_runtime_action_router()
