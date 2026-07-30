"""Device/action catalog projection shared by the Edge and local bridge.

The execution OS owns device presence and the callable action set.  This
module converts that runtime state into a JSON-only schedule-wire snapshot and
provides the inverse projection used by the unified API/runtime catalog.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable, Mapping
from typing import Any

DEVICE_CATALOG_SCHEMA = "unilab/device-catalog-v1"


def build_device_catalog(
    host_node: Any,
    *,
    machine_name: str,
    is_action_busy: Callable[[str, str], bool],
    request_id: str = "",
) -> dict[str, Any]:
    """Build one complete device/action snapshot from the live HostNode."""

    namespaces = dict(getattr(host_node, "devices_names", {}) or {})
    machine_names = dict(
        getattr(host_node, "device_machine_names", {}) or {}
    )
    action_mappings = dict(
        getattr(host_node, "_action_value_mappings", {}) or {}
    )
    online_devices = set(getattr(host_node, "_online_devices", set()) or set())
    device_ids = sorted(set(namespaces) | set(action_mappings))
    devices: list[dict[str, Any]] = []

    for device_id in device_ids:
        namespace = str(namespaces.get(device_id, "") or "")
        device_key = _device_key(namespace, device_id)
        raw_actions = action_mappings.get(device_id, {})
        actions = []
        if isinstance(raw_actions, Mapping):
            for action_name in sorted(raw_actions):
                if str(action_name).startswith("_execute_driver_command"):
                    continue
                raw_definition = raw_actions[action_name]
                definition = (
                    raw_definition
                    if isinstance(raw_definition, Mapping)
                    else {}
                )
                actions.append(
                    _project_action(
                        device_id,
                        str(action_name),
                        definition,
                        busy=is_action_busy(device_id, str(action_name)),
                    )
                )
        devices.append(
            {
                "device_id": str(device_id),
                "device_key": device_key,
                "namespace": namespace,
                "machine_name": str(
                    machine_names.get(device_id, machine_name) or machine_name
                ),
                "is_online": device_key in online_devices,
                "actions": actions,
            }
        )

    payload: dict[str, Any] = {
        "schema": DEVICE_CATALOG_SCHEMA,
        "timestamp": time.time(),
        "machine_name": machine_name,
        "devices": devices,
    }
    if request_id:
        payload["request_id"] = request_id
    return payload


def device_catalog_from_action_catalog(
    action_catalog: Mapping[str, Mapping[str, Any]],
    *,
    machine_name: str = "Offline Edge",
    request_id: str = "",
) -> dict[str, Any]:
    """Create an offline Edge snapshot from the same runtime action catalog."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for action_ref in sorted(action_catalog):
        device_id, separator, action_name = str(action_ref).rpartition(".")
        if not separator:
            continue
        definition = action_catalog[action_ref]
        grouped.setdefault(device_id, []).append(
            {
                "action_name": action_name,
                "action_ref": action_ref,
                "label": str(
                    definition.get("label")
                    or definition.get("title")
                    or definition.get("display_name")
                    or action_name
                ),
                "type_name": str(definition.get("type_name") or ""),
                "input_schema": copy.deepcopy(
                    dict(definition.get("inputs") or {})
                ),
                "output_schema": copy.deepcopy(
                    dict(definition.get("outputs") or {})
                ),
                "contract": copy.deepcopy(
                    dict(definition.get("contract") or {})
                ),
                "is_busy": False,
            }
        )
    devices = [
        {
            "device_id": device_id,
            "device_key": f"/offline/{device_id}",
            "namespace": "/offline",
            "machine_name": device_id,
            "is_online": True,
            "actions": actions,
        }
        for device_id, actions in sorted(grouped.items())
    ]
    payload: dict[str, Any] = {
        "schema": DEVICE_CATALOG_SCHEMA,
        "timestamp": time.time(),
        "machine_name": machine_name,
        "devices": devices,
    }
    if request_id:
        payload["request_id"] = request_id
    return payload


