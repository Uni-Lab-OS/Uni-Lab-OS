"""把资源图、ROS 在线事实与注册表合同投影为统一设备目录。"""

from __future__ import annotations

import time
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

DeviceMaterialIdentityResolver = Callable[[str], Mapping[str, Any] | None]

_INTERNAL_ACTIONS = {
    "_execute_driver_command",
    "_execute_driver_command_async",
}
_JSON_TYPES = {
    "bool": "boolean",
    "boolean": "boolean",
    "dict": "object",
    "float": "number",
    "int": "integer",
    "integer": "integer",
    "list": "array",
    "number": "number",
    "object": "object",
    "str": "string",
    "string": "string",
}
_DEFINITION_FQID = re.compile(r"^community\.[a-z_][a-z0-9_]*\.[A-Za-z0-9_]+$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def project_device_catalog(
    *,
    resources: Any,
    registry_devices: Iterable[Mapping[str, Any]],
    online_devices: Mapping[str, Any],
    material_identity_resolver: DeviceMaterialIdentityResolver,
    generated_at: float | None = None,
) -> dict[str, Any]:
    """生成前端与 OS 共享的设备目录（Device Catalog）。

    参数说明：``resources`` 是 Host 持有的资源树集合，``registry_devices`` 是
    注册表设备类型合同，``online_devices`` 是 ROS 图当前在线实例；
    ``material_identity_resolver`` 按部署设备 ID 从库存权威（Inventory Authority）
    解析稳定设备物料（Material）身份；``generated_at`` 仅供确定性测试覆盖。
    返回按实例 ID 排序的目录，非设备资源不进入结果；未解析到稳定物料身份时保留
    空 ``materialUuid``，禁止把资源树运行时 UUID 冒充稳定身份。
    """

    registry = {
        str(item.get("id") or ""): item
        for item in registry_devices
        if isinstance(item, Mapping) and str(item.get("id") or "")
    }
    items = []
    for raw in _resource_nodes(resources):
        if str(raw.get("type") or "") != "device":
            continue
        device_id = str(raw.get("id") or "").strip()
        if not device_id:
            continue
        device_type_id = str(raw.get("class") or "").strip()
        online = online_devices.get(device_id)
        online_fact = online if isinstance(online, Mapping) else {}
        definition = registry.get(device_type_id, {})
        material_identity = material_identity_resolver(device_id)
        material_uuid = (
            str(material_identity.get("uuid") or "")
            if isinstance(material_identity, Mapping)
            else ""
        )
        items.append(
            {
                "id": device_id,
                "materialUuid": material_uuid,
                "definition": _definition_reference(definition, device_type_id),
                "deviceTypeId": device_type_id,
                "deviceKey": str(
                    online_fact.get("device_key")
                    or f"/devices/{device_id}/{device_id}"
                ),
                "namespace": str(
                    online_fact.get("namespace") or f"/devices/{device_id}"
                ),
                "name": str(raw.get("name") or device_id),
                "online": bool(online_fact),
                "stateSchema": _state_schema(definition),
                "actions": _actions(device_id, definition),
            }
        )
    return {
        "schemaVersion": "device-catalog/v2",
        "source": "edge",
        "generatedAt": time.time() if generated_at is None else generated_at,
        "items": sorted(items, key=lambda item: item["id"]),
    }


def _resource_nodes(resources: Any) -> list[dict[str, Any]]:
    """把资源树节点安全转换为普通字典。

    参数：``resources`` 是 Host 资源树组合根。
    返回：可被目录投影消费的节点字典副本。
    异常：无；不符合资源节点协议的成员被忽略。
    """

    nodes = getattr(resources, "all_nodes", None)
    if not isinstance(nodes, list):
        return []
    result = []
    for node in nodes:
        content = getattr(node, "res_content", None)
        dump = getattr(content, "model_dump", None)
        if not callable(dump):
            continue
        value = dump(by_alias=True)
        if isinstance(value, Mapping):
            result.append(dict(value))
    return result


def _device_class(definition: Mapping[str, Any]) -> Mapping[str, Any]:
    """读取注册表设备实现合同。

    参数：``definition`` 是注册表设备定义条目。
    返回：class 合同映射，缺失或类型错误时返回空映射。
    异常：无。
    """

    value = definition.get("class")
    return value if isinstance(value, Mapping) else {}


def _state_schema(definition: Mapping[str, Any]) -> dict[str, Any]:
    """投影设备 Driver 正式状态合同。

    参数：``definition`` 是注册表设备定义条目。
    返回：带来源和解析状态的前端状态 Schema。
    异常：无；没有正式状态声明时返回空字典。
    """

    values = _device_class(definition).get("status_types")
    if not isinstance(values, Mapping):
        return {}
    return {
        str(name): {
            "type": _json_type(value),
            "source": "driver",
            "status": "resolved",
        }
        for name, value in values.items()
        if str(name)
    }


