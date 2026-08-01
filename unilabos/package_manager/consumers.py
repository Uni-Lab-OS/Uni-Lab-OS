"""把 source-neutral PackageCatalog 投影到 OS 进程内权威模块。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from unilabos.package_manager import DefinitionRecord, PackageCatalog


def register_package_catalog(registry: Any, catalog: PackageCatalog) -> None:
    """登记完整定义元数据；不 import definition module，也不创建实例。"""

    for record in catalog.definitions.devices:
        if record.fqid in registry.device_type_registry:
            raise ValueError(f"Registry definition 重复: {record.fqid}")
        entry = registry._build_device_entry_from_ast(
            record.fqid,
            _device_ast_metadata(record),
            allow_definition_imports=False,
        )
        entry["source_fqid"] = record.fqid
        entry["content_hash"] = record.content_hash
        registry.device_type_registry[record.fqid] = entry

    for record in catalog.definitions.resources:
        if record.fqid in registry.resource_type_registry:
            raise ValueError(f"Registry definition 重复: {record.fqid}")
        entry = registry._build_resource_entry_from_ast(
            record.fqid, _resource_ast_metadata(record)
        )
        entry["source_fqid"] = record.fqid
        entry["content_hash"] = record.content_hash
        registry.resource_type_registry[record.fqid] = entry


def action_catalog_from_package_catalog(
    catalog: PackageCatalog,
) -> dict[str, dict[str, Any]]:
    """投影离线 authoring contract；definition id 仅作无 Graph 时的实例默认值。"""

    actions: dict[str, dict[str, Any]] = {}
    for device in catalog.definitions.devices:
        details = _plain(device.details)
        for action in details.get("actions", []):
            decorator = action.get("decorator", {})
            parameters = action.get("parameters", [])
            outputs = decorator.get("result", {}) if isinstance(decorator, dict) else {}
            actions[f"{device.id}.{action['name']}"] = {
                "inputs": {
                    str(parameter["name"]): _parameter_contract(parameter)
                    for parameter in parameters
                },
                "outputs": {str(name): {} for name in outputs}
                if isinstance(outputs, dict)
                else {},
                "contract": dict(decorator.get("contract") or {})
                if isinstance(decorator, dict)
                else {},
            }
    return dict(sorted(actions.items()))


def workflow_template_imports_from_package_catalog(
    catalog: PackageCatalog,
    *,
    resource_template_uuids: Mapping[str, str],
) -> tuple[Any, ...]:
    """生成现有 D-042 TemplateCatalog 的完整 action aggregates。

    ResourceTemplate identity 必须由其权威模块显式提供；PackageCatalog 只携带
    source identity，绝不生成数据库 UUID。WorkflowNodeTemplate/Handle UUID 则
    继续由 TemplateCatalog 首次写入时分配并在后续 replace 中复用。
    """

    from unilabos.workflow.catalog import NodeTemplateImport

    imports: list[NodeTemplateImport] = []
    for device in catalog.definitions.devices:
        try:
            resource_template_uuid = resource_template_uuids[device.fqid]
        except KeyError as exc:
            raise ValueError(
                f"Catalog device 缺少 ResourceTemplate identity: {device.fqid}"
            ) from exc
        details = _plain(device.details)
        for action in details.get("actions", []):
            if not isinstance(action, Mapping):
                continue
            action_name = str(action.get("name") or "").strip()
            if not action_name:
                raise ValueError(f"Catalog action identity 缺失: {device.fqid}")
            decorator = action.get("decorator")
            decorator = decorator if isinstance(decorator, Mapping) else {}
            parameters = action.get("parameters")
            parameters = parameters if isinstance(parameters, list) else []
            explicit_goal = decorator.get("goal")
            goal = {
                str(item["name"]): str(item["name"])
                for item in parameters
                if isinstance(item, Mapping) and item.get("name")
            }
            if isinstance(explicit_goal, Mapping):
                goal.update(
                    {str(key): _plain(value) for key, value in explicit_goal.items()}
                )
            goal_default = {
                str(item["name"]): _plain(item["default"])
                for item in parameters
                if isinstance(item, Mapping) and item.get("name") and "default" in item
            }
            explicit_defaults = decorator.get("goal_default")
            if isinstance(explicit_defaults, Mapping):
                goal_default.update(
                    {
                        str(key): _plain(value)
                        for key, value in explicit_defaults.items()
                    }
                )
            source_fqid = f"{device.fqid}.{action_name}"
            handles = [
                _parameter_handle(item)
                for item in parameters
                if isinstance(item, Mapping) and item.get("name")
            ]
            # FE authoring represents lexical source order with the existing
            # dependency handle pair. These are action-template contracts,
            # not Graph connection parameters or runtime instances.
            handles.extend((_ready_handle("target"), _ready_handle("source")))
            result_mapping = decorator.get("result")
            if isinstance(result_mapping, Mapping):
                handles.extend(_result_handle(str(name)) for name in result_mapping)
            imports.append(
                NodeTemplateImport(
                    template={
                        "resource_template_uuid": resource_template_uuid,
                        "name": action_name,
                        "display_name": str(
                            decorator.get("displayname")
                            or decorator.get("description")
                            or action_name
                        ),
                        "description": str(
                            decorator.get("description")
                            or action.get("docstring")
                            or ""
                        ),
                        "class": f"{device.module}:{device.symbol}",
                        "type": str(
                            decorator.get("action_type") or "UniLabJsonCommand"
                        ),
                        "node_type": str(decorator.get("node_type") or "device"),
                        "goal": goal,
                        "goal_default": goal_default,
                        "feedback": _plain_mapping(decorator.get("feedback")),
                        "result": _plain_mapping(decorator.get("result")),
                        "meta_data": {
                            "unilab": {
                                "source_fqid": source_fqid,
                                "content_hash": device.content_hash,
                            }
                        },
                    },
                    handles=handles,
                )
            )
    return tuple(imports)


def _parameter_handle(parameter: Mapping[str, Any]) -> dict[str, Any]:
    name = str(parameter["name"])
    return {
        "handle_key": name,
        "io_type": "target",
        "display_name": name,
        "type": _workflow_value_type(str(parameter.get("type") or "Any")),
        "required": bool(parameter.get("required", False)),
        "data_source": "goal",
        "data_key": name,
        "description": "",
        "meta_data": {},
    }


def _result_handle(name: str) -> dict[str, Any]:
    return {
        "handle_key": name,
        "io_type": "source",
        "display_name": name,
        "type": "object",
        "required": False,
        "data_source": "result",
        "data_key": name,
        "description": "",
        "meta_data": {},
    }


def _ready_handle(io_type: str) -> dict[str, Any]:
    return {
        "handle_key": "ready",
        "io_type": io_type,
        "display_name": "Ready",
        "type": "any",
        "required": False,
        "data_source": "dependency",
        "data_key": "ready",
        "description": "Lexical source-order dependency",
        "meta_data": {},
    }


def _workflow_value_type(annotation: str) -> str:
    normalized = annotation.replace(" ", "")
    members = [
        item for item in normalized.split("|") if item not in {"None", "NoneType"}
    ]
    if len(members) == 1:
        normalized = members[0]
    direct = {
        "bool": "bool",
        "float": "float",
        "int": "int",
        "str": "str",
    }.get(normalized)
    if direct is not None:
        return direct
    if normalized.startswith(("list[", "List[", "tuple[", "Tuple[")):
        return "array"
    if normalized.startswith(("dict[", "Dict[", "Mapping[")):
        return "object"
    return "object"


def _plain_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _plain(item) for key, item in value.items()}


def _parameter_contract(parameter: Mapping[str, Any]) -> dict[str, Any]:
    type_name = str(parameter.get("type") or "Any")
    json_type = {
        "bool": "boolean",
        "float": "number",
        "int": "integer",
        "str": "string",
    }.get(type_name, "object")
    result: dict[str, Any] = {"type": json_type}
    if "default" in parameter:
        result["default"] = _plain(parameter["default"])
    if parameter.get("required"):
        result["required"] = True
    return result


def _device_ast_metadata(record: DefinitionRecord) -> dict[str, Any]:
    details = _plain(record.details)
    imports = {
        str(name): _import_reference(str(reference))
        for name, reference in details.get("imports", {}).items()
        if _is_trusted_schema_import(str(reference))
    }
    actions = {
        str(action["name"]): {
            "action_args": _registry_value(
                action.get("decorator", {}),
                imports=imports,
                module=record.module,
            ),
            "docstring": str(action.get("docstring") or ""),
            "is_async": bool(action.get("is_async", False)),
            "params": _registry_value(
                action.get("parameters", []),
                imports=imports,
                module=record.module,
            ),
            "return_type": str(action.get("return_type") or "Any"),
        }
        for action in details.get("actions", [])
    }
    statuses = {
        str(status["name"]): {
            "is_property": bool(status.get("is_property", False)),
            "name": str(status["name"]),
            "return_type": str(status.get("return_type") or "Any"),
            "topic_config": _registry_value(
                status.get("topic_config", {}),
                imports=imports,
                module=record.module,
            )
            or None,
        }
        for status in details.get("status_properties", [])
    }
    return {
        "actions": actions,
        "auto_methods": {},
        "category": list(record.category),
        "description": record.description,
        "device_id": record.fqid,
        "device_type": str(details.get("device_type") or "python"),
        "displayname": record.displayname,
        "file_path": record.declaring_file,
        "handles": _registry_value(
            details.get("handles", []), imports=imports, module=record.module
        ),
        "hardware_interface": _registry_value(
            details.get("hardware_interface"), imports=imports, module=record.module
        ),
        "icon": str(details.get("icon") or ""),
        "import_map": imports,
        "init_docstring": str(details.get("init_docstring") or ""),
        "init_params": _registry_value(
            details.get("init_parameters", []),
            imports=imports,
            module=record.module,
        ),
        "model": details.get("model"),
        "metadata": _plain_mapping(details.get("metadata")),
        "module": f"{record.module}:{record.symbol}",
        "status_properties": statuses,
        "version": record.version,
    }


def _resource_ast_metadata(record: DefinitionRecord) -> dict[str, Any]:
    details = _plain(record.details)
    return {
        "category": list(record.category),
        "class_type": str(details.get("class_type") or "python"),
        "description": record.description,
        "displayname": record.displayname,
        "file_path": record.declaring_file,
        "handles": _registry_value(
            details.get("handles", []), imports={}, module=record.module
        ),
        "icon": str(details.get("icon") or ""),
        "init_params": _registry_value(
            details.get("parameters", []), imports={}, module=record.module
        ),
        "is_function": details.get("factory_kind") == "function",
        "model": details.get("model"),
        "metadata": _plain_mapping(details.get("metadata")),
        "module": f"{record.module}:{record.symbol}",
        "name": record.symbol,
        "resource_id": record.fqid,
        "version": record.version,
    }


def _registry_value(value: Any, *, imports: Mapping[str, str], module: str) -> Any:
    if isinstance(value, list):
        return [_registry_value(item, imports=imports, module=module) for item in value]
    if not isinstance(value, Mapping):
        return value
    if "$name" in value:
        return _resolve_static_name(str(value["$name"]), imports, module)
    if "$call" in value:
        call = _resolve_static_name(str(value["$call"]), imports, module)
        result = {"_call": call}
        for index, item in enumerate(value.get("args", [])):
            result[f"_pos_{index}"] = _registry_value(
                item, imports=imports, module=module
            )
        for name, item in value.get("kwargs", {}).items():
            result[str(name)] = _registry_value(item, imports=imports, module=module)
        return result
    if "$ast" in value:
        raise ValueError("Catalog consumer 不接受动态 AST contract")
    return {
        str(name): _registry_value(item, imports=imports, module=module)
        for name, item in value.items()
    }


def _resolve_static_name(name: str, imports: Mapping[str, str], module: str) -> str:
    base, separator, attribute = name.partition(".")
    resolved = imports.get(base)
    if resolved is None:
        return name
    if not separator:
        return resolved
    if resolved.startswith("unilabos.registry.decorators:") and base in {
        "DataSource",
        "NodeType",
        "Side",
    }:
        return attribute
    return f"{resolved}.{attribute}"


def _import_reference(reference: str) -> str:
    module, separator, symbol = reference.rpartition(".")
    return f"{module}:{symbol}" if separator else reference


def _is_trusted_schema_import(reference: str) -> bool:
    root = reference.split(".", 1)[0]
    return root in {
        "builtins",
        "collections",
        "pydantic",
        "types",
        "typing",
        "unilabos",
        "unilabos_msgs",
    }


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(name): _plain(item) for name, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


__all__ = [
    "action_catalog_from_package_catalog",
    "register_package_catalog",
    "workflow_template_imports_from_package_catalog",
]
