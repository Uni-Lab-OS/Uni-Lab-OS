"""WorkflowTask input preflight、ResourceSlot 解析与 Handle binding。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from typing import Any, Protocol

from unilabos.workflow.graph_validation import (
    declared_handle_type_matches,
    workflow_schema_matches_handle_type,
)
from unilabos.workflow.json_codec import clone_json
from unilabos.workflow.models import validate_uuid
from unilabos.workflow.schema import (
    WorkflowSchemaError,
    normalize_value,
    parse_input_contract,
    parse_value_schema,
)
from unilabos.workflow.workflow_io import (
    ValidatedWorkflowIO,
    validate_workflow_graph_io,
)

_EMPTY_INPUT_CONTRACT = {"version": 1, "parameters": []}
_STABLE_ERROR_CODES = {"invalid_input", "not_found", "conflict"}


class TaskInputError(ValueError):
    """可在 transport 边界稳定映射的 Task input preflight 失败。"""

    def __init__(self, code: str = "invalid_input") -> None:
        if code not in _STABLE_ERROR_CODES:
            code = "conflict"
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ResolvedResourceSlot:
    """Material authority 返回给 Workflow 的最小 immutable identity。"""

    uuid: str
    resource_template_uuid: str


class ResourceSlotResolver(Protocol):
    """由未来 Material Module 实现的 ResourceSlot lookup port。"""

    def resolve(
        self,
        *,
        material_uuid: str,
        allowed_resource_template_uuids: tuple[str, ...] | None,
    ) -> ResolvedResourceSlot: ...


class UnconfiguredResourceSlotResolver:
    """02H production 的显式 fail-closed Material adapter。"""

    def resolve(
        self,
        *,
        material_uuid: str,
        allowed_resource_template_uuids: tuple[str, ...] | None,
    ) -> ResolvedResourceSlot:
        del material_uuid, allowed_resource_template_uuids
        raise TaskInputError("conflict")


@dataclass(frozen=True)
class PreparedTaskInput:
    """Task transaction 在首次 INSERT 前准备好的全部 JSON 事实。"""

    workflow_snapshot: dict[str, Any]
    resolved_input: dict[str, Any]
    execution_plan: dict[str, Any]
    jobs: list[dict[str, Any]]
    material_root_uuids: tuple[str, ...]


def preflight_task_input(
    *,
    graph: Mapping[str, Any],
    raw_input: Mapping[str, Any],
    execution_plan: Mapping[str, Any],
    jobs: Sequence[Mapping[str, Any]],
    resource_resolver: ResourceSlotResolver | None,
) -> PreparedTaskInput:
    """在 Task/Job 写入前完成合同、slot、binding 与 provider 校验。"""

    try:
        workflow_snapshot = clone_json(graph)
        if type(workflow_snapshot) is not dict:
            raise TaskInputError()
        validated_io = validate_workflow_graph_io(workflow_snapshot)
        contract = validated_io.input_contract.to_dict()
        resolved_input = _resolve_input_values(
            contract,
            raw_input,
            resource_resolver=resource_resolver,
        )
        bindings = _task_input_bindings(validated_io)
        bound_plan, bound_jobs = _bind_active_plan(
            workflow_snapshot,
            execution_plan,
            jobs,
            bindings=bindings,
            resolved_input=resolved_input,
            resource_resolver=resource_resolver,
        )
        material_roots = _material_root_uuids_from_contract(
            workflow_snapshot,
            resolved_input,
            bound_plan,
            contract=contract,
        )
    except TaskInputError:
        raise
    except (KeyError, TypeError, ValueError, WorkflowSchemaError):
        raise TaskInputError("invalid_input") from None
    return PreparedTaskInput(
        workflow_snapshot=workflow_snapshot,
        resolved_input=clone_json(resolved_input),
        execution_plan=bound_plan,
        jobs=bound_jobs,
        material_root_uuids=material_roots,
    )


def material_root_uuids_from_task_snapshot(
    graph: Mapping[str, Any],
    resolved_input: Mapping[str, Any],
    execution_plan: Mapping[str, Any],
) -> tuple[str, ...]:
    """按 frozen Input Contract 与 active typed target Handles 提取 roots。"""

    try:
        contract = _parse_frozen_input_contract(graph)
        return _material_root_uuids_from_contract(
            graph,
            resolved_input,
            execution_plan,
            contract=contract,
        )
    except TaskInputError:
        raise
    except (KeyError, TypeError, ValueError, WorkflowSchemaError):
        raise TaskInputError("invalid_input") from None


def _material_root_uuids_from_contract(
    graph: Mapping[str, Any],
    resolved_input: Mapping[str, Any],
    execution_plan: Mapping[str, Any],
    *,
    contract: Mapping[str, Any],
) -> tuple[str, ...]:
    if type(resolved_input) is not dict:
        raise TaskInputError()
    parameters = contract["parameters"]
    expected_names = {parameter["name"] for parameter in parameters}
    if set(resolved_input) != expected_names:
        raise TaskInputError()
    roots: set[str] = set()
    for parameter in parameters:
        roots.update(
            _material_roots_for_value(
                parameter["schema"],
                resolved_input[parameter["name"]],
            )
        )
    plan_nodes = execution_plan.get("nodes")
    graph_handles = graph.get("handle_templates")
    graph_nodes = graph.get("nodes")
    if (
        type(plan_nodes) is not list
        or type(graph_handles) is not list
        or type(graph_nodes) is not list
    ):
        raise TaskInputError()
    handles: dict[str, Mapping[str, Any]] = {}
    for handle in graph_handles:
        if type(handle) is not dict or type(handle.get("uuid")) is not str:
            raise TaskInputError()
        if handle["uuid"] in handles:
            raise TaskInputError()
        handles[handle["uuid"]] = handle
    nodes: dict[str, Mapping[str, Any]] = {}
    for node in graph_nodes:
        if type(node) is not dict or type(node.get("uuid")) is not str:
            raise TaskInputError()
        if node["uuid"] in nodes:
            raise TaskInputError()
        nodes[node["uuid"]] = node
    seen_plan_nodes: set[str] = set()
    for planned_node in plan_nodes:
        if type(planned_node) is not dict or type(planned_node.get("uuid")) is not str:
            raise TaskInputError()
        node_uuid = planned_node["uuid"]
        if node_uuid in seen_plan_nodes:
            raise TaskInputError()
        seen_plan_nodes.add(node_uuid)
        graph_node = nodes.get(node_uuid)
        if graph_node is None:
            raise TaskInputError()
        param = planned_node.get("param")
        if type(param) is not dict:
            raise TaskInputError()
        template_uuid = graph_node.get("workflow_node_template_uuid")
        target_handles = (
            handle
            for handle in handles.values()
            if handle.get("workflow_node_template_uuid") == template_uuid
            and handle.get("io_type") == "target"
        )
        for handle in target_handles:
            data_key = _final_target_data_key(_handle_data_key(handle))
            if not data_key:
                raise TaskInputError()
            value_schema = _typed_handle_value_schema(handle)
            if (
                value_schema is None
                or not _schema_contains_resource_slot(value_schema)
                or data_key not in param
                or param[data_key] is None
            ):
                continue
            roots.update(_material_roots_for_value(value_schema, param[data_key]))
    return tuple(sorted(roots))


def _material_roots_for_value(
    schema: Mapping[str, Any],
    value: Any,
) -> tuple[str, ...]:
    if "anyOf" in schema:
        if value is None:
            return ()
        members = schema["anyOf"]
        concrete = next(
            (member for member in members if member.get("type") != "null"),
            None,
        )
        if concrete is None:
            raise TaskInputError()
        return _material_roots_for_value(concrete, value)
    if schema.get("$slot") == "ResourceSlot":
        if type(value) is not dict or set(value) != {
            "uuid",
            "resource_template_uuid",
        }:
            raise TaskInputError()
        try:
            material_uuid = validate_uuid(value["uuid"])
            template_uuid = validate_uuid(value["resource_template_uuid"])
        except (TypeError, ValueError):
            raise TaskInputError() from None
        allowed = schema.get("allowed_resource_template_uuids")
        if allowed is not None and template_uuid not in allowed:
            raise TaskInputError()
        return (material_uuid,)
    if schema.get("type") == "array":
        if type(value) is not list:
            raise TaskInputError()
        return tuple(
            root
            for item in value
            for root in _material_roots_for_value(schema["items"], item)
        )
    return ()


def _schema_contains_resource_slot(schema: Mapping[str, Any]) -> bool:
    if schema.get("$slot") == "ResourceSlot":
        return True
    if "anyOf" in schema:
        return any(
            _schema_contains_resource_slot(member)
            for member in schema.get("anyOf", ())
            if type(member) is dict
        )
    if schema.get("type") == "array" and type(schema.get("items")) is dict:
        return _schema_contains_resource_slot(schema["items"])
    return False


def _contains_closed_resource_slot_value(
    schema: Mapping[str, Any],
    value: Any,
) -> bool:
    if "anyOf" in schema:
        if value is None:
            return False
        return any(
            _contains_closed_resource_slot_value(member, value)
            for member in schema.get("anyOf", ())
            if type(member) is dict and member.get("type") != "null"
        )
    if schema.get("$slot") == "ResourceSlot":
        return type(value) is dict and "resource_template_uuid" in value
    if schema.get("type") == "array" and type(schema.get("items")) is dict:
        return type(value) is list and any(
            _contains_closed_resource_slot_value(schema["items"], item)
            for item in value
        )
    return False


def _apply_handle_slot_allowlist(
    schema: Mapping[str, Any],
    allowed: Any,
) -> dict[str, Any]:
    result = clone_json(dict(schema))
    if result.get("$slot") == "ResourceSlot":
        if allowed is not None:
            result["allowed_resource_template_uuids"] = clone_json(allowed)
        return result
    if "anyOf" in result:
        result["anyOf"] = [
            _apply_handle_slot_allowlist(member, allowed)
            if type(member) is dict
            else member
            for member in result["anyOf"]
        ]
    if result.get("type") == "array" and type(result.get("items")) is dict:
        result["items"] = _apply_handle_slot_allowlist(result["items"], allowed)
    return result


def _strip_handle_schema_annotations(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Remove catalog presentation metadata before strict execution parsing."""

    result = clone_json(dict(schema))
    result.pop("title", None)
    result.pop("description", None)
    if "anyOf" in result:
        result["anyOf"] = [
            _strip_handle_schema_annotations(member)
            if type(member) is dict
            else member
            for member in result["anyOf"]
        ]
    if result.get("type") == "array" and type(result.get("items")) is dict:
        result["items"] = _strip_handle_schema_annotations(result["items"])
    return result


