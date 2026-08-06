"""从冻结动作合同构造作业执行占用意图。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _resource_slot_schema(schema: Any) -> bool:
    if not isinstance(schema, Mapping):
        return False
    if schema.get("$slot") == "ResourceSlot":
        return schema.get("x-unilabos-material-lock") is not False
    if schema.get("type") == "array":
        return _resource_slot_schema(schema.get("items"))
    alternatives = schema.get("anyOf")
    return isinstance(alternatives, list) and any(
        _resource_slot_schema(item) for item in alternatives
    )


def _resource_slot_uuids(value: Any) -> set[str]:
    if isinstance(value, list):
        result: set[str] = set()
        for item in value:
            result.update(_resource_slot_uuids(item))
        return result
    if not isinstance(value, Mapping):
        return set()
    identity = value.get("uuid")
    if not isinstance(identity, str) or not identity:
        return set()
    return {identity}


def mutable_material_roots(
    param_schema: Mapping[str, Any] | None,
    param: Mapping[str, Any],
) -> tuple[str, ...]:
    """按冻结动作合同提取默认独占的物料占位符（ResourceSlot）。"""

    if not isinstance(param_schema, Mapping):
        return ()
    properties = param_schema.get("properties")
    goal = properties.get("goal") if isinstance(properties, Mapping) else None
    fields = goal.get("properties") if isinstance(goal, Mapping) else None
    if not isinstance(fields, Mapping):
        return ()
    roots: set[str] = set()
    for name, value_schema in fields.items():
        if isinstance(name, str) and _resource_slot_schema(value_schema):
            roots.update(_resource_slot_uuids(param.get(name)))
    return tuple(sorted(roots))


__all__ = ["mutable_material_roots"]