def action_catalog_from_device_snapshot(
    snapshot: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Project a validated Edge snapshot into RuntimeService definitions."""

    if snapshot.get("schema") != DEVICE_CATALOG_SCHEMA:
        return {}
    result: dict[str, dict[str, Any]] = {}
    devices = snapshot.get("devices")
    if not isinstance(devices, list):
        return result
    for raw_device in devices:
        if not isinstance(raw_device, Mapping):
            continue
        actions = raw_device.get("actions")
        if not isinstance(actions, list):
            continue
        for raw_action in actions:
            if not isinstance(raw_action, Mapping):
                continue
            action_ref = str(raw_action.get("action_ref") or "")
            if not action_ref:
                continue
            result[action_ref] = {
                "label": str(
                    raw_action.get("label")
                    or raw_action.get("action_name")
                    or action_ref
                ),
                "type_name": str(raw_action.get("type_name") or ""),
                "inputs": copy.deepcopy(
                    dict(raw_action.get("input_schema") or {})
                ),
                "outputs": copy.deepcopy(
                    dict(raw_action.get("output_schema") or {})
                ),
                "contract": copy.deepcopy(
                    dict(raw_action.get("contract") or {})
                ),
            }
    return result


def apply_action_locks(
    snapshot: Mapping[str, Any] | None,
    locks: list[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Return a copy with Edge-reported action busy states applied."""

    if snapshot is None:
        return None
    updated = copy.deepcopy(dict(snapshot))
    by_key = {
        (str(lock.get("device_id") or ""), str(lock.get("action_name") or "")):
        not bool(lock.get("free", False))
        for lock in locks
    }
    for device in updated.get("devices") or []:
        if not isinstance(device, dict):
            continue
        device_id = str(device.get("device_id") or "")
        for action in device.get("actions") or []:
            if not isinstance(action, dict):
                continue
            key = (device_id, str(action.get("action_name") or ""))
            if key in by_key:
                action["is_busy"] = by_key[key]
    updated["timestamp"] = time.time()
    return updated


def public_device_catalog(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Convert the schedule-wire snapshot to the unified frontend contract."""

    items = []
    for device in snapshot.get("devices") or []:
        if not isinstance(device, Mapping):
            continue
        actions = []
        for action in device.get("actions") or []:
            if not isinstance(action, Mapping):
                continue
            actions.append(
                {
                    "id": str(action.get("action_name") or ""),
                    "actionRef": str(action.get("action_ref") or ""),
                    "name": str(
                        action.get("label")
                        or action.get("action_name")
                        or ""
                    ),
                    "typeName": str(action.get("type_name") or ""),
                    "inputSchema": copy.deepcopy(
                        dict(action.get("input_schema") or {})
                    ),
                    "outputSchema": copy.deepcopy(
                        dict(action.get("output_schema") or {})
                    ),
                    "busy": bool(action.get("is_busy", False)),
                }
            )
        items.append(
            {
                "id": str(device.get("device_id") or ""),
                "deviceKey": str(device.get("device_key") or ""),
                "namespace": str(device.get("namespace") or ""),
                "name": str(
                    device.get("machine_name")
                    or device.get("device_id")
                    or ""
                ),
                "online": bool(device.get("is_online", False)),
                "actions": actions,
            }
        )
    return {
        "schemaVersion": "device-catalog/v1",
        "source": "edge",
        "generatedAt": float(snapshot.get("timestamp") or 0),
        "items": items,
    }


def _project_action(
    device_id: str,
    action_name: str,
    definition: Mapping[str, Any],
    *,
    busy: bool,
) -> dict[str, Any]:
    schema = definition.get("schema")
    schema = schema if isinstance(schema, Mapping) else {}
    properties = schema.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    goal = properties.get("goal")
    result = properties.get("result")
    goal = goal if isinstance(goal, Mapping) else {}
    result = result if isinstance(result, Mapping) else {}
    input_schema = copy.deepcopy(dict(goal.get("properties") or {}))
    output_schema = copy.deepcopy(dict(result.get("properties") or {}))
    defaults = definition.get("goal_default")
    defaults = defaults if isinstance(defaults, Mapping) else {}
    for name in goal.get("required") or []:
        if name in input_schema and isinstance(input_schema[name], dict):
            input_schema[name]["required"] = True
    for name, value in defaults.items():
        if name in input_schema and isinstance(input_schema[name], dict):
            input_schema[name].setdefault("default", copy.deepcopy(value))
    for name in result.get("required") or []:
        if name in output_schema and isinstance(output_schema[name], dict):
            output_schema[name]["required"] = True

    contract = definition.get("contract")
    if hasattr(contract, "model_dump"):
        contract = contract.model_dump(mode="json")
    contract = copy.deepcopy(dict(contract or {}))
    type_value = definition.get("type")
    if hasattr(type_value, "__module__") and hasattr(type_value, "__name__"):
        type_name = f"{type_value.__module__}.{type_value.__name__}"
    else:
        type_name = str(type_value or "")
    return {
        "action_name": action_name,
        "action_ref": f"{device_id}.{action_name}",
        "label": str(
            definition.get("label")
            or definition.get("title")
            or action_name
        ),
        "type_name": type_name,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "contract": contract,
        "is_busy": busy,
    }


def _device_key(namespace: str, device_id: str) -> str:
    clean = namespace.rstrip("/")
    if not clean:
        return f"/{device_id}"
    return f"{clean}/{device_id}" if clean.startswith("/") else f"/{clean}/{device_id}"
