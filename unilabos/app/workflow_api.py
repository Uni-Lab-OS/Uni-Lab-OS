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
from pydantic import BaseModel, ConfigDict, Field

from unilabos.workflow.models import WorkflowEdgeWrite, WorkflowNodeWrite
from unilabos.workflow.service import WorkflowError, WorkflowService


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _BackendModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


HashToken = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
_SIGNED_DECIMAL = re.compile(r"[+-]?[0-9]+\Z")
_INT64_MAX = (1 << 63) - 1
_GO_WHITE_SPACE = (
    "\t\n\v\f\r "
    "\u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)


class WorkflowCreateRequest(_BackendModel):
    name: str
    tags: List[Any] = Field(default_factory=list)
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)


class WorkflowUpdateRequest(WorkflowCreateRequest):
    pass


class GraphWriteRequest(_BackendModel):
    revision: int = Field(ge=1, le=_INT64_MAX, strict=True)
    nodes: List[WorkflowNodeWrite] = Field(default_factory=list)
    edges: List[WorkflowEdgeWrite] = Field(default_factory=list)


class WorkflowTaskCreateRequest(_BackendModel):
    workflow_uuid: str
    run_mode: str = "normal"
    target_node_uuid: Optional[str] = None
    input: Dict[str, Any] = Field(default_factory=dict)
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)


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


def _success(data: Any, *, status: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": 0, "data": data})


def _error(error: WorkflowError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status,
        content={
            "code": error.status,
            "error": {
                "code": error.code,
                "message": error.message,
            },
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

    router = APIRouter(prefix="/api/v1", tags=["workflow"])

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

    @router.delete("/workflows/{workflow_uuid}", status_code=204)
    def delete_workflow(workflow_uuid: str) -> Response:
        service.delete_workflow(workflow_uuid)
        return Response(status_code=204)

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
                input_value=body.input,
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
            elif not _SIGNED_DECIMAL.fullmatch(cursor_text):
                raise ValueError
            else:
                cursor = int(cursor_text, 10)
            if not 0 <= cursor <= _INT64_MAX:
                raise ValueError
        except (UnicodeError, ValueError):
            cursor = -1
        if cursor == -1:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_input",
                        "message": ("Last-Event-ID must be a non-negative integer"),
                    }
                },
            )

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


def install_workflow_api(app: FastAPI, service: WorkflowService) -> None:
    """Install error mapping and routes into an OS FastAPI application."""

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
            "/api/v1/events",
        )
        if any(
            request.url.path == prefix or request.url.path.startswith(f"{prefix}/")
            for prefix in workflow_prefixes
        ):
            return _error(WorkflowError("invalid_input"))
        return await request_validation_exception_handler(request, error)

    app.include_router(create_workflow_router(service))


def create_workflow_app(service: WorkflowService) -> FastAPI:
    """Create a focused application used by composition and contract tests."""

    app = FastAPI(title="Uni-Lab Workflow", version="0.1.0")
    install_workflow_api(app, service)
    return app


__all__ = [
    "create_workflow_app",
    "create_workflow_router",
    "format_sse_event",
    "install_workflow_api",
]
