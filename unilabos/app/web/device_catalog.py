"""把资源图、ROS 在线事实与注册表合同投影为统一设备目录。"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from unilabos.package_manager.package_catalog.registry_snapshot import (
    RegistrySnapshot,
    RegistrySnapshotError,
)

DeviceMaterialIdentityResolver = Callable[[str], Mapping[str, Any] | None]

_DEFINITION_FQID = re.compile(r"^community\.[a-z_][a-z0-9_]*\.[A-Za-z0-9_]+$")
_IMPORT_PACKAGE = re.compile(r"^[a-z_][a-z0-9_]*$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

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


def project_device_catalog(
    *,
    resources: Any,
    registry_devices: Iterable[Mapping[str, Any]],
    online_devices: Mapping[str, Any],
    material_identity_resolver: DeviceMaterialIdentityResolver,
    registry_snapshot: RegistrySnapshot | None = None,
    generated_at: float | None = None,
) -> dict[str, Any]:
    """生成前端与 OS 共享的设备目录（Device Catalog）。

    参数说明：``resources`` 是 Host 持有的资源树集合，``registry_devices`` 是
    注册表设备类型合同，``online_devices`` 是 ROS 图当前在线实例；
    ``material_identity_resolver`` 按部署设备 ID 从库存权威（Inventory Authority）
    解析稳定设备物料（Material）身份；``registry_snapshot`` 是已发布的包目录
    注册表快照，用于投影完整 ``definition`` 来源证据；``generated_at`` 仅供
    确定性测试覆盖。
    返回按实例 ID 排序的目录，非设备资源不进入结果；未解析到稳定物料身份时保留
    空 ``materialUuid``，禁止把资源树运行时 UUID 冒充稳定身份。包托管设备在快照
    能给出完整 PackageCatalog 证据时附带 ``definition``；遗留类型省略该字段，
    禁止输出半截 provenance。
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
        item = {
            "id": device_id,
            "materialUuid": material_uuid,
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
        package_definition = _package_definition_reference(
            device_type_id,
            registry_snapshot,
        )
        if package_definition is not None:
            item["definition"] = package_definition
        items.append(item)
    return {
        "schemaVersion": "device-catalog/v1",
        "source": "edge",
        "generatedAt": time.time() if generated_at is None else generated_at,
        "items": sorted(items, key=lambda item: item["id"]),
    }


def _package_definition_reference(
    device_type_id: str,
    registry_snapshot: RegistrySnapshot | None,
) -> dict[str, Any] | None:
    """从注册表快照投影前端 DeviceDefinitionReference；不完整则省略。

    参数：``device_type_id`` 是图节点声明的设备定义身份；``registry_snapshot``
    是当前已发布的包目录快照。
    返回：字段完整且与所属 PackageCatalog 自洽时返回 camelCase 来源证据；快照
    缺失、定义不存在或证据不完整时返回 ``None``，禁止半截输出。
    异常：无；快照解析失败转为省略，不中断整份设备目录。
    """

    if not isinstance(registry_snapshot, RegistrySnapshot) or not device_type_id:
        return None
    try:
        definition = registry_snapshot.resolve("device", device_type_id)
    except RegistrySnapshotError:
        return None
    catalog = next(
        (
            item
            for item in registry_snapshot.package_catalogs
            if any(device.fqid == definition.fqid for device in item.definitions.devices)
        ),
        None,
    )
    if catalog is None:
        return None
    registry_entry = definition.details.get("registry_entry")
    entry = registry_entry if isinstance(registry_entry, Mapping) else {}
    raw_category = entry.get("category")
    reference = {
        "fqid": definition.fqid,
        "version": definition.version,
        "contentHash": definition.content_hash,
        "sourceIdentity": f"{definition.module}:{definition.symbol}",
        "title": definition.title or definition.id,
        "description": definition.description,
        "category": (
            [str(item) for item in raw_category]
            if isinstance(raw_category, (list, tuple))
            else []
        ),
        "manufacturer": str(entry.get("manufacturer") or ""),
        "packageCatalog": {
            "schemaVersion": catalog.schema_version,
            "distribution": {
                "name": catalog.distribution.name,
                "normalizedName": catalog.distribution.normalized_name,
                "version": catalog.distribution.version,
            },
            "importPackage": catalog.import_package,
            "namespace": catalog.namespace,
            "contentDigest": catalog.content_digest,
            "catalogDigest": catalog.catalog_digest,
        },
    }
    if not _is_device_definition_reference(reference):
        return None
    return reference


def _is_device_definition_reference(value: Mapping[str, Any]) -> bool:
    """校验投影结果是否满足 Core #147 前端 DeviceDefinitionReference 合同。

    参数：``value`` 是即将写入设备目录的 camelCase 定义对象。
    返回：FQID、摘要、源码身份和 PackageCatalog 命名空间自洽时为 True。
    异常：无。
    """

    package_catalog = value.get("packageCatalog")
    if not isinstance(package_catalog, Mapping):
        return False
    distribution = package_catalog.get("distribution")
    if not isinstance(distribution, Mapping):
        return False
    import_package = package_catalog.get("importPackage")
    namespace = package_catalog.get("namespace")
    fqid = value.get("fqid")
    source_identity = value.get("sourceIdentity")
    category = value.get("category")
    return (
        package_catalog.get("schemaVersion") == "1"
        and isinstance(distribution.get("name"), str)
        and bool(distribution.get("name"))
        and isinstance(distribution.get("normalizedName"), str)
        and bool(_IMPORT_PACKAGE.fullmatch(str(distribution.get("normalizedName"))))
        and isinstance(distribution.get("version"), str)
        and bool(distribution.get("version"))
        and isinstance(import_package, str)
        and import_package == distribution.get("normalizedName")
        and isinstance(namespace, str)
        and namespace == f"community.{import_package}"
        and isinstance(package_catalog.get("contentDigest"), str)
        and bool(_DIGEST.fullmatch(str(package_catalog.get("contentDigest"))))
        and isinstance(package_catalog.get("catalogDigest"), str)
        and bool(_DIGEST.fullmatch(str(package_catalog.get("catalogDigest"))))
        and isinstance(fqid, str)
        and bool(_DEFINITION_FQID.fullmatch(fqid))
        and fqid.startswith(f"{namespace}.")
        and isinstance(value.get("version"), str)
        and bool(value.get("version"))
        and isinstance(value.get("contentHash"), str)
        and bool(_DIGEST.fullmatch(str(value.get("contentHash"))))
        and isinstance(source_identity, str)
        and ":" in source_identity
        and isinstance(value.get("title"), str)
        and bool(value.get("title"))
        and isinstance(value.get("description"), str)
        and isinstance(category, list)
        and all(isinstance(item, str) for item in category)
        and isinstance(value.get("manufacturer"), str)
    )


def _resource_nodes(resources: Any) -> list[dict[str, Any]]:
    """把资源树节点安全转换为普通字典。"""

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
    value = definition.get("class")
    return value if isinstance(value, Mapping) else {}


def _state_schema(definition: Mapping[str, Any]) -> dict[str, Any]:
    values = _device_class(definition).get("status_types")
    if not isinstance(values, Mapping):
        return {}
    return {
        str(name): {"type": _json_type(value)}
        for name, value in values.items()
        if str(name)
    }


def _json_type(value: Any) -> str:
    name = str(getattr(value, "__name__", value)).strip().lower()
    return _JSON_TYPES.get(name, "string")


def _actions(
    device_id: str,
    definition: Mapping[str, Any],
) -> list[dict[str, Any]]:
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
    return dict(value) if isinstance(value, Mapping) else {}


__all__ = ["DeviceMaterialIdentityResolver", "project_device_catalog"]
