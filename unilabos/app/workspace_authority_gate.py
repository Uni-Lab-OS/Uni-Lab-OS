"""Runtime gate for the stable Workspace Backend Local Domain surface."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response

_CONFIGURATION_ENV = "UNILABOS_WORKSPACE_AUTHORITY_CONFIG"
_ALWAYS_AVAILABLE_PATHS = frozenset({"/api/v1/health"})
_WORKSPACE_AUTHORING_PREFIX = "/api/v1/workspace/"


def _selected_authority(configuration_path: Path) -> str:
    """Read the atomically replaced Authority selection; invalid state closes."""

    try:
        payload = json.loads(configuration_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # A first-run Workspace defaults to Local in Workspace Host too.
        return "local"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "invalid"
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
        return "invalid"
    mode = payload.get("domainMode", "local")
    return mode if mode in {"local", "backend"} else "invalid"


def install_workspace_authority_gate(
    app: FastAPI,
    configuration_path: str | os.PathLike[str] | None = None,
) -> None:
    """Install one live Authority gate without rebuilding the FastAPI app.

    Workspace Authoring and health stay local in every mode.  All other
    ``/api/v1`` Domain interfaces are exposed only when Local is selected;
    Workbench routes those same public interfaces to the external Backend when
    Backend Authority is selected.
    """

    if getattr(app.state, "workspace_authority_gate_installed", False):
        return
    configured = configuration_path or os.environ.get(_CONFIGURATION_ENV)
    if configured is None:
        return
    path = Path(configured).expanduser().resolve()

    @app.middleware("http")
    async def workspace_authority_gate(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_path = request.url.path
        if (
            request_path.startswith("/api/v1/")
            and request_path not in _ALWAYS_AVAILABLE_PATHS
            and not request_path.startswith(_WORKSPACE_AUTHORING_PREFIX)
        ):
            authority = _selected_authority(path)
            if authority != "local":
                message = (
                    "Workspace Local Domain 当前不是选中的 Authority"
                    if authority == "backend"
                    else "Workspace Authority 配置无效，Local Domain 已关闭"
                )
                return JSONResponse(
                    status_code=409 if authority == "backend" else 503,
                    content={
                        "code": 3003 if authority == "backend" else 5001,
                        "error": {
                            "code": "authority_inactive",
                            "msg": message,
                        },
                    },
                )
        return await call_next(request)

    app.state.workspace_authority_gate_installed = True
    app.state.workspace_authority_configuration_path = str(path)


__all__ = ["install_workspace_authority_gate"]
