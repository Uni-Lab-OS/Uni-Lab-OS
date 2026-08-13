"""把 Edge Registry 作为一个完整模板图同步到正式后端。"""

from __future__ import annotations

import gzip
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional
from urllib.parse import urlsplit

import requests

from unilabos.registry.template_snapshot import (
    RegistryTemplateSnapshot,
    RegistryTemplateSnapshotError,
)
from unilabos.utils.tracing import inject_trace_context, span


DEVELOPER_TOKEN_ENV = "UNILAB_TEMPLATE_SYNC_DEVELOPER_TOKEN"
logger = logging.getLogger(__name__)


class TemplateSyncError(RuntimeError):
    """模板收集或后端事务同步失败。"""


@dataclass(frozen=True)
class TemplateSyncReport:
    """一次完整同步的稳定结果，供初始化 Job 和测试流程消费。"""

    device_count: int
    resource_count: int
    template_uuids: Dict[str, str]


class TemplateSynchronizer:
    """隐藏 Registry 遍历、协议映射、压缩和 HTTP 提交细节。"""

    def __init__(
        self,
        backend_address: str,
        developer_token: str,
        *,
        session: Optional[requests.Session] = None,
        timeout: float = 60.0,
    ) -> None:
        """创建模板同步器并固定一次同步所需的 Backend 连接参数。

        参数说明：``backend_address`` 是 Backend 服务地址；``developer_token``
        是初始化任务使用的开发者凭据；``session`` 是可替换的 HTTP 适配器；
        ``timeout`` 是单次同步请求的秒级超时。无返回值；凭据缺失时抛出
        ``TemplateSyncError``，避免发送无权写入的模板事务。
        """

        token = str(developer_token or "").strip()
        if not token:
            raise TemplateSyncError("developer token is required")
        self.backend_api = _api_base(backend_address)
        self.developer_token = token
        self.session = session or requests.Session()
        self.timeout = timeout

    def sync(
        self,
        registry: Any,
        *,
        deployment_graph: Optional[Mapping[str, Any]] = None,
    ) -> TemplateSyncReport:
        """通过一个不可变 Registry 快照执行确定性的 Backend 模板同步。

        参数说明：``registry`` 可以是已编译的 ``RegistryTemplateSnapshot``，也可
        以兼容方式传入 Registry；后者只遍历一次并立即冻结。返回同步报告。
        """

        try:
            # ``registry_snapshot`` 是本次同步唯一的不可变设备注册表定义代际；
            # 后续设备模板和资源模板都从它分离复制，禁止再次遍历活 Registry。
            registry_snapshot = (
                registry
                if isinstance(registry, RegistryTemplateSnapshot)
                else RegistryTemplateSnapshot.from_registry(registry)
            )
        except RegistryTemplateSnapshotError as error:
            raise TemplateSyncError(str(error)) from error
        # ``devices`` 与 ``resources`` 是发送给 Backend 的相同快照副本，
        # ``definitions`` 保留完整事务成员集合，供身份回执做全量核对。
        devices = registry_snapshot.detached_devices()
        resources = registry_snapshot.detached_resources()
        definitions = [*devices, *resources]
        if not definitions:
            raise TemplateSyncError("Edge Registry does not contain any templates")
        if deployment_graph is not None:
            # 首次事务先取得 Backend 稳定模板身份；库位准入必须引用 UUID，不能把
            # 部署图里的资源类名伪装成 Material.type。
            template_uuids = self._upload_definitions(
                definitions,
                device_count=len(devices),
                resource_count=len(resources),
            )
            _project_graph_available_sites(definitions, deployment_graph)
            _resolve_available_site_template_identities(definitions, template_uuids)
        template_uuids = self._upload_definitions(
            definitions,
            device_count=len(devices),
            resource_count=len(resources),
        )

        return TemplateSyncReport(
            device_count=len(devices),
            resource_count=len(resources),
            template_uuids=template_uuids,
        )

    def _upload_definitions(
        self,
        definitions: list[dict[str, Any]],
        *,
        device_count: int,
        resource_count: int,
    ) -> Dict[str, str]:
        """原子上传一代模板定义，并严格核对 Backend 返回的稳定身份全集。"""

        payload = {"resources": definitions}
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.developer_token}",
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        }
        url = f"{self.backend_api}/resource-templates"
        target = urlsplit(url)
        with span(
            "edge.http.template.sync",
            kind="client",
            attributes={
                "http.request.method": "POST",
                "http.route": "/api/v1/resource-templates",
                "server.address": target.hostname or "",
                "template.device.count": device_count,
                "template.resource.count": resource_count,
            },
        ) as request_span:
            inject_trace_context(headers)
            response = self.session.post(
                url,
                data=gzip.compress(encoded),
                headers=headers,
                timeout=self.timeout,
            )
            try:
                request_span.set_attribute(
                    "http.response.status_code", response.status_code
                )
            except Exception:  # noqa: BLE001 - tracing must remain fail-open
                pass

        result = _decode_sync_response(response)
        template_uuids = {
            str(identity["name"]): str(identity["uuid"])
            for identity in result.get("templates", [])
            if isinstance(identity, Mapping)
            and identity.get("name")
            and identity.get("uuid")
        }
        expected_names = {definition["id"] for definition in definitions}
        if set(template_uuids) != expected_names:
            missing = sorted(expected_names - set(template_uuids))
            raise TemplateSyncError(
                f"backend response is missing template identities: {missing}"
            )
        return template_uuids


