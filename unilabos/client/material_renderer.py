"""Attached Material renderer client discovered through the Workspace Host."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

import httpx

from unilabos.workspace_host.client import ensure_workspace_host
from unilabos.workspace_host.model import WorkspaceHostError


MATERIAL_RENDERER_CLIENT_SCHEMA = "unilab-material-renderer-client/v1"
MATERIAL_RENDERER_CONTRACT = "unilab-material-renderer/v1"


class MaterialRendererClientError(RuntimeError):
    """Stable failure returned by attached renderer discovery or capture."""

    def __init__(self, code: str, message: str, *, details: object = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"code": self.code, "message": self.message}
        if self.details is not None:
            result["details"] = self.details
        return result


class MaterialRendererClient:
    """Deep client for the one currently attached Workbench Material renderer."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        workspace_path: str,
        renderer_generation: str,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.workspace_path = workspace_path
        self.renderer_generation = renderer_generation
        self._client = httpx.Client(
            base_url=self.base_url + "/",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def discover(
        cls,
        workspace: str | Path,
        *,
        timeout: float = 30.0,
    ) -> "MaterialRendererClient":
        try:
            host = ensure_workspace_host(workspace)
            snapshot = host.snapshot()
        except WorkspaceHostError as error:
            raise MaterialRendererClientError(
                error.code, str(error), details=error.details
            ) from error
        components = snapshot.get("components")
        renderer = components.get("renderer") if isinstance(components, Mapping) else None
        if not isinstance(renderer, Mapping) or renderer.get("phase") != "ready":
            raise MaterialRendererClientError(
                "material_renderer_not_attached",
                "Workbench renderer 尚未附着；请先打开工作区中的物料画布",
                details={"renderer": renderer},
            )
        capabilities = renderer.get("capabilities")
        required = {"material-scene-inspect", "material-scene-capture"}
        if not isinstance(capabilities, list) or not required.issubset(capabilities):
            raise MaterialRendererClientError(
                "material_renderer_incompatible",
                "当前 Workbench renderer 不支持物料场景检查与截图",
                details={"capabilities": capabilities},
            )
        metadata = renderer.get("metadata")
        base_url = (
            metadata.get("automationBaseUrl")
            if isinstance(metadata, Mapping)
            else None
        )
        contract = (
            metadata.get("automationContract")
            if isinstance(metadata, Mapping)
            else None
        )
        if (
            not isinstance(base_url, str)
            or not _loopback_http_url(base_url)
            or contract != MATERIAL_RENDERER_CONTRACT
        ):
            raise MaterialRendererClientError(
                "material_renderer_incompatible",
                "Renderer 自动化地址或合同无效",
                details={"metadata": metadata},
            )
        return cls(
            base_url,
            host.token,
            workspace_path=str(snapshot.get("workspacePath") or Path(workspace).resolve()),
            renderer_generation=str(renderer.get("generation") or ""),
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "MaterialRendererClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def inspect_scene(
        self,
        *,
        view: str | None = None,
        show_sites: bool | None = None,
        show_material_transfers: bool | None = None,
        selected_material_ids: Sequence[str] = (),
        hidden_material_ids: Sequence[str] = (),
        timeout: float | None = None,
    ) -> dict[str, Any]:
        options = _renderer_options(
            view=view,
            show_sites=show_sites,
            show_material_transfers=show_material_transfers,
            selected_material_ids=selected_material_ids,
            hidden_material_ids=hidden_material_ids,
            timeout=timeout,
        )
        query = {
            key: ",".join(value) if isinstance(value, list) else value
            for key, value in options.items()
        }
        response = self._request("GET", "material/scene", params=query)
        return self._result(response.get("result"))

    def capture_scene(
        self,
        output: str | Path,
        *,
        view: str | None = None,
        show_sites: bool | None = None,
        show_material_transfers: bool | None = None,
        selected_material_ids: Sequence[str] = (),
        hidden_material_ids: Sequence[str] = (),
        camera_preset: str | None = None,
        viewport: tuple[int, int] | None = None,
        pixel_ratio: float = 1.0,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        options = _renderer_options(
            view=view,
            show_sites=show_sites,
            show_material_transfers=show_material_transfers,
            selected_material_ids=selected_material_ids,
            hidden_material_ids=hidden_material_ids,
            timeout=timeout,
        )
        if camera_preset is not None:
            options["cameraPreset"] = camera_preset
        if viewport is not None:
            options["viewport"] = {
                "width": viewport[0],
                "height": viewport[1],
                "pixelRatio": pixel_ratio,
            }
        response = self._request("POST", "material/capture", json=options)
        capture = response.get("result")
        if not isinstance(capture, Mapping):
            raise MaterialRendererClientError(
                "material_capture_invalid", "Renderer 未返回截图结果"
            )
        image = capture.get("image")
        encoded = image.get("base64") if isinstance(image, Mapping) else None
        if not isinstance(encoded, str):
            raise MaterialRendererClientError(
                "material_capture_invalid", "Renderer 未返回 PNG 数据"
            )
        try:
            png = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise MaterialRendererClientError(
                "material_capture_invalid", "Renderer PNG 数据不是合法 Base64"
            ) from error
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise MaterialRendererClientError(
                "material_capture_invalid", "Renderer 截图不是 PNG"
            )
        output_path = Path(output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_name(f"{output_path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(png)
        os.replace(temporary, output_path)
        sanitized_image = {
            key: value for key, value in image.items() if key != "base64"
        }
        sanitized_image.update(
            {
                "path": str(output_path),
                "bytes": len(png),
                "sha256": hashlib.sha256(png).hexdigest(),
            }
        )
        return self._result(
            {
                **dict(capture),
                "image": sanitized_image,
            }
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise MaterialRendererClientError(
                "material_renderer_unreachable",
                f"Workbench renderer 请求失败：{error}",
            ) from error
        if not isinstance(payload, Mapping):
            raise MaterialRendererClientError(
                "material_renderer_protocol_invalid", "Renderer 响应不是 JSON object"
            )
        if response.is_error or payload.get("ok") is not True:
            failure = payload.get("error")
            failure = failure if isinstance(failure, Mapping) else {}
            raise MaterialRendererClientError(
                str(failure.get("code") or "material_renderer_failed"),
                str(failure.get("message") or f"Renderer HTTP {response.status_code}"),
                details=failure.get("details"),
            )
        if payload.get("schemaVersion") != MATERIAL_RENDERER_CONTRACT:
            raise MaterialRendererClientError(
                "material_renderer_protocol_invalid", "Renderer 合同版本不兼容"
            )
        return dict(payload)

    def _result(self, data: object) -> dict[str, Any]:
        return {
            "schemaVersion": MATERIAL_RENDERER_CLIENT_SCHEMA,
            "rendererIdentity": {
                "workspacePath": self.workspace_path,
                "generation": self.renderer_generation,
                "automationBaseUrl": self.base_url,
            },
            "data": data,
        }


def _renderer_options(
    *,
    view: str | None,
    show_sites: bool | None,
    show_material_transfers: bool | None,
    selected_material_ids: Sequence[str],
    hidden_material_ids: Sequence[str],
    timeout: float | None,
) -> dict[str, object]:
    result: dict[str, object] = {}
    if view is not None:
        result["view"] = view
    if show_sites is not None:
        result["showSites"] = show_sites
    if show_material_transfers is not None:
        result["showMaterialTransfers"] = show_material_transfers
    if selected_material_ids:
        result["selectedMaterialIds"] = list(selected_material_ids)
    if hidden_material_ids:
        result["hiddenMaterialIds"] = list(hidden_material_ids)
    if timeout is not None:
        result["timeoutMs"] = round(timeout * 1000)
    return result


def _loopback_http_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}


__all__ = [
    "MATERIAL_RENDERER_CLIENT_SCHEMA",
    "MATERIAL_RENDERER_CONTRACT",
    "MaterialRendererClient",
    "MaterialRendererClientError",
]
