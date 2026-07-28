"""Registry 模板目录的 local_bridge HTTP 代理与内存缓存。"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote

import httpx


class ResourceTemplateProxyError(RuntimeError):
    """模板代理请求失败。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 503,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class TemplateProxyResult:
    data: dict[str, Any]
    etag: str
    stale: bool


@dataclass(frozen=True)
class TemplateAssetResult:
    status: int
    content: bytes
    headers: dict[str, str]


@dataclass
class _CacheEntry:
    data: dict[str, Any]
    etag: str
    fetched_at: float


class ResourceTemplateProxy:
    """请求 OS internal HTTP，并按上游地址隔离只读内存缓存。"""

    def __init__(
        self,
        execution_http_url: str,
        *,
        internal_token: str | None = None,
        ttl_seconds: float = 5.0,
        timeout_seconds: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not execution_http_url.strip():
            raise ValueError("execution_http_url 不能为空")
        self.execution_http_url = execution_http_url.rstrip("/")
        self.internal_token = internal_token
        self.ttl_seconds = max(0.0, ttl_seconds)
        self.timeout_seconds = timeout_seconds
        self._transport = transport
        self._catalog: _CacheEntry | None = None
        self._details: dict[str, _CacheEntry] = {}

    async def list_templates(
        self,
        *,
        force: bool = False,
    ) -> TemplateProxyResult:
        cached = self._catalog
        if not force and self._is_fresh(cached):
            return _fresh_result(cached)
        try:
            response = await self._request(
                "/internal/v1/resource-templates",
                etag=cached.etag if cached is not None else None,
            )
            entry = self._consume_json_response(response, cached)
            self._catalog = entry
            return _fresh_result(entry)
        except ResourceTemplateProxyError as exc:
            if cached is None or not exc.retryable:
                raise
            return _stale_result(cached)

    async def get_template(
        self,
        template_uuid: str,
        *,
        force: bool = False,
    ) -> TemplateProxyResult:
        cached = self._details.get(template_uuid)
        if not force and self._is_fresh(cached):
            return _fresh_result(cached)
        safe_uuid = quote(template_uuid, safe="")
        try:
            response = await self._request(
                f"/internal/v1/resource-templates/{safe_uuid}",
                etag=cached.etag if cached is not None else None,
            )
            entry = self._consume_json_response(response, cached)
            self._details[template_uuid] = entry
            return _fresh_result(entry)
        except ResourceTemplateProxyError as exc:
            if cached is None or not exc.retryable:
                raise
            return _stale_result(cached)

    async def get_asset(
        self,
        template_uuid: str,
        asset_key: str,
        *,
        range_header: str | None = None,
    ) -> TemplateAssetResult:
        headers = self._headers()
        if range_header:
            headers["Range"] = range_header
        path = (
            "/internal/v1/resource-templates/"
            f"{quote(template_uuid, safe='')}/assets/{quote(asset_key, safe='')}"
        )
        try:
            async with httpx.AsyncClient(
                base_url=self.execution_http_url,
                timeout=self.timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(path, headers=headers)
        except httpx.HTTPError as exc:
            raise ResourceTemplateProxyError(
                "CATALOG_UNAVAILABLE",
                f"无法连接 OS 模板资源接口: {exc}",
            ) from exc
        if response.status_code >= 400:
            raise _upstream_error(response)
        forwarded = {
            key: value
            for key, value in response.headers.items()
            if key.lower()
            in {
                "accept-ranges",
                "cache-control",
                "content-length",
                "content-range",
                "content-type",
                "etag",
                "last-modified",
            }
        }
        return TemplateAssetResult(
            status=response.status_code,
            content=response.content,
            headers=forwarded,
        )

    async def _request(
        self,
        path: str,
        *,
        etag: str | None,
    ) -> httpx.Response:
        headers = self._headers()
        if etag:
            headers["If-None-Match"] = etag
        try:
            async with httpx.AsyncClient(
                base_url=self.execution_http_url,
                timeout=self.timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(path, headers=headers)
        except httpx.HTTPError as exc:
            raise ResourceTemplateProxyError(
                "CATALOG_UNAVAILABLE",
                f"无法连接 OS Registry 接口: {exc}",
            ) from exc
        if response.status_code == 304:
            return response
        if response.status_code >= 400:
            raise _upstream_error(response)
        return response

    def _consume_json_response(
        self,
        response: httpx.Response,
        cached: _CacheEntry | None,
    ) -> _CacheEntry:
        now = time.monotonic()
        if response.status_code == 304:
            if cached is None:
                raise ResourceTemplateProxyError(
                    "INVALID_CATALOG_RESPONSE",
                    "OS 返回 304，但本地没有对应缓存",
                    retryable=False,
                )
            cached.fetched_at = now
            return cached
        try:
            payload = response.json()
        except ValueError as exc:
            raise ResourceTemplateProxyError(
                "INVALID_CATALOG_RESPONSE",
                "OS 模板接口没有返回 JSON",
                retryable=False,
            ) from exc
        if not isinstance(payload, Mapping):
            raise ResourceTemplateProxyError(
                "INVALID_CATALOG_RESPONSE",
                "OS 模板接口响应不是对象",
                retryable=False,
            )
        data = copy.deepcopy(dict(payload))
        data["stale"] = False
        etag = response.headers.get("ETag")
        if not etag:
            revision = data.get("revision") or data.get("content_hash")
            if not isinstance(revision, str) or not revision:
                raise ResourceTemplateProxyError(
                    "INVALID_CATALOG_RESPONSE",
                    "OS 模板接口缺少 ETag/revision",
                    retryable=False,
                )
            etag = f'"{revision}"'
        return _CacheEntry(data=data, etag=etag, fetched_at=now)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.internal_token:
            headers["Authorization"] = f"Bearer {self.internal_token}"
        return headers

    def _is_fresh(self, entry: _CacheEntry | None) -> bool:
        return (
            entry is not None
            and time.monotonic() - entry.fetched_at < self.ttl_seconds
        )


def _fresh_result(entry: _CacheEntry) -> TemplateProxyResult:
    return TemplateProxyResult(
        data=copy.deepcopy(entry.data),
        etag=entry.etag,
        stale=False,
    )


def _stale_result(entry: _CacheEntry) -> TemplateProxyResult:
    data = copy.deepcopy(entry.data)
    data["stale"] = True
    _disable_creation(data)
    return TemplateProxyResult(data=data, etag=entry.etag, stale=True)


def _disable_creation(value: Any) -> None:
    if isinstance(value, Mapping):
        creation = value.get("creation")
        if isinstance(creation, dict):
            creation["available"] = False
            creation["reason"] = "当前展示缓存目录，重新连接 OS 后方可创建"
        for item in value.values():
            _disable_creation(item)
    elif isinstance(value, list):
        for item in value:
            _disable_creation(item)


def _upstream_error(response: httpx.Response) -> ResourceTemplateProxyError:
    code = "CATALOG_UNAVAILABLE"
    message = f"OS 模板接口返回 {response.status_code}"
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
    return ResourceTemplateProxyError(
        code,
        message,
        status=response.status_code,
        retryable=retryable,
    )
