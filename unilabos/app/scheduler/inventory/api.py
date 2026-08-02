"""Inventory 私有 adapter 与 Backend Material 公开读模型。"""

from __future__ import annotations

from tempfile import TemporaryDirectory
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response

from unilabos.app.scheduler.inventory.commands import execute_command
from unilabos.app.scheduler.inventory.domain import InventoryError, MaterialNotFound
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.sync import build_snapshot


def create_router(service: InventoryService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/inventory", tags=["inventory"])

    @router.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "edge_id": service.edge_id, "lab_id": service.lab_id}

    @router.post("/commands")
    def post_command(command: dict[str, Any]) -> dict[str, Any]:
        return execute_command(service, command)

    @router.get("/materials")
    def list_materials(limit: int = 500) -> dict[str, Any]:
        snapshot = build_snapshot(service)
        return {"materials": snapshot["materials"][:limit]}

    @router.get("/materials/{material_uuid}")
    def get_material(material_uuid: str) -> dict[str, Any]:
        return service.get_material(material_uuid).to_dict()

    @router.get("/sites")
    def list_sites(limit: int = 500) -> dict[str, Any]:
        snapshot = build_snapshot(service)
        return {"sites": snapshot["sites"][:limit]}

    @router.get("/sites/{site_uuid}")
    def get_site(site_uuid: str) -> dict[str, Any]:
        return service.get_site(site_uuid).to_dict()

    @router.get("/lots")
    def list_lots(limit: int = 500) -> dict[str, Any]:
        snapshot = build_snapshot(service)
        return {"lots": snapshot["inventory_lots"][:limit]}

    @router.get("/lots/{lot_id}")
    def get_lot(lot_id: str) -> dict[str, Any]:
        rows = build_snapshot(service)["inventory_lots"]
        for row in rows:
            if row["lot_id"] == lot_id:
                return row
        return JSONResponse(
            status_code=404,
            content={"code": "not_found", "error": f"lot {lot_id} not found"},
        )

    @router.get("/reservations")
    def list_reservations(limit: int = 500) -> dict[str, Any]:
        snapshot = build_snapshot(service)
        return {
            "reservations": snapshot["material_reservations"][:limit],
        }

    @router.get("/snapshot")
    def snapshot() -> dict[str, Any]:
        return build_snapshot(service)

    @router.get("/ledger")
    def ledger(limit: int = 200, after_id: int = 0) -> dict[str, Any]:
        return {
            "entries": list(service.read_ledger(after_id=after_id, limit=limit)),
        }

    @router.get("/outbox/backlog")
    def outbox_backlog() -> dict[str, int]:
        return service.outbox_status()

    return router


def create_backend_material_router(service: InventoryService) -> APIRouter:
    """冻结 Backend Material read Interface；Inventory 私有路由保持隔离。"""

    router = APIRouter(prefix="/api/v1", tags=["materials"])

    @router.get("/materials")
    def list_materials(
        page: int = 1,
        page_size: int = 20,
        name: str = "",
        barcode: str = "",
        resource_template_uuid: str = "",
    ) -> dict[str, Any]:
        return {
            "code": 0,
            "data": service.list_backend_materials(
                page=page,
                page_size=page_size,
                name=name,
                barcode=barcode,
                resource_template_uuid=resource_template_uuid,
            ),
        }

    @router.get("/materials/graph")
    def material_graph() -> dict[str, Any]:
        return {"code": 0, "data": service.backend_material_graph()}

    @router.get("/materials/{material_uuid}")
    def material_detail(material_uuid: str) -> dict[str, Any]:
        return {"code": 0, "data": service.backend_material_detail(material_uuid)}

    @router.get("/material-shapes")
    def material_shapes() -> dict[str, Any]:
        return {"code": 0, "data": {"items": service.list_material_shapes()}}

    @router.get("/material-models/{asset_path:path}")
    def material_model_asset(asset_path: str) -> Response:
        public_path = f"/api/v1/material-models/{asset_path}"
        try:
            asset, content = service.read_material_model_asset(public_path)
        except MaterialNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(
            content=content,
            media_type=asset.media_type,
            headers={
                "Cache-Control": "private, max-age=0, must-revalidate",
                "ETag": f'"{asset.digest}"',
            },
        )

    return router


def create_app(service: InventoryService | None = None) -> FastAPI:
    """Create a standalone adapter for tests; production injects its service."""

    temporary_directory: TemporaryDirectory[str] | None = None
    if service is None:
        temporary_directory = TemporaryDirectory(prefix="unilabos-inventory-")
        service = InventoryService.open(
            working_dir=temporary_directory.name,
            resource_templates={},
        )
    app = FastAPI(title="Uni-Lab Edge Inventory", version="0.2.0")
    app.include_router(create_router(service))
    app.include_router(create_backend_material_router(service))
    app.state.inventory_temporary_directory = temporary_directory

    @app.exception_handler(InventoryError)
    def _domain_error(_request: Any, exc: InventoryError) -> JSONResponse:
        status_code = {
            "invalid_input": 400,
            "not_found": 404,
            "conflict": 409,
            "material_authority_unavailable": 503,
        }.get(exc.code, 409)
        return JSONResponse(
            status_code=status_code,
            content={"error": str(exc), "code": exc.code},
        )

    return app