def _json_type(value: Any) -> str:
    """把 Python 或 ROS 类型名映射为 JSON Schema 类型。

    参数：``value`` 是类型对象或静态类型名。
    返回：受支持的 JSON 类型；未知类型安全降级为 string。
    异常：无。
    """

    name = str(getattr(value, "__name__", value)).strip().lower()
    return _JSON_TYPES.get(name, "string")


def _actions(
    device_id: str,
    definition: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """投影当前设备定义的公开动作合同。

    参数：``device_id`` 是 Graph 实例身份；``definition`` 是注册表设备定义。
    返回：过滤内部命令后的稳定动作合同列表。
    异常：无；缺少正式动作映射时返回空列表。
    """

    mappings = _device_class(definition).get("action_value_mappings")
    if not isinstance(mappings, Mapping):
        return []
    result = []
    for name, raw in mappings.items():
        action_name = str(name).strip()
        if not action_name or action_name in _INTERNAL_ACTIONS:
            continue
        action = raw if isinstance(raw, Mapping) else {}
        schema = action.get("schema")
        schema_value = schema if isinstance(schema, Mapping) else {}
        properties = schema_value.get("properties")
        contracts = properties if isinstance(properties, Mapping) else {}
        result.append(
            {
                "id": action_name,
                "actionRef": f"{device_id}.{action_name}",
                "name": str(
                    action.get("display_name")
                    or action.get("displayname")
                    or action_name
                ),
                "typeName": str(
                    getattr(action.get("type"), "__name__", action.get("type"))
                    or f"{device_id}.{action_name}"
                ),
                "riskLevel": str(action.get("risk_level") or "normal"),
                "inputSchema": _contract(contracts.get("goal")),
                "outputSchema": _contract(contracts.get("result")),
                "busy": False,
                "currentJobId": None,
            }
        )
    return result


def _contract(value: Any) -> dict[str, Any]:
    """复制一个动作输入或输出 JSON Schema。

    参数：``value`` 是注册表动作合同值。
    返回：普通字典副本；非映射返回空合同。
    异常：无。
    """

    return dict(value) if isinstance(value, Mapping) else {}


def _definition_reference(
    registry_entry: Mapping[str, Any],
    device_type_id: str,
) -> dict[str, Any] | None:
    """把注册表包证据投影为 Core #147 设备定义引用。

    参数：``registry_entry`` 是 PackageCatalog 发布的设备定义条目；
    ``device_type_id`` 是物理图（Graph）当前实例声明的定义 FQID。
    返回：身份和摘要自洽的前端定义引用；遗留或不完整条目返回 ``None``。
    异常：无；边界数据错误时关闭式隐藏定义，禁止猜测软件包来源。
    """

    raw_definition = registry_entry.get("package_definition")
    raw_catalog = registry_entry.get("package_catalog")
    if not isinstance(raw_definition, Mapping) or not isinstance(
        raw_catalog,
        Mapping,
    ):
        return None
    fqid = str(raw_definition.get("fqid") or "")
    namespace = str(raw_catalog.get("namespace") or "")
    import_package = str(raw_catalog.get("import_package") or "")
    content_hash = str(raw_definition.get("content_hash") or "")
    content_digest = str(raw_catalog.get("content_digest") or "")
    catalog_digest = str(raw_catalog.get("catalog_digest") or "")
    distribution = raw_catalog.get("distribution")
    if (
        fqid != device_type_id
        or not _DEFINITION_FQID.fullmatch(fqid)
        or namespace != f"community.{import_package}"
        or not fqid.startswith(f"{namespace}.")
        or not _DIGEST.fullmatch(content_hash)
        or not _DIGEST.fullmatch(content_digest)
        or not _DIGEST.fullmatch(catalog_digest)
        or not isinstance(distribution, Mapping)
    ):
        return None
    normalized_name = str(distribution.get("normalized_name") or "")
    if normalized_name != import_package:
        return None
    metadata = registry_entry.get("metadata")
    metadata_value = metadata if isinstance(metadata, Mapping) else {}
    category = registry_entry.get("category")
    category_value = category if isinstance(category, (list, tuple)) else ()
    return {
        "fqid": fqid,
        "version": str(raw_definition.get("version") or ""),
        "contentHash": content_hash,
        "sourceIdentity": str(raw_definition.get("source_identity") or ""),
        "title": str(raw_definition.get("title") or fqid),
        "description": str(raw_definition.get("description") or ""),
        "category": [str(item) for item in category_value],
        "manufacturer": str(
            registry_entry.get("manufacturer")
            or metadata_value.get("manufacturer")
            or metadata_value.get("vendor")
            or ""
        ),
        "packageCatalog": {
            "schemaVersion": str(raw_catalog.get("schema_version") or ""),
            "distribution": {
                "name": str(distribution.get("name") or ""),
                "normalizedName": normalized_name,
                "version": str(distribution.get("version") or ""),
            },
            "importPackage": import_package,
            "namespace": namespace,
            "contentDigest": content_digest,
            "catalogDigest": catalog_digest,
        },
    }


__all__ = ["DeviceMaterialIdentityResolver", "project_device_catalog"]