def _typed_handle_value_schema(
    handle: Mapping[str, Any],
) -> dict[str, Any] | None:
    meta_data = handle.get("meta_data", {})
    if type(meta_data) is not dict:
        raise TaskInputError()
    unilab = meta_data.get("unilab", {})
    if type(unilab) is not dict:
        raise TaskInputError()
    raw_schema = unilab.get("value_schema")
    if raw_schema is None:
        if handle.get("type") != "ResourceSlot":
            return None
        raw_schema = {"$slot": "ResourceSlot"}
    if type(raw_schema) is not dict:
        raise TaskInputError()
    if not _schema_contains_resource_slot(raw_schema):
        return None
    with_allowlist = _apply_handle_slot_allowlist(
        raw_schema,
        unilab.get("allowed_resource_template_uuids"),
    )
    execution_schema = _strip_handle_schema_annotations(with_allowlist)
    return parse_value_schema(execution_schema).to_dict()


def _parse_frozen_input_contract(
    graph: Mapping[str, Any],
) -> dict[str, Any]:
    """读取已创建 Task 的 immutable input contract，不重验其历史 output。"""

    workflow = graph.get("workflow")
    if type(workflow) is not dict:
        raise TaskInputError()
    meta_data = workflow.get("meta_data", {})
    if type(meta_data) is not dict:
        raise TaskInputError()
    if "unilab" not in meta_data:
        raw_contract: Any = _EMPTY_INPUT_CONTRACT
    else:
        unilab = meta_data["unilab"]
        if type(unilab) is not dict:
            raise TaskInputError()
        raw_contract = unilab.get("input_contract", _EMPTY_INPUT_CONTRACT)
    return parse_input_contract(raw_contract).to_dict()


