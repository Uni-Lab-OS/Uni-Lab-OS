"""Adapters from decorated Registry entries to the workflow action catalog."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from unilabos.registry.registry import Registry


def _parameter_schema(
    raw_schema: Mapping[str, Any],
    section: str,
) -> dict[str, dict[str, Any]]:
    properties = raw_schema.get("properties", {})
    section_schema = (
        properties.get(section, {}) if isinstance(properties, Mapping) else {}
    )
    fields = (
        section_schema.get("properties", {})
        if isinstance(section_schema, Mapping)
        else {}
    )
    result = (
        {
            str(name): dict(definition)
            for name, definition in fields.items()
            if isinstance(definition, Mapping)
        }
        if isinstance(fields, Mapping)
        else {}
    )
    required = (
        section_schema.get("required", [])
        if isinstance(section_schema, Mapping)
        else []
    )
    if isinstance(required, list):
        for name in required:
            if name in result:
                result[name] = {**result[name], "required": True}
    return result


def action_catalog_from_runtime_mappings(
    runtime_action_mappings: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Project current device-instance action mappings into Canonical contracts."""

    catalog: dict[str, dict[str, Any]] = {}
    for device_id, actions in runtime_action_mappings.items():
        if not isinstance(actions, Mapping):
            continue
        for action_name, raw_entry in actions.items():
            if str(action_name).startswith("_") or not isinstance(raw_entry, Mapping):
                continue
            schema = raw_entry.get("schema") or {}
            if not isinstance(schema, Mapping):
                continue
            contract = dict(raw_entry.get("contract") or {})
            catalog[f"{device_id}.{action_name}"] = {
                "inputs": _parameter_schema(schema, "goal"),
                "outputs": _parameter_schema(schema, "result"),
                "contract": contract,
                "resource_claims": list(contract.get("resource_claims") or []),
                "effects": list(contract.get("effects") or []),
                "timing": dict(contract.get("timing") or {}),
            }
    return catalog


def action_catalog_from_device_registry(
    device_type_registry: Mapping[str, Mapping[str, Any]],
    *,
    device_ids: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Project Registry-owned ``@action`` schemas into Canonical contracts."""

    selected = set(device_ids) if device_ids is not None else None
    catalog: dict[str, dict[str, Any]] = {}
    for device_id, device_entry in device_type_registry.items():
        if selected is not None and device_id not in selected:
            continue
        actions = (
            device_entry.get("class", {}).get("action_value_mappings", {})
            if isinstance(device_entry, Mapping)
            else {}
        )
        if not isinstance(actions, Mapping):
            continue
        for action_name, raw_entry in actions.items():
            if str(action_name).startswith("_") or not isinstance(raw_entry, Mapping):
                continue
            schema = raw_entry.get("schema") or {}
            contract = dict(raw_entry.get("contract") or {})
            catalog[f"{device_id}.{action_name}"] = {
                "inputs": (
                    _parameter_schema(schema, "goal")
                    if isinstance(schema, Mapping)
                    else {}
                ),
                "outputs": (
                    _parameter_schema(schema, "result")
                    if isinstance(schema, Mapping)
                    else {}
                ),
                "contract": contract,
                "resource_claims": list(contract.get("resource_claims") or []),
                "effects": list(contract.get("effects") or []),
                "timing": dict(contract.get("timing") or {}),
            }
    return catalog


def scan_decorated_device_package(
    package_root: str | Path,
) -> dict[str, dict[str, Any]]:
    """Scan one installed/source device package without persistent cache writes."""

    root = Path(package_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    registry = Registry()
    registry._load_config_cache = lambda: {}  # type: ignore[method-assign]
    registry._save_config_cache = lambda _cache: None  # type: ignore[method-assign]
    registry._run_ast_scan(devices_dirs=[root], external_only=True)
    discovered_ids = {
        device_id
        for device_id, entry in registry.device_type_registry.items()
        if Path(str(entry.get("file_path") or "/")).resolve().is_relative_to(root)
    }
    return action_catalog_from_device_registry(
        registry.device_type_registry,
        device_ids=discovered_ids,
    )
