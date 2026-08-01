"""local_bridge 对 OS 内部设备控制命令的窄代理。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx


class DeviceControlProxyError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable


class DeviceControlProxy:
    """只代理已冻结的 Action 手动解锁命令，不接管锁状态。"""

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

    async def force_unlock_action(
        self,
        device_id: str,
        action_name: str,
        *,
        expected_job_id: str,
        reason: str,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if self.internal_token:
            headers["Authorization"] = f"Bearer {self.internal_token}"
        path = (
            "/internal/v1/device-actions/"
            f"{quote(device_id, safe='')}/{quote(action_name, safe='')}/commands"
        )
        try:
            async with httpx.AsyncClient(
                base_url=self.execution_http_url,
                timeout=self.timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    path,
                    headers=headers,
                    json={
                        "command": "force_unlock",
                        "expectedJobId": expected_job_id,
                        "reason": reason,
                    },
                )
        except httpx.HTTPError as exc:
            raise DeviceControlProxyError(
                "DEVICE_CONTROL_UNAVAILABLE",
                f"无法连接 OS 设备控制接口: {exc}",
                status=503,
                retryable=True,
            ) from exc
        if response.status_code >= 400:
            raise _proxy_error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise DeviceControlProxyError(
                "INVALID_DEVICE_CONTROL_RESPONSE",
                "OS 设备控制接口没有返回 JSON",
                status=502,
                retryable=False,
            ) from exc
        if not isinstance(payload, Mapping) or payload.get("status") not in {
            "released",
            "already_unlocked",
        }:
            raise DeviceControlProxyError(
                "INVALID_DEVICE_CONTROL_RESPONSE",
                "OS 设备控制接口返回了无效状态",
                status=502,
                retryable=False,
            )
        return dict(payload)


def _proxy_error(response: httpx.Response) -> DeviceControlProxyError:
    code = "DEVICE_CONTROL_UNAVAILABLE"
    message = f"OS 设备控制接口返回 {response.status_code}"
    retryable = response.status_code >= 500
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            code = str(error.get("code") or code)
            message = str(error.get("message") or message)
            if isinstance(error.get("retryable"), bool):
                retryable = bool(error["retryable"])
    return DeviceControlProxyError(
        code,
        message,
        status=response.status_code,
        retryable=retryable,
    )