def _resolve_input_values(
    contract: Mapping[str, Any],
    raw_input: Mapping[str, Any],
    *,
    resource_resolver: ResourceSlotResolver | None,
) -> dict[str, Any]:
    if type(raw_input) is not dict or any(type(key) is not str for key in raw_input):
        raise TaskInputError()
    parameters = contract.get("parameters")
    if type(parameters) is not list:
        raise TaskInputError()
    names = {parameter["name"] for parameter in parameters}
    if any(key not in names for key in raw_input):
        raise TaskInputError()

    resolved: dict[str, Any] = {}
    for parameter in parameters:
        name = parameter["name"]
        supplied = name in raw_input and raw_input[name] is not None
        if not supplied:
            if parameter["required"]:
                raise TaskInputError()
            raw_value = clone_json(parameter["default"])
        else:
            raw_value = raw_input[name]
        value_schema = parse_value_schema(parameter["schema"])
        normalized = normalize_value(value_schema, raw_value)
        resolved[name] = _resolve_slots(
            parameter["schema"],
            normalized,
            resource_resolver=resource_resolver,
        )
    return resolved


def _resolve_slots(
    schema: Mapping[str, Any],
    value: Any,
    *,
    resource_resolver: ResourceSlotResolver | None,
) -> Any:
    if "anyOf" in schema:
        if value is None:
            return None
        members = schema["anyOf"]
        return _resolve_slots(
            members[0],
            value,
            resource_resolver=resource_resolver,
        )
    if schema.get("$slot") == "ResourceSlot":
        allowed_raw = schema.get("allowed_resource_template_uuids")
        allowed = tuple(allowed_raw) if allowed_raw is not None else None
        return _resolve_one_slot(
            value,
            allowed_resource_template_uuids=allowed,
            resource_resolver=resource_resolver,
        )
    if schema.get("type") == "array":
        return [
            _resolve_slots(
                schema["items"],
                item,
                resource_resolver=resource_resolver,
            )
            for item in value
        ]
    return clone_json(value)