def sync_registry_from_environment(
    registry: Any,
    backend_address: str,
    *,
    session: Optional[requests.Session] = None,
) -> TemplateSyncReport:
    """使用初始化 Job 注入的开发者身份同步一个 Registry。"""

    developer_token = os.environ.get(DEVELOPER_TOKEN_ENV, "")
    return TemplateSynchronizer(
        backend_address,
        developer_token,
        session=session,
    ).sync(registry)


def run_template_sync_command(
    arguments: Mapping[str, Any],
    *,
    backend_address: str,
    environment: Optional[Mapping[str, str]] = None,
    registry_builder: Optional[Callable[..., Any]] = None,
    session: Optional[requests.Session] = None,
) -> TemplateSyncReport:
    """执行独立初始化命令，不进入设备图和驱动启动流程。"""

    if registry_builder is None:
        from unilabos.registry.registry import build_registry

        registry_builder = build_registry
    registry = registry_builder(
        registry_paths=arguments.get("registry_path"),
        devices_dirs=arguments.get("devices"),
        # 这里必须保持纯静态收集；旧开关会实例化 PyLabRobot 器材来补配置，
        # 会把初始化 Job 意外耦合到驱动和硬件环境。
        upload_registry=False,
        complete_registry=bool(arguments.get("complete_registry", False)),
        external_only=bool(arguments.get("external_devices_only", False)),
    )
    token_source = environment if environment is not None else os.environ
    developer_token = token_source.get(DEVELOPER_TOKEN_ENV, "")
    deployment_graph = _read_deployment_graph(arguments.get("graph"))
    return TemplateSynchronizer(
        backend_address,
        developer_token,
        session=session,
    ).sync(registry, deployment_graph=deployment_graph)


def _read_deployment_graph(graph_path: Any) -> Optional[Mapping[str, Any]]:
    """读取可选部署图，供模板同步提取固定库位（Site）定义。"""

    path = str(graph_path or "").strip()
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as graph_file:
            graph = json.load(graph_file)
    except (OSError, ValueError) as error:
        raise TemplateSyncError(f"cannot read device graph {path}: {error}") from error
    if not isinstance(graph, Mapping):
        raise TemplateSyncError("device graph root must be an object")
    return graph


