"""Workflow Input/Output 合同、binding identity 与 schema assignability。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from unilabos.workflow.models import WorkflowNodeWrite, validate_uuid
from unilabos.workflow.schema import (
    WorkflowInputContract,
    WorkflowOutputContract,
    WorkflowSchemaError,
    WorkflowValueSchema,
    normalize_value,
    parse_input_contract,
    parse_output_contract,
    parse_value_schema,
)

_EMPTY_INPUT_CONTRACT = {"version": 1, "parameters": []}
_EMPTY_OUTPUT_CONTRACT = {"version": 1, "outputs": []}


class WorkflowIOValidationError(ValueError):
    """Graph 中的 Workflow I/O authority 不自洽。"""


@dataclass(frozen=True)
class ValidatedWorkflowIO:
    """一次公共校验产生的 canonical I/O 事实。"""

    input_contract: WorkflowInputContract
    output_contract: WorkflowOutputContract
    input_bindings: Mapping[str, Mapping[str, Mapping[str, str]]]
    output_bindings: Mapping[str, Mapping[str, str]]


def validate_workflow_io(
    *,
    nodes: Mapping[str, WorkflowNodeWrite],
    handles: Mapping[str, Mapping[str, Any]],
    workflow_meta_data: Mapping[str, Any],
    node_meta_data: Mapping[str, Mapping[str, Any]],
) -> ValidatedWorkflowIO:
    """在 transport 之外验证完整 Workflow I/O authority。"""

    try:
        unilab = _unilab_metadata(workflow_meta_data, label="Workflow")
        input_contract = parse_input_contract(
            unilab.get("input_contract", _EMPTY_INPUT_CONTRACT)
        )
        output_contract = parse_output_contract(
            unilab.get("output_contract", _EMPTY_OUTPUT_CONTRACT)
        )
        input_parameters = {
            item["name"]: item for item in input_contract.to_dict()["parameters"]
        }
        input_bindings = _validate_input_bindings(
            nodes=nodes,
            handles=handles,
            node_meta_data=node_meta_data,
            input_parameters=input_parameters,
        )
        output_bindings = _validate_output_bindings(
            nodes=nodes,
            handles=handles,
            raw_bindings=unilab.get("output_bindings", {}),
            input_parameters=input_parameters,
            output_contract=output_contract,
        )
    except WorkflowIOValidationError:
        raise
    except (KeyError, TypeError, ValueError, WorkflowSchemaError) as exc:
        raise WorkflowIOValidationError("Workflow I/O 合同无效") from exc
    return ValidatedWorkflowIO(
        input_contract=input_contract,
        output_contract=output_contract,
        input_bindings=MappingProxyType(input_bindings),
        output_bindings=MappingProxyType(output_bindings),
    )


def validate_workflow_graph_io(graph: Mapping[str, Any]) -> ValidatedWorkflowIO:
    """从 Backend-shaped graph 构造并验证唯一的 Workflow I/O authority。"""

    try:
        workflow = graph.get("workflow")
        raw_nodes = graph.get("nodes")
        raw_handles = graph.get("handle_templates")
        if (
            not isinstance(workflow, Mapping)
            or not isinstance(raw_nodes, list)
            or not isinstance(raw_handles, list)
        ):
            raise WorkflowIOValidationError("Workflow graph I/O projection 无效")

        nodes: dict[str, WorkflowNodeWrite] = {}
        node_meta_data: dict[str, Mapping[str, Any]] = {}
        for raw_node in raw_nodes:
            if not isinstance(raw_node, Mapping):
                raise WorkflowIOValidationError("Workflow Node projection 无效")
            node = WorkflowNodeWrite.model_validate(raw_node)
            if node.uuid in nodes:
                raise WorkflowIOValidationError("Workflow Node UUID 重复")
            nodes[node.uuid] = node
            node_meta_data[node.uuid] = node.meta_data

        handles: dict[str, Mapping[str, Any]] = {}
        for handle in raw_handles:
            if not isinstance(handle, Mapping):
                raise WorkflowIOValidationError("Workflow Handle projection 无效")
            handle_uuid = handle.get("uuid")
            if not isinstance(handle_uuid, str):
                raise WorkflowIOValidationError("Workflow Handle UUID 无效或重复")
            canonical_handle_uuid = validate_uuid(handle_uuid)
            if canonical_handle_uuid != handle_uuid or handle_uuid in handles:
                raise WorkflowIOValidationError("Workflow Handle UUID 无效或重复")
            handles[handle_uuid] = handle

        return validate_workflow_io(
            nodes=nodes,
            handles=handles,
            workflow_meta_data=workflow.get("meta_data", {}),
            node_meta_data=node_meta_data,
        )
    except WorkflowIOValidationError:
        raise
    except (KeyError, TypeError, ValueError, WorkflowSchemaError) as exc:
        raise WorkflowIOValidationError("Workflow graph I/O projection 无效") from exc


def handle_value_schema(handle: Mapping[str, Any]) -> WorkflowValueSchema:
    """从 A1 Handle projection 读取 canonical value schema。"""

    meta_data = handle.get("meta_data", {})
    unilab = _unilab_metadata(meta_data, label="Handle")
    raw_schema = unilab.get("value_schema")
    if raw_schema is None:
        raw_schema = _legacy_handle_schema(handle.get("type"))
    if not isinstance(raw_schema, Mapping):
        raise WorkflowIOValidationError("Handle value_schema 无效")
    schema = _value_set_schema(_plain_mapping(raw_schema))
    allowlist = unilab.get("allowed_resource_template_uuids")
    if allowlist is not None:
        schema, applied = _apply_slot_allowlist(schema, allowlist)
        if not applied:
            raise WorkflowIOValidationError("非 ResourceSlot Handle 携带物料模板约束")
    try:
        return parse_value_schema(schema)
    except WorkflowSchemaError as exc:
        raise WorkflowIOValidationError("Handle value_schema 无效") from exc


def _value_set_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """从 A1 JSON Schema property 投影出只影响可赋值集的 I1 schema。"""

    for key in ("default", "title", "description"):
        schema.pop(key, None)
    if "anyOf" in schema:
        members = schema.get("anyOf")
        if not isinstance(members, list):
            return schema
        schema["anyOf"] = [
            _value_set_schema(member) if isinstance(member, dict) else member
            for member in members
        ]
    items = schema.get("items")
    if isinstance(items, dict):
        schema["items"] = _value_set_schema(items)
    if schema.get("type") == "object" and schema.get("additionalProperties") is True:
        schema.pop("additionalProperties")
    return schema


def schema_is_assignable(
    producer: WorkflowValueSchema | Mapping[str, Any],
    consumer: WorkflowValueSchema | Mapping[str, Any],
) -> bool:
    """证明 producer 的全部 canonical value 都可赋给 consumer。"""

    try:
        producer_schema = (
            producer
            if isinstance(producer, WorkflowValueSchema)
            else parse_value_schema(producer)
        )
        consumer_schema = (
            consumer
            if isinstance(consumer, WorkflowValueSchema)
            else parse_value_schema(consumer)
        )
    except WorkflowSchemaError:
        return False
    return _schema_dict_is_assignable(
        producer_schema.to_dict(),
        consumer_schema.to_dict(),
    )


def resource_slot_passthrough_is_compatible(
    input_schema: WorkflowValueSchema | Mapping[str, Any],
    output_schema: WorkflowValueSchema | Mapping[str, Any],
    *,
    exact: bool = False,
) -> bool:
    """验证同名 ResourceSlot input/output 是否可作为安全透传合同。"""

    try:
        parsed_input = (
            input_schema
            if isinstance(input_schema, WorkflowValueSchema)
            else parse_value_schema(input_schema)
        )
        parsed_output = (
            output_schema
            if isinstance(output_schema, WorkflowValueSchema)
            else parse_value_schema(output_schema)
        )
    except WorkflowSchemaError:
        return False
    input_dict = parsed_input.to_dict()
    output_dict = parsed_output.to_dict()
    if not _schema_contains_resource_slot(input_dict):
        return False
    if exact:
        return input_dict == output_dict
    return _schema_dict_is_assignable(input_dict, output_dict)


def _validate_input_bindings(
    *,
    nodes: Mapping[str, WorkflowNodeWrite],
    handles: Mapping[str, Mapping[str, Any]],
    node_meta_data: Mapping[str, Mapping[str, Any]],
    input_parameters: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Mapping[str, str]]]:
    result: dict[str, Mapping[str, Mapping[str, str]]] = {}
    for node_uuid, node in nodes.items():
        unilab = _unilab_metadata(
            node_meta_data.get(node_uuid, {}),
            label="Node",
        )
        raw_bindings = unilab.get("input_bindings", {})
        if not isinstance(raw_bindings, Mapping):
            raise WorkflowIOValidationError("input_bindings 必须是对象")
        if raw_bindings and node.workflow_node_template_uuid is None:
            raise WorkflowIOValidationError("无模板节点不能声明 input_bindings")
        bindings: dict[str, Mapping[str, str]] = {}
        for handle_uuid, raw_binding in raw_bindings.items():
            handle = handles.get(handle_uuid)
            if (
                not isinstance(handle_uuid, str)
                or handle is None
                or handle.get("workflow_node_template_uuid")
                != node.workflow_node_template_uuid
                or handle.get("io_type") != "target"
            ):
                raise WorkflowIOValidationError(
                    "input_binding 未引用本节点的 target Handle"
                )
            if (
                not isinstance(raw_binding, Mapping)
                or set(raw_binding) != {"parameter"}
                or not isinstance(raw_binding.get("parameter"), str)
            ):
                raise WorkflowIOValidationError("input_binding 必须是闭合对象")
            parameter_name = str(raw_binding["parameter"])
            parameter = input_parameters.get(parameter_name)
            if parameter is None or not schema_is_assignable(
                parameter["schema"],
                handle_value_schema(handle),
            ):
                raise WorkflowIOValidationError(
                    "input_binding 与 Workflow 参数类型不兼容: "
                    f"parameter={parameter_name!r}, handle={handle_uuid!r}"
                )
            bindings[handle_uuid] = MappingProxyType({"parameter": parameter_name})
        result[node_uuid] = MappingProxyType(bindings)
    return result


def _validate_output_bindings(
    *,
    nodes: Mapping[str, WorkflowNodeWrite],
    handles: Mapping[str, Mapping[str, Any]],
    raw_bindings: Any,
    input_parameters: Mapping[str, Mapping[str, Any]],
    output_contract: WorkflowOutputContract,
) -> dict[str, Mapping[str, str]]:
    outputs = {item["name"]: item for item in output_contract.to_dict()["outputs"]}
    if not isinstance(raw_bindings, Mapping) or set(raw_bindings) != set(outputs):
        raise WorkflowIOValidationError("Workflow output bindings 不完整")
    _validate_resource_slot_output_authority(
        input_parameters=input_parameters,
        outputs=outputs,
        raw_bindings=raw_bindings,
    )
    result: dict[str, Mapping[str, str]] = {}
    for output_name, output in outputs.items():
        binding = raw_bindings[output_name]
        if not isinstance(binding, Mapping):
            raise WorkflowIOValidationError("Workflow output binding 必须是对象")
        kind = binding.get("kind")
        if kind == "workflow_input":
            if set(binding) != {"kind", "parameter"}:
                raise WorkflowIOValidationError("workflow_input binding 不闭合")
            parameter_name = binding.get("parameter")
            parameter = (
                input_parameters.get(parameter_name)
                if isinstance(parameter_name, str)
                else None
            )
            if parameter is None or not schema_is_assignable(
                parameter["schema"],
                output["schema"],
            ):
                raise WorkflowIOValidationError("Workflow input 不能满足 output schema")
            normalized = {
                "kind": "workflow_input",
                "parameter": parameter_name,
            }
        elif kind == "node_output":
            if set(binding) != {
                "kind",
                "workflow_node_uuid",
                "source_handle_uuid",
            }:
                raise WorkflowIOValidationError("node_output binding 不闭合")
            node_uuid = binding.get("workflow_node_uuid")
            handle_uuid = binding.get("source_handle_uuid")
            node = nodes.get(node_uuid) if isinstance(node_uuid, str) else None
            handle = handles.get(handle_uuid) if isinstance(handle_uuid, str) else None
            if (
                node is None
                or handle is None
                or handle.get("workflow_node_template_uuid")
                != node.workflow_node_template_uuid
                or handle.get("io_type") != "source"
            ):
                raise WorkflowIOValidationError(
                    "node_output 未引用本 Node 的 source Handle"
                )
            if not schema_is_assignable(
                handle_value_schema(handle),
                output["schema"],
            ):
                raise WorkflowIOValidationError(
                    "Node output 不能满足 Workflow output schema"
                )
            normalized = {
                "kind": "node_output",
                "workflow_node_uuid": node_uuid,
                "source_handle_uuid": handle_uuid,
            }
        else:
            raise WorkflowIOValidationError("未知 Workflow output binding kind")
        result[output_name] = MappingProxyType(normalized)
    return result


def _validate_resource_slot_output_authority(
    *,
    input_parameters: Mapping[str, Mapping[str, Any]],
    outputs: Mapping[str, Mapping[str, Any]],
    raw_bindings: Mapping[str, Any],
) -> None:
    for output_name, output in outputs.items():
        if not output.get("implicit", False):
            continue
        parameter = input_parameters.get(output_name)
        binding = raw_bindings.get(output_name)
        if (
            parameter is None
            or not resource_slot_passthrough_is_compatible(
                parameter["schema"],
                output["schema"],
                exact=True,
            )
            or not isinstance(binding, Mapping)
            or dict(binding) != {"kind": "workflow_input", "parameter": output_name}
        ):
            raise WorkflowIOValidationError(
                "implicit output 必须是 server-managed 同名 ResourceSlot 透传"
            )

    for parameter_name, parameter in input_parameters.items():
        if not _schema_contains_resource_slot(parameter["schema"]):
            continue
        output = outputs.get(parameter_name)
        if output is None or not resource_slot_passthrough_is_compatible(
            parameter["schema"],
            output["schema"],
            exact=bool(output.get("implicit", False)),
        ):
            raise WorkflowIOValidationError("ResourceSlot input 缺少兼容的同名 output")


def _schema_dict_is_assignable(
    producer: Mapping[str, Any],
    consumer: Mapping[str, Any],
) -> bool:
    producer_base, producer_nullable = _unwrap_nullable(producer)
    consumer_base, consumer_nullable = _unwrap_nullable(consumer)
    if producer_nullable and not consumer_nullable:
        return False

    if "$slot" in producer_base or "$slot" in consumer_base:
        producer_slot = producer_base.get("$slot")
        consumer_slot = consumer_base.get("$slot")
        if producer_slot != consumer_slot:
            return False
        if producer_slot == "SiteRef":
            return True
        if producer_slot != "ResourceSlot":
            return False
        producer_allowed = producer_base.get("allowed_resource_template_uuids")
        consumer_allowed = consumer_base.get("allowed_resource_template_uuids")
        if consumer_allowed is None:
            return True
        if producer_allowed is None:
            return False
        return set(producer_allowed).issubset(consumer_allowed)

    producer_kind = producer_base.get("type")
    consumer_kind = consumer_base.get("type")
    if producer_kind != consumer_kind and not (
        producer_kind == "integer" and consumer_kind == "number"
    ):
        return False
    if producer_kind == "array":
        producer_items = producer_base.get("items")
        consumer_items = consumer_base.get("items")
        if not isinstance(producer_items, Mapping) or not isinstance(
            consumer_items, Mapping
        ):
            return False
        if not _schema_dict_is_assignable(producer_items, consumer_items):
            return False
        return _bounds_are_subset(
            producer_base,
            consumer_base,
            minimum="minItems",
            maximum="maxItems",
        )
    if producer_kind == "object":
        return consumer_kind == "object"

    producer_enum = producer_base.get("enum")
    consumer_enum = consumer_base.get("enum")
    if producer_enum is not None:
        return all(
            _value_satisfies_schema(value, consumer_base) for value in producer_enum
        )
    if consumer_enum is not None:
        return False
    if producer_kind in {"integer", "number"}:
        return _bounds_are_subset(
            producer_base,
            consumer_base,
            minimum="minimum",
            maximum="maximum",
        )
    if producer_kind == "string":
        return _bounds_are_subset(
            producer_base,
            consumer_base,
            minimum="minLength",
            maximum="maxLength",
        )
    return producer_kind == consumer_kind


def _bounds_are_subset(
    producer: Mapping[str, Any],
    consumer: Mapping[str, Any],
    *,
    minimum: str,
    maximum: str,
) -> bool:
    consumer_minimum = consumer.get(minimum)
    producer_minimum = producer.get(minimum)
    if consumer_minimum is not None and (
        producer_minimum is None or producer_minimum < consumer_minimum
    ):
        return False
    consumer_maximum = consumer.get(maximum)
    producer_maximum = producer.get(maximum)
    return not (
        consumer_maximum is not None
        and (producer_maximum is None or producer_maximum > consumer_maximum)
    )


def _value_satisfies_schema(value: Any, schema: Mapping[str, Any]) -> bool:
    try:
        normalize_value(parse_value_schema(schema), value)
    except WorkflowSchemaError:
        return False
    return True


def _unwrap_nullable(
    schema: Mapping[str, Any],
) -> tuple[Mapping[str, Any], bool]:
    members = schema.get("anyOf")
    if not isinstance(members, list):
        return schema, False
    return members[0], True


def _schema_contains_resource_slot(schema: Mapping[str, Any]) -> bool:
    base, _ = _unwrap_nullable(schema)
    if base.get("$slot") == "ResourceSlot":
        return True
    items = base.get("items")
    return (
        base.get("type") == "array"
        and isinstance(items, Mapping)
        and _schema_contains_resource_slot(items)
    )


def _unilab_metadata(
    meta_data: Mapping[str, Any],
    *,
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(meta_data, Mapping):
        raise WorkflowIOValidationError(f"{label} meta_data 必须是对象")
    unilab = meta_data.get("unilab", {})
    if not isinstance(unilab, Mapping):
        raise WorkflowIOValidationError(f"{label} meta_data.unilab 必须是对象")
    return unilab


def _plain_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): (
            _plain_mapping(item)
            if isinstance(item, Mapping)
            else [
                _plain_mapping(child) if isinstance(child, Mapping) else child
                for child in item
            ]
            if isinstance(item, list)
            else item
        )
        for key, item in value.items()
    }


def _apply_slot_allowlist(
    schema: dict[str, Any],
    allowlist: Any,
) -> tuple[dict[str, Any], bool]:
    result = _plain_mapping(schema)
    if result.get("$slot") == "ResourceSlot":
        existing = result.get("allowed_resource_template_uuids")
        if existing is not None and existing != allowlist:
            raise WorkflowIOValidationError("ResourceSlot allowlist 双真相冲突")
        result["allowed_resource_template_uuids"] = allowlist
        return result, True
    if result.get("type") == "array" and isinstance(result.get("items"), dict):
        items, applied = _apply_slot_allowlist(result["items"], allowlist)
        result["items"] = items
        return result, applied
    if isinstance(result.get("anyOf"), list):
        members: list[Any] = []
        applied = False
        for member in result["anyOf"]:
            if isinstance(member, dict) and member.get("type") != "null":
                member, applied = _apply_slot_allowlist(member, allowlist)
            members.append(member)
        result["anyOf"] = members
        return result, applied
    return result, False


def _legacy_handle_schema(value: Any) -> dict[str, Any]:
    raw = str(value or "").strip().lower()
    scalars = {
        "str": "string",
        "string": "string",
        "int": "integer",
        "integer": "integer",
        "float": "number",
        "number": "number",
        "bool": "boolean",
        "boolean": "boolean",
        "dict": "object",
        "object": "object",
        "json": "object",
    }
    if raw == "resourceslot":
        return {"$slot": "ResourceSlot"}
    if raw == "siteref":
        return {"$slot": "SiteRef"}
    if raw.startswith("list[") and raw.endswith("]"):
        item = _legacy_handle_schema(raw[5:-1])
        return {"type": "array", "items": item}
    if raw in scalars:
        return {"type": scalars[raw]}
    raise WorkflowIOValidationError("Handle 缺少 canonical value_schema")


__all__ = [
    "ValidatedWorkflowIO",
    "WorkflowIOValidationError",
    "handle_value_schema",
    "resource_slot_passthrough_is_compatible",
    "schema_is_assignable",
    "validate_workflow_graph_io",
    "validate_workflow_io",
]
