"""本地前端读取冻结运动渲染模型的只读 HTTP 接口。"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse, Response

from unilabos.device_mesh.package_moveit_model import (
    get_package_render_mesh,
    get_package_render_model,
)


def create_kinematic_model_router() -> APIRouter:
    """创建实例化 URDF 与 exact mesh 的只读路由。"""

    router = APIRouter(prefix="/api/v1/kinematic-models")

    @router.get("/{device_id}.urdf", include_in_schema=False)
    def read_render_model(device_id: str) -> Response:
        model = get_package_render_model(device_id)
        if model is None:
            return Response(status_code=404)
        return Response(
            content=model.render_urdf,
            media_type="application/xml",
            headers=_headers(model.device_id, model.topology_digest),
        )

    @router.get("/{device_id}/meshes/{asset_name}", include_in_schema=False)
    def read_mesh(device_id: str, asset_name: str) -> Response:
        model = get_package_render_model(device_id)
        asset = get_package_render_mesh(device_id, asset_name)
        if model is None or asset is None:
            return Response(status_code=404)
        return FileResponse(
            asset,
            media_type="model/stl" if asset.suffix.lower() == ".stl" else None,
            headers=_headers(model.device_id, model.topology_digest),
        )

    return router


def _headers(device_id: str, topology_digest: str) -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "X-UniLab-Device-Id": device_id,
        "X-UniLab-Topology-Digest": topology_digest,
    }


__all__ = ["create_kinematic_model_router"]
