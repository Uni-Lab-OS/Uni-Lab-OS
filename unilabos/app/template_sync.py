"""把 Edge Registry 作为一个完整模板图同步到正式后端。"""

from __future__ import annotations

import gzip
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import requests

from unilabos.registry.template_snapshot import (
    RegistryTemplateSnapshot,
    RegistryTemplateSnapshotError,
)
from unilabos.utils.tracing import inject_trace_context, span

DEVELOPER_TOKEN_ENV = "UNILAB_TEMPLATE_SYNC_DEVELOPER_TOKEN"


class TemplateSyncError(RuntimeError):
    """模板收集或后端事务同步失败。"""


@dataclass(frozen=True)
class TemplateSyncReport:
    """一次完整同步的稳定结果，供初始化 Job 和测试流程消费。"""

    device_count: int
    resource_count: int
    template_uuids: dict[str, str]


class TemplateSyncWorkspaceRuntime(Protocol):
    """模板同步消费的最小工作区候选代 Interface。"""

    material_shapes_by_template: Mapping[str, Mapping[str, Any]]

    def publish(self, registry: Any) -> None:
        """把同代包模板原子发布到活动注册表（Registry）。

        参数：``registry`` 是本次模板同步唯一活动注册表。返回：无。
        异常：候选冲突或发布失败时传播原异常，由同步命令统一映射。
        """


