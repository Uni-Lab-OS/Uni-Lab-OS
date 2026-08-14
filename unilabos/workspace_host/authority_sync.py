"""Prepare a Backend Authority from the currently running local projection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import requests

from unilabos.app.instance_sync import InstanceSynchronizer

from .model import WorkspaceHostError


@dataclass(frozen=True)
class AuthorityBootstrapReport:
    """Stable counts emitted by one idempotent authority bootstrap."""

    template_count: int
    created_material_count: int
    existing_material_count: int


class BackendAuthorityBootstrapper:
    """Copy the local authoring projection, then initialize graph instances.

    The source is the already-running Local Backend rather than a second registry
    build.  That guarantees the target receives the exact template generation the
    author has inspected in Workbench, including workspace-qualified identities.
    """

    def __init__(
        self,
        source_address: str,
        target_address: str,
        credential: str,
        *,
        source_workspace: Path | None = None,
        session: requests.Session | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.source_api = _api_base(source_address)
        self.target_api = _api_base(target_address)
        self.credential = str(credential or "").strip()
        self.source_workspace = (
            str(source_workspace.resolve()) if source_workspace is not None else None
        )
        if not self.credential:
            raise WorkspaceHostError(
                "backend_authority_credentials_missing",
                "Backend Authority 初始化凭据缺失",
            )
        self.session = session or requests.Session()
        self.timeout = timeout

    def bootstrap(self, graph_path: Path) -> AuthorityBootstrapReport:
        """Synchronize templates and graph materials without starting an Edge."""

        try:
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WorkspaceHostError(
                "backend_authority_bootstrap_failed",
                f"无法读取当前设备图：{graph_path}",
            ) from error
        if not isinstance(graph, Mapping):
            raise WorkspaceHostError(
                "backend_authority_bootstrap_failed",
                "当前设备图根必须是 JSON object",
            )
        graph_node_ids = {
            str(node.get("id") or "").strip()
            for node in graph.get("nodes", [])
            if isinstance(node, Mapping) and str(node.get("id") or "").strip()
        }
        released_node_ids = self._target_released_node_ids()
        released_graph_node_ids = graph_node_ids & released_node_ids
        if released_graph_node_ids:
            return AuthorityBootstrapReport(
                template_count=0,
                created_material_count=0,
                existing_material_count=len(released_graph_node_ids),
            )

        definitions = [
            _template_definition(self._source_template(item))
            for item in self._source_templates()
        ]
        if not definitions:
            raise WorkspaceHostError(
                "backend_authority_bootstrap_failed",
                "Local Backend 未提供可同步的资源模板",
            )
        response = self.session.post(
            f"{self.target_api}/resource-templates",
            json={"resources": definitions},
            headers=self._headers(),
            timeout=self.timeout,
        )
        payload = _response_data(response, "资源模板同步")
        identities = payload.get("templates")
        if not isinstance(identities, list) or len(identities) != len(definitions):
            raise WorkspaceHostError(
                "backend_authority_bootstrap_failed",
                "Backend Authority 未返回完整模板身份",
            )
        try:
            material_report = InstanceSynchronizer(
                self.target_api,
                self.credential,
                session=self.session,
                timeout=self.timeout,
            ).sync_graph(graph)
        except Exception as error:  # noqa: BLE001 - normalize the transaction seam.
            raise WorkspaceHostError(
                "backend_authority_bootstrap_failed",
                f"Backend Authority 资源实例初始化失败：{error}",
            ) from error
        return AuthorityBootstrapReport(
            template_count=len(definitions),
            created_material_count=material_report.created_count,
            existing_material_count=material_report.existing_count,
        )

    def _target_released_node_ids(self) -> set[str]:
        """Return graph node identities already installed by a verified release."""

        released: set[str] = set()
        page_number = 1
        while True:
            response = self.session.get(
                f"{self.target_api}/materials",
                params={
                    "page": page_number,
                    "page_size": 100,
                    "with_children": "true",
                },
                headers=self._headers(),
                timeout=self.timeout,
            )
            page = _response_data(response, "Backend Authority 物料列表")
            raw_items = page.get("items")
            if not isinstance(raw_items, list):
                raise WorkspaceHostError(
                    "backend_authority_bootstrap_failed",
                    "Backend Authority 物料列表结构无效",
                )
            for item in raw_items:
                if not isinstance(item, Mapping):
                    continue
                metadata = item.get("meta_data")
                if not isinstance(metadata, Mapping):
                    continue
                release = metadata.get("unilab_release")
                if not isinstance(release, Mapping):
                    continue
                if (
                    self.source_workspace is not None
                    and str(release.get("source_workspace") or "")
                    != self.source_workspace
                ):
                    continue
                node_id = str(metadata.get("source_node_id") or "").strip()
                if node_id:
                    released.add(node_id)
            total = page.get("total")
            if (
                isinstance(total, int)
                and not isinstance(total, bool)
                and page_number * 100 < total
            ):
                page_number += 1
                continue
            return released

    def _source_templates(self) -> list[Mapping[str, Any]]:
        items: list[Mapping[str, Any]] = []
        cursor: str | None = None
        page_number = 1
        pagination_mode: str | None = None
        while True:
            params: dict[str, object]
            if pagination_mode == "cursor":
                params = {"limit": 100}
            else:
                params = {"page": page_number, "page_size": 100}
            if pagination_mode == "cursor" and cursor:
                params["cursor_uuid"] = cursor
            response = self.session.get(
                f"{self.source_api}/resource-templates",
                params=params,
                timeout=self.timeout,
            )
            page = _response_data(response, "Local Backend 模板列表")
            raw_items = page.get("items")
            if not isinstance(raw_items, list):
                raise WorkspaceHostError(
                    "backend_authority_bootstrap_failed",
                    "Local Backend 模板列表结构无效",
                )
            items.extend(item for item in raw_items if isinstance(item, Mapping))
            if not page.get("has_more"):
                return items

            has_numbered_fields = "page" in page or "page_size" in page
            next_cursor = next(
                (
                    value
                    for key in ("next_cursor_uuid", "next_cursor", "cursor")
                    if isinstance((value := page.get(key)), str) and value
                ),
                None,
            )
            has_cursor_fields = next_cursor is not None
            if has_numbered_fields and has_cursor_fields:
                raise WorkspaceHostError(
                    "backend_authority_bootstrap_failed",
                    "Local Backend 模板列表混合了页码与游标合同",
                )
            # Local projections have shipped both a fully annotated numbered
            # response and a compact ``items + has_more`` response.  The
            # request itself is numbered until the server explicitly returns a
            # cursor, so a compact response must keep advancing page numbers.
            response_mode = (
                "cursor"
                if has_cursor_fields
                else "numbered"
            )
            if pagination_mode is not None and response_mode != pagination_mode:
                raise WorkspaceHostError(
                    "backend_authority_bootstrap_failed",
                    "Local Backend 模板列表分页合同发生漂移",
                )
            pagination_mode = response_mode
            if pagination_mode == "numbered":
                returned_page = page.get("page")
                page_size = page.get("page_size")
                if returned_page is not None and (
                    not isinstance(returned_page, int)
                    or isinstance(returned_page, bool)
                    or returned_page != page_number
                ):
                    raise WorkspaceHostError(
                        "backend_authority_bootstrap_failed",
                        "Local Backend 模板列表页码无效",
                    )
                if page_size is not None and (
                    not isinstance(page_size, int)
                    or isinstance(page_size, bool)
                    or page_size <= 0
                ):
                    raise WorkspaceHostError(
                        "backend_authority_bootstrap_failed",
                        "Local Backend 模板列表页大小无效",
                    )
                page_number += 1
                continue

            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor == cursor
            ):
                raise WorkspaceHostError(
                    "backend_authority_bootstrap_failed",
                    "Local Backend 模板列表游标无效",
                )
            cursor = next_cursor

    def _source_template(self, item: Mapping[str, Any]) -> Mapping[str, Any]:
        template_uuid = str(item.get("uuid") or "").strip()
        if not template_uuid:
            raise WorkspaceHostError(
                "backend_authority_bootstrap_failed",
                "Local Backend 资源模板缺少 UUID",
            )
        response = self.session.get(
            f"{self.source_api}/resource-templates/{template_uuid}",
            timeout=self.timeout,
        )
        return _response_data(response, "Local Backend 模板详情")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.credential}",
            "Content-Type": "application/json",
        }


def _template_definition(template: Mapping[str, Any]) -> dict[str, Any]:
    """Convert the public Backend DTO back into the registry sync contract."""

    name = str(template.get("name") or "").strip()
    if not name:
        raise WorkspaceHostError(
            "backend_authority_bootstrap_failed",
            "Local Backend 资源模板缺少业务身份",
        )
    meta_data = template.get("meta_data")
    unilab_metadata = (
        meta_data.get("unilab")
        if isinstance(meta_data, Mapping)
        and isinstance(meta_data.get("unilab"), Mapping)
        else {}
    )
    handles = []
    for raw_handle in template.get("handles") or []:
        if not isinstance(raw_handle, Mapping):
            continue
        handle = {
            key: raw_handle.get(key)
            for key in (
                "name",
                "display_name",
                "type",
                "io_type",
                "source",
                "key",
                "side",
            )
            if raw_handle.get(key) is not None
        }
        handles.append(handle)
    return {
        "id": name,
        "display_name": template.get("display_name") or name,
        "description": template.get("description"),
        "registry_type": template.get("resource_type") or "resource",
        "icon": template.get("icon"),
        "model": template.get("model") or {},
        "class": {
            "module": template.get("module"),
            "type": template.get("language") or "python",
        },
        "category": template.get("tags") or [],
        "init_param_schema": {
            "data": {"properties": template.get("data_schema") or {}},
            "config": {"properties": template.get("config_schema") or {}},
        },
        "config_info": template.get("config_info") or [],
        "available_sites": template.get("available_sites") or [],
        "cover": template.get("cover"),
        "scene": template.get("scene") or [],
        "device_params": template.get("device_params") or {},
        "handles": handles,
        "source_uri": unilab_metadata.get("source_uri"),
    }


def _response_data(response: Any, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except (TypeError, ValueError) as error:
        raise WorkspaceHostError(
            "backend_authority_bootstrap_failed",
            f"{operation}返回非 JSON 响应",
        ) from error
    if not 200 <= int(response.status_code) < 300 or not isinstance(payload, Mapping):
        raise WorkspaceHostError(
            "backend_authority_bootstrap_failed",
            f"{operation}失败：HTTP {response.status_code}",
        )
    if int(payload.get("code") or 0) != 0:
        raise WorkspaceHostError(
            "backend_authority_bootstrap_failed",
            f"{operation}失败：{payload.get('error')}",
        )
    data = payload.get("data", payload)
    if not isinstance(data, Mapping):
        raise WorkspaceHostError(
            "backend_authority_bootstrap_failed",
            f"{operation}返回数据结构无效",
        )
    return dict(data)


def _api_base(address: str) -> str:
    base = str(address or "").strip().rstrip("/")
    if not base:
        raise WorkspaceHostError(
            "backend_authority_bootstrap_failed", "Backend 服务地址缺失"
        )
    return base if base.endswith("/api/v1") else f"{base}/api/v1"
