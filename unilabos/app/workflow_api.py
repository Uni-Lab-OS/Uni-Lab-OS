"""Thin FastAPI adapter for the Backend-shaped local Workflow authority."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Dict, List, Optional, Protocol
from uuid import UUID

from fastapi import APIRouter, FastAPI, Header, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator

from unilabos.workflow.candidate_validation import validate_candidate_bundle
from unilabos.workflow.catalog import (
    CatalogAuthority,
    TemplateCatalog,
    TemplateCatalogUnavailable,
)
from unilabos.workflow.json_codec import decode_json_bytes, encode_json
from unilabos.workflow.models import (
    CandidateChangeset,
    CandidateCompilation,
    CandidateDiagnostic,
    CandidateSourceMapEntry,
    WorkflowEdgeWrite,
    WorkflowNodeWrite,
    normalize_json_array,
    normalize_json_object,
    validate_json_value,
    validate_uuid,
)
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.source_coordinates import (
    require_utf8_text,
    source_ranges_fit,
)

_LOGGER = logging.getLogger(__name__)


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
    """增量读取公共 Workflow 请求体，并在超限 chunk 后立即停止。"""

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length, 10)
        except ValueError:
            raise ValueError("invalid Content-Length") from None
        if declared_length < 0 or declared_length > _WORKFLOW_BODY_LIMIT:
            raise ValueError("Workflow body exceeds the public limit")

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _WORKFLOW_BODY_LIMIT:
            raise ValueError("Workflow body exceeds the public limit")
        body.extend(chunk)
    payload = bytes(body)
    request._body = payload
    return payload


class _BackendJSONRoute(APIRoute):
    """限制有请求体的路由，再按冻结 Backend 规则预载 JSON。"""

    def get_route_handler(self):
        route_handler = super().get_route_handler()
        expects_body = self.body_field is not None

        async def backend_json_route_handler(request: Request) -> Response:
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


def _parse_positive_decimal(value: str, *, maximum: int) -> int:
    """Match Go strconv.Atoi followed by a positive bounded range check."""

    if _SIGNED_DECIMAL.fullmatch(value) is None:
        raise ValueError
    parsed = int(value, 10)
    if parsed < 1 or parsed > maximum:
        raise ValueError
    return parsed


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
    input: Dict[str, Any] = Field(default_factory=dict)
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("input", "meta_data", mode="before")
    @classmethod
    def _json_object(cls, value: Any) -> Dict[str, Any]:
        return normalize_json_object(value)


class WorkflowTaskCommandRequest(_BackendModel):
    type: str
    target_node_uuid: Optional[UUID] = None
    idempotency_key: str
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

    @field_validator("python_source")
    @classmethod
    def _utf8_source(cls, value: str) -> str:
        return require_utf8_text(value)


class ApplyRequest(_StrictModel):
    candidate_hash: HashToken


class _AuthoringTransformRequest(_StrictModel):
    workflow_uuid: str
    revision: int = Field(ge=1, le=_INT64_MAX, strict=True)
    source_uri: str

    @field_validator("workflow_uuid")
    @classmethod
    def _workflow_uuid(cls, value: str) -> str:
        return validate_uuid(value)

    @field_validator("source_uri")
    @classmethod
    def _source_uri(cls, value: str) -> str:
        require_utf8_text(value)
        if not value.strip():
            raise ValueError("source_uri must not be blank")
        return value

    @field_validator("python_source", check_fields=False)
    @classmethod
    def _python_source(cls, value: str) -> str:
        return require_utf8_text(value)


class AuthoringCompileRequest(_AuthoringTransformRequest):
    python_source: str
    applied_graph: Dict[str, Any]


class AuthoringGeneratePythonRequest(_AuthoringTransformRequest):
    graph: Dict[str, Any]


class AuthoringValidateRequest(_AuthoringTransformRequest):
    graph: Dict[str, Any]
    python_source: str


class AuthoringTransform(Protocol):
    """02D production engine 的三个只读操作。"""

    def compile(self, **values: Any) -> CandidateCompilation: ...

    def generate_python(self, **values: Any) -> CandidateCompilation: ...

    def validate(self, **values: Any) -> CandidateCompilation: ...


class _BackendJSONResponse(JSONResponse):
    """Render deeply nested Backend JSON without process-global recursion state."""

    def render(self, content: Any) -> bytes:
        return encode_json(content)


def _success(data: Any, *, status: int = 200) -> _BackendJSONResponse:
    return _BackendJSONResponse(status_code=status, content={"code": 0, "data": data})


def _error(error: WorkflowError) -> _BackendJSONResponse:
    return _BackendJSONResponse(
        status_code=error.status,
        content={
            "code": error.status,
            "error": {
                "code": error.code,
                "message": error.message,
            },
        },
    )


def _diagnostic_ranges(
    diagnostics: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    ranges: List[Dict[str, Any]] = []
    for item in diagnostics:
        source_range = item.get("source_range")
        if source_range is not None:
            ranges.append(source_range)
        ranges.extend(item.get("occurrence_ranges") or [])
        for alternative in item.get("repair_alternatives") or []:
            ranges.append(alternative["retained_range"])
            ranges.extend(
                replacement["source_range"]
                for replacement in alternative["replacements"]
            )
    return ranges


def _transform_data(
    result: Any,
    *,
    input_source: Optional[str],
    workflow_uuid: str,
    revision: int,
    base_graph: Dict[str, Any],
    require_unchanged_graph: bool,
) -> Dict[str, Any]:
    """把 engine 结果收紧为唯一公开 DTO，拒绝内部或越界值。"""

    compilation = CandidateCompilation.model_validate(result)
    if not isinstance(compilation.diagnostics, list):
        raise ValueError("diagnostics must be an array")
    diagnostics = [
        CandidateDiagnostic.model_validate(item).model_dump(exclude_none=True)
        for item in compilation.diagnostics
    ]
    ranges = _diagnostic_ranges(diagnostics)
    if ranges and (input_source is None or not source_ranges_fit(input_source, ranges)):
        raise ValueError("diagnostic range is outside the request source")

    if not isinstance(compilation.source_map, list):
        raise ValueError("source_map must be an array")
    source_map = [
        CandidateSourceMapEntry.model_validate(item).model_dump()
        for item in compilation.source_map
    ]
    normalized_source = compilation.normalized_python_source
    graph = compilation.graph
    changeset: Optional[Dict[str, Any]] = None
    has_error = any(item["severity"].strip().lower() == "error" for item in diagnostics)

    if graph is None:
        if (
            not diagnostics
            or not has_error
            or normalized_source is not None
            or source_map
            or compilation.changeset is not None
        ):
            raise ValueError("invalid failed transform result")
    else:
        if has_error or not isinstance(normalized_source, str):
            raise ValueError("invalid successful transform result")
        validate_json_value(graph)
        require_utf8_text(normalized_source)
        if not source_ranges_fit(normalized_source, source_map):
            raise ValueError("source map is outside normalized source")
        changeset = CandidateChangeset.model_validate(
            compilation.changeset
        ).model_dump()
        graph = validate_candidate_bundle(
            graph=graph,
            base_graph=base_graph,
            workflow_uuid=workflow_uuid,
            revision=revision,
            source_map=source_map,
            changeset=changeset,
            require_unchanged_graph=require_unchanged_graph,
        )

    compiler_version = compilation.compiler_version
    fingerprint = compilation.template_catalog_fingerprint
    if not isinstance(compiler_version, str) or not compiler_version.strip():
        raise ValueError("compiler_version must not be blank")
    require_utf8_text(compiler_version)
    if (
        not isinstance(fingerprint, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None
    ):
        raise ValueError("invalid template catalog fingerprint")

    return {
        "diagnostics": diagnostics,
        "graph": graph,
        "normalized_python_source": normalized_source,
        "source_map": source_map,
        "changeset": changeset,
        "compiler_version": compiler_version,
        "template_catalog_fingerprint": fingerprint,
    }


def _transform_response(
    operation: Callable[[], Any],
    *,
    input_source: Optional[str],
    workflow_uuid: str,
    revision: int,
    base_graph: Dict[str, Any],
    require_unchanged_graph: bool = False,
) -> _BackendJSONResponse:
    try:
        data = _transform_data(
            operation(),
            input_source=input_source,
            workflow_uuid=workflow_uuid,
            revision=revision,
            base_graph=base_graph,
            require_unchanged_graph=require_unchanged_graph,
        )
        if any(
            item["code"] == "template_catalog_unavailable"
            for item in data["diagnostics"]
        ):
            return _error(WorkflowError("template_catalog_unavailable"))
        return _success(data)
    except Exception:  # noqa: BLE001 - HTTP 边界不得泄漏 engine/adapter 内部异常
        _LOGGER.exception("Authoring pure transform 失败")
        return _error(WorkflowError("internal_error"))


def format_sse_event(event: Dict[str, Any]) -> str:
    payload = json.dumps(
        event["data"],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"id: {event['id']}\nevent: {event['event']}\ndata: {payload}\n\n"


def create_authoring_transform_router(
    engine: AuthoringTransform,
) -> APIRouter:
    """创建仅含 D-040 三个纯转换操作的 OS-only router。"""

    router = APIRouter(
        prefix="/api/v1/authoring",
        tags=["authoring-transform"],
        route_class=_BackendJSONRoute,
    )

    @router.post("/compile")
    def compile_authoring(body: AuthoringCompileRequest) -> JSONResponse:
        values = {
            "workflow_uuid": body.workflow_uuid,
            "workflow_revision": body.revision,
            "python_source": body.python_source,
            "source_uri": body.source_uri,
            "applied_graph": body.applied_graph,
        }
        return _transform_response(
            lambda: engine.compile(**values),
            input_source=body.python_source,
            workflow_uuid=body.workflow_uuid,
            revision=body.revision,
            base_graph=body.applied_graph,
        )

    @router.post("/generate-python")
    def generate_authoring_python(
        body: AuthoringGeneratePythonRequest,
    ) -> JSONResponse:
        values = {
            "workflow_uuid": body.workflow_uuid,
            "workflow_revision": body.revision,
            "graph": body.graph,
            "source_uri": body.source_uri,
        }
        return _transform_response(
            lambda: engine.generate_python(**values),
            input_source=None,
            workflow_uuid=body.workflow_uuid,
            revision=body.revision,
            base_graph=body.graph,
            require_unchanged_graph=True,
        )

    @router.post("/validate")
    def validate_authoring(body: AuthoringValidateRequest) -> JSONResponse:
        values = {
            "workflow_uuid": body.workflow_uuid,
            "workflow_revision": body.revision,
            "graph": body.graph,
            "python_source": body.python_source,
            "source_uri": body.source_uri,
        }
        return _transform_response(
            lambda: engine.validate(**values),
            input_source=body.python_source,
            workflow_uuid=body.workflow_uuid,
            revision=body.revision,
            base_graph=body.graph,
            require_unchanged_graph=True,
        )

    return router


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

    @router.post("/workflow-tasks/{task_uuid}/commands")
    def create_workflow_task_command(
        task_uuid: str,
        body: WorkflowTaskCommandRequest,
    ) -> JSONResponse:
        return _success(
            service.create_workflow_task_command(
                task_uuid,
                command_type=body.type,
                target_node_uuid=(
                    str(body.target_node_uuid)
                    if body.target_node_uuid is not None
                    else None
                ),
                idempotency_key=body.idempotency_key,
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

    @router.get("/workflow-node-jobs/{job_uuid}/feedback")
    def list_workflow_node_job_feedback(
        job_uuid: str,
        after_sequence: str = Query(default=""),
        limit: str = Query(default=""),
    ) -> JSONResponse:
        try:
            after_text = after_sequence.strip(_GO_WHITE_SPACE)
            limit_text = limit.strip(_GO_WHITE_SPACE)
            parsed_after = (
                _parse_non_negative_int64_decimal(after_text) if after_text else 0
            )
            parsed_limit = (
                _parse_positive_decimal(limit_text, maximum=500) if limit_text else 100
            )
        except ValueError:
            raise WorkflowError("invalid_input") from None
        return _success(
            service.list_workflow_node_job_feedback(
                job_uuid,
                after_sequence=parsed_after,
                limit=parsed_limit,
            )
        )

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
                candidate_hash=body.candidate_hash,
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


def create_workflow_template_catalog_router(
    catalog: TemplateCatalog,
    authority: CatalogAuthority,
) -> APIRouter:
    """以 Backend DTO 形状公开同一份持久 TemplateCatalog snapshot。"""

    if not isinstance(catalog, TemplateCatalog):
        raise TypeError("catalog 必须是 TemplateCatalog")
    if not isinstance(authority, CatalogAuthority):
        raise TypeError("authority 必须是 CatalogAuthority")
    router = APIRouter(prefix="/api/v1")

    def read_snapshot() -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
        try:
            with catalog.snapshot(authority) as snapshot:
                return (
                    snapshot.fingerprint,
                    [_detached_catalog_value(item) for item in snapshot.node_templates],
                    [
                        _detached_catalog_value(item)
                        for item in snapshot.handle_templates
                    ],
                )
        except TemplateCatalogUnavailable:
            raise WorkflowError("template_catalog_unavailable") from None

    def envelope(fingerprint: str) -> dict[str, Any]:
        return {
            "authority": {
                "authority_id": authority.authority_id,
                "kind": authority.kind,
            },
            "catalog_fingerprint": fingerprint,
        }

    @router.get("/workflow-node-templates")
    def list_workflow_node_templates() -> JSONResponse:
        fingerprint, nodes, _handles = read_snapshot()
        items = [_workflow_node_template_summary(item) for item in nodes]
        return _success(
            {
                **envelope(fingerprint),
                "items": items,
                "total": len(items),
                "page": 1,
                "page_size": len(items),
            }
        )

    @router.get("/workflow-node-templates/{template_uuid}")
    def get_workflow_node_template(template_uuid: str) -> JSONResponse:
        fingerprint, nodes, handles = read_snapshot()
        template = next(
            (item for item in nodes if item.get("uuid") == template_uuid),
            None,
        )
        if template is None:
            return _template_catalog_not_found()
        owned_handles = [
            item
            for item in handles
            if item.get("workflow_node_template_uuid") == template_uuid
        ]
        return _success(
            {
                **envelope(fingerprint),
                "template": template,
                "handles": owned_handles,
            }
        )

    @router.get("/workflow-node-templates/{template_uuid}/handles")
    def list_workflow_node_template_handles(template_uuid: str) -> JSONResponse:
        fingerprint, nodes, handles = read_snapshot()
        if not any(item.get("uuid") == template_uuid for item in nodes):
            return _template_catalog_not_found()
        return _success(
            {
                **envelope(fingerprint),
                "items": [
                    item
                    for item in handles
                    if item.get("workflow_node_template_uuid") == template_uuid
                ],
            }
        )

    @router.get("/workflow-handle-templates/{handle_uuid}")
    def get_workflow_handle_template(handle_uuid: str) -> JSONResponse:
        fingerprint, _nodes, handles = read_snapshot()
        handle = next(
            (item for item in handles if item.get("uuid") == handle_uuid),
            None,
        )
        if handle is None:
            return _template_catalog_not_found()
        return _success({**envelope(fingerprint), "handle": handle})

    return router


def _workflow_node_template_summary(template: Mapping[str, Any]) -> dict[str, Any]:
    unilab = template.get("meta_data", {}).get("unilab", {})
    resource = unilab.get("resource_template") if isinstance(unilab, Mapping) else None
    if not isinstance(resource, Mapping):
        resource = {
            "uuid": template["resource_template_uuid"],
            "name": template["resource_template_uuid"],
            "display_name": template["resource_template_uuid"],
        }
    return {
        "uuid": template["uuid"],
        "name": template["name"],
        "display_name": template["display_name"],
        "type": template["type"],
        "node_type": template["node_type"],
        "resource_template": {
            "uuid": resource["uuid"],
            "name": resource["name"],
            "display_name": resource["display_name"],
        },
    }


def _detached_catalog_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _detached_catalog_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_detached_catalog_value(item) for item in value]
    return value


def _template_catalog_not_found() -> _BackendJSONResponse:
    return _BackendJSONResponse(
        status_code=404,
        content={
            "code": 404,
            "error": {"code": "not_found", "message": "资源不存在"},
        },
    )


def _install_error_handlers(app: FastAPI) -> None:
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
            "/api/v1/authoring",
        )
        if any(
            request.url.path == prefix or request.url.path.startswith(f"{prefix}/")
            for prefix in workflow_prefixes
        ):
            return _error(WorkflowError("invalid_input"))
        return await request_validation_exception_handler(request, error)


def install_workflow_api(app: FastAPI, service: WorkflowService) -> None:
    """Install error mapping and routes into an OS FastAPI application."""

    _install_error_handlers(app)

    app.include_router(create_workflow_router(service))


def install_authoring_transform_api(
    app: FastAPI,
    engine: AuthoringTransform,
) -> None:
    """把 pure Authoring router 安装到显式选择的 OS application。"""

    _install_error_handlers(app)
    app.include_router(create_authoring_transform_router(engine))


def install_composed_workflow_authoring_api(
    app: FastAPI,
    service: WorkflowService,
    engine: AuthoringTransform,
    *,
    template_catalog: TemplateCatalog | None = None,
    catalog_authority: CatalogAuthority | None = None,
) -> None:
    """完整构造 production Authoring 路由后，以一次 app mutation 安装。"""

    if (template_catalog is None) != (catalog_authority is None):
        raise ValueError("TemplateCatalog 与 CatalogAuthority 必须同时配置")
    router = APIRouter()
    if template_catalog is not None and catalog_authority is not None:
        router.include_router(
            create_workflow_template_catalog_router(
                template_catalog,
                catalog_authority,
            )
        )
    router.include_router(create_workflow_router(service))
    router.include_router(create_authoring_transform_router(engine))
    _install_error_handlers(app)
    app.include_router(router)


def create_workflow_app(service: WorkflowService) -> FastAPI:
    """Create a focused application used by composition and contract tests."""

    app = FastAPI(title="Uni-Lab Workflow", version="0.1.0")
    install_workflow_api(app, service)
    return app


def create_authoring_transform_app(engine: AuthoringTransform) -> FastAPI:
    """创建只暴露三个 pure transform 的 focused application。"""

    app = FastAPI(title="Uni-Lab Authoring Transform", version="0.1.0")
    install_authoring_transform_api(app, engine)
    return app


__all__ = [
    "AuthoringCompileRequest",
    "AuthoringGeneratePythonRequest",
    "AuthoringValidateRequest",
    "create_authoring_transform_app",
    "create_authoring_transform_router",
    "create_workflow_template_catalog_router",
    "create_workflow_app",
    "create_workflow_router",
    "format_sse_event",
    "install_authoring_transform_api",
    "install_composed_workflow_authoring_api",
    "install_workflow_api",
]
