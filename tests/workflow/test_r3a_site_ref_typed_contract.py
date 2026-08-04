"""R3A SiteRef typed Workflow contract 的独立 public RED。

这些测试刻意只经过 Workflow schema、Annotation、Registry Catalog、Handle
compatibility 与 Task input preflight 的公共接口。SiteRef 是 Site authority 的稳定
身份，不是带 ``site_selector`` 展示提示的普通字符串，也不继承 ResourceSlot 的
物料透传语义。
"""

from __future__ import annotations

import ast
import importlib
import textwrap
from typing import Any

import pytest

SITE_UUID = "72b51092-21f7-4d77-a478-9803dcfe5c1a"
OTHER_SITE_UUID = "82d6b145-951b-47f1-82d3-ee48e860cbb2"
DEVICE_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000001"
SITE_SCHEMA = {"$slot": "SiteRef"}
RESOURCE_SCHEMA = {"$slot": "ResourceSlot"}
SITE_REF_IMPORT = "unilabos.registry.placeholder_type:SiteRef"


def _public(module_name: str, member: str) -> Any:
    module = importlib.import_module(module_name)
    if not hasattr(module, member):
        pytest.fail(
            f"R3A 缺少公共 Interface: {module_name}.{member}",
            pytrace=False,
        )
    return getattr(module, member)


