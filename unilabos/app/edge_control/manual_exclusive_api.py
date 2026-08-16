"""手动独占（Exclusive）的本地 HTTP 薄适配器。"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from unilabos.app.scheduler.manual_exclusive import (
    ManualExclusiveBusyError,
    ManualExclusiveGate,
    ManualExclusiveSnapshot,
)


def create_manual_exclusive_router(
    gate: ManualExclusiveGate,
    device_exists: Callable[[str], bool],
) -> APIRouter:
    """创建本地设备手动独占（Exclusive）读取、取得与释放路由。

    参数：``gate`` 是调度准入深模块；``device_exists`` 校验当前 Edge 注册设备
    身份。返回：挂载在 ``/api/v1`` 的 FastAPI 路由。异常：组合依赖错误在调用
    阶段原样传播；业务拒绝转换为稳定 ``code/data`` 或 ``code/message`` 响应。
    """

    router = APIRouter(prefix="/api/v1", tags=["manual-exclusive"])

    @router.get("/devices/{local_device_id}/exclusive")
    def read_exclusive(local_device_id: str) -> JSONResponse:
        """读取设备当前手动独占（Exclusive）准入状态。

        参数：路径中的本地设备身份。返回：``idle/busy/exclusive`` exact 快照；
        未注册设备返回 404。异常：非法身份返回 400，其他错误原样传播。
        """

        missing = _require_device(local_device_id, device_exists)
        if missing is not None:
            return missing
        try:
            return _success(gate.snapshot(local_device_id))
        except ValueError as error:
            return _error(400, 1000, str(error))

    @router.put("/devices/{local_device_id}/exclusive")
    def acquire_exclusive(local_device_id: str) -> JSONResponse:
        """在设备空闲时幂等取得手动独占（Exclusive）。

        参数：路径中的本地设备身份。返回：取得后的 exact 状态快照；设备忙碌
        返回 HTTP 409/业务码 7002。异常：非法身份返回 400，其他错误原样传播。
        """

        missing = _require_device(local_device_id, device_exists)
        if missing is not None:
            return missing
        try:
            return _success(gate.acquire(local_device_id))
        except ManualExclusiveBusyError as error:
            return _error(409, 7002, str(error))
        except ValueError as error:
            return _error(400, 1000, str(error))

    @router.delete("/devices/{local_device_id}/exclusive")
    def release_exclusive(local_device_id: str) -> JSONResponse:
        """幂等释放手动独占（Exclusive）并推进等待作业。

        参数：路径中的本地设备身份。返回：释放并重排后的 exact 状态快照；
        未注册设备返回 404。异常：非法身份返回 400，调度错误原样传播。
        """

        missing = _require_device(local_device_id, device_exists)
        if missing is not None:
            return missing
        try:
            return _success(gate.release(local_device_id))
        except ValueError as error:
            return _error(400, 1000, str(error))

    return router


def _require_device(
    local_device_id: str,
    device_exists: Callable[[str], bool],
) -> JSONResponse | None:
    """验证设备属于当前 Edge 注册快照。

    参数：候选本地设备身份与注册查询函数。返回：存在时为 ``None``，否则为
    HTTP 404/业务码 7000 响应。异常：查询错误原样传播，禁止误把未知设备放行。
    """

    if device_exists(local_device_id):
        return None
    return _error(404, 7000, "device binding not found")


def _success(snapshot: ManualExclusiveSnapshot) -> JSONResponse:
    """把准入快照编码为 exact 成功响应。

    参数：不可变设备状态快照。返回：HTTP 200 ``code/data`` 响应。异常：无。
    """

    return JSONResponse(
        status_code=200,
        content={
            "code": 0,
            "data": {
                "local_device_id": snapshot.local_device_id,
                "state": snapshot.state,
                "exclusive": snapshot.exclusive,
            },
        },
    )


def _error(status_code: int, code: int, message: str) -> JSONResponse:
    """编码稳定手动独占（Exclusive）业务错误。

    参数：HTTP 状态、业务码和安全消息。返回：``code/message`` JSON 响应。
    异常：无。
    """

    return JSONResponse(
        status_code=status_code,
        content={"code": code, "message": message},
    )


__all__ = ["create_manual_exclusive_router"]
