"""Thin FastAPI adapter for the Backend-shaped local Workflow authority."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, FastAPI, Header, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator

from unilabos.workflow.json_codec import decode_json_bytes, encode_json
from unilabos.workflow.models import (
    WorkflowEdgeWrite,
    WorkflowNodeWrite,
    normalize_json_array,
    normalize_json_object,
)
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.app.workflow_template_api import (
    TemplateSnapshotProvider,
    WorkflowTemplateQueryService,
    create_workflow_template_router,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _BackendModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


HashToken = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
_SIGNED_DECIMAL = re.compile(r"[+-]?[0-9]+\Z")
_INT64_MAX = (1 << 63) - 1
_WORKFLOW_BODY_LIMIT = 8 * 1024 * 1024
_WORKFLOW_JSON_INTEGER_DIGITS = 4096
_GO_WHITE_SPACE = (
    "\t\n\v\f\r "
    "\u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)


async def _read_limited_body(request: Request) -> bytes:
    """增量读取工作流（Workflow）请求体并在首次超限时停止。

    参数说明：`request` 是当前 ASGI 请求。函数先校验声明长度，再逐块读取，
    最多保留 8 MiB；返回缓存后的原始字节，超限或非法长度抛出 `ValueError`。
    """

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length, 10)
        except ValueError:
            raise ValueError("Content-Length 无效") from None
        if declared_length < 0 or declared_length > _WORKFLOW_BODY_LIMIT:
            raise ValueError("工作流请求体超过公共预算")

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _WORKFLOW_BODY_LIMIT:
            raise ValueError("工作流请求体超过公共预算")
        body.extend(chunk)
    payload = bytes(body)
    request._body = payload
    return payload


class _BackendJSONRoute(APIRoute):
    """限制有请求体的路由，并按后端（Backend）规则预载 JSON。"""

    def get_route_handler(self):
        """构造只对有请求体路由执行预算与 JSON 解码的处理器。"""

        route_handler = super().get_route_handler()
        expects_body = self.body_field is not None

        async def backend_json_route_handler(request: Request) -> Response:
            """在业务处理前校验请求体；`request` 是单次 HTTP 请求。"""

            if expects_body:
                content_type = request.headers.get("content-type", "")
                mime = content_type.split(";", 1)[0].strip().lower()
                try:
                    body = await _read_limited_body(request)
                    if mime == "application/json" or mime.endswith("+json"):
                        request._json = decode_json_bytes(
                            body,
                            max_integer_digits=_WORKFLOW_JSON_INTEGER_DIGITS,
                        )
                except (
                    OverflowError,
                    UnicodeError,
                    ValueError,
                ):
                    return _error(WorkflowError("invalid_input"))
            return await route_handler(request)

        return backend_json_route_handler


def _parse_non_negative_int64_decimal(value: str) -> int:
    """Match Go strconv.ParseInt(value, 10, 64) for an SSE cursor."""

    if _SIGNED_DECIMAL.fullmatch(value) is None:
        raise ValueError
    negative = value.startswith("-")
    digits = value[1:] if value[:1] in {"+", "-"} else value
    significant = digits.lstrip("0") or "0"
    if negative and significant != "0":
        raise ValueError
    maximum = str(_INT64_MAX)
    if len(significant) > len(maximum) or (
        len(significant) == len(maximum) and significant > maximum
    ):
        raise ValueError
    return int(significant, 10)


class WorkflowCreateRequest(_BackendModel):
    name: str
    tags: List[Any] = Field(default_factory=list)
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("tags", mode="before")
    @classmethod
    def _json_array(cls, value: Any) -> List[Any]:
        return normalize_json_array(value)

    @field_validator("meta_data", mode="before")
    @classmethod
    def _json_object(cls, value: Any) -> Dict[str, Any]:
        return normalize_json_object(value)


class WorkflowUpdateRequest(WorkflowCreateRequest):
    pass


class GraphWriteRequest(_BackendModel):
    revision: int = Field(ge=1, le=_INT64_MAX, strict=True)
    nodes: List[WorkflowNodeWrite] = Field(default_factory=list)
    edges: List[WorkflowEdgeWrite] = Field(default_factory=list)

    @field_validator("nodes", "edges", mode="before")
    @classmethod
    def _json_array(cls, value: Any) -> List[Any]:
        return [] if value is None else value


class WorkflowTaskCreateRequest(_BackendModel):
    workflow_uuid: str
    run_mode: str = "normal"
    target_node_uuid: Optional[str] = None
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("meta_data", mode="before")
    @classmethod
    def _json_object(cls, value: Any) -> Dict[str, Any]:
        return normalize_json_object(value)


class DraftWriteRequest(_StrictModel):
    python_source: str
    expected_draft_hash: Optional[HashToken]
    expected_workflow_revision: int = Field(
        ge=1,
        le=_INT64_MAX,
        strict=True,
    )


class ApplyRequest(_StrictModel):
    expected_draft_hash: HashToken
    expected_workflow_revision: int = Field(
        ge=1,
        le=_INT64_MAX,
        strict=True,
    )
    expected_candidate_hash: HashToken


class _BackendJSONResponse(JSONResponse):
    """Render deeply nested Backend JSON without process-global recursion state."""

    def render(self, content: Any) -> bytes:
        return encode_json(content)


def _public_data(data: Any) -> Any:
    """Remove fields dropped by Backend migrations 000037/000040/000042."""

    if isinstance(data, list):
        return [_public_data(value) for value in data]
    if not isinstance(data, dict):
        return data
    result = {key: _public_data(value) for key, value in data.items()}
    if "workflow_snapshot" in result and "workflow_uuid" in result:
        result.pop("input", None)
        result.pop("output", None)
    if "workflow_uuid" in result and "pose" in result and "param" in result:
        result.pop("status", None)
    return result


def _success(data: Any = None, *, status: int = 200) -> _BackendJSONResponse:
    content: Dict[str, Any] = {"code": 0}
    if data is not None:
        content["data"] = _public_data(data)
    return _BackendJSONResponse(status_code=status, content=content)


def _error(error: WorkflowError) -> _BackendJSONResponse:
    conflict_codes = {
        "conflict",
        "draft_hash_conflict",
        "workflow_revision_conflict",
        "candidate_hash_conflict",
        "template_catalog_conflict",
        "candidate_not_ready",
        "draft_invalid",
        "candidate_invalid",
    }
    if error.code == "invalid_input":
        business_code = 1000
    elif error.code in {"not_found", "workflow_not_found"}:
        business_code = 3002
    elif error.code in conflict_codes:
        business_code = 3003
    elif error.code == "template_catalog_unavailable":
        business_code = 5001
    else:
        business_code = 1
    return _BackendJSONResponse(
        status_code=200,
        content={
            "code": business_code,
            "error": {"msg": error.message},
        },
    )


def format_sse_event(event: Dict[str, Any]) -> str:
    payload = json.dumps(
        event["data"],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"id: {event['id']}\nevent: {event['event']}\ndata: {payload}\n\n"


def create_workflow_router(service: WorkflowService) -> APIRouter:
    """Build the public Workflow router around one injected authority."""

    router = APIRouter(
        prefix="/api/v1",
        tags=["workflow"],
        route_class=_BackendJSONRoute,
    )

    @router.post("/workflows")
    def create_workflow(body: WorkflowCreateRequest) -> JSONResponse:
        return _success(
            service.create_workflow(**body.model_dump()),
            status=201,
        )

    @router.get("/workflows")
    def list_workflows(
        page: int = Query(default=1),
        page_size: int = Query(default=20),
        name: str = Query(default=""),
    ) -> JSONResponse:
        return _success(
            service.list_workflows(page=page, page_size=page_size, name=name)
        )

    @router.get("/workflows/{workflow_uuid}")
    def get_workflow(workflow_uuid: str) -> JSONResponse:
        return _success(service.get_workflow(workflow_uuid))

    @router.put("/workflows/{workflow_uuid}")
    def update_workflow(
        workflow_uuid: str,
        body: WorkflowUpdateRequest,
    ) -> JSONResponse:
        return _success(service.update_workflow(workflow_uuid, **body.model_dump()))

    @router.delete("/workflows/{workflow_uuid}")
    def delete_workflow(workflow_uuid: str) -> JSONResponse:
        service.delete_workflow(workflow_uuid)
        return _success()

    @router.get("/workflows/{workflow_uuid}/graph")
    def get_graph(workflow_uuid: str) -> JSONResponse:
        return _success(service.get_graph(workflow_uuid))

    @router.put("/workflows/{workflow_uuid}/graph")
    def save_graph(
        workflow_uuid: str,
        body: GraphWriteRequest,
    ) -> JSONResponse:
        return _success(
            service.save_graph(
                workflow_uuid,
                revision=body.revision,
                nodes=body.nodes,
                edges=body.edges,
            )
        )

    @router.post("/workflow-tasks")
    def create_workflow_task(
        body: WorkflowTaskCreateRequest,
    ) -> JSONResponse:
        return _success(
            service.create_workflow_task(
                workflow_uuid=body.workflow_uuid,
                run_mode=body.run_mode,
                target_node_uuid=body.target_node_uuid,
                input_value={},
                description=body.description,
                meta_data=body.meta_data,
            ),
            status=201,
        )

    @router.get("/workflow-tasks")
    def list_workflow_tasks(
        page: int = Query(default=1),
        page_size: int = Query(default=20),
        workflow_uuid: Optional[str] = Query(default=None),
        status: str = Query(default=""),
        cleanup_status: str = Query(default=""),
    ) -> JSONResponse:
        return _success(
            service.list_workflow_tasks(
                page=page,
                page_size=page_size,
                workflow_uuid=workflow_uuid,
                status=status,
                cleanup_status=cleanup_status,
            )
        )

    @router.get("/workflow-tasks/{task_uuid}")
    def get_workflow_task(task_uuid: str) -> JSONResponse:
        return _success(service.get_workflow_task(task_uuid))

    @router.get("/workflow-tasks/{task_uuid}/jobs")
    def list_workflow_node_jobs(task_uuid: str) -> JSONResponse:
        return _success(service.list_workflow_node_jobs(task_uuid))

    @router.get("/workflow-node-jobs/{job_uuid}")
    def get_workflow_node_job(job_uuid: str) -> JSONResponse:
        return _success(service.get_workflow_node_job(job_uuid))

    @router.get("/workflows/{workflow_uuid}/authoring")
    def get_authoring(workflow_uuid: str) -> JSONResponse:
        return _success(service.get_authoring(workflow_uuid))

    @router.put("/workflows/{workflow_uuid}/authoring/draft")
    def save_draft(
        workflow_uuid: str,
        body: DraftWriteRequest,
    ) -> JSONResponse:
        return _success(
            service.save_draft(
                workflow_uuid,
                python_source=body.python_source,
                expected_draft_hash=body.expected_draft_hash,
                expected_workflow_revision=body.expected_workflow_revision,
            )
        )

    @router.post("/workflows/{workflow_uuid}/authoring/apply")
    def apply_authoring(
        workflow_uuid: str,
        body: ApplyRequest,
    ) -> JSONResponse:
        return _success(
            service.apply_authoring(
                workflow_uuid,
                expected_draft_hash=body.expected_draft_hash,
                expected_workflow_revision=body.expected_workflow_revision,
                expected_candidate_hash=body.expected_candidate_hash,
            )
        )

    @router.get("/events")
    async def events(
        request: Request,
        last_event_id: Optional[str] = Header(
            default=None,
            alias="Last-Event-ID",
        ),
    ) -> Response:
        try:
            raw_cursor = next(
                (
                    value
                    for name, value in request.scope["headers"]
                    if name.lower() == b"last-event-id"
                ),
                None,
            )
            cursor_text = (
                raw_cursor.decode("utf-8")
                if raw_cursor is not None
                else (last_event_id or "")
            ).strip(_GO_WHITE_SPACE)
            if not cursor_text:
                cursor = 0
            else:
                cursor = _parse_non_negative_int64_decimal(cursor_text)
        except (UnicodeError, ValueError):
            cursor = -1
        if cursor == -1:
            return _error(WorkflowError("invalid_input"))

        async def stream():
            nonlocal cursor
            yield "retry: 3000\n: connected\n\n"
            while not await request.is_disconnected():
                events_page = service.list_events(
                    after_id=cursor,
                    limit=100,
                )["items"]
                for event in events_page:
                    cursor = event["id"]
                    yield format_sse_event(event)
                if not events_page:
                    yield ": keepalive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router


def install_workflow_api(
    app: FastAPI,
    service: WorkflowService,
    *,
    template_snapshot_provider: Optional[TemplateSnapshotProvider] = None,
) -> None:
    """向 OS FastAPI 应用安装工作流及可选模板查询接口。

    参数说明：``app`` 是共享 HTTP 应用，``service`` 是工作流权威；本地调度模式
    传入 ``template_snapshot_provider`` 后，模板查询与 F02 编译器共享同一投影。
    """

    @app.exception_handler(WorkflowError)
    async def workflow_error_handler(
        _request: Request,
        error: WorkflowError,
    ) -> JSONResponse:
        return _error(error)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        workflow_prefixes = (
            "/api/v1/workflows",
            "/api/v1/workflow-tasks",
            "/api/v1/workflow-node-jobs",
            "/api/v1/workflow-node-templates",
            "/api/v1/events",
        )
        if any(
            request.url.path == prefix or request.url.path.startswith(f"{prefix}/")
            for prefix in workflow_prefixes
        ):
            return _error(WorkflowError("invalid_input"))
        return await request_validation_exception_handler(request, error)

    app.include_router(create_workflow_router(service))
    if template_snapshot_provider is not None:
        app.include_router(
            create_workflow_template_router(
                WorkflowTemplateQueryService(template_snapshot_provider)
            )
        )


def create_workflow_app(
    service: WorkflowService,
    *,
    template_snapshot_provider: Optional[TemplateSnapshotProvider] = None,
) -> FastAPI:
    """创建工作流合同测试应用。

    参数说明：``service`` 是唯一工作流权威；可选模板快照提供者用于本地完整应用
    合同测试。返回已安装统一错误映射的 FastAPI 应用。
    """

    app = FastAPI(title="Uni-Lab Workflow", version="0.1.0")
    install_workflow_api(
        app,
        service,
        template_snapshot_provider=template_snapshot_provider,
    )
    return app


__all__ = [
    "create_workflow_app",
    "create_workflow_router",
    "format_sse_event",
    "install_workflow_api",
]
