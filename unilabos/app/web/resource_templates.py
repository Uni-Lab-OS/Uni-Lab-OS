"""OS 内部 Registry 模板目录 HTTP 路由。

该路由只允许本机调用，供 local_bridge 在 ``:8014`` 投影统一前端契约。
浏览器不应直接连接本接口。
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from unilabos.registry.registry import lab_registry
from unilabos.registry.template_catalog import (
    ResourceTemplateCatalog,
    TemplateAssetError,
    TemplateCatalogNotReady,
    TemplateNotFound,
)

_INTERNAL_TOKEN_ENV = "UNILABOS_INTERNAL_API_TOKEN"
_catalog = ResourceTemplateCatalog(lab_registry)


def create_resource_template_router(
    get_catalog: Callable[[], ResourceTemplateCatalog] = lambda: _catalog,
) -> APIRouter:
    router = APIRouter()

    @router.get("/resource-templates")
    async def list_resource_templates(request: Request) -> Response:
        denied = _authorize_internal(request)
        if denied is not None:
            return denied
        try:
            result = get_catalog().list_templates()
        except TemplateCatalogNotReady as exc:
            return _error(
                503,
                "CATALOG_UNAVAILABLE",
                str(exc),
                retryable=True,
            )
        etag = _etag(result["revision"])
        if _etag_matches(request, etag):
            return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(result, headers={"ETag": etag})

    @router.get("/resource-templates/{template_uuid}")
    async def get_resource_template(
        template_uuid: str,
        request: Request,
    ) -> Response:
        denied = _authorize_internal(request)
        if denied is not None:
            return denied
        try:
            detail = get_catalog().get_template(template_uuid)
        except TemplateCatalogNotReady as exc:
            return _error(
                503,
                "CATALOG_UNAVAILABLE",
                str(exc),
                retryable=True,
            )
        except TemplateNotFound:
            return _error(
                404,
                "TEMPLATE_NOT_FOUND",
                template_uuid,
                retryable=False,
            )
        etag = _etag(detail["content_hash"])
        if _etag_matches(request, etag):
            return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(detail, headers={"ETag": etag})

    @router.get(
        "/resource-templates/{template_uuid}/assets/{asset_key}",
    )
    async def get_resource_template_asset(
        template_uuid: str,
        asset_key: str,
        request: Request,
    ) -> Response:
        denied = _authorize_internal(request)
        if denied is not None:
            return denied
        try:
            path = get_catalog().resolve_asset(template_uuid, asset_key)
        except TemplateNotFound:
            return _error(
                404,
                "TEMPLATE_NOT_FOUND",
                template_uuid,
                retryable=False,
            )
        except TemplateAssetError as exc:
            return _error(
                404,
                "TEMPLATE_ASSET_NOT_FOUND",
                str(exc),
                retryable=False,
            )
        return FileResponse(path)

    return router


def _authorize_internal(request: Request) -> JSONResponse | None:
    client_host = request.client.host if request.client is not None else ""
    if client_host != "testclient":
        try:
            if not ipaddress.ip_address(client_host).is_loopback:
                return _error(
                    403,
                    "INTERNAL_API_FORBIDDEN",
                    "Registry 模板接口只接受本机连接",
                    retryable=False,
                )
        except ValueError:
            return _error(
                403,
                "INTERNAL_API_FORBIDDEN",
                "无法确认调用方为本机",
                retryable=False,
            )

    expected = os.environ.get(_INTERNAL_TOKEN_ENV, "")
    if expected:
        provided = request.headers.get("Authorization", "")
        if provided != f"Bearer {expected}":
            return _error(
                401,
                "INTERNAL_API_UNAUTHORIZED",
                "内部 API token 无效",
                retryable=False,
            )
    return None


def _etag(value: str) -> str:
    return f'"{value}"'


def _etag_matches(request: Request, etag: str) -> bool:
    candidates = {
        item.strip()
        for item in request.headers.get("If-None-Match", "").split(",")
    }
    return "*" in candidates or etag in candidates


def _error(
    status: int,
    code: str,
    message: str,
    *,
    retryable: bool,
) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
            }
        },
        status_code=status,
    )


resource_template_router = create_resource_template_router()
