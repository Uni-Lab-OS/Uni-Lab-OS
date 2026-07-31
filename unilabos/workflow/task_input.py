"""WorkflowTask input preflight、ResourceSlot 解析与 Handle binding。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, fields, is_dataclass
from typing import Any, Protocol

from unilabos.workflow.graph_validation import (
    declared_handle_type_matches,
    workflow_schema_matches_handle_type,
)
from unilabos.workflow.models import validate_uuid
from unilabos.workflow.schema import (
    WorkflowSchemaError,
    normalize_value,
    parse_input_contract,
    parse_value_schema,
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

    resolved_input: dict[str, Any]
    execution_plan: dict[str, Any]
    jobs: list[dict[str, Any]]


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
        contract = _parse_graph_input_contract(graph)
        resolved_input = _resolve_input_values(
            contract,
            raw_input,
            resource_resolver=resource_resolver,
        )
        bindings = _validate_graph_bindings(graph, contract)
        bound_plan, bound_jobs = _bind_active_plan(
            graph,
            execution_plan,
            jobs,
            bindings=bindings,
            resolved_input=resolved_input,
        )
    except TaskInputError:
        raise
    except (KeyError, TypeError, ValueError, WorkflowSchemaError):
        raise TaskInputError("invalid_input") from None
    return PreparedTaskInput(
        resolved_input=deepcopy(resolved_input),
        execution_plan=bound_plan,
        jobs=bound_jobs,
    )


def _parse_graph_input_contract(
    graph: Mapping[str, Any],
) -> dict[str, Any]:
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
            raw_value = deepcopy(parameter["default"])
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
    return deepcopy(value)


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


def _validate_graph_bindings(
    graph: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    graph_nodes = graph.get("nodes")
    graph_handles = graph.get("handle_templates")
    if type(graph_nodes) is not list or type(graph_handles) is not list:
        raise TaskInputError()
    handles: dict[str, Mapping[str, Any]] = {}
    for handle in graph_handles:
        if type(handle) is not dict or type(handle.get("uuid")) is not str:
            raise TaskInputError()
        if handle["uuid"] in handles:
            raise TaskInputError()
        handles[handle["uuid"]] = handle
    parameters = {parameter["name"]: parameter for parameter in contract["parameters"]}

    bindings: dict[str, dict[str, Any]] = {}
    seen_nodes: set[str] = set()
    for node in graph_nodes:
        if type(node) is not dict or type(node.get("uuid")) is not str:
            raise TaskInputError()
        node_uuid = node["uuid"]
        if node_uuid in seen_nodes:
            raise TaskInputError()
        seen_nodes.add(node_uuid)
        meta_data = node.get("meta_data", {})
        if type(meta_data) is not dict:
            raise TaskInputError()
        unilab = meta_data.get("unilab", {})
        if type(unilab) is not dict:
            raise TaskInputError()
        raw_bindings = unilab.get("input_bindings", {})
        if type(raw_bindings) is not dict:
            raise TaskInputError()
        template_uuid = node.get("workflow_node_template_uuid")
        for handle_uuid, raw_binding in raw_bindings.items():
            if type(handle_uuid) is not str:
                raise TaskInputError()
            handle = handles.get(handle_uuid)
            if (
                handle is None
                or handle.get("io_type") != "target"
                or handle.get("workflow_node_template_uuid") != template_uuid
            ):
                raise TaskInputError()
            if (
                type(raw_binding) is not dict
                or set(raw_binding) != {"parameter"}
                or type(raw_binding.get("parameter")) is not str
                or raw_binding["parameter"] not in parameters
            ):
                raise TaskInputError()
            parameter = parameters[raw_binding["parameter"]]
            bindings[f"{node_uuid}:{handle_uuid}"] = {
                "parameter": raw_binding["parameter"],
                "schema": deepcopy(parameter["schema"]),
            }
    return bindings


def _bind_active_plan(
    graph: Mapping[str, Any],
    execution_plan: Mapping[str, Any],
    jobs: Sequence[Mapping[str, Any]],
    *,
    bindings: Mapping[str, Mapping[str, Any]],
    resolved_input: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    plan = deepcopy(execution_plan)
    bound_jobs = deepcopy(list(jobs))
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
        plan_param = deepcopy(raw_param)
        job_param = deepcopy(raw_param)
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
            if binding is not None and not workflow_schema_matches_handle_type(
                binding["schema"],
                handle.get("type"),
            ):
                raise TaskInputError()
            if has_static and not declared_handle_type_matches(
                raw_param[data_key],
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
                plan_param[data_key] = deepcopy(binding_value)
                job_param[data_key] = deepcopy(binding_value)
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
    "preflight_task_input",
]
