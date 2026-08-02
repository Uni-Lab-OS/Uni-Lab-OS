"""Live device/Action read projection for the public Edge API.

The HostNode owns device presence and callable Action definitions. The
process-live DeviceActionManager owns the corresponding busy holder. This
module only combines immutable copies of those facts into a JSON DTO; it does
not own either lifecycle.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable, Mapping
from typing import Any


def build_public_device_catalog(
    host_node: Any,
    *,
    machine_name: str,
    is_action_busy: Callable[[str], bool],
    current_action_job_id: Callable[[str], str | None],
) -> dict[str, Any]:
    """Project one complete live HostNode/lock snapshot for the frontend."""

    namespaces = dict(getattr(host_node, "devices_names", {}) or {})
    machine_names = dict(getattr(host_node, "device_machine_names", {}) or {})
    action_mappings = dict(getattr(host_node, "_action_value_mappings", {}) or {})
    online_devices = set(getattr(host_node, "_online_devices", set()) or set())
    device_ids = sorted(set(namespaces) | set(action_mappings))

    items: list[dict[str, Any]] = []
    for device_id in device_ids:
        namespace = str(namespaces.get(device_id, "") or "")
        device_key = _device_key(namespace, str(device_id))
        actions: list[dict[str, Any]] = []
        raw_actions = action_mappings.get(device_id, {})
        if isinstance(raw_actions, Mapping):
            for raw_action_name in sorted(raw_actions):
                action_name = str(raw_action_name)
                if action_name.startswith("_execute_driver_command"):
                    continue
                raw_definition = raw_actions[raw_action_name]
                definition = (
                    raw_definition if isinstance(raw_definition, Mapping) else {}
                )
                action_key = f"/devices/{device_id}/{action_name}"
                busy = is_action_busy(action_key)
                current_job_id = current_action_job_id(action_key) if busy else None
                actions.append(
                    _project_action(
                        str(device_id),
                        action_name,
                        definition,
                        busy=busy,
                        current_job_id=current_job_id,
                    )
                )
        items.append(
            {
                "id": str(device_id),
                "deviceKey": device_key,
                "namespace": namespace,
                "name": str(
                    machine_names.get(device_id, machine_name)
                    or machine_name
                    or device_id
                ),
                "online": device_key in online_devices,
                "actions": actions,
            }
        )

    return {
        "schemaVersion": "device-catalog/v1",
        "source": "edge",
        "generatedAt": time.time(),
        "items": items,
    }


def _project_action(
    device_id: str,
    action_name: str,
    definition: Mapping[str, Any],
    *,
    busy: bool,
    current_job_id: str | None,
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
    _apply_required_and_defaults(
        input_schema,
        required=goal.get("required"),
        defaults=defaults,
    )
    _apply_required_and_defaults(
        output_schema,
        required=result.get("required"),
        defaults={},
    )

    type_value = definition.get("type")
    if hasattr(type_value, "__module__") and hasattr(type_value, "__name__"):
        type_name = f"{type_value.__module__}.{type_value.__name__}"
    else:
        type_name = str(type_value or "")

    return {
        "id": action_name,
        "actionRef": f"{device_id}.{action_name}",
        "name": str(definition.get("label") or definition.get("title") or action_name),
        "typeName": type_name,
        "inputSchema": input_schema,
        "outputSchema": output_schema,
        "busy": busy,
        "currentJobId": current_job_id if busy else None,
    }


def _apply_required_and_defaults(
    schema: dict[str, Any],
    *,
    required: Any,
    defaults: Mapping[str, Any],
) -> None:
    for name in required or []:
        definition = schema.get(name)
        if isinstance(definition, dict):
            definition["required"] = True
    for name, value in defaults.items():
        definition = schema.get(name)
        if isinstance(definition, dict):
            definition.setdefault("default", copy.deepcopy(value))


def _device_key(namespace: str, device_id: str) -> str:
    clean_namespace = namespace.rstrip("/")
    if not clean_namespace:
        return f"/{device_id}"
    if not clean_namespace.startswith("/"):
        clean_namespace = f"/{clean_namespace}"
    return f"{clean_namespace}/{device_id}"
