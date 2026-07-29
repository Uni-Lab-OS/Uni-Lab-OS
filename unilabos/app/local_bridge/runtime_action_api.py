"""OS Runtime Action Catalog 的 local_bridge 内存代理。"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

import httpx


class RuntimeActionCatalogProxyError(RuntimeError):
    """真实 OS 动作目录无法安全投影。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class RuntimeActionCatalogProxy:
    """从显式 OS internal HTTP 地址读取当前设备实例动作合同。"""

    def __init__(
        self,
        execution_http_url: str,
        *,
        internal_token: str | None = None,
        timeout_seconds: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not execution_http_url.strip():
            raise ValueError("execution_http_url 不能为空")
        self.execution_http_url = execution_http_url.rstrip("/")
        self.internal_token = internal_token
        self.timeout_seconds = timeout_seconds
        self._transport = transport
        self._etag: str | None = None
        self._revision: str | None = None
        self._actions: dict[str, dict[str, Any]] | None = None

    async def fetch(
        self,
        *,
        force: bool = False,
    ) -> tuple[dict[str, dict[str, Any]], str]:
        if self._actions is not None and not force:
            return copy.deepcopy(self._actions), str(self._revision)
        headers = {"Accept": "application/json"}
        if self.internal_token:
            headers["Authorization"] = f"Bearer {self.internal_token}"
        if self._etag:
            headers["If-None-Match"] = self._etag
        try:
            async with httpx.AsyncClient(
                base_url=self.execution_http_url,
                timeout=self.timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(
                    "/internal/v1/runtime-actions",
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise RuntimeActionCatalogProxyError(
                "ACTION_CATALOG_UNAVAILABLE",
                f"无法连接 OS Runtime 动作目录: {exc}",
            ) from exc

        if response.status_code == 304:
            if self._actions is None or self._revision is None:
                raise RuntimeActionCatalogProxyError(
                    "INVALID_ACTION_CATALOG_RESPONSE",
                    "OS 返回 304，但桥没有动作目录缓存",
                    retryable=False,
                )
            return copy.deepcopy(self._actions), self._revision
        if response.status_code >= 400:
            raise _upstream_error(response)
        actions, revision = _parse_catalog(response)
        self._actions = actions
        self._revision = revision
        self._etag = response.headers.get("ETag") or f'"{revision}"'
        return copy.deepcopy(actions), revision


def _parse_catalog(
    response: httpx.Response,
) -> tuple[dict[str, dict[str, Any]], str]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeActionCatalogProxyError(
            "INVALID_ACTION_CATALOG_RESPONSE",
            "OS Runtime 动作目录没有返回 JSON",
            retryable=False,
        ) from exc
    if not isinstance(payload, Mapping):
        raise RuntimeActionCatalogProxyError(
            "INVALID_ACTION_CATALOG_RESPONSE",
            "OS Runtime 动作目录响应不是对象",
            retryable=False,
        )
    if payload.get("schema_version") != "runtime-actions/v1":
        raise RuntimeActionCatalogProxyError(
            "INVALID_ACTION_CATALOG_RESPONSE",
            "OS Runtime 动作目录 schema_version 不受支持",
            retryable=False,
        )
    revision = payload.get("revision")
    raw_actions = payload.get("actions")
    if not isinstance(revision, str) or not revision:
        raise RuntimeActionCatalogProxyError(
            "INVALID_ACTION_CATALOG_RESPONSE",
            "OS Runtime 动作目录缺少 revision",
            retryable=False,
        )
    if not isinstance(raw_actions, Mapping):
        raise RuntimeActionCatalogProxyError(
            "INVALID_ACTION_CATALOG_RESPONSE",
            "OS Runtime 动作目录缺少 actions",
            retryable=False,
        )
    actions: dict[str, dict[str, Any]] = {}
    for action_ref, definition in raw_actions.items():
        if (
            not isinstance(action_ref, str)
            or "." not in action_ref
            or not isinstance(definition, Mapping)
        ):
            raise RuntimeActionCatalogProxyError(
                "INVALID_ACTION_CATALOG_RESPONSE",
                f"OS Runtime 动作合同无效: {action_ref!r}",
                retryable=False,
            )
        inputs = definition.get("inputs")
        outputs = definition.get("outputs")
        if not isinstance(inputs, Mapping) or not isinstance(outputs, Mapping):
            raise RuntimeActionCatalogProxyError(
                "INVALID_ACTION_CATALOG_RESPONSE",
                f"OS Runtime 动作合同缺少输入/输出 schema: {action_ref}",
                retryable=False,
            )
        actions[action_ref] = copy.deepcopy(dict(definition))
    return actions, revision


def _upstream_error(response: httpx.Response) -> RuntimeActionCatalogProxyError:
    code = "ACTION_CATALOG_UNAVAILABLE"
    message = f"OS Runtime 动作目录返回 {response.status_code}"
    retryable = response.status_code >= 500
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            if isinstance(error.get("code"), str):
                code = error["code"]
            if isinstance(error.get("message"), str):
                message = error["message"]
            if isinstance(error.get("retryable"), bool):
                retryable = error["retryable"]
    return RuntimeActionCatalogProxyError(
        code,
        message,
        retryable=retryable,
    )