def _resolve_one_slot(
    value: Any,
    *,
    allowed_resource_template_uuids: tuple[str, ...] | None,
    resource_resolver: ResourceSlotResolver | None,
) -> dict[str, str]:
    material_uuid = value["uuid"]
    if resource_resolver is None:
        raise TaskInputError("conflict")
    try:
        returned = resource_resolver.resolve(
            material_uuid=material_uuid,
            allowed_resource_template_uuids=allowed_resource_template_uuids,
        )
    except TaskInputError:
        raise
    # 这是外部 Material adapter 的 fail-closed 边界；未知实现异常不能越过
    # preflight 形成 500 或 partial write。
    except Exception as error:  # noqa: BLE001
        code = getattr(error, "code", "conflict")
        raise TaskInputError(code) from None

    identity = _closed_resolved_slot(returned)
    if identity.uuid != material_uuid:
        raise TaskInputError()
    if (
        allowed_resource_template_uuids is not None
        and identity.resource_template_uuid not in allowed_resource_template_uuids
    ):
        raise TaskInputError()
    return {
        "uuid": identity.uuid,
        "resource_template_uuid": identity.resource_template_uuid,
    }


def _closed_resolved_slot(value: Any) -> ResolvedResourceSlot:
    if is_dataclass(value) and not isinstance(value, type):
        if {field.name for field in fields(value)} != {
            "uuid",
            "resource_template_uuid",
        }:
            raise TaskInputError()
        parameters = getattr(type(value), "__dataclass_params__", None)
        if parameters is None or not parameters.frozen:
            raise TaskInputError()
        raw_uuid = value.uuid
        raw_template_uuid = value.resource_template_uuid
    else:
        raise TaskInputError()
    if type(raw_uuid) is not str or type(raw_template_uuid) is not str:
        raise TaskInputError()
    try:
        identity = validate_uuid(raw_uuid)
        template_identity = validate_uuid(raw_template_uuid)
    except (TypeError, ValueError):
        raise TaskInputError() from None
    return ResolvedResourceSlot(identity, template_identity)


