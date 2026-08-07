"""后端形状工作流节点模板查询的只读 HTTP 适配器。"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Annotated, Any, Protocol
from uuid import UUID

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from unilabos.workflow.authoring_kernel import (
    AuthoringCatalogAction,
    AuthoringCatalogSnapshot,
)
from unilabos.workflow.models import validate_uuid

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100
_DEFAULT_VISIBLE_NODE_TYPES = frozenset(
    {"ILab", "device_action", "py_script", "tool_call", "manual_confirm"}
)


class TemplateSnapshotProvider(Protocol):
    """提供最近一次完整模板投影的窄读取接口。"""

    def snapshot(self) -> AuthoringCatalogSnapshot:
        """返回已提交的不可变设备注册表模板投影。"""


class WorkflowTemplateQueryError(RuntimeError):
    """模板查询的稳定业务错误。"""

    def __init__(self, code: int, message: str) -> None:
        """保存后端（Backend）业务码和可读消息。

        参数说明：``code`` 是响应 envelope（响应外壳）业务码，``message`` 是
        前端可显示的错误说明。
        """

        super().__init__(message)
        self.code = code
        self.message = message


class WorkflowTemplateQueryService:
    """从一个不可变快照完成列表、筛选、游标和详情查询。"""

    def __init__(
        self,
        snapshot_provider: TemplateSnapshotProvider,
        *,
        authority_id: str = "local",
        authority_kind: str = "local",
    ) -> None:
        """绑定模板快照提供者。

        参数说明：``snapshot_provider`` 通常是设备注册表模板投影；
        ``authority_id`` 和 ``authority_kind`` 标识目录权威（Authority）。每次
        请求只取一次快照，避免一条响应混合两个发布代际。
        """

        if not authority_id.strip() or authority_kind not in {"local", "backend"}:
            raise ValueError("模板目录权威身份非法")
        self._snapshot_provider = snapshot_provider
        self._authority = {
            "authority_id": authority_id.strip(),
            "kind": authority_kind,
        }

    def list_node_templates(
        self,
        *,
        limit: int,
        cursor_uuid: str | None,
        keyword: str,
        resource_template_uuid: str | None,
        action_type: str,
        node_type: str,
        page: int | None = None,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        """按后端（Backend）当前查询合同返回节点模板摘要页。

        参数说明：``limit`` / ``cursor_uuid`` 是游标分页；``page`` / ``page_size``
        是调试台/FE 使用的偏移分页。二者择一：显式 ``page`` 时返回
        ``total/page/page_size``；否则返回 ``has_more/next_cursor_uuid``。
        其余字段分别筛选名称、资源模板 UUID、动作类型和节点类型。
        """

        snapshot = self._snapshot_provider.snapshot()
        # ``ordered_actions`` 使用 Backend 的创建时间降序、UUID 降序规则。
        ordered_actions = sorted(snapshot.actions, key=_action_order, reverse=True)
        cursor_order: tuple[str, str] | None = None
        use_offset_page = page is not None
        if use_offset_page and cursor_uuid is not None:
            raise WorkflowTemplateQueryError(
                1000,
                "page and cursor_uuid cannot be combined",
            )
        if cursor_uuid is not None:
            cursor_identity = _validated_uuid(cursor_uuid, "cursor_uuid")
            cursor_action = next(
                (
                    action
                    for action in ordered_actions
                    if str(action.template["uuid"]) == cursor_identity
                ),
                None,
            )
            if cursor_action is None:
                raise WorkflowTemplateQueryError(
                    1000,
                    "cursor_uuid does not reference an existing workflow node template",
                )
            cursor_order = _action_order(cursor_action)

        normalized_resource_uuid = (
            _validated_uuid(resource_template_uuid, "resource_template_uuid")
            if resource_template_uuid is not None
            else None
        )
        normalized_keyword = keyword.strip().lower()
        normalized_type = action_type.strip()
        normalized_node_type = node_type.strip()
        matches: list[AuthoringCatalogAction] = []
        for action in ordered_actions:
            template = action.template
            if cursor_order is not None and _action_order(action) >= cursor_order:
                continue
            if normalized_resource_uuid is not None and str(
                template.get("resource_template_uuid")
            ) != normalized_resource_uuid:
                continue
            if normalized_keyword and normalized_keyword not in str(
                template.get("name", "")
            ).lower() and normalized_keyword not in str(
                template.get("display_name", "")
            ).lower():
                continue
            if normalized_type and template.get("type") != normalized_type:
                continue
            candidate_node_type = str(template.get("node_type") or "")
            if normalized_node_type:
                if candidate_node_type != normalized_node_type:
                    continue
            elif candidate_node_type not in _DEFAULT_VISIBLE_NODE_TYPES:
                continue
            matches.append(action)

        if use_offset_page:
            normalized_page = 1 if page is None or page < 1 else page
            normalized_page_size = (
                _DEFAULT_LIMIT
                if page_size is None or page_size < 1
                else min(page_size, _MAX_LIMIT)
            )
            total = len(matches)
            start = (normalized_page - 1) * normalized_page_size
            page_actions = matches[start : start + normalized_page_size]
            return {
                "authority": dict(self._authority),
                "catalog_fingerprint": snapshot.fingerprint,
                "items": [_summary(action) for action in page_actions],
                "total": total,
                "page": normalized_page,
                "page_size": normalized_page_size,
            }

        normalized_limit = _DEFAULT_LIMIT if limit < 1 else min(limit, _MAX_LIMIT)
        page_actions = matches[:normalized_limit]
        has_more = len(matches) > normalized_limit
        return {
            "authority": dict(self._authority),
            "catalog_fingerprint": snapshot.fingerprint,
            "items": [_summary(action) for action in page_actions],
            "has_more": has_more,
            "next_cursor_uuid": (
                str(page_actions[-1].template["uuid"])
                if has_more and page_actions
                else None
            ),
        }

    def get_node_template(self, template_uuid: str) -> dict[str, Any]:
        """按 UUID 返回节点模板及其全部句柄模板。

        参数说明：``template_uuid`` 是路径身份；返回后端（Backend）详情形状
        ``template + handles``，未知身份使用业务码 5001。
        """

        template_identity = _validated_uuid(template_uuid, "template_uuid")
        snapshot = self._snapshot_provider.snapshot()
        action = next(
            (
                candidate
                for candidate in snapshot.actions
                if str(candidate.template["uuid"]) == template_identity
            ),
            None,
        )
        if action is None:
            raise WorkflowTemplateQueryError(
                5001,
                f"workflow node template {template_identity} does not exist",
            )
        template = action.detached_template()
        handles = action.detached_handles()
        http_template = _http_authoring_template(template)
        http_handles = _http_authoring_handles(handles)
        return _restore_fe_null_fields(
            _omit_none(
                {
                    "authority": dict(self._authority),
                    "catalog_fingerprint": snapshot.fingerprint,
                    "template": http_template,
                    "handles": http_handles,
                }
            )
        )


def _http_authoring_template(template: Mapping[str, Any]) -> dict[str, Any]:
    """把存储态节点模板投影为调试台 Action Catalog 可读详情。

    存储列 ``schema`` 是 Backend goal 参数子模式（常为 JSON 字符串）；完整
    ``x-unilabos-action-contract`` 在 ``meta_data.unilab.action_contract_schema``。
    FE 只消费详情里的 ``template.schema`` 对象，且要求合同 ``version=1``。
    """

    projected = copy.deepcopy(dict(template))
    meta = projected.get("meta_data")
    unilab = meta.get("unilab") if isinstance(meta, Mapping) else None
    action_contract_schema = (
        unilab.get("action_contract_schema") if isinstance(unilab, Mapping) else None
    )
    if isinstance(action_contract_schema, Mapping):
        schema = copy.deepcopy(dict(action_contract_schema))
        contract = schema.get("x-unilabos-action-contract")
        if isinstance(contract, dict) and contract.get("version") == 2:
            contract["version"] = 1
        projected["schema"] = schema
        return projected

    schema = projected.get("schema")
    if isinstance(schema, str) and schema.strip():
        try:
            decoded = json.loads(schema)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            projected["schema"] = decoded
    return projected


def _http_authoring_handles(
    handles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """补齐 FE ``projectHandle`` 所需的 ready / allowlist 连接点元数据。"""

    projected: list[dict[str, Any]] = []
    for handle in handles:
        item = copy.deepcopy(dict(handle))
        if item.get("handle_key") == "ready":
            item["type"] = "boolean"
            item["data_source"] = "dependency"
            item["data_key"] = "ready"
            item["meta_data"] = {
                "unilab": {
                    "value_schema": {"type": "boolean"},
                    "editor_control": "variable_selector",
                    "allowed_resource_template_uuids": None,
                    "implicit_passthrough": False,
                    "structural_role": "ready",
                }
            }
            projected.append(item)
            continue

        meta = item.get("meta_data")
        if not isinstance(meta, dict):
            meta = {}
            item["meta_data"] = meta
        unilab = meta.get("unilab")
        if not isinstance(unilab, dict):
            unilab = {}
            meta["unilab"] = unilab
        if "allowed_resource_template_uuids" not in unilab:
            unilab["allowed_resource_template_uuids"] = None
        projected.append(item)
    return projected


def _restore_fe_null_fields(payload: Any) -> Any:
    """在 ``_omit_none`` 之后写回 FE 解码器要求显式 ``null`` 的字段。

    FE ``allowlistValue`` 只接受 ``null`` 或非空 UUID 数组；缺键会走
    ``invalidCatalog``。Backend omitempty 会剥掉 ``null``，因此详情响应必须
    在序列化前把 allowlist 空值写回。
    """

    if not isinstance(payload, dict):
        return payload
    handles = payload.get("handles")
    if not isinstance(handles, list):
        return payload
    for handle in handles:
        if not isinstance(handle, dict):
            continue
        meta = handle.get("meta_data")
        if not isinstance(meta, dict):
            meta = {}
            handle["meta_data"] = meta
        unilab = meta.get("unilab")
        if not isinstance(unilab, dict):
            unilab = {}
            meta["unilab"] = unilab
        if "allowed_resource_template_uuids" not in unilab:
            unilab["allowed_resource_template_uuids"] = None
    return payload


def _validated_uuid(value: str, field: str) -> str:
    """校验并规范化查询 UUID。

    参数说明：``value`` 是调用方身份，``field`` 用于错误消息；返回非空规范 UUID。
    """

    try:
        return validate_uuid(value)
    except (TypeError, ValueError):
        raise WorkflowTemplateQueryError(1000, f"invalid {field}") from None


def _action_order(action: AuthoringCatalogAction) -> tuple[str, str]:
    """取得后端（Backend）兼容的模板游标排序键。

    参数说明：``action`` 是快照动作；返回 ``(create_time, uuid)``，旧投影缺失
    创建时间时使用空字符串但仍保持 UUID 稳定排序。
    """

    return str(action.template.get("create_time") or ""), str(
        action.template["uuid"]
    )


def _summary(action: AuthoringCatalogAction) -> dict[str, Any]:
    """把快照动作映射为节点模板列表摘要。

    参数说明：``action`` 是一个聚合后的节点与句柄；返回值仅包含 Backend 列表
    字段，资源模板摘要来自投影时固化的元数据。
    异常：资源模板摘要缺失时抛出 ``WorkflowTemplateQueryError``。
    """

    template = action.template
    meta_data = template.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    resource_template = (
        unilab.get("resource_template") if isinstance(unilab, Mapping) else None
    )
    # 已发布工作流的 ``meta_data.unilab`` 是与前端冻结的封闭来源合同；框架
    # 所有者摘要因此位于相邻 ``meta_data.resource_template``，避免扩张 wire。
    if not isinstance(resource_template, Mapping) and isinstance(meta_data, Mapping):
        resource_template = meta_data.get("resource_template")
    if not isinstance(resource_template, Mapping):
        raise WorkflowTemplateQueryError(
            5004,
            "workflow node template is missing its resource template summary",
        )
    result = {
        "uuid": template["uuid"],
        "name": template["name"],
        "display_name": template["display_name"],
        "type": template["type"],
        "node_type": template["node_type"],
        "icon": template.get("icon"),
        "resource_template": {
            "uuid": resource_template.get("uuid"),
            "name": resource_template.get("name"),
            "display_name": resource_template.get("display_name"),
        },
    }
    return _omit_none(result)


def _omit_none(value: Any) -> Any:
    """递归移除 Go ``omitempty`` 对应的空可选字段。

    参数说明：``value`` 是待返回 JSON；字典中的 ``None`` 被移除，数组和标量保持
    结构，返回值与不可变快照不共享可变容器。
    """

    if isinstance(value, Mapping):
        return {
            key: _omit_none(child)
            for key, child in value.items()
            if child is not None
        }
    if isinstance(value, (list, tuple)):
        return [_omit_none(child) for child in value]
    return value


def _success(data: Any) -> JSONResponse:
    """构造后端（Backend）成功响应外壳。

    参数说明：``data`` 是查询结果；返回 HTTP 200 和业务码 0。
    """

    return JSONResponse(status_code=200, content={"code": 0, "data": data})


def _error(error: WorkflowTemplateQueryError) -> JSONResponse:
    """构造后端（Backend）业务错误响应外壳。

    参数说明：``error`` 持有稳定业务码和消息；HTTP 状态保持 200。
    """

    return JSONResponse(
        status_code=200,
        content={"code": error.code, "error": {"msg": error.message}},
    )


def _call(callback: Any, *args: Any, **kwargs: Any) -> JSONResponse:
    """调用查询服务并统一映射业务错误。

    参数说明：``callback`` 是查询方法，其余参数原样转发；返回统一响应外壳。
    """

    try:
        return _success(callback(*args, **kwargs))
    except WorkflowTemplateQueryError as error:
        return _error(error)


def create_workflow_template_router(
    service: WorkflowTemplateQueryService,
) -> APIRouter:
    """创建后端形状工作流节点模板只读路由。

    参数说明：``service`` 绑定单一模板快照来源；返回只含列表与详情的路由。
    """

    router = APIRouter(prefix="/api/v1", tags=["workflow-template"])

    @router.get("/workflow-node-templates")
    def list_node_templates(
        limit: int = Query(default=0),
        cursor_uuid: Annotated[UUID | None, Query()] = None,
        page: Annotated[int | None, Query()] = None,
        page_size: Annotated[int | None, Query()] = None,
        keyword: str = Query(default=""),
        resource_template_uuid: Annotated[UUID | None, Query()] = None,
        action_type: str = Query(default="", alias="type"),
        node_type: str = Query(default=""),
    ) -> JSONResponse:
        """按 Backend 查询参数返回工作流节点模板摘要页。

        参数说明：``limit`` 和 ``cursor_uuid`` 控制 UUID 游标；``page`` /
        ``page_size`` 控制偏移分页（调试台 Action Catalog）；``keyword``、资源
        模板 UUID、动作类型和节点类型执行服务端筛选。返回统一 JSON 外壳。
        """

        return _call(
            service.list_node_templates,
            limit=limit,
            cursor_uuid=str(cursor_uuid) if cursor_uuid else None,
            page=page,
            page_size=page_size,
            keyword=keyword,
            resource_template_uuid=(
                str(resource_template_uuid) if resource_template_uuid else None
            ),
            action_type=action_type,
            node_type=node_type,
        )

    @router.get("/workflow-node-templates/{template_uuid}")
    def get_node_template(template_uuid: UUID) -> JSONResponse:
        """返回一个节点模板及其句柄详情。"""

        return _call(service.get_node_template, str(template_uuid))

    return router


def install_workflow_template_api(
    app: FastAPI,
    service: WorkflowTemplateQueryService,
) -> None:
    """向现有 FastAPI 应用安装模板查询路由和校验错误映射。

    参数说明：``app`` 是 OS HTTP 应用，``service`` 是只读查询服务。
    """

    @app.exception_handler(RequestValidationError)
    async def template_validation_error(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        """把模板路由的参数校验错误映射为 Backend 业务错误。

        参数说明：``request`` 用于识别模板路径，``error`` 转交非模板路由的默认
        FastAPI 处理器。
        """

        if request.url.path.startswith("/api/v1/workflow-node-templates"):
            return _error(WorkflowTemplateQueryError(1000, "Invalid request parameter"))
        return await request_validation_exception_handler(request, error)

    app.include_router(create_workflow_template_router(service))


def create_workflow_template_app(
    snapshot_provider: TemplateSnapshotProvider,
) -> FastAPI:
    """创建模板查询聚焦测试应用。

    参数说明：``snapshot_provider`` 提供最近已提交快照；返回独立 FastAPI 应用。
    """

    app = FastAPI(title="Uni-Lab Workflow Templates", version="0.1.0")
    install_workflow_template_api(
        app,
        WorkflowTemplateQueryService(snapshot_provider),
    )
    return app


__all__ = [
    "TemplateSnapshotProvider",
    "WorkflowTemplateQueryError",
    "WorkflowTemplateQueryService",
    "create_workflow_template_app",
    "create_workflow_template_router",
    "install_workflow_template_api",
]