def _project_graph_available_sites(
    definitions: list[dict[str, Any]],
    graph: Mapping[str, Any],
) -> None:
    """把部署图的固定库位投影到对应资源模板，拒绝同模板多份冲突定义。"""

    raw_nodes = graph.get("nodes")
    if not isinstance(raw_nodes, list):
        raise TemplateSyncError("device graph nodes are required")
    definitions_by_name = {
        str(definition.get("id") or ""): definition for definition in definitions
    }
    projected_by_template: dict[str, list[dict[str, Any]]] = {}
    instance_specific_templates: set[str] = set()
    for raw_node in raw_nodes:
        if not isinstance(raw_node, Mapping):
            raise TemplateSyncError("device graph node must be an object")
        config = raw_node.get("config")
        raw_sites = config.get("sites") if isinstance(config, Mapping) else None
        if raw_sites is None:
            continue
        if not isinstance(raw_sites, list):
            raise TemplateSyncError("device graph config.sites must be an array")
        class_name = str(raw_node.get("class") or "").strip()
        definition = definitions_by_name.get(class_name) or definitions_by_name.get(
            class_name.rsplit(".", 1)[-1]
        )
        if definition is None:
            raise TemplateSyncError(
                f"resource template {class_name} has not been synchronized"
            )
        template_name = str(definition["id"])
        if template_name in instance_specific_templates:
            continue
        sites = [
            _normalize_available_site(raw_site, index)
            for index, raw_site in enumerate(raw_sites)
        ]
        previous = projected_by_template.get(template_name)
        if previous is not None and previous != sites:
            projected_by_template.pop(template_name)
            instance_specific_templates.add(template_name)
            logger.warning(
                "资源模板 %s 的部署图库位随实例变化，不能投影为 available_sites",
                template_name,
            )
            continue
        projected_by_template[template_name] = sites
    for template_name, sites in projected_by_template.items():
        definitions_by_name[template_name]["available_sites"] = sites


def _normalize_available_site(raw_site: Any, index: int) -> dict[str, Any]:
    """规范化一项部署图库位定义，并剥离 occupied_by 等实例状态。"""

    if not isinstance(raw_site, Mapping):
        raise TemplateSyncError(f"device graph site {index} must be an object")
    label = str(raw_site.get("label") or raw_site.get("name") or "").strip()
    if not label:
        raise TemplateSyncError(f"device graph site {index} requires label")
    position = raw_site.get("position")
    size = raw_site.get("size")
    rotation = raw_site.get("rotation")
    position = position if isinstance(position, Mapping) else {}
    size = size if isinstance(size, Mapping) else {}
    rotation = rotation if isinstance(rotation, Mapping) else {}
    content_types = raw_site.get("content_type")
    if not isinstance(content_types, list) or not all(
        isinstance(content_type, str) and content_type.strip()
        for content_type in content_types
    ):
        raise TemplateSyncError(
            f"device graph site {label} content_type must be a string array"
        )
    known_fields = {
        "label",
        "name",
        "position",
        "size",
        "rotation",
        "content_type",
        "visible",
        "occupied_by",
        "parent_link",
        "description",
        "meta_data",
    }
    metadata = dict(raw_site.get("meta_data") or {})
    metadata.update(
        {
            str(key): value
            for key, value in raw_site.items()
            if key not in known_fields
        }
    )
    site = {
        "schema_version": 1,
        "index": index,
        "label": label,
        "visible": bool(raw_site.get("visible", True)),
        "position_x": _site_number(position.get("x"), label, "position.x"),
        "position_y": _site_number(position.get("y"), label, "position.y"),
        "position_z": _site_number(position.get("z"), label, "position.z"),
        "rotation_x": _site_number(rotation.get("x"), label, "rotation.x"),
        "rotation_y": _site_number(rotation.get("y"), label, "rotation.y"),
        "rotation_z": _site_number(rotation.get("z"), label, "rotation.z"),
        "width": _site_number(size.get("width"), label, "size.width"),
        "length": _site_number(
            size.get("length", size.get("height")), label, "size.height"
        ),
        "depth": _site_number(size.get("depth"), label, "size.depth"),
        "content_type": [],
        "_allowed_resource_template_names": [
            content_type.strip() for content_type in content_types
        ],
        "parent_link": str(raw_site.get("parent_link") or "").strip(),
        "meta_data": metadata,
    }
    description = raw_site.get("description")
    if description is not None:
        site["description"] = str(description)
    return site


