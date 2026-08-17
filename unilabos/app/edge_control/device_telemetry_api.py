"""设备遥测投影（DeviceTelemetryProjection）的 HTTP 与 SSE 适配器。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any, Protocol

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, StreamingResponse

from unilabos.app.edge_control.device_telemetry import (
    DeviceTelemetryError,
    DeviceTelemetryHub,
    TelemetryCommit,
)


class DeviceTelemetryAuthority(Protocol):
    """设备遥测传输适配器依赖的最小本地权威接口。"""

    telemetry: DeviceTelemetryHub


def create_device_telemetry_router(
    authority: DeviceTelemetryAuthority,
) -> APIRouter:
    """创建正式后端（Backend）形状的 Edge 写入和前端 SSE 路由。

    参数：``authority`` 提供设备遥测深模块。返回：可挂载到本地后端（Local
    Backend）服务的路由。异常：组合输入缺失时由属性访问错误关闭启动。
    """

    router = APIRouter(prefix="/api/v1", tags=["device-telemetry"])

    @router.post("/edge/devices/{material_uuid}/telemetry/properties")
    def commit_properties(
        material_uuid: str,
        payload: Any = Body(default=None),
    ) -> JSONResponse:
        """提交一批通用设备属性完整快照。

        参数：路径物料 UUID 与严格请求体。返回：正式后端 ``code/data`` 形状
        响应，首次接受为 HTTP 201，幂等重复为 200。异常：全部转换为稳定业务
        错误封装，不向 FastAPI ``detail`` 泄漏合同。
        """

        try:
            commit = authority.telemetry.ingest_properties(material_uuid, payload)
        except DeviceTelemetryError as error:
            return _error_response(error)
        return _commit_response(commit)

    @router.post("/edge/devices/{material_uuid}/telemetry/joint-states")
    def commit_joint_states(
        material_uuid: str,
        payload: Any = Body(default=None),
    ) -> JSONResponse:
        """提交一批机械臂关节状态完整快照。

        参数：路径物料 UUID 与严格请求体。返回：正式后端 ``code/data`` 形状
        响应，首次接受为 HTTP 201，幂等重复为 200。异常：全部转换为稳定业务
        错误封装。
        """

        try:
            commit = authority.telemetry.ingest_joint_states(material_uuid, payload)
        except DeviceTelemetryError as error:
            return _error_response(error)
        return _commit_response(commit)

    @router.get("/device-telemetry/events")
    async def stream_device_telemetry(
        request: Request,
        material_uuid: str = "",
        local_device_id: str = "",
        telemetry_type: str = "",
    ) -> Any:
        """建立非持久、先快照后更新的设备遥测 SSE。

        参数：请求用于感知断连，三个查询参数可过滤物料、设备和遥测类型。
        返回：不产生 ``id`` 的 SSE 流；重连总是重新取得 latest 快照。异常：非法
        过滤在建流前返回正式后端业务错误封装。
        """

        try:
            subscription, snapshot = authority.telemetry.subscribe(
                material_uuid=material_uuid,
                local_device_id=local_device_id,
                telemetry_type=telemetry_type,
            )
        except DeviceTelemetryError as error:
            return _error_response(error)

        async def stream():
            """产生当前连接的快照、更新和保活帧。

            参数：无，闭包持有订阅与请求。返回：异步文本迭代器。异常：客户端
            断连时正常结束，并在 ``finally`` 中释放订阅。
            """

            try:
                yield "retry: 1000\nevent: stream.ready\ndata: {\"version\":1}\n\n"
                yield _sse("device.telemetry.snapshot", {"items": snapshot})
                elapsed = 0.0
                while not await request.is_disconnected():
                    authority.telemetry.expire()
                    for event in subscription.drain():
                        yield _sse("device.telemetry.changed", event)
                    await asyncio.sleep(0.05)
                    elapsed += 0.05
                    if elapsed >= 15.0:
                        yield ": heartbeat\n\n"
                        elapsed = 0.0
            finally:
                authority.telemetry.unsubscribe(subscription)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router


def _commit_response(commit: TelemetryCommit) -> JSONResponse:
    """编码成功提交响应。

    参数：``commit`` 是深模块返回的引用。返回：201 或幂等 200 JSON 响应。
    异常：无。
    """

    return JSONResponse(
        status_code=201 if commit.created else 200,
        content={"code": 0, "data": commit.as_dict()},
    )


def _error_response(error: DeviceTelemetryError) -> JSONResponse:
    """编码稳定业务错误。

    参数：带业务码和 HTTP 状态的合同异常。返回：正式后端 ``code/error``
    JSON 响应。异常：无。
    """

    return JSONResponse(
        status_code=error.http_status,
        content={
            "code": error.business_code,
            "error": {"msg": str(error)},
        },
    )


def _sse(event_type: str, data: Mapping[str, Any]) -> str:
    """编码一帧无持久游标的服务器发送事件（SSE）。

    参数：事件类型与 JSON 数据。返回：以空行结束的 SSE 文本。异常：数据不
    可 JSON 编码时原样抛出，由连接关闭避免发送半帧。
    """

    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_type}\ndata: {encoded}\n\n"


__all__ = ["DeviceTelemetryAuthority", "create_device_telemetry_router"]