def _task_input_bindings(
    validated_io: ValidatedWorkflowIO,
) -> dict[str, dict[str, Any]]:
    contract = validated_io.input_contract.to_dict()
    parameters = {parameter["name"]: parameter for parameter in contract["parameters"]}
    bindings: dict[str, dict[str, Any]] = {}
    for node_uuid, node_bindings in validated_io.input_bindings.items():
        for handle_uuid, binding in node_bindings.items():
            parameter_name = binding["parameter"]
            parameter = parameters[parameter_name]
            bindings[f"{node_uuid}:{handle_uuid}"] = {
                "parameter": parameter_name,
                "schema": clone_json(parameter["schema"]),
            }
    return bindings


def _bind_active_plan(
    graph: Mapping[str, Any],
    execution_plan: Mapping[str, Any],
    jobs: Sequence[Mapping[str, Any]],
    *,
    bindings: Mapping[str, Mapping[str, Any]],
    resolved_input: Mapping[str, Any],
    resource_resolver: ResourceSlotResolver | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    plan = clone_json(execution_plan)
    bound_jobs = clone_json(list(jobs))
    if type(plan) is not dict or type(plan.get("nodes")) is not list:
        raise TaskInputError()
    graph_nodes = {
        node["uuid"]: node
        for node in graph["nodes"]
        if type(node) is dict and type(node.get("uuid")) is str
    }
    handles = {
        handle["uuid"]: handle
        for handle in graph["handle_templates"]
        if type(handle) is dict and type(handle.get("uuid")) is str
    }
    active_nodes = {
        node["uuid"]: node
        for node in plan["nodes"]
        if type(node) is dict and type(node.get("uuid")) is str
    }
    if len(active_nodes) != len(plan["nodes"]):
        raise TaskInputError()
    job_by_node: dict[str, dict[str, Any]] = {}
    for job in bound_jobs:
        if type(job) is not dict or type(job.get("workflow_node_uuid")) is not str:
            raise TaskInputError()
        node_uuid = job["workflow_node_uuid"]
        if node_uuid in job_by_node:
            raise TaskInputError()
        job_by_node[node_uuid] = job
    if set(job_by_node) != set(active_nodes):
        raise TaskInputError()

    active_edges: dict[tuple[str, str], int] = {}
    plan_edges = plan.get("edges")
    if type(plan_edges) is not list:
        raise TaskInputError()
    for edge in plan_edges:
        if type(edge) is not dict:
            raise TaskInputError()
        if edge.get("dependency_only") is True:
            continue
        node_uuid = edge.get("target_node_uuid")
        handle_uuid = edge.get("target_handle_uuid")
        if type(node_uuid) is str and type(handle_uuid) is str:
            edge_target = (node_uuid, handle_uuid)
            active_edges[edge_target] = active_edges.get(edge_target, 0) + 1

    for node_uuid, planned_node in active_nodes.items():
        graph_node = graph_nodes.get(node_uuid)
        if graph_node is None:
            raise TaskInputError()
        raw_param = graph_node.get("param", {})
        if type(raw_param) is not dict:
            raise TaskInputError()
        plan_param = clone_json(raw_param)
        job_param = clone_json(raw_param)
        snapshot_param = clone_json(raw_param)
        template_uuid = graph_node.get("workflow_node_template_uuid")
        target_handles = [
            handle
            for handle in handles.values()
            if handle.get("workflow_node_template_uuid") == template_uuid
            and handle.get("io_type") == "target"
        ]
        for handle in target_handles:
            handle_uuid = handle["uuid"]
            data_key = _final_target_data_key(_handle_data_key(handle))
            if not data_key:
                raise TaskInputError()
            binding = bindings.get(f"{node_uuid}:{handle_uuid}")
            has_static = data_key in raw_param and raw_param[data_key] is not None
            edge_count = active_edges.get((node_uuid, handle_uuid), 0)
            has_binding = binding is not None
            provider_count = int(has_static) + edge_count + int(has_binding)
            if provider_count > 1:
                raise TaskInputError()
            binding_value = (
                resolved_input[binding["parameter"]] if binding is not None else None
            )
            value_schema = _typed_handle_value_schema(handle)
            if (
                has_static
                and value_schema is not None
                and _schema_contains_resource_slot(value_schema)
            ):
                raw_static = raw_param[data_key]
                if _contains_closed_resource_slot_value(value_schema, raw_static):
                    # Applied Authoring graphs already carry the Material
                    # Authority-owned identity. Validate its exact closed shape
                    # and Handle allowlist without performing a second lookup.
                    _material_roots_for_value(value_schema, raw_static)
                    resolved_static = clone_json(raw_static)
                else:
                    normalized_static = normalize_value(
                        parse_value_schema(value_schema),
                        raw_static,
                    )
                    resolved_static = _resolve_slots(
                        value_schema,
                        normalized_static,
                        resource_resolver=resource_resolver,
                    )
                plan_param[data_key] = clone_json(resolved_static)
                job_param[data_key] = clone_json(resolved_static)
                snapshot_param[data_key] = clone_json(resolved_static)
            if (
                binding is not None
                and binding_value is not None
                and value_schema is not None
                and _schema_contains_resource_slot(value_schema)
            ):
                # Workflow input 已由 Material Authority 规范化；这里只按
                # A1 target Handle schema/allowlist 验证 authority-owned identity，
                # 不做第二次 lookup。
                _material_roots_for_value(value_schema, binding_value)
            if binding is not None and not workflow_schema_matches_handle_type(
                binding["schema"],
                handle.get("type"),
            ):
                raise TaskInputError()
            if has_static and not declared_handle_type_matches(
                plan_param[data_key],
                handle.get("type"),
            ):
                raise TaskInputError()
            if has_binding and not declared_handle_type_matches(
                binding_value,
                handle.get("type"),
            ):
                raise TaskInputError()
            if handle.get("required") and (
                provider_count != 1 or (has_binding and binding_value is None)
            ):
                raise TaskInputError()
            if binding is not None:
                plan_param[data_key] = clone_json(binding_value)
                job_param[data_key] = clone_json(binding_value)
        graph_node["param"] = snapshot_param
        planned_node["param"] = plan_param
        job_by_node[node_uuid]["param"] = job_param
    return plan, bound_jobs


def _handle_data_key(handle: Mapping[str, Any]) -> str:
    return str(handle.get("data_key") or handle.get("handle_key") or "").strip()


def _final_target_data_key(data_key: str) -> str:
    return data_key.split("@@@")[-1].strip()


__all__ = [
    "PreparedTaskInput",
    "ResolvedResourceSlot",
    "ResourceSlotResolver",
    "TaskInputError",
    "UnconfiguredResourceSlotResolver",
    "material_root_uuids_from_task_snapshot",
    "preflight_task_input",
]
