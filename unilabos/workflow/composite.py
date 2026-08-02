"""Published Workflow Contract 与 Composite authoring 的唯一深 Module。

R1 只发布 Applied child 的 typed contract。静态展开、compatibility 与 authoring
fixed-point 在后续 C1 rounds 继续放入本 Module，不把算法复制到 PackageCatalog、
TemplateCatalog、HTTP handler 或 frontend。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import rfc8785

from unilabos.workflow.catalog import (
    NodeTemplateImport,
    TemplateCatalogMismatch,
)
from unilabos.workflow.models import validate_uuid
from unilabos.workflow.schema import WorkflowSchemaError
from unilabos.workflow.workflow_io import (
    WorkflowIOValidationError,
    validate_workflow_graph_io,
)


@dataclass(frozen=True, slots=True)
class PublishedWorkflowSource:
    """PackageCatalog 冻结的一条可移植 Workflow source identity。"""

    workflow_uuid: str
    definition_fqid: str
    module: str
    symbol: str
    package_catalog_digest: str
    definition_content_hash: str


class PublishedWorkflowResolver(Protocol):
    """absolute module/symbol 到 PackageCatalog Workflow identity 的 Interface。"""

    def resolve(self, module: str, symbol: str) -> PublishedWorkflowSource: ...


def project_published_workflow_contract(
    *,
    source: PublishedWorkflowSource,
    applied_snapshot: Mapping[str, Any],
    host_node_resource_template_uuid: str | None,
) -> NodeTemplateImport | None:
    """把一个 coherent Applied Workflow snapshot 投影为现有 Catalog aggregate。

    Unapplied/stale child 不是错误：它不进入 Published Catalog。Package source、Applied
    graph 或 host renderer owner 不自洽则 fail closed，绝不合成替代 identity。
    """

    if not isinstance(source, PublishedWorkflowSource):
        raise TypeError("source 必须是 PublishedWorkflowSource")
    host_uuid = _host_owner_uuid(host_node_resource_template_uuid)
    workflow = applied_snapshot.get("workflow")
    applied_source = applied_snapshot.get("applied_source")
    if not isinstance(workflow, Mapping):
        raise TemplateCatalogMismatch("/published_workflow/workflow")
    workflow_uuid = _uuid(workflow.get("uuid"), "/published_workflow/workflow/uuid")
    if workflow_uuid != source.workflow_uuid:
        raise TemplateCatalogMismatch("/published_workflow/source/workflow_uuid")
    revision = workflow.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise TemplateCatalogMismatch("/published_workflow/workflow/revision")
    if not isinstance(applied_source, Mapping):
        return None
    if applied_source.get("workflow_revision") != revision:
        return None
    applied_source_hash = _sha256(
        applied_source.get("source_hash"),
        "/published_workflow/applied_source/source_hash",
    )

    graph = {
        "workflow": _plain(workflow),
        "nodes": _sequence(applied_snapshot.get("nodes"), "/published_workflow/nodes"),
        "edges": _sequence(applied_snapshot.get("edges"), "/published_workflow/edges"),
        "node_templates": _sequence(
            applied_snapshot.get("node_templates"),
            "/published_workflow/node_templates",
        ),
        "handle_templates": _sequence(
            applied_snapshot.get("handle_templates"),
            "/published_workflow/handle_templates",
        ),
    }
    try:
        workflow_io = validate_workflow_graph_io(graph)
    except (WorkflowIOValidationError, WorkflowSchemaError, TypeError, ValueError):
        raise TemplateCatalogMismatch("/published_workflow/io_contract") from None

    input_contract = workflow_io.input_contract.to_dict()
    output_contract = workflow_io.output_contract.to_dict()
    inputs = [_semantic_descriptor(item) for item in input_contract["parameters"]]
    outputs = [_semantic_descriptor(item) for item in output_contract["outputs"]]
    mode = _composition_mode(workflow)
    digest_payload = {
        "version": 1,
        "composition_allow_transparent": mode,
        "inputs": inputs,
        "outputs": outputs,
    }
    contract_digest = (
        "sha256:" + hashlib.sha256(rfc8785.dumps(digest_payload)).hexdigest()
    )
    schema = _workflow_schema(
        inputs=inputs,
        outputs=outputs,
        workflow_uuid=workflow_uuid,
        workflow_revision=revision,
        applied_source_hash=applied_source_hash,
        contract_digest=contract_digest,
        composition_allow_transparent=mode,
    )
    handles = tuple(
        [_value_handle(item, io_type="target") for item in input_contract["parameters"]]
        + [_value_handle(item, io_type="source") for item in output_contract["outputs"]]
        + [_ready_handle("target"), _ready_handle("source")]
    )
    return NodeTemplateImport(
        template={
            "resource_template_uuid": host_uuid,
            "name": f"workflow:{workflow_uuid}",
            "display_name": str(workflow.get("name") or source.symbol),
            "description": str(workflow.get("description") or ""),
            "class": f"{source.module}:{source.symbol}",
            "type": "workflow",
            "node_type": "workflow",
            "goal": {
                str(item["name"]): str(item["name"])
                for item in input_contract["parameters"]
            },
            "goal_default": {
                str(item["name"]): _plain(item["default"])
                for item in input_contract["parameters"]
                if "default" in item
            },
            "feedback": {},
            "result": {
                str(item["name"]): str(item["name"])
                for item in output_contract["outputs"]
            },
            "schema": schema,
            "meta_data": {
                "unilab": {
                    "framework_owner_only": True,
                    "workflow_source": {
                        "kind": "package",
                        "definition_fqid": source.definition_fqid,
                        "module": source.module,
                        "symbol": source.symbol,
                        "package_catalog_digest": source.package_catalog_digest,
                        "definition_content_hash": source.definition_content_hash,
                    },
                }
            },
        },
        handles=handles,
    )


def _workflow_schema(
    *,
    inputs: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    workflow_uuid: str,
    workflow_revision: int,
    applied_source_hash: str,
    contract_digest: str,
    composition_allow_transparent: bool,
) -> dict[str, Any]:
    goal_properties = {str(item["name"]): _plain(item["schema"]) for item in inputs}
    result_properties = {str(item["name"]): _plain(item["schema"]) for item in outputs}
    required = [str(item["name"]) for item in inputs if item.get("required") is True]
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "goal": {
                "type": "object",
                "additionalProperties": False,
                "properties": goal_properties,
                "required": required,
            },
            "result": {
                "type": "object",
                "additionalProperties": False,
                "properties": result_properties,
                "required": [str(item["name"]) for item in outputs],
            },
        },
        "required": ["goal", "result"],
        "x-unilabos-workflow-contract": {
            "version": 1,
            "compatibility_version": 1,
            "workflow_uuid": workflow_uuid,
            "workflow_revision": workflow_revision,
            "applied_source_hash": applied_source_hash,
            "contract_digest": contract_digest,
            "composition_allow_transparent": composition_allow_transparent,
            "input_order": [str(item["name"]) for item in inputs],
            "output_order": [str(item["name"]) for item in outputs],
        },
    }


def _semantic_descriptor(raw: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        str(key): _plain(value)
        for key, value in raw.items()
        if key not in {"title", "description"}
    }
    return result


def _value_handle(
    descriptor: Mapping[str, Any],
    *,
    io_type: str,
) -> dict[str, Any]:
    name = str(descriptor["name"])
    schema = _plain(descriptor["schema"])
    slot_schema = _resource_slot_schema(schema)
    allowed = (
        _plain(slot_schema.get("allowed_resource_template_uuids"))
        if slot_schema is not None
        else None
    )
    implicit = bool(descriptor.get("implicit", False)) if io_type == "source" else False
    return {
        "handle_key": name,
        "io_type": io_type,
        "display_name": str(descriptor.get("title") or name),
        "description": str(descriptor.get("description") or ""),
        "type": _handle_type(schema),
        "required": bool(descriptor.get("required", False))
        if io_type == "target"
        else False,
        "data_source": "goal" if io_type == "target" else "result",
        "data_key": name,
        "meta_data": {
            "unilab": {
                "value_schema": schema,
                "editor_control": (
                    "material_port" if slot_schema is not None else "variable_selector"
                ),
                "allowed_resource_template_uuids": allowed,
                "implicit_passthrough": implicit,
            }
        },
    }


def _ready_handle(io_type: str) -> dict[str, Any]:
    return {
        "handle_key": "ready",
        "io_type": io_type,
        "display_name": "Ready",
        "description": "Composite structural dependency",
        "type": "any",
        "required": False,
        "data_source": "dependency",
        "data_key": "ready",
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


def _resource_slot_schema(schema: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if schema.get("$slot") == "ResourceSlot":
        return schema
    items = schema.get("items")
    if (
        schema.get("type") == "array"
        and isinstance(items, Mapping)
        and items.get("$slot") == "ResourceSlot"
    ):
        return items
    members = schema.get("anyOf")
    if isinstance(members, list):
        for member in members:
            if isinstance(member, Mapping):
                found = _resource_slot_schema(member)
                if found is not None:
                    return found
    return None


def _handle_type(schema: Mapping[str, Any]) -> str:
    if _resource_slot_schema(schema) is not None:
        return "ResourceSlot" if schema.get("type") != "array" else "array"
    if isinstance(schema.get("type"), str):
        return str(schema["type"])
    members = schema.get("anyOf")
    if isinstance(members, list):
        for member in members:
            if isinstance(member, Mapping) and member.get("type") != "null":
                return _handle_type(member)
    return "object"


def _composition_mode(workflow: Mapping[str, Any]) -> bool:
    meta_data = workflow.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    raw = (
        unilab.get("composition_allow_transparent", False)
        if isinstance(unilab, Mapping)
        else False
    )
    if not isinstance(raw, bool):
        raise TemplateCatalogMismatch(
            "/published_workflow/composition_allow_transparent"
        )
    return raw


def _host_owner_uuid(value: Any) -> str:
    if value is None:
        raise TemplateCatalogMismatch("/host_node/resource_template_uuid")
    return _uuid(value, "/host_node/resource_template_uuid")


def _uuid(value: Any, path: str) -> str:
    try:
        canonical = validate_uuid(value)
    except (TypeError, ValueError):
        raise TemplateCatalogMismatch(path) from None
    if canonical != value:
        raise TemplateCatalogMismatch(path)
    return canonical


def _sha256(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise TemplateCatalogMismatch(path)
    digest = value.removeprefix("sha256:")
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise TemplateCatalogMismatch(path)
    return value


def _sequence(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise TemplateCatalogMismatch(path)
    return _plain(value)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


__all__ = [
    "PublishedWorkflowResolver",
    "PublishedWorkflowSource",
    "project_published_workflow_contract",
]
