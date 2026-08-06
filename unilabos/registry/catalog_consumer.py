"""Registry 到 Workflow Template Catalog 的只读投影。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

# 这些 re-export 只维持 F006 以前的稳定 import path；A1 production 发布只能走
# 本模块的 Registry snapshot adapter。
from unilabos.package_manager.consumers import (
    action_catalog_from_package_catalog,
    register_package_catalog,
    workflow_template_imports_from_package_catalog,
)
from unilabos.workflow.catalog import NodeTemplateImport
from unilabos.workflow.handle_projection import (
    resource_slot_schema,
    structural_ready_handle,
    workflow_handle_type,
)


class RegistryTemplateProjectionError(ValueError):
    """完整 Registry snapshot 无法原子发布为一个 Catalog。"""

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
    """把已完成构建的只读 Registry snapshot 投影为完整聚合。

    Adapter 只消费带版本的 canonical schema；不读取 live HostNode mapping，
    也不重新解析 annotation 或 decorator 字段。
    """

    if not isinstance(registry_snapshot, Mapping):
        raise RegistryTemplateProjectionError("invalid_action_contract", "/registry")
    if not isinstance(authority_id, str) or not authority_id:
        raise RegistryTemplateProjectionError("template_catalog_mismatch", "/authority")
    if any(not isinstance(key, str) or not key for key in registry_snapshot):
        raise RegistryTemplateProjectionError("invalid_action_contract", "/registry")
    imports: list[NodeTemplateImport] = []
    host_owner: tuple[str, str, str] | None = None
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
        if any(not isinstance(key, str) or not key for key in actions):
            raise RegistryTemplateProjectionError(
                "invalid_action_contract",
                f"/devices/{registry_key}/actions",
            )
        typed_actions: list[tuple[str, Mapping[str, Any]]] = []
        for action_name in sorted(actions):
            action = actions[action_name]
            if not isinstance(action, Mapping):
                raise RegistryTemplateProjectionError(
                    "invalid_action_contract",
                    f"/devices/{registry_key}/actions/{action_name}",
                )
            # Auto-actions are legacy runtime transport conveniences, never A1
            # authoring templates, even when their scanner recorded a diagnostic.
            if action_name.startswith("auto-"):
                continue
            diagnostic = action.get("contract_diagnostic")
            if diagnostic is not None:
                if not isinstance(diagnostic, Mapping):
                    raise RegistryTemplateProjectionError(
                        "invalid_action_contract",
                        f"/devices/{registry_key}/actions/{action_name}",
                    )
                code = diagnostic.get("code")
                diagnostic_path = diagnostic.get("path")
                if not isinstance(code, str) or not isinstance(
                    diagnostic_path,
                    str,
                ):
                    raise RegistryTemplateProjectionError(
                        "invalid_action_contract",
                        f"/devices/{registry_key}/actions/{action_name}",
                    )
                raise RegistryTemplateProjectionError(
                    code,
                    (
                        f"/devices/{registry_key}{diagnostic_path}"
                        if diagnostic_path.startswith(f"/actions/{action_name}/")
                        else (
                            f"/devices/{registry_key}/actions/"
                            f"{action_name}{diagnostic_path}"
                        )
                    ),
                )
            schema = action.get("schema")
            if not isinstance(schema, Mapping):
                continue  # legacy auto-action
            extension = schema.get("x-unilabos-action-contract")
            if extension is None:
                continue  # legacy auto-action 或无类型 transport action
            typed_actions.append((action_name, action))
        is_host_node = registry_key == "host_node"
        if not typed_actions and not is_host_node:
            continue
        owner_identity = str(device.get("source_fqid") or registry_key)
        try:
            owner_uuid = resource_template_identity_resolver(owner_identity)
        except (KeyError, LookupError, TypeError, ValueError):
            raise RegistryTemplateProjectionError(
                "template_catalog_mismatch",
                f"/devices/{registry_key}/resource_template_uuid",
            ) from None
        if is_host_node:
            host_owner = (
                owner_identity,
                owner_uuid,
                str(
                    device.get("display_name")
                    or device.get("displayname")
                    or owner_identity
                ),
            )
        for action_name, action in typed_actions:
            schema = action["schema"]
            assert isinstance(schema, Mapping)
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
    if host_owner is not None:
        owner_identity, owner_uuid, owner_display_name = host_owner
        imports.append(
            NodeTemplateImport(
                template={
                    "resource_template_uuid": owner_uuid,
                    "name": "group",
                    "display_name": "Group",
                    "description": "Presentation group for Workflow authoring",
                    "class": "unilabos.workflow.authoring:group",
                    "goal": {},
                    "goal_default": {},
                    "feedback": {},
                    "result": {},
                    "schema": None,
                    "type": "group",
                    "node_type": "group",
                    "meta_data": {
                        "unilab": {
                            "authority_id": authority_id,
                            "framework_owner_only": True,
                            "source_fqid": "unilabos.workflow.authoring:group",
                        }
                    },
                },
                handles=(),
            )
        )
        imports.append(
            NodeTemplateImport(
                template={
                    "resource_template_uuid": owner_uuid,
                    "name": "material_source",
                    "display_name": "Material Source",
                    "description": "Declare one material at an OS-owned mount",
                    "class": "unilabos.workflow.authoring:material_source",
                    "goal": {},
                    "goal_default": {},
                    "feedback": {},
                    "result": {},
                    "schema": None,
                    "type": "material_source",
                    "node_type": "material_source",
                    "meta_data": {
                        "unilab": {
                            "authority_id": authority_id,
                            "source_fqid": (
                                "unilabos.workflow.authoring:material_source"
                            ),
                            "resource_template": {
                                "uuid": owner_uuid,
                                "name": owner_identity,
                                "display_name": owner_display_name,
                            },
                        }
                    },
                },
                handles=(
                    {
                        "description": "The selected or newly declared material",
                        "meta_data": {},
                        "handle_key": "material",
                        "io_type": "source",
                        "display_name": "Material",
                        "type": "ResourceSlot",
                        "required": False,
                        "data_source": "executor",
                        "data_key": "material",
                    },
                ),
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
        or any(not isinstance(value, Mapping) for value in goal_fields.values())
        or any(not isinstance(value, Mapping) for value in result_fields.values())
    ):
        raise RegistryTemplateProjectionError("invalid_action_contract", path)
    _validate_resource_template_symbols(
        extension,
        goal_names=set(goal_fields),
        result_names=set(result_fields),
        path=path,
    )
    return schema


def _validate_resource_template_symbols(
    extension: Mapping[str, Any],
    *,
    goal_names: set[str],
    result_names: set[str],
    path: str,
) -> None:
    symbols = extension.get("resource_template_symbols")
    if not isinstance(symbols, Mapping) or set(symbols) != {"goal", "result"}:
        raise RegistryTemplateProjectionError(
            "invalid_action_contract",
            f"{path}/x-unilabos-action-contract/resource_template_symbols",
        )
    for section, field_names in (("goal", goal_names), ("result", result_names)):
        fields = symbols.get(section)
        section_path = (
            f"{path}/x-unilabos-action-contract/resource_template_symbols/{section}"
        )
        if not isinstance(fields, Mapping) or any(
            not isinstance(name, str) or name not in field_names for name in fields
        ):
            raise RegistryTemplateProjectionError(
                "invalid_action_contract",
                section_path,
            )
        for name, values in fields.items():
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(item, str) or not item for item in values)
                or len(values) != len(set(values))
            ):
                raise RegistryTemplateProjectionError(
                    "invalid_action_contract",
                    f"{section_path}/{name}",
                )


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
                symbols=_symbol_list(goal_symbols, name, path=path),
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
                symbols=_symbol_list(result_symbols, name, path=path),
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
                symbols=_symbol_list(goal_symbols, name, path=path),
                resolver=resolver,
                implicit=True,
                path=f"{path}/properties/goal/properties/{name}",
            )
        )
    handles.extend(
        (
            structural_ready_handle("target"),
            structural_ready_handle("source"),
        )
    )
    return tuple(handles)


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
    is_slot = resource_slot_schema(value_schema) is not None
    control = str(value_schema.get("x-unilabos-editor-control") or "")
    if is_slot:
        control = "material_port"
    elif control != "site_selector":
        control = "variable_selector"
    value_type = workflow_handle_type(value_schema)
    workflow_value_schema = (
        _without_presentation_fields(value_schema)
        if is_slot
        else _plain_mapping(value_schema)
    )
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
                "value_schema": workflow_value_schema,
                "editor_control": control,
                "allowed_resource_template_uuids": allowed,
                "implicit_passthrough": implicit,
            }
        },
    }


def _without_presentation_fields(value: Any) -> Any:
    """Keep ResourceSlot handle schemas within the strict Workflow v1 dialect."""

    if isinstance(value, Mapping):
        return {
            str(key): _without_presentation_fields(item)
            for key, item in value.items()
            if key not in {"title", "description"}
        }
    if isinstance(value, list):
        return [_without_presentation_fields(item) for item in value]
    return value


def _schema_base(schema: Mapping[str, Any]) -> Mapping[str, Any]:
    members = schema.get("anyOf")
    if isinstance(members, list):
        for member in members:
            if isinstance(member, Mapping) and member.get("type") != "null":
                return member
    return schema


def _symbol_list(value: Any, name: str, *, path: str) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        raise RegistryTemplateProjectionError("invalid_action_contract", path)
    symbols = value.get(name)
    if symbols is None:
        return ()
    if (
        not isinstance(symbols, list)
        or not symbols
        or any(not isinstance(item, str) or not item for item in symbols)
        or len(symbols) != len(set(symbols))
    ):
        raise RegistryTemplateProjectionError("invalid_action_contract", path)
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
