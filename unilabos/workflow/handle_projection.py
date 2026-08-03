"""A1-shaped Workflow Handle projection 的共享深模块。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def structural_ready_handle(io_type: str) -> dict[str, Any]:
    """投影 Action 与 Published Workflow 共用的 structural ready Handle。"""

    if io_type not in {"target", "source"}:
        raise ValueError("ready Handle direction 必须是 target/source")
    return {
        "handle_key": "ready",
        "io_type": io_type,
        "display_name": "Ready",
        "type": "boolean",
        "required": False,
        "data_source": "dependency",
        "data_key": "ready",
        "description": "Lexical source-order dependency",
        "meta_data": {
            "unilab": {
                "value_schema": {"type": "boolean"},
                "editor_control": "variable_selector",
                "allowed_resource_template_uuids": None,
                "implicit_passthrough": False,
                "structural_role": "ready",
            }
        },
    }


def resource_slot_schema(schema: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """返回 scalar/list/nullable value schema 内唯一可见的 ResourceSlot schema。"""

    if schema.get("$slot") == "ResourceSlot":
        return schema
    items = schema.get("items")
    if isinstance(items, Mapping):
        found = resource_slot_schema(items)
        if found is not None:
            return found
    members = schema.get("anyOf")
    if isinstance(members, list):
        for member in members:
            if isinstance(member, Mapping):
                found = resource_slot_schema(member)
                if found is not None:
                    return found
    return None


def workflow_handle_type(schema: Mapping[str, Any]) -> str:
    """按 A1 public shape 选择 Handle type，并保留 collection 外形。"""

    base = _non_null_schema(schema)
    if base.get("type") == "array":
        return "array"
    if resource_slot_schema(base) is not None:
        return "ResourceSlot"
    value_type = base.get("type")
    return str(value_type) if isinstance(value_type, str) else "object"


def _non_null_schema(schema: Mapping[str, Any]) -> Mapping[str, Any]:
    members = schema.get("anyOf")
    if isinstance(members, list):
        for member in members:
            if isinstance(member, Mapping) and member.get("type") != "null":
                return member
    return schema


__all__ = [
    "resource_slot_schema",
    "structural_ready_handle",
    "workflow_handle_type",
]
