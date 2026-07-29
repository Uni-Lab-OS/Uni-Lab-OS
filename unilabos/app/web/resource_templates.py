"""OS 内部 Registry 模板目录 HTTP 路由。

该路由只允许本机调用，供 local_bridge 在 ``:8014`` 投影统一前端契约。
浏览器不应直接连接本接口。
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from unilabos.app.web.internal_api import (
    authorize_internal,
    etag,
    etag_matches,
    internal_error,
)
from unilabos.registry.registry import lab_registry
from unilabos.registry.template_catalog import (
    ResourceTemplateCatalog,
    TemplateAssetError,
    TemplateCatalogNotReady,
    TemplateNotFound,
)

_catalog = ResourceTemplateCatalog(lab_registry)


def create_resource_template_router(
    get_catalog: Callable[[], ResourceTemplateCatalog] = lambda: _catalog,
) -> APIRouter:
    router = APIRouter()

    @router.get("/resource-templates")
    async def list_resource_templates(request: Request) -> Response:
        denied = authorize_internal(request, capability="Registry 模板接口")
        if denied is not None:
            return denied
        try:
            result = get_catalog().list_templates()
        except TemplateCatalogNotReady as exc:
            return internal_error(
                503,
                "CATALOG_UNAVAILABLE",
                str(exc),
                retryable=True,
            )
        response_etag = etag(result["revision"])
        if etag_matches(request, response_etag):
            return Response(status_code=304, headers={"ETag": response_etag})
        return JSONResponse(result, headers={"ETag": response_etag})

    @router.get("/resource-templates/{template_uuid}")
    async def get_resource_template(
        template_uuid: str,
        request: Request,
    ) -> Response:
        denied = authorize_internal(request, capability="Registry 模板接口")
        if denied is not None:
            return denied
        try:
            detail = get_catalog().get_template(template_uuid)
        except TemplateCatalogNotReady as exc:
            return internal_error(
                503,
                "CATALOG_UNAVAILABLE",
                str(exc),
                retryable=True,
            )
        except TemplateNotFound:
            return internal_error(
                404,
                "TEMPLATE_NOT_FOUND",
                template_uuid,
                retryable=False,
            )
        response_etag = etag(detail["content_hash"])
        if etag_matches(request, response_etag):
            return Response(status_code=304, headers={"ETag": response_etag})
        return JSONResponse(detail, headers={"ETag": response_etag})

    @router.get(
        "/resource-templates/{template_uuid}/assets/{asset_key}",
    )
    async def get_resource_template_asset(
        template_uuid: str,
        asset_key: str,
        request: Request,
    ) -> Response:
        denied = authorize_internal(request, capability="Registry 模板接口")
        if denied is not None:
            return denied
        try:
            path = get_catalog().resolve_asset(template_uuid, asset_key)
        except TemplateNotFound:
            return internal_error(
                404,
                "TEMPLATE_NOT_FOUND",
                template_uuid,
                retryable=False,
            )
        except TemplateAssetError as exc:
            return internal_error(
                404,
                "TEMPLATE_ASSET_NOT_FOUND",
                str(exc),
                retryable=False,
            )
        return FileResponse(path)

    return router


resource_template_router = create_resource_template_router()