def _resolve_available_site_template_identities(
    definitions: list[dict[str, Any]],
    template_uuids: Mapping[str, str],
) -> None:
    """把部署图 Site 的资源类白名单解析为 Backend 稳定模板 UUID。"""

    for definition in definitions:
        raw_sites = definition.get("available_sites")
        if not isinstance(raw_sites, list):
            continue
        for site in raw_sites:
            if not isinstance(site, dict):
                raise TemplateSyncError("available_sites member must be an object")
            raw_names = site.pop("_allowed_resource_template_names", [])
            if not isinstance(raw_names, list):
                raise TemplateSyncError(
                    "available site resource template names must be an array"
                )
            allowed_uuids: list[str] = []
            for raw_name in raw_names:
                template_name = str(raw_name).strip()
                template_uuid = template_uuids.get(template_name) or template_uuids.get(
                    template_name.rsplit(".", 1)[-1]
                )
                if not template_uuid:
                    raise TemplateSyncError(
                        f"available site references unknown resource template {template_name}"
                    )
                if template_uuid not in allowed_uuids:
                    allowed_uuids.append(template_uuid)
            site["allowed_resource_template_uuids"] = allowed_uuids


def _site_number(value: Any, label: str, field: str) -> float:
    """把可选库位几何值规范为有限浮点数。"""

    if value is None:
        return 0.0
    if isinstance(value, bool):
        raise TemplateSyncError(f"device graph site {label} {field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise TemplateSyncError(
            f"device graph site {label} {field} must be numeric"
        ) from error
    if number != number or number in (float("inf"), float("-inf")):
        raise TemplateSyncError(f"device graph site {label} {field} must be finite")
    if field.startswith("size.") and number < 0:
        raise TemplateSyncError(
            f"device graph site {label} {field} must not be negative"
        )
    return number


def _decode_sync_response(response: Any) -> Dict[str, Any]:
    """校验 Backend 模板事务响应并返回模板稳定身份映射载荷。

    参数说明：``response`` 是 HTTP 适配器返回的响应对象。返回 ``data`` 中的
    JSON 对象；HTTP、业务错误或模板身份结构不完整时抛出
    ``TemplateSyncError``，禁止调用者接受部分同步结果。
    """

    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise TemplateSyncError(
            f"template sync returned non-JSON HTTP {response.status_code}"
        ) from exc
    if response.status_code < 200 or response.status_code >= 300:
        raise TemplateSyncError(
            f"template sync returned HTTP {response.status_code}: {payload}"
        )
    if not isinstance(payload, Mapping):
        raise TemplateSyncError("template sync returned a non-object response")
    code = int(payload.get("code") or 0)
    if code != 0:
        raise TemplateSyncError(
            f"template sync returned business error {code}: {payload.get('error')}"
        )
    result = payload.get("data", payload)
    if not isinstance(result, Mapping) or not isinstance(
        result.get("templates"), list
    ):
        raise TemplateSyncError("template sync returned invalid template identities")
    return dict(result)


def _api_base(address: str) -> str:
    """把 Backend 地址规范化为唯一的 ``/api/v1`` 接口前缀。

    参数说明：``address`` 是用户或启动配置提供的 Backend 地址。返回去除末尾
    斜杠后的接口根地址；空地址抛出 ``TemplateSyncError``。
    """

    base = str(address or "").strip().rstrip("/")
    if not base:
        raise TemplateSyncError("backend address is required")
    if base.endswith("/api/v1"):
        return base
    return f"{base}/api/v1"