class TemplateSynchronizer:
    """隐藏 Registry 遍历、协议映射、压缩和 HTTP 提交细节。"""

    def __init__(
        self,
        backend_address: str,
        developer_token: str,
        *,
        session: requests.Session | None = None,
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
        material_shapes_by_template: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> TemplateSyncReport:
        """通过一个不可变 Registry 快照执行一次 Backend 事务同步。

        参数说明：``registry`` 可以是已编译的 ``RegistryTemplateSnapshot``，也可
        以兼容方式传入 Registry；后者只遍历一次并立即冻结；
        ``material_shapes_by_template`` 按模板业务身份提供同代编译后的 2.5D
        外形。返回同步报告；外形结构或目标模板无效时抛出
        ``TemplateSyncError``，且不会发送部分模板事务。
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
        _embed_material_shapes(
            definitions,
            material_shapes_by_template or {},
        )

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
                "template.device.count": len(devices),
                "template.resource.count": len(resources),
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
            except Exception:  # noqa: BLE001,S110 - tracing must remain fail-open
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
        return TemplateSyncReport(
            device_count=len(devices),
            resource_count=len(resources),
            template_uuids=template_uuids,
        )


def _embed_material_shapes(
    definitions: list[dict[str, Any]],
    material_shapes_by_template: Mapping[str, Mapping[str, Any]],
) -> None:
    """把完整 2.5D 外形嵌入本次上传模板的 ``model.shape``。

    参数说明：``definitions`` 是从不可变注册表（Registry）快照分离出的本次
    上传模板；``material_shapes_by_template`` 以模板业务身份关联包目录
    （PackageCatalog）同代编译结果。返回：无，成功时仅修改分离模板副本；外形
    不是严格 JSON、协议版本错误、图元为空或目标模板不存在时抛出
    ``TemplateSyncError``，禁止把损坏绑定或部分候选发送给 Backend。
    """

    if not isinstance(material_shapes_by_template, Mapping):
        raise TemplateSyncError("material shape bindings must be an object")
    # ``definitions_by_name`` 是本次完整模板事务唯一可写副本索引；外形只能按
    # 稳定模板业务身份精确绑定，禁止按 category 或外形名称猜测所有者。
    definitions_by_name = {
        str(definition.get("id") or ""): definition for definition in definitions
    }
    for template_name, raw_shape in material_shapes_by_template.items():
        if not isinstance(template_name, str) or not template_name.strip():
            raise TemplateSyncError("material shape template identity is invalid")
        definition = definitions_by_name.get(template_name)
        if definition is None:
            raise TemplateSyncError(
                f"material shape target template is missing: {template_name}"
            )
        if not isinstance(raw_shape, Mapping):
            raise TemplateSyncError(
                f"material shape must be an object: {template_name}"
            )
        try:
            # ``shape`` 通过严格 JSON 往返与运行时代容器隔离；设备包原有
            # ``entry`` 由下方注册表绑定合并保留，不由编译结果重复构造。
            shape = json.loads(
                json.dumps(
                    dict(raw_shape),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            )
        except (TypeError, ValueError) as error:
            raise TemplateSyncError(
                f"material shape is not strict JSON: {template_name}"
            ) from error
        if shape.get("schema_version") != "unilab.shape/v1":
            raise TemplateSyncError(
                f"material shape schema version is invalid: {template_name}"
            )
        if not isinstance(shape.get("parts"), list) or not shape["parts"]:
            raise TemplateSyncError(
                f"material shape parts are required: {template_name}"
            )
        # ``model`` 保留 Xacro/URDF 等模板模型字段；``shape_binding`` 继续保留
        # 设备包既有的 format/entry 等兼容绑定，完整编译结果只补齐云端可直接
        # 使用的公共外形字段，避免新增 2.5D 同步破坏旧消费者。
        model = definition.get("model")
        if not isinstance(model, dict):
            model = {}
        else:
            model = dict(model)
        raw_shape_binding = model.get("shape")
        shape_binding = (
            dict(raw_shape_binding)
            if isinstance(raw_shape_binding, Mapping)
            else {}
        )
        shape_binding.update(shape)
        model["shape"] = shape_binding
        definition["model"] = model


def sync_registry_from_environment(
    registry: Any,
    backend_address: str,
    *,
    session: requests.Session | None = None,
) -> TemplateSyncReport:
    """使用初始化 Job 注入的开发者身份同步一个注册表（Registry）。

    参数：``registry`` 是待同步的活动注册表；``backend_address`` 是 Backend
    地址；``session`` 是可选 HTTP Adapter。返回模板同步报告；环境缺少凭据、
    模板合同或 HTTP 事务失败时抛出 ``TemplateSyncError``。
    """

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
    environment: Mapping[str, str] | None = None,
    registry_builder: Callable[..., Any] | None = None,
    session: requests.Session | None = None,
    workspace_runtime: TemplateSyncWorkspaceRuntime | None = None,
) -> TemplateSyncReport:
    """执行独立模板初始化命令，不进入设备图和驱动启动流程。

    参数说明：``arguments`` 是公共命令行参数；``backend_address`` 是 Backend
    地址；``environment`` 提供开发者凭据；``registry_builder`` 与 ``session`` 是
    可测试 Adapter；``workspace_runtime`` 是可选的同代工作区注册表运行时，负责
    发布包模板并提供 2.5D 外形精确绑定。返回模板同步报告；工作区发布、外形
    合同或 HTTP 事务失败时抛出 ``TemplateSyncError``。
    """

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
    # ``material_shapes_by_template`` 默认保持遗留 Registry 模板同步行为；启用
    # Workspace 时则必须先把已经编译的完整包代发布到同一个活动 Registry。
    material_shapes_by_template: Mapping[str, Mapping[str, Any]] = {}
    if workspace_runtime is not None:
        publish = getattr(workspace_runtime, "publish", None)
        raw_shape_bindings = getattr(
            workspace_runtime,
            "material_shapes_by_template",
            None,
        )
        if not callable(publish) or not isinstance(raw_shape_bindings, Mapping):
            raise TemplateSyncError("workspace runtime shape generation is invalid")
        try:
            publish(registry)
        except Exception as error:
            raise TemplateSyncError(
                f"workspace registry publication failed: {error}"
            ) from error
        material_shapes_by_template = raw_shape_bindings
    token_source = environment if environment is not None else os.environ
    developer_token = token_source.get(DEVELOPER_TOKEN_ENV, "")
    return TemplateSynchronizer(
        backend_address,
        developer_token,
        session=session,
    ).sync(
        registry,
        material_shapes_by_template=material_shapes_by_template,
    )


def _decode_sync_response(response: Any) -> dict[str, Any]:
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
    if not isinstance(result, Mapping) or not isinstance(result.get("templates"), list):
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
