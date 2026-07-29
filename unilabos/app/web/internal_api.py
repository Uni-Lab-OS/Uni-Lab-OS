"""OS 内部 HTTP 路由共享的鉴权与缓存辅助函数。"""

from __future__ import annotations

import ipaddress
import os

from fastapi import Request
from fastapi.responses import JSONResponse

_INTERNAL_TOKEN_ENV = "UNILABOS_INTERNAL_API_TOKEN"


def authorize_internal(request: Request, *, capability: str) -> JSONResponse | None:
    """只允许本机及可选 Bearer token 访问 OS 内部能力。"""

    client_host = request.client.host if request.client is not None else ""
    if client_host != "testclient":
        try:
            if not ipaddress.ip_address(client_host).is_loopback:
                return internal_error(
                    403,
                    "INTERNAL_API_FORBIDDEN",
                    f"{capability}只接受本机连接",
                    retryable=False,
                )
        except ValueError:
            return internal_error(
                403,
                "INTERNAL_API_FORBIDDEN",
                "无法确认调用方为本机",
                retryable=False,
            )

    expected = os.environ.get(_INTERNAL_TOKEN_ENV, "")
    if expected:
        provided = request.headers.get("Authorization", "")
        if provided != f"Bearer {expected}":
            return internal_error(
                401,
                "INTERNAL_API_UNAUTHORIZED",
                "内部 API token 无效",
                retryable=False,
            )
    return None


def etag(value: str) -> str:
    return f'"{value}"'


def etag_matches(request: Request, expected: str) -> bool:
    candidates = {
        item.strip() for item in request.headers.get("If-None-Match", "").split(",")
    }
    return "*" in candidates or expected in candidates


def internal_error(
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
