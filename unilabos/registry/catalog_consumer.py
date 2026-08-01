"""Registry consumers for package discovery and Workflow template Catalog."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from unilabos.package_manager.consumers import (
    action_catalog_from_package_catalog,
    register_package_catalog,
    workflow_template_imports_from_package_catalog,
)
from unilabos.workflow.catalog import NodeTemplateImport


class RegistryTemplateProjectionError(ValueError):
    """A detached Registry snapshot cannot be published as one Catalog."""

    def __init__(self, code: str, path: str) -> None:
        super().__init__(code)
        self.code = code
        self.path = path


def workflow_template_imports_from_registry_snapshot(
    registry_snapshot: Mapping[str, Any],
    *,
    authority_id: str,
    resource_template_identity_resolver: Callable[[str], str],
) -> tuple[NodeTemplateImport, ...]:
    """Project a completed, read-only Registry snapshot as full aggregates.

    The adapter consumes only the versioned canonical schema.  It never reads
    live HostNode mappings and never reparses annotations or decorator fields.
    """

    if not isinstance(registry_snapshot, Mapping):
        raise RegistryTemplateProjectionError("invalid_action_contract", "/registry")
    if not isinstance(authority_id, str) or not authority_id:
        raise RegistryTemplateProjectionError("template_catalog_mismatch", "/authority")
    imports: list[NodeTemplateImport] = []
    for registry_key in sorted(registry_snapshot):
        device = registry_snapshot[registry_key]
        if not isinstance(device, Mapping):
            raise RegistryTemplateProjectionError(
                "invalid_action_contract", f"/devices/{registry_key}"
            )
        class_info = device.get("class")
        if not isinstance(class_info, Mapping):
            continue
        actions = class_info.get("action_value_mappings")
        if not isinstance(actions, Mapping):
            continue
        owner_identity = str(device.get("source_fqid") or registry_key)
        try:
            owner_uuid = resource_template_identity_resolver(owner_identity)
        except (KeyError, LookupError, TypeError, ValueError):
            raise RegistryTemplateProjectionError(
                "template_catalog_mismatch",
                f"/devices/{registry_key}/resource_template_uuid",
            ) from None
        for action_name in sorted(actions):
            action = actions[action_name]
            if not isinstance(action, Mapping):
                continue
            schema = action.get("schema")
            if not isinstance(schema, Mapping):
                continue  # legacy auto-action
            extension = schema.get("x-unilabos-action-contract")
            if extension is None:
                continue  # legacy auto-action or untyped transport action
            path = f"/devices/{registry_key}/actions/{action_name}/schema"
            canonical = _canonical_schema(schema, path=path)
            handles = _template_handles(
                canonical,
                resolver=resource_template_identity_resolver,
                path=path,
            )
            resource_summary = {
                "uuid": owner_uuid,
                "name": owner_identity,
                "display_name": str(
                    device.get("display_name")
                    or device.get("displayname")
                    or owner_identity
                ),
            }
            imports.append(
                NodeTemplateImport(
                    template={
                        "resource_template_uuid": owner_uuid,
                        "name": str(action_name),
                        "display_name": str(
                            action.get("displayname")
                            or action.get("display_name")
                            or action_name
                        ),
                        "description": str(action.get("description") or ""),
                        "class": str(class_info.get("module") or "") or None,
                        "goal": _plain_mapping(action.get("goal")),
                        "goal_default": _plain_mapping(action.get("goal_default")),
                        "feedback": _plain_mapping(action.get("feedback")),
                        "result": _plain_mapping(action.get("result")),
                        "schema": canonical,
                        "type": str(action.get("type") or "UniLabJsonCommand"),
                        "node_type": str(action.get("node_type") or "device"),
                        "meta_data": {
                            "unilab": {
                                "authority_id": authority_id,
                                "source_fqid": f"{owner_identity}.{action_name}",
                                "resource_template": resource_summary,
                            }
                        },
                    },
                    handles=handles,
                )
            )
    return tuple(imports)


def _canonical_schema(value: Mapping[str, Any], *, path: str) -> dict[str, Any]:
    schema = _plain_mapping(value)
    extension = schema.get("x-unilabos-action-contract")
    if not isinstance(extension, dict) or extension.get("version") != 1:
        raise RegistryTemplateProjectionError(
            "invalid_action_contract",
            f"{path}/x-unilabos-action-contract/version",
        )
    properties = schema.get("properties")
    goal = properties.get("goal") if isinstance(properties, dict) else None
    result = properties.get("result") if isinstance(properties, dict) else None
    goal_fields = goal.get("properties") if isinstance(goal, dict) else None
    result_fields = result.get("properties") if isinstance(result, dict) else None
    input_order = extension.get("input_order")
    output_order = extension.get("output_order")
    if (
        not isinstance(goal_fields, dict)
        or not isinstance(result_fields, dict)
        or not isinstance(input_order, list)
        or not isinstance(output_order, list)
        or any(not isinstance(item, str) for item in (*input_order, *output_order))
        or len(input_order) != len(set(input_order))
        or len(output_order) != len(set(output_order))
        or set(input_order) != set(goal_fields)
        or set(output_order) != set(result_fields)
    ):
        raise RegistryTemplateProjectionError("invalid_action_contract", path)
    return schema


def _template_handles(
    schema: Mapping[str, Any],
    *,
    resolver: Callable[[str], str],
    path: str,
) -> tuple[dict[str, Any], ...]:
    extension = schema["x-unilabos-action-contract"]
    properties = schema["properties"]
    goal = properties["goal"]
    result = properties["result"]
    required = set(goal.get("required") or [])
    symbols = extension.get("resource_template_symbols") or {}
    goal_symbols = symbols.get("goal") if isinstance(symbols, dict) else {}
    result_symbols = symbols.get("result") if isinstance(symbols, dict) else {}
    handles: list[dict[str, Any]] = []
    for name in extension["input_order"]:
        value_schema = goal["properties"][name]
        handles.append(
            _handle(
                name,
                value_schema,
                io_type="target",
                required=name in required,
                data_source="goal",
                symbols=_symbol_list(goal_symbols, name),
                resolver=resolver,
                implicit=False,
                path=f"{path}/properties/goal/properties/{name}",
            )
        )
    output_names = set(extension["output_order"])
    for name in extension["output_order"]:
        handles.append(
            _handle(
                name,
                result["properties"][name],
                io_type="source",
                required=False,
                data_source="result",
                symbols=_symbol_list(result_symbols, name),
                resolver=resolver,
                implicit=False,
                path=f"{path}/properties/result/properties/{name}",
            )
        )
    for name in extension["input_order"]:
        value_schema = goal["properties"][name]
        if (
            _schema_base(value_schema).get("$slot") != "ResourceSlot"
            or name in output_names
        ):
            continue
        handles.append(
            _handle(
                name,
                value_schema,
                io_type="source",
                required=False,
                data_source="result",
                symbols=_symbol_list(goal_symbols, name),
                resolver=resolver,
                implicit=True,
                path=f"{path}/properties/goal/properties/{name}",
            )
        )
    handles.extend(
        (
            _structural_ready_handle("target"),
            _structural_ready_handle("source"),
        )
    )
    return tuple(handles)


def _structural_ready_handle(io_type: str) -> dict[str, Any]:
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


def _handle(
    name: str,
    value_schema: Mapping[str, Any],
    *,
    io_type: str,
    required: bool,
    data_source: str,
    symbols: Sequence[str],
    resolver: Callable[[str], str],
    implicit: bool,
    path: str,
) -> dict[str, Any]:
    allowed: list[str] | None = None
    if symbols:
        allowed = []
        for symbol in symbols:
            try:
                identity = resolver(symbol)
            except (KeyError, LookupError, TypeError, ValueError):
                raise RegistryTemplateProjectionError(
                    "template_catalog_mismatch", path
                ) from None
            if identity not in allowed:
                allowed.append(identity)
    base = _schema_base(value_schema)
    is_slot = base.get("$slot") == "ResourceSlot"
    control = str(value_schema.get("x-unilabos-editor-control") or "")
    if is_slot:
        control = "material_port"
    elif control != "site_selector":
        control = "variable_selector"
    value_type = "ResourceSlot" if is_slot else str(base.get("type") or "object")
    return {
        "handle_key": name,
        "io_type": io_type,
        "display_name": str(value_schema.get("title") or name),
        "type": value_type,
        "required": required,
        "data_source": data_source,
        "data_key": name,
        "description": str(value_schema.get("description") or ""),
        "meta_data": {
            "unilab": {
                "value_schema": _plain_mapping(value_schema),
                "editor_control": control,
                "allowed_resource_template_uuids": allowed,
                "implicit_passthrough": implicit,
            }
        },
    }


def _schema_base(schema: Mapping[str, Any]) -> Mapping[str, Any]:
    members = schema.get("anyOf")
    if isinstance(members, list):
        for member in members:
            if isinstance(member, Mapping) and member.get("type") != "null":
                return member
    return schema


def _symbol_list(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    symbols = value.get(name)
    if not isinstance(symbols, list) or any(
        not isinstance(item, str) for item in symbols
    ):
        return ()
    return tuple(symbols)


def _plain_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _plain(item) for key, item in value.items()}


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


__all__ = [
    "RegistryTemplateProjectionError",
    "action_catalog_from_package_catalog",
    "register_package_catalog",
    "workflow_template_imports_from_package_catalog",
    "workflow_template_imports_from_registry_snapshot",
]
