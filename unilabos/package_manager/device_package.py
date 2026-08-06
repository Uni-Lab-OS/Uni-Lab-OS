"""云端设备模板到本地受管设备包的稳定下载 Interface。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from .catalog import DefinitionRecord, PackageCatalog, PackageCompileError
from .community import (
    CommunityDownloadPort,
    CommunityPackageError,
    acquire_community_package,
)

_DEFINITION_FQID = re.compile(
    r"^(community\.[a-z_][a-z0-9_]*)\.([A-Za-z0-9_]+)$"
)
_SECRET_PARAMETER = re.compile(
    r"(^|_)(password|passwd|secret|token|api_key|access_key|private_key|sk)($|_)",
    re.IGNORECASE,
)
_DYNAMIC_DEFAULT_KEYS = frozenset({"$ast", "$call", "$name"})


class DevicePackageError(RuntimeError):
    """设备包无法形成可供本地接入使用的可信描述。"""


@dataclass(frozen=True)
class DevicePackageDownloadResult:
    """设备包进入受管缓存后的稳定 CLI 投影。"""

    cache_key: str
    cache_hit: bool
    distribution: str
    version: str
    namespace: str
    definition_fqid: str
    catalog_digest: str
    configuration_schema: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """返回不暴露本地绝对路径的 JSON 安全结果。"""

        return {
            "status": "package_cached",
            "cache_key": self.cache_key,
            "cache_hit": self.cache_hit,
            "distribution": self.distribution,
            "version": self.version,
            "namespace": self.namespace,
            "definition_fqid": self.definition_fqid,
            "catalog_digest": self.catalog_digest,
            "configuration_schema": dict(self.configuration_schema),
        }


def download_device_package(
    *,
    template_uuid: str,
    definition_fqid: str,
    artifact_digest: str,
    backend_base_url: str,
    working_dir: str,
    port: CommunityDownloadPort,
) -> DevicePackageDownloadResult:
    """下载云端模板对应 wheel，并返回目标设备的配置合同。

    此操作只写受管 community package 缓存，不写设备图、不安装实例，也不
    启动驱动。模板 UUID 仅用于构造既有公开 302 路由；Artifact 摘要、Catalog
    namespace 和目标 definition 必须全部一致，否则失败关闭。
    """

    namespace = _namespace_from_definition(definition_fqid)
    download_url = _release_download_url(backend_base_url, template_uuid)
    try:
        acquisition = acquire_community_package(
            namespace=namespace,
            artifact_digest=artifact_digest,
            download_url=download_url,
            working_dir=working_dir,
            port=port,
            catalog_validator=lambda catalog: configuration_schema_for_definition(
                device_definition_from_catalog(
                    catalog,
                    definition_fqid,
                )
            ),
        )
    except (
        CommunityPackageError,
        PackageCompileError,
        ValueError,
        OSError,
    ) as exc:
        raise DevicePackageError(str(exc)) from exc

    definition = device_definition_from_catalog(
        acquisition.catalog,
        definition_fqid,
    )
    schema = configuration_schema_for_definition(definition)
    catalog = acquisition.catalog
    return DevicePackageDownloadResult(
        cache_key=(
            f"{catalog.namespace}@{catalog.distribution.version}#{artifact_digest}"
        ),
        cache_hit=acquisition.cache_hit,
        distribution=catalog.distribution.name,
        version=catalog.distribution.version,
        namespace=catalog.namespace,
        definition_fqid=definition.fqid,
        catalog_digest=catalog.catalog_digest,
        configuration_schema=schema,
    )


def configuration_schema_for_definition(
    definition: DefinitionRecord,
) -> dict[str, Any]:
    """把 Catalog 初始化参数投影为 Electron 可渲染的固定 JSON Schema。

    参数 ``definition`` 是已验证 PackageCatalog 中的目标设备定义。返回封闭的
    object Schema；初始化参数结构、秘密类型或默认值无法安全投影时抛出
    :class:`DevicePackageError`。

    疑似秘密参数只允许是字符串，并投影为写入专用字段；它们不得携带 Catalog
    默认值。未知 Python 注解保留为 object 并附带原注解，调用方不得据此猜测
    字符串输入。
    """

    raw_parameters = definition.details.get("init_parameters", ())
    if not isinstance(raw_parameters, (list, tuple)):
        raise DevicePackageError(
            f"设备 definition 的 init_parameters 无效: {definition.fqid}"
        )
    properties: dict[str, Any] = {}
    required: list[str] = []
    for raw in raw_parameters:
        if not isinstance(raw, Mapping):
            raise DevicePackageError(
                f"设备 definition 包含无效初始化参数: {definition.fqid}"
            )
        name = str(raw.get("name") or "")
        if not name or name in properties:
            raise DevicePackageError(
                f"设备 definition 包含空或重复初始化参数: {definition.fqid}"
            )
        property_schema = _parameter_schema(str(raw.get("type") or "Any"))
        is_secret = _SECRET_PARAMETER.search(name) is not None
        if is_secret and property_schema.get("type") != "string":
            raise DevicePackageError(f"设备秘密参数 {name} 必须声明为 str")
        if is_secret:
            property_schema["writeOnly"] = True
            property_schema["x-unilab-secret"] = True
        elif "default" in raw and not _contains_dynamic_default(raw["default"]):
            property_schema["default"] = _plain_json(raw["default"])
        properties[name] = property_schema
        if bool(raw.get("required")):
            required.append(name)
    return {
        "type": "object",
        "required": required,
        "properties": properties,
        "additionalProperties": False,
    }


def _namespace_from_definition(definition_fqid: str) -> str:
    """从完整设备 definition 身份提取 community namespace。"""

    match = _DEFINITION_FQID.fullmatch(definition_fqid)
    if match is None:
        raise DevicePackageError(f"设备 definition FQID 无效: {definition_fqid}")
    return match.group(1)


def _release_download_url(backend_base_url: str, template_uuid: str) -> str:
    """基于现有 Backend API 根地址构造公开 302 下载路由。"""

    try:
        canonical_uuid = str(UUID(template_uuid))
    except ValueError as exc:
        raise DevicePackageError(f"template UUID 无效: {template_uuid}") from exc
    parsed = urlsplit(backend_base_url.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise DevicePackageError(
            "Backend base URL 必须是无凭据、query 和 fragment 的 HTTP(S) 地址"
        )
    base_path = parsed.path.rstrip("/")
    path = (
        f"{base_path}/lab/square/packages/releases/"
        f"{canonical_uuid}/download"
    )
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def device_definition_from_catalog(
    catalog: PackageCatalog,
    definition_fqid: str,
) -> DefinitionRecord:
    """在可信 Catalog 中唯一选择调用方指定的设备 definition。

    参数 ``catalog`` 是已经过 Artifact/Catalog 校验的包目录；参数
    ``definition_fqid`` 是云端详情解析出的规范设备身份。返回唯一匹配的设备
    definition；不存在或不唯一时抛出 :class:`DevicePackageError` 并失败关闭。
    """

    matches = [
        definition
        for definition in catalog.definitions.devices
        if definition.fqid == definition_fqid
    ]
    if len(matches) != 1:
        raise DevicePackageError(
            f"目标设备 definition 在 PackageCatalog 中不存在或不唯一: {definition_fqid}"
        )
    return matches[0]


def validate_configuration_for_definition(
    definition: DefinitionRecord,
    configuration: Mapping[str, Any],
) -> dict[str, Any]:
    """按设备 definition 的固定配置 Schema 校验并补齐静态默认值。

    参数 ``definition`` 是目标设备定义，参数 ``configuration`` 是用户通过封闭
    stdin 提交的一次性初始化参数。返回完成类型校验的普通 JSON 对象；返回值仍
    可能含短生命周期秘密，调用方必须在写图前转换为秘密引用。未知字段、缺失
    必填字段或 JSON 类型不匹配时抛出 :class:`DevicePackageError`。该函数不做
    字符串到数字等隐式转换，避免配置含义随调用端改变。
    """

    if not isinstance(configuration, Mapping):
        raise DevicePackageError("设备 configuration 必须是 JSON object")
    schema = configuration_schema_for_definition(definition)
    properties = schema["properties"]
    unknown = sorted(set(str(key) for key in configuration) - set(properties))
    if unknown:
        raise DevicePackageError(f"设备 configuration 包含未知参数: {', '.join(unknown)}")
    result: dict[str, Any] = {}
    required = set(schema["required"])
    for name, property_schema in properties.items():
        if name in configuration:
            value = configuration[name]
            _validate_configuration_value(name, value, property_schema)
            result[name] = _plain_json(value)
        elif "default" in property_schema:
            result[name] = _plain_json(property_schema["default"])
        elif name in required:
            raise DevicePackageError(f"设备 configuration 缺少必填参数: {name}")
    return result


def _validate_configuration_value(
    name: str,
    value: Any,
    schema: Mapping[str, Any],
) -> None:
    """校验一个配置值的 JSON 类型，不执行任何隐式类型转换。

    参数 ``name`` 是 PackageCatalog 字段身份，``value`` 是用户值，``schema``
    是该字段的冻结 Schema。函数无返回值；类型不匹配时抛出
    :class:`DevicePackageError`。
    """

    expected = schema.get("type")
    valid = False
    if expected == "string":
        valid = isinstance(value, str)
    elif expected == "boolean":
        valid = isinstance(value, bool)
    elif expected == "integer":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif expected == "number":
        valid = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )
    elif expected == "array":
        valid = isinstance(value, list)
    elif expected == "object":
        valid = isinstance(value, Mapping)
    if not valid:
        raise DevicePackageError(
            f"设备 configuration 参数 {name} 必须是 {expected}"
        )


def _parameter_schema(type_name: str) -> dict[str, Any]:
    """将常见 Python 类型注解映射成稳定 JSON Schema 属性。"""

    normalized = type_name.replace(" ", "")
    normalized = normalized.removeprefix("builtins.")
    if normalized.startswith("Optional[") and normalized.endswith("]"):
        normalized = normalized[9:-1]
    if normalized.endswith("|None"):
        normalized = normalized[:-5]
    direct = {
        "bool": "boolean",
        "float": "number",
        "int": "integer",
        "str": "string",
    }.get(normalized)
    if direct is not None:
        return {"type": direct}
    if normalized.startswith(("list[", "List[", "tuple[", "Tuple[", "set[", "Set[")):
        return {"type": "array", "x-unilab-python-type": type_name}
    if normalized.startswith(("dict[", "Dict[", "Mapping[")):
        return {"type": "object", "x-unilab-python-type": type_name}
    return {"type": "object", "x-unilab-python-type": type_name}


def _contains_dynamic_default(value: Any) -> bool:
    """识别编译器为无法静态求值默认值保留的 AST 标记。"""

    if isinstance(value, Mapping):
        if _DYNAMIC_DEFAULT_KEYS.intersection(str(key) for key in value):
            return True
        return any(_contains_dynamic_default(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_dynamic_default(item) for item in value)
    return False


def _plain_json(value: Any) -> Any:
    """把 Catalog 的只读 Mapping/tuple 递归转换为普通 JSON 容器。"""

    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


__all__ = [
    "DevicePackageDownloadResult",
    "DevicePackageError",
    "configuration_schema_for_definition",
    "device_definition_from_catalog",
    "download_device_package",
    "validate_configuration_for_definition",
]
