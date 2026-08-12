"""把资源图、ROS 在线事实与注册表合同投影为统一设备目录。"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

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

DeviceMaterialResolver = Callable[[str], Mapping[str, Any] | None]


def project_device_catalog(
    *,
    resources: Any,
    registry_devices: Iterable[Mapping[str, Any]],
    online_devices: Mapping[str, Any],
    material_resolver: DeviceMaterialResolver | None = None,
    generated_at: float | None = None,
) -> dict[str, Any]:
    """生成前端与 OS 共享的设备目录（Device Catalog）。

    参数说明：``resources`` 是 Host 持有的资源树集合，``registry_devices`` 是
    注册表设备类型合同，``online_devices`` 是 ROS 图当前在线实例，
    ``material_resolver`` 按设备部署 ID 从库存权威解析实际设备物料（Material）
    身份；``generated_at`` 仅供确定性测试覆盖。返回按实例 ID 排序的目录，非设备
    资源不进入结果；物料身份不存在时输出空 ``materialUuid``，禁止用设备 ID 或
    运行时 UUID 猜测。解析器自身异常原样传播，使库存读取故障关闭式失败。
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
        # ``material_uuid`` 是库存权威证明的实际设备物料身份；资源树中的 ``uuid``
        # 仅属于本次运行快照，不能授权设备动作（Action）执行。
        material_uuid = _device_material_uuid(material_resolver, device_id)
        items.append(
            {
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
        )
    return {
        "schemaVersion": "device-catalog/v1",
        "source": "edge",
        "generatedAt": time.time() if generated_at is None else generated_at,
        "items": sorted(items, key=lambda item: item["id"]),
    }


def _device_material_uuid(
    resolver: DeviceMaterialResolver | None,
    device_id: str,
) -> str:
    """从库存只读端口提取设备物料（Material）稳定身份。

    参数：``resolver`` 是可选库存身份解析器，``device_id`` 是资源图部署 ID。
    返回：解析成功时为库存物料 UUID，否则为空字符串。异常：解析器读取库存失败
    或发现身份歧义时原样传播，调用方不得回退到其他身份命名空间。
    """

    if resolver is None:
        return ""
    identity = resolver(device_id)
    if not isinstance(identity, Mapping):
        return ""
    return str(identity.get("uuid") or "").strip()


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


__all__ = ["DeviceMaterialResolver", "project_device_catalog"]