def _action_contract(source: str) -> Any:
    module = ast.parse(textwrap.dedent(source))
    action = next(
        node
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    parse_action_contract = _public(
        "unilabos.registry.action_contract_schema",
        "parse_action_contract",
    )
    return parse_action_contract(module, action, module_name="r3a_site_ref.device")


def _site_input_graph() -> dict[str, Any]:
    return {
        "workflow": {
            "meta_data": {
                "unilab": {
                    "input_contract": {
                        "version": 1,
                        "parameters": [
                            {
                                "name": "target_site",
                                "schema": SITE_SCHEMA,
                                "required": True,
                            }
                        ],
                    },
                    "output_contract": {"version": 1, "outputs": []},
                    "output_bindings": {},
                }
            }
        },
        "nodes": [],
        "handle_templates": [],
    }


class _RecordingSiteResolver:
    def __init__(self, *, returned_uuid: str | None = None) -> None:
        self.returned_uuid = returned_uuid
        self.calls: list[str] = []

    def resolve(self, *, site_uuid: str) -> Any:
        self.calls.append(site_uuid)
        resolved_type = _public(
            "unilabos.workflow.task_input",
            "ResolvedSiteRef",
        )
        return resolved_type(uuid=self.returned_uuid or site_uuid)


def _preflight(raw_site: Any, resolver: Any) -> Any:
    preflight_task_input = _public(
        "unilabos.workflow.task_input",
        "preflight_task_input",
    )
    return preflight_task_input(
        graph=_site_input_graph(),
        raw_input={"target_site": raw_site},
        execution_plan={"nodes": [], "edges": []},
        jobs=[],
        resource_resolver=None,
        site_ref_resolver=resolver,
    )


def test_site_ref_is_a_distinct_public_typed_value() -> None:
    placeholder_type = importlib.import_module("unilabos.registry.placeholder_type")
    site_ref = _public("unilabos.registry.placeholder_type", "SiteRef")
    resource_slot = placeholder_type.ResourceSlot

    assert isinstance(site_ref, type)
    assert site_ref is not resource_slot
    assert not issubclass(site_ref, resource_slot)


def test_site_ref_schema_and_handle_projection_remain_canonical() -> None:
    parse_value_schema = _public(
        "unilabos.workflow.schema",
        "parse_value_schema",
    )
    workflow_handle_type = _public(
        "unilabos.workflow.handle_projection",
        "workflow_handle_type",
    )

    canonical = parse_value_schema(SITE_SCHEMA).to_dict()

    assert canonical == SITE_SCHEMA
    assert "type" not in canonical
    assert "x-unilabos-editor-control" not in canonical
    assert workflow_handle_type(canonical) == "SiteRef"


def test_action_annotation_and_renderer_round_trip_site_ref() -> None:
    annotation_schema = importlib.import_module("unilabos.registry.annotation_schema")
    annotation = ast.Name(id="SiteRef", ctx=ast.Load())
    parsed = annotation_schema.parse_parameter_annotation(
        "target_site",
        annotation,
        default=annotation_schema.NO_DEFAULT,
        imports={"SiteRef": SITE_REF_IMPORT},
    )

    assert parsed.to_dict() == {
        "name": "target_site",
        "schema": SITE_SCHEMA,
        "required": True,
    }

    rendered = annotation_schema.render_parameter_annotation(parsed)
    assert ast.unparse(rendered) == "SiteRef"
    reparsed = annotation_schema.parse_parameter_annotation(
        "target_site",
        rendered,
        default=annotation_schema.NO_DEFAULT,
        imports={"SiteRef": SITE_REF_IMPORT},
    )
    assert reparsed.to_dict() == parsed.to_dict()


def test_action_contract_and_catalog_preserve_site_ref_without_passthrough() -> None:
    contract = _action_contract(
        """
        from typing import TypedDict
        from unilabos.registry.placeholder_type import SiteRef

        class MoveResult(TypedDict):
            reached_site: SiteRef

        def move_to_site(target_site: SiteRef) -> MoveResult:
            pass
        """
    )
    assert contract.to_dict()["input_contract"]["parameters"] == [
        {
            "name": "target_site",
            "schema": SITE_SCHEMA,
            "required": True,
        }
    ]
    assert contract.to_dict()["output_contract"]["outputs"] == [
        {
            "name": "reached_site",
            "schema": SITE_SCHEMA,
            "implicit": False,
        }
    ]

    action_schema = contract.to_action_schema(action_name="move_to_site")
    registry_snapshot = {
        "robot": {
            "source_fqid": "r3a_site_ref.robot",
            "display_name": "Robot",
            "class": {
                "module": "r3a_site_ref.device:Robot",
                "action_value_mappings": {
                    "move_to_site": {
                        "displayname": "Move to site",
                        "description": "Move by stable Site identity",
                        "schema": action_schema,
                        "goal": {},
                        "goal_default": {},
                        "feedback": {},
                        "result": {},
                        "type": "UniLabJsonCommand",
                        "node_type": "device",
                    }
                },
            },
        }
    }
    project = _public(
        "unilabos.registry.catalog_consumer",
        "workflow_template_imports_from_registry_snapshot",
    )
    projected = project(
        registry_snapshot,
        authority_id="os-local",
        resource_template_identity_resolver=lambda _identity: DEVICE_TEMPLATE_UUID,
    )
    assert len(projected) == 1
    business_handles = [
        handle for handle in projected[0].handles if handle["handle_key"] != "ready"
    ]

    assert len(business_handles) == 2
    target = next(
        handle
        for handle in business_handles
        if handle["handle_key"] == "target_site"
    )
    assert target["handle_key"] == "target_site"
    assert target["io_type"] == "target"
    assert target["type"] == "SiteRef"
    assert target["meta_data"]["unilab"]["value_schema"] == SITE_SCHEMA
    assert target["meta_data"]["unilab"]["implicit_passthrough"] is False
    result = next(
        handle
        for handle in business_handles
        if handle["handle_key"] == "reached_site"
    )
    assert result["io_type"] == "source"
    assert result["type"] == "SiteRef"
    assert result["meta_data"]["unilab"]["value_schema"] == SITE_SCHEMA
    assert result["meta_data"]["unilab"]["implicit_passthrough"] is False
    assert not any(
        handle["handle_key"] == "target_site" and handle["io_type"] == "source"
        for handle in business_handles
    )


def test_site_ref_never_acquires_resource_slot_implicit_passthrough() -> None:
    passthrough = _public(
        "unilabos.workflow.workflow_io",
        "resource_slot_passthrough_is_compatible",
    )

    assert passthrough(SITE_SCHEMA, SITE_SCHEMA) is False


def test_site_ref_and_resource_slot_providers_are_not_interchangeable() -> None:
    schema_is_assignable = _public(
        "unilabos.workflow.workflow_io",
        "schema_is_assignable",
    )
    schema_matches_handle = _public(
        "unilabos.workflow.graph_validation",
        "workflow_schema_matches_handle_type",
    )

    assert schema_is_assignable(SITE_SCHEMA, SITE_SCHEMA) is True
    assert schema_is_assignable(SITE_SCHEMA, RESOURCE_SCHEMA) is False
    assert schema_is_assignable(RESOURCE_SCHEMA, SITE_SCHEMA) is False
    assert schema_matches_handle(SITE_SCHEMA, "SiteRef") is True
    assert schema_matches_handle(SITE_SCHEMA, "ResourceSlot") is False
    assert schema_matches_handle(RESOURCE_SCHEMA, "SiteRef") is False


def test_task_input_resolves_and_freezes_canonical_site_uuid() -> None:
    resolver = _RecordingSiteResolver()
    raw_value = {"uuid": SITE_UUID.upper()}

    prepared = _preflight(raw_value, resolver)
    raw_value["uuid"] = OTHER_SITE_UUID

    assert resolver.calls == [SITE_UUID]
    assert prepared.resolved_input == {"target_site": {"uuid": SITE_UUID}}


@pytest.mark.parametrize(
    "raw_value",
    [
        pytest.param(SITE_UUID, id="bare-uuid-string"),
        pytest.param({"id": SITE_UUID}, id="wrong-identity-key"),
        pytest.param(
            {"uuid": SITE_UUID, "label": "S04"},
            id="presentation-field",
        ),
        pytest.param(
            {"uuid": SITE_UUID, "resource_template_uuid": DEVICE_TEMPLATE_UUID},
            id="resource-slot-closed-shape",
        ),
    ],
)
def test_task_input_accepts_only_closed_uuid_site_ref_shape(raw_value: Any) -> None:
    task_input = importlib.import_module("unilabos.workflow.task_input")
    resolver = _RecordingSiteResolver()

    with pytest.raises(task_input.TaskInputError) as failure:
        _preflight(raw_value, resolver)

    assert failure.value.code == "invalid_input"
    assert resolver.calls == []


def test_task_input_rejects_resolver_identity_substitution() -> None:
    task_input = importlib.import_module("unilabos.workflow.task_input")
    resolver = _RecordingSiteResolver(returned_uuid=OTHER_SITE_UUID)

    with pytest.raises(task_input.TaskInputError) as failure:
        _preflight({"uuid": SITE_UUID}, resolver)

    assert failure.value.code == "invalid_input"
    assert resolver.calls == [SITE_UUID]
