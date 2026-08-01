"""Loopback-only device Action lock reconciliation commands."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from unilabos.app.communication import get_communication_client
from unilabos.app.web.internal_api import authorize_internal, internal_error


class DeviceActionCommand(BaseModel):
    command: Literal["force_unlock"]
    expected_job_id: str = Field(alias="expectedJobId", min_length=1)
    reason: Literal["operator_confirmed_device_safe"]


def force_unlock_current_action(
    device_id: str,
    action_name: str,
    *,
    expected_job_id: str,
    reason: str,
) -> Mapping[str, Any]:
    client = get_communication_client()
    force_unlock = getattr(client, "force_unlock_action", None)
    if not callable(force_unlock):
        raise RuntimeError("当前通信客户端不支持设备 Action 手动解锁")
    return force_unlock(
        device_id,
        action_name,
        expected_job_id=expected_job_id,
        reason=reason,
    )


def create_device_control_router(
    force_unlock: Callable[..., Mapping[str, Any]] = force_unlock_current_action,
) -> APIRouter:
    router = APIRouter()

    @router.post("/device-actions/{device_id}/{action_name}/commands")
    async def command_device_action(
        device_id: str,
        action_name: str,
        command: DeviceActionCommand,
        request: Request,
    ) -> Response:
        denied = authorize_internal(request, capability="设备 Action 手动解锁接口")
        if denied is not None:
            return denied
        try:
            result = dict(
                force_unlock(
                    device_id,
                    action_name,
                    expected_job_id=command.expected_job_id,
                    reason=command.reason,
                )
            )
        except RuntimeError as exc:
            return internal_error(
                503,
                "DEVICE_CONTROL_UNAVAILABLE",
                str(exc),
                retryable=True,
            )

        if result.get("status") == "lock_changed":
            return internal_error(
                409,
                "DEVICE_LOCK_CHANGED",
                "设备 Action 锁持有者已变化，请刷新后重新确认",
                retryable=False,
            )
        return JSONResponse(result)

    return router


device_control_router = create_device_control_router()

