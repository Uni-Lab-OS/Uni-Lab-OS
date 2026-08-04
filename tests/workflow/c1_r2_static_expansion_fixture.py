"""C1 R2 public expansion tests 的真实 Store/Catalog fixture。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import unilabos.workflow.composite as composite_module
from unilabos.workflow.catalog import (
    CatalogAuthority,
    NodeTemplateImport,
    TemplateCatalog,
)
from unilabos.workflow.composite import (
    PublishedWorkflowSource,
    project_published_workflow_contract,
)
from unilabos.workflow.handle_projection import (
    resource_slot_schema as find_resource_slot_schema,
)
from unilabos.workflow.handle_projection import workflow_handle_type
from unilabos.workflow.models import WorkflowEdgeWrite, WorkflowNodeWrite
from unilabos.workflow.store import WorkflowStore

AUTHORITY = CatalogAuthority(authority_id="os-c1-r2", kind="backend")
PARENT_WORKFLOW_UUID = "44444444-4444-4444-8444-444444444444"
INVOCATION_UUID = "11111111-1111-4111-8111-111111111111"
CHILD_WORKFLOW_UUID = "a1000000-0000-4000-8000-000000000001"
LEAF_WORKFLOW_UUID = "a1000000-0000-4000-8000-000000000002"
THIRD_WORKFLOW_UUID = "a1000000-0000-4000-8000-000000000003"

CHILD_NODE_UUID = "22222222-2222-4222-8222-222222222222"
GRANDCHILD_NODE_UUID = "33333333-3333-4333-8333-333333333333"
GROUP_NODE_UUID = "77777777-7777-4777-8777-777777777777"
SECOND_CHILD_NODE_UUID = "22222222-2222-4222-8222-222222222223"
EXPANDED_CHILD_NODE_UUID = "b6b35f79-80d0-5b77-a0eb-9646bcb36808"
EXPANDED_GRANDCHILD_NODE_UUID = "7b221513-105e-5c92-9859-1a3c2015fafb"
STORED_EXPANDED_GRANDCHILD_UUID = "c4f12353-a256-572a-a790-851e211182b4"
STORED_EXPANDED_GROUP_UUID = "72b77e66-444e-5787-a914-d48a4d6ccf47"
EXPANDED_EDGE_UUID = "b3e67370-ee6e-54b5-9dd1-6d44c5a5854f"

HOST_RESOURCE_TEMPLATE_UUID = "a2000000-0000-4000-8000-000000000001"
ACTION_RESOURCE_TEMPLATE_UUID = "a2000000-0000-4000-8000-000000000002"
SECOND_ACTION_RESOURCE_TEMPLATE_UUID = "a2000000-0000-4000-8000-000000000003"
MATERIAL_TEMPLATE_A_UUID = "a2000000-0000-4000-8000-000000000011"
MATERIAL_TEMPLATE_B_UUID = "a2000000-0000-4000-8000-000000000012"
MATERIAL_TEMPLATE_C_UUID = "a2000000-0000-4000-8000-000000000013"

ACTION_TEMPLATE_UUID = "a3000000-0000-4000-8000-000000000001"
SECOND_ACTION_TEMPLATE_UUID = "a3000000-0000-4000-8000-000000000002"
CHILD_TEMPLATE_UUID = "a3000000-0000-4000-8000-000000000011"
LEAF_TEMPLATE_UUID = "a3000000-0000-4000-8000-000000000012"
THIRD_TEMPLATE_UUID = "a3000000-0000-4000-8000-000000000013"
GROUP_TEMPLATE_UUID = "a3000000-0000-4000-8000-000000000021"

ACTION_VALUE_TARGET_UUID = "a4000000-0000-4000-8000-000000000001"
ACTION_VALUE_SOURCE_UUID = "55555555-5555-4555-8555-555555555555"
ACTION_READY_TARGET_UUID = "a4000000-0000-4000-8000-000000000003"
ACTION_READY_SOURCE_UUID = "a4000000-0000-4000-8000-000000000004"
SECOND_ACTION_VALUE_TARGET_UUID = "66666666-6666-4666-8666-666666666666"
SECOND_ACTION_VALUE_SOURCE_UUID = "a4000000-0000-4000-8000-000000000006"
SECOND_ACTION_READY_TARGET_UUID = "a4000000-0000-4000-8000-000000000007"
SECOND_ACTION_READY_SOURCE_UUID = "a4000000-0000-4000-8000-000000000008"

CHILD_VALUE_TARGET_UUID = "a5000000-0000-4000-8000-000000000001"
CHILD_VALUE_SOURCE_UUID = "a5000000-0000-4000-8000-000000000002"
CHILD_READY_TARGET_UUID = "a5000000-0000-4000-8000-000000000003"
CHILD_READY_SOURCE_UUID = "a5000000-0000-4000-8000-000000000004"
LEAF_VALUE_TARGET_UUID = "a5000000-0000-4000-8000-000000000005"
LEAF_VALUE_SOURCE_UUID = "a5000000-0000-4000-8000-000000000006"
LEAF_READY_TARGET_UUID = "a5000000-0000-4000-8000-000000000007"
LEAF_READY_SOURCE_UUID = "a5000000-0000-4000-8000-000000000008"
THIRD_VALUE_TARGET_UUID = "a5000000-0000-4000-8000-000000000009"
THIRD_VALUE_SOURCE_UUID = "a5000000-0000-4000-8000-00000000000a"
THIRD_READY_TARGET_UUID = "a5000000-0000-4000-8000-00000000000b"
THIRD_READY_SOURCE_UUID = "a5000000-0000-4000-8000-00000000000c"
STORED_NESTED_EDGE_UUID = "a6000000-0000-4000-8000-000000000001"


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


@dataclass
class MemoryPublishedWorkflowResolver:
    """PackageCatalog boundary 的最小 immutable in-memory Adapter。"""

    sources: dict[tuple[str, str], PublishedWorkflowSource] = field(
        default_factory=dict
    )

    def resolve(self, module: str, symbol: str) -> PublishedWorkflowSource:
        try:
            return self.sources[(module, symbol)]
        except KeyError:
            raise LookupError((module, symbol)) from None

    def add(self, source: PublishedWorkflowSource) -> None:
        self.sources[(source.module, source.symbol)] = source


@dataclass
class WorkflowContractFixture:
    source: PublishedWorkflowSource
    template_uuid: str
    handles: dict[tuple[str, str], str]
    contract_pin: dict[str, Any]


@dataclass
class ExpansionWorld:
    store: WorkflowStore
    catalog: TemplateCatalog
    resolver: MemoryPublishedWorkflowResolver
    imports: dict[str, NodeTemplateImport]
    child: WorkflowContractFixture
    contracts: dict[str, WorkflowContractFixture] = field(default_factory=dict)
    parent_workflow_uuid: str = PARENT_WORKFLOW_UUID

    def close(self) -> None:
        self.store.close()

    def publish(self) -> None:
        self.catalog.replace(
            AUTHORITY,
            tuple(self.imports[key] for key in sorted(self.imports)),
        )

    def compile(
        self,
        *,
        invocation_uuid: str = INVOCATION_UUID,
        source: PublishedWorkflowSource | None = None,
        keyword_arguments: dict[str, object] | None = None,
    ) -> Any:
        authoring_type = composite_module.CompositeAuthoring
        authoring = authoring_type(
            store=self.store,
            catalog=self.catalog,
            authority=AUTHORITY,
            resolver=self.resolver,
        )
        selected = source or self.child.source
        return authoring.compile_invocation(
            parent_workflow_uuid=self.parent_workflow_uuid,
            invocation_uuid=invocation_uuid,
            module=selected.module,
            symbol=selected.symbol,
            keyword_arguments=keyword_arguments or {},
        )

    def catalog_snapshot(self) -> dict[str, Any]:
        with self.catalog.snapshot(AUTHORITY) as snapshot:
            return {
                "fingerprint": snapshot.fingerprint,
                "nodes": [dict(item) for item in snapshot.node_templates],
                "handles": [dict(item) for item in snapshot.handle_templates],
            }


def scalar_schema() -> dict[str, Any]:
    return {"type": "number"}


def resource_slot_schema(*allowlist: str) -> dict[str, Any]:
    schema: dict[str, Any] = {"$slot": "ResourceSlot"}
    if allowlist:
        schema["allowed_resource_template_uuids"] = list(allowlist)
    return schema


def resource_slot_wrapper(
    *allowlist: str,
    collection: bool = False,
    nullable: bool = False,
) -> dict[str, Any]:
    """构造已经由 I1 schema 接受的 D-064 包装形状。"""

    schema: dict[str, Any] = resource_slot_schema(*allowlist)
    if collection:
        schema = {"type": "array", "items": schema}
    if nullable:
        schema = {"anyOf": [schema, {"type": "null"}]}
    return schema


def action_import(
    *,
    template_uuid: str = ACTION_TEMPLATE_UUID,
    resource_template_uuid: str = ACTION_RESOURCE_TEMPLATE_UUID,
    target_handle_uuid: str = ACTION_VALUE_TARGET_UUID,
    source_handle_uuid: str = ACTION_VALUE_SOURCE_UUID,
    ready_target_uuid: str = ACTION_READY_TARGET_UUID,
    ready_source_uuid: str = ACTION_READY_SOURCE_UUID,
    value_schema: dict[str, Any] | None = None,
    with_value_handles: bool = True,
) -> NodeTemplateImport:
    schema = value_schema or scalar_schema()
    handles: list[dict[str, Any]] = []
    if with_value_handles:
        handles.extend(
            [
                _handle(
                    target_handle_uuid,
                    template_uuid,
                    "value",
                    "target",
                    schema,
                    required=True,
                ),
                _handle(
                    source_handle_uuid,
                    template_uuid,
                    "result",
                    "source",
                    schema,
                    required=False,
                ),
            ]
        )
    handles.extend(
        [
            _ready_handle(ready_target_uuid, template_uuid, "target"),
            _ready_handle(ready_source_uuid, template_uuid, "source"),
        ]
    )
    return NodeTemplateImport(
        template={
            "uuid": template_uuid,
            "resource_template_uuid": resource_template_uuid,
            "name": f"action:{template_uuid}",
            "display_name": "Fixture Action",
            "description": "C1 R2 fixture",
            "class": "tests.c1_r2:FixtureAction",
            "goal": {"value": "value"} if with_value_handles else {},
            "goal_default": {},
            "feedback": {},
            "result": {"result": "result"} if with_value_handles else {},
            "schema": None,
            "type": "action",
            "node_type": "compute",
            "meta_data": {},
        },
        handles=tuple(handles),
    )


def presentation_group_import() -> NodeTemplateImport:
    """构造 C1/R1 展示分组：一个有意不带 Handle 的 Catalog 节点。"""

    return NodeTemplateImport(
        template={
            "uuid": GROUP_TEMPLATE_UUID,
            "resource_template_uuid": HOST_RESOURCE_TEMPLATE_UUID,
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
                    "authority_id": AUTHORITY.authority_id,
                    "framework_owner_only": True,
                    "source_fqid": "unilabos.workflow.authoring:group",
                }
            },
        },
        handles=(),
    )


def _handle(
    handle_uuid: str,
    template_uuid: str,
    key: str,
    io_type: str,
    value_schema: dict[str, Any],
    *,
    required: bool,
) -> dict[str, Any]:
    slot = find_resource_slot_schema(value_schema)
    allowed = slot.get("allowed_resource_template_uuids") if slot is not None else None
    return {
        "uuid": handle_uuid,
        "workflow_node_template_uuid": template_uuid,
        "handle_key": key,
        "io_type": io_type,
        "display_name": key.title(),
        "description": "",
        "type": workflow_handle_type(value_schema),
        "required": required,
        "data_source": "goal" if io_type == "target" else "result",
        "data_key": key,
        "meta_data": {
            "unilab": {
                "value_schema": value_schema,
                "editor_control": (
                    "material_port" if slot is not None else "variable_selector"
                ),
                "allowed_resource_template_uuids": allowed,
            }
        },
    }


def _ready_handle(
    handle_uuid: str,
    template_uuid: str,
    io_type: str,
) -> dict[str, Any]:
    return {
        "uuid": handle_uuid,
        "workflow_node_template_uuid": template_uuid,
        "handle_key": "ready",
        "io_type": io_type,
        "display_name": "Ready",
        "description": "",
        "type": "boolean",
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


def action_node(
    *,
    node_uuid: str,
    template_uuid: str = ACTION_TEMPLATE_UUID,
    target_handle_uuid: str = ACTION_VALUE_TARGET_UUID,
    parameter: str | None = "value",
    parent_uuid: str | None = None,
) -> WorkflowNodeWrite:
    bindings = (
        {target_handle_uuid: {"parameter": parameter}} if parameter is not None else {}
    )
    return WorkflowNodeWrite(
        uuid=node_uuid,
        workflow_node_template_uuid=template_uuid,
        parent_uuid=parent_uuid,
        name=f"node-{node_uuid[-4:]}",
        status="idle",
        type="compute",
        pose={},
        param={},
        execution_policy={},
        disabled=False,
        minimized=False,
        meta_data={"unilab": {"input_bindings": bindings}},
    )


def group_node(*, node_uuid: str, parent_uuid: str | None = None) -> WorkflowNodeWrite:
    return WorkflowNodeWrite(
        uuid=node_uuid,
        workflow_node_template_uuid=GROUP_TEMPLATE_UUID,
        parent_uuid=parent_uuid,
        name="Fixture Group",
        status="idle",
        type="group",
        pose={},
        param={},
        execution_policy={},
        disabled=False,
        minimized=False,
        meta_data={"presentation": {"collapsed": False}},
    )


def workflow_node(
    *,
    node_uuid: str,
    template_uuid: str,
    input_handle_uuid: str | None,
    parameter: str | None,
    composite: dict[str, Any],
    parent_uuid: str | None = None,
) -> WorkflowNodeWrite:
    bindings = (
        {input_handle_uuid: {"parameter": parameter}}
        if input_handle_uuid is not None and parameter is not None
        else {}
    )
    return WorkflowNodeWrite(
        uuid=node_uuid,
        workflow_node_template_uuid=template_uuid,
        parent_uuid=parent_uuid,
        name=f"composite-{node_uuid[-4:]}",
        status="idle",
        type="workflow",
        pose={},
        param={},
        execution_policy={},
        disabled=False,
        minimized=False,
        meta_data={
            "unilab": {
                "input_bindings": bindings,
                "composite": composite,
            }
        },
    )


def composite_metadata(
    contract: WorkflowContractFixture,
    *,
    target_node_uuid: str | None,
    target_handle_uuid: str | None,
    source_node_uuid: str | None,
    source_handle_uuid: str | None,
    entry_node_uuid: str,
    entry_handle_uuid: str,
    completion_node_uuid: str,
    completion_handle_uuid: str,
) -> dict[str, Any]:
    pin = contract.contract_pin
    target_mappings: dict[str, Any] = {}
    boundary_target = contract.handles.get(("value", "target"))
    if (
        boundary_target is not None
        and target_node_uuid is not None
        and target_handle_uuid is not None
    ):
        target_mappings[boundary_target] = [
            {
                "workflow_node_uuid": target_node_uuid,
                "target_handle_uuid": target_handle_uuid,
            }
        ]
    source_mappings: dict[str, Any] = {}
    boundary_source = contract.handles.get(("result", "source"))
    if boundary_source is None:
        boundary_source = contract.handles.get(("value", "source"))
    if (
        boundary_source is not None
        and source_node_uuid is not None
        and source_handle_uuid is not None
    ):
        source_mappings[boundary_source] = {
            "kind": "node_output",
            "workflow_node_uuid": source_node_uuid,
            "source_handle_uuid": source_handle_uuid,
        }
    elif boundary_source == contract.handles.get(("value", "source")):
        source_mappings[boundary_source] = {
            "kind": "workflow_input",
            "parameter": "value",
        }
    return {
        "version": 1,
        "child_workflow_uuid": contract.source.workflow_uuid,
        "child_workflow_revision": pin["workflow_revision"],
        "child_applied_source_hash": pin["applied_source_hash"],
        "contract_digest": pin["contract_digest"],
        "composition_allow_transparent": pin["composition_allow_transparent"],
        "target_mappings": target_mappings,
        "source_mappings": source_mappings,
        "structural_mappings": {
            "entry_targets": [
                {
                    "workflow_node_uuid": entry_node_uuid,
                    "target_handle_uuid": entry_handle_uuid,
                }
            ],
            "completion_sources": [
                {
                    "workflow_node_uuid": completion_node_uuid,
                    "source_handle_uuid": completion_handle_uuid,
                }
            ],
        },
    }


def authoring_edge(
    *,
    edge_uuid: str,
    source_node_uuid: str,
    source_handle_uuid: str,
    target_node_uuid: str,
    target_handle_uuid: str,
    description: str | None = None,
    meta_data: dict[str, Any] | None = None,
) -> WorkflowEdgeWrite:
    return WorkflowEdgeWrite(
        uuid=edge_uuid,
        source_node_uuid=source_node_uuid,
        source_handle_uuid=source_handle_uuid,
        target_node_uuid=target_node_uuid,
        target_handle_uuid=target_handle_uuid,
        description=description,
        meta_data=meta_data or {},
    )


def workflow_meta(
    *,
    input_schema: dict[str, Any] | None,
    output_schema: dict[str, Any] | None,
    output_binding: dict[str, Any] | None,
    output_implicit: bool = False,
) -> dict[str, Any]:
    parameters: list[dict[str, Any]] = []
    if input_schema is not None:
        nullable = isinstance(input_schema.get("anyOf"), list)
        parameter = {
            "name": "value",
            "schema": input_schema,
            "required": not nullable,
        }
        if nullable:
            parameter["default"] = None
        parameters.append(parameter)
    outputs: list[dict[str, Any]] = []
    bindings: dict[str, Any] = {}
    if output_schema is not None:
        output_name = "value" if output_implicit else "result"
        outputs.append(
            {
                "name": output_name,
                "schema": output_schema,
                "implicit": output_implicit,
            }
        )
        assert output_binding is not None
        bindings[output_name] = output_binding
    return {
        "unilab": {
            "input_contract": {"version": 1, "parameters": parameters},
            "output_contract": {"version": 1, "outputs": outputs},
            "output_bindings": bindings,
            "composition_allow_transparent": False,
        }
    }


def create_applied_workflow(
    world: ExpansionWorld,
    *,
    workflow_uuid: str,
    module: str,
    symbol: str,
    template_uuid: str,
    boundary_handle_uuids: dict[tuple[str, str], str],
    nodes: list[WorkflowNodeWrite],
    edges: list[WorkflowEdgeWrite],
    meta_data: dict[str, Any],
    hierarchical_fixture: bool = False,
) -> WorkflowContractFixture:
    world.store.create_workflow(
        workflow_uuid=workflow_uuid,
        name=symbol,
        tags=["c1-r2-fixture"],
        description="C1 R2 child",
        meta_data=meta_data,
    )
    saved = (
        _seed_hierarchical_graph(world.store, workflow_uuid, nodes, edges)
        if hierarchical_fixture
        else world.store.save_graph(
            workflow_uuid,
            revision=1,
            nodes=nodes,
            edges=edges,
        )
    )
    source = PublishedWorkflowSource(
        workflow_uuid=workflow_uuid,
        definition_fqid=f"tests.c1_r2.{symbol}",
        module=module,
        symbol=symbol,
        package_catalog_digest=_digest(f"package:{module}"),
        definition_content_hash=_digest(f"definition:{module}:{symbol}"),
    )
    world.resolver.add(source)
    _mark_applied(world.store, workflow_uuid, saved["workflow"]["revision"])
    projected = project_published_workflow_contract(
        source=source,
        applied_snapshot=world.store.get_published_workflow_snapshot(workflow_uuid),
        host_node_resource_template_uuid=HOST_RESOURCE_TEMPLATE_UUID,
    )
    assert projected is not None
    imported = backend_contract_import(
        projected,
        template_uuid=template_uuid,
        handle_uuids=boundary_handle_uuids,
    )
    world.imports[template_uuid] = imported
    world.publish()
    contract = dict(imported.template["schema"]["x-unilabos-workflow-contract"])
    return WorkflowContractFixture(
        source=source,
        template_uuid=template_uuid,
        handles=dict(boundary_handle_uuids),
        contract_pin=contract,
    )


def _seed_hierarchical_graph(
    store: WorkflowStore,
    workflow_uuid: str,
    nodes: list[WorkflowNodeWrite],
    edges: list[WorkflowEdgeWrite],
) -> dict[str, Any]:
    """在生产校验器支持 type=workflow 前植入仅供 R2 使用的层级。"""

    timestamp = "2026-08-02T00:00:00Z"
    with store.transaction() as connection:
        for node in nodes:
            connection.execute(
                """
                INSERT INTO workflow_node(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_uuid, workflow_node_template_uuid,
                    parent_uuid, material_uuid, name, status, type, icon, pose,
                    param, footer, action_name, action_type, execution_policy,
                    disabled, minimized, script
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?)
                """,
                (
                    node.uuid,
                    timestamp,
                    timestamp,
                    node.description,
                    json.dumps(node.meta_data, sort_keys=True),
                    workflow_uuid,
                    node.workflow_node_template_uuid,
                    node.parent_uuid,
                    node.material_uuid,
                    node.name,
                    node.status,
                    node.type,
                    node.icon,
                    json.dumps(node.pose, sort_keys=True),
                    json.dumps(node.param or {}, sort_keys=True),
                    node.footer,
                    node.action_name,
                    node.action_type,
                    json.dumps(node.execution_policy, sort_keys=True),
                    int(node.disabled),
                    int(node.minimized),
                    node.script,
                ),
            )
        for edge in edges:
            connection.execute(
                """
                INSERT INTO workflow_edge(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_uuid, source_node_uuid,
                    target_node_uuid, source_handle_uuid, target_handle_uuid
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    edge.uuid,
                    timestamp,
                    timestamp,
                    edge.description,
                    json.dumps(edge.meta_data, sort_keys=True),
                    workflow_uuid,
                    edge.source_node_uuid,
                    edge.target_node_uuid,
                    edge.source_handle_uuid,
                    edge.target_handle_uuid,
                ),
            )
        connection.execute(
            "UPDATE workflow SET revision = 2, update_time = ? WHERE uuid = ?",
            (timestamp, workflow_uuid),
        )
    return store.get_graph(workflow_uuid)


def backend_contract_import(
    projected: NodeTemplateImport,
    *,
    template_uuid: str,
    handle_uuids: dict[tuple[str, str], str],
) -> NodeTemplateImport:
    template = dict(projected.template)
    template["uuid"] = template_uuid
    handles: list[dict[str, Any]] = []
    for raw in projected.handles:
        handle = dict(raw)
        key = (str(handle["handle_key"]), str(handle["io_type"]))
        handle["uuid"] = handle_uuids[key]
        handle["workflow_node_template_uuid"] = template_uuid
        handles.append(handle)
    return NodeTemplateImport(template=template, handles=tuple(handles))


def replace_applied_graph(
    world: ExpansionWorld,
    contract: WorkflowContractFixture,
    *,
    nodes: list[WorkflowNodeWrite],
    edges: list[WorkflowEdgeWrite],
    meta_data: dict[str, Any],
    reproject: bool = True,
) -> WorkflowContractFixture:
    """用 public Store mutation 替换 child，并按需用 R1 产物重新发布合同。"""

    current = world.store.get_graph(contract.source.workflow_uuid)
    with world.store.transaction() as connection:
        connection.execute(
            "UPDATE workflow SET meta_data = ? WHERE uuid = ?",
            (
                json.dumps(meta_data, sort_keys=True),
                contract.source.workflow_uuid,
            ),
        )
    saved = world.store.save_graph(
        contract.source.workflow_uuid,
        revision=current["workflow"]["revision"],
        nodes=nodes,
        edges=edges,
    )
    _mark_applied(
        world.store,
        contract.source.workflow_uuid,
        saved["workflow"]["revision"],
    )
    if not reproject:
        return contract
    projected = project_published_workflow_contract(
        source=contract.source,
        applied_snapshot=world.store.get_published_workflow_snapshot(
            contract.source.workflow_uuid
        ),
        host_node_resource_template_uuid=HOST_RESOURCE_TEMPLATE_UUID,
    )
    assert projected is not None
    imported = backend_contract_import(
        projected,
        template_uuid=contract.template_uuid,
        handle_uuids=contract.handles,
    )
    world.imports[contract.template_uuid] = imported
    world.publish()
    return WorkflowContractFixture(
        source=contract.source,
        template_uuid=contract.template_uuid,
        handles=dict(contract.handles),
        contract_pin=dict(imported.template["schema"]["x-unilabos-workflow-contract"]),
    )


def _mark_applied(
    store: WorkflowStore,
    workflow_uuid: str,
    revision: int,
) -> None:
    source_hash = _digest(f"applied:{workflow_uuid}:{revision}")
    store.record_draft_compilation(
        workflow_uuid=workflow_uuid,
        draft_hash=None,
        draft_update_time=None,
        diagnostics=[],
        candidate_hash=None,
        candidate=None,
        event_data={"workflow_uuid": workflow_uuid, "cause": "fixture"},
    )
    applied_source = {
        "workflow_revision": revision,
        "source_hash": source_hash,
        "python_source": f"def workflow_{workflow_uuid[-4:]}(): ...\n",
        "source_map": [],
        "compiler_version": "c1-r2-fixture",
        "template_catalog_fingerprint": _digest("fixture-catalog"),
    }
    with store.transaction() as connection:
        connection.execute(
            """
            UPDATE workflow_authoring
            SET applied_source = ?, update_time = update_time
            WHERE workflow_uuid = ?
            """,
            (json.dumps(applied_source, sort_keys=True), workflow_uuid),
        )


def make_direct_world(
    tmp_path: Path,
    *,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    ready_only: bool = False,
) -> ExpansionWorld:
    store = WorkflowStore(tmp_path / "workflow.db")
    catalog = TemplateCatalog(store)
    resolver = MemoryPublishedWorkflowResolver()
    action = action_import(
        value_schema=input_schema or scalar_schema(),
        with_value_handles=not ready_only,
    )
    placeholder = WorkflowContractFixture(
        source=PublishedWorkflowSource(
            workflow_uuid=CHILD_WORKFLOW_UUID,
            definition_fqid="tests.c1_r2.pending",
            module="tests.c1_r2.pending",
            symbol="pending",
            package_catalog_digest=_digest("pending-package"),
            definition_content_hash=_digest("pending-definition"),
        ),
        template_uuid=CHILD_TEMPLATE_UUID,
        handles={},
        contract_pin={},
    )
    world = ExpansionWorld(
        store=store,
        catalog=catalog,
        resolver=resolver,
        imports={ACTION_TEMPLATE_UUID: action},
        child=placeholder,
        contracts={},
    )
    world.publish()
    actual_input = None if ready_only else (input_schema or scalar_schema())
    actual_output = None if ready_only else (output_schema or actual_input)
    node = action_node(
        node_uuid=CHILD_NODE_UUID,
        parameter=None if ready_only else "value",
    )
    material_passthrough = (
        isinstance(actual_input, dict) and actual_input.get("$slot") == "ResourceSlot"
    )
    output_binding = (
        None
        if ready_only
        else {"kind": "workflow_input", "parameter": "value"}
        if material_passthrough
        else {
            "kind": "node_output",
            "workflow_node_uuid": CHILD_NODE_UUID,
            "source_handle_uuid": ACTION_VALUE_SOURCE_UUID,
        }
    )
    boundary_handles = (
        {
            ("ready", "target"): CHILD_READY_TARGET_UUID,
            ("ready", "source"): CHILD_READY_SOURCE_UUID,
        }
        if ready_only
        else {
            ("value", "target"): CHILD_VALUE_TARGET_UUID,
            (
                "value" if material_passthrough else "result",
                "source",
            ): CHILD_VALUE_SOURCE_UUID,
            ("ready", "target"): CHILD_READY_TARGET_UUID,
            ("ready", "source"): CHILD_READY_SOURCE_UUID,
        }
    )
    child = create_applied_workflow(
        world,
        workflow_uuid=CHILD_WORKFLOW_UUID,
        module="tests.c1_r2.child",
        symbol="child",
        template_uuid=CHILD_TEMPLATE_UUID,
        boundary_handle_uuids=boundary_handles,
        nodes=[node],
        edges=[],
        meta_data=workflow_meta(
            input_schema=actual_input,
            output_schema=actual_output,
            output_binding=output_binding,
            output_implicit=material_passthrough,
        ),
    )
    world.child = child
    world.contracts[child.source.workflow_uuid] = child
    world.store.create_workflow(
        workflow_uuid=PARENT_WORKFLOW_UUID,
        name="parent",
        tags=[],
        description=None,
        meta_data=workflow_meta(
            input_schema=actual_input,
            output_schema=None,
            output_binding=None,
        ),
    )
    return world


def make_nested_world(
    tmp_path: Path,
    *,
    edge_description: str | None = None,
    edge_meta_data: dict[str, Any] | None = None,
    leaf_group: bool = False,
) -> ExpansionWorld:
    """建立一个含完整 nested hierarchy 与固定 edge vector 的 Applied child。"""

    store = WorkflowStore(tmp_path / "workflow.db")
    catalog = TemplateCatalog(store)
    resolver = MemoryPublishedWorkflowResolver()
    nested_action = action_import(
        template_uuid=SECOND_ACTION_TEMPLATE_UUID,
        resource_template_uuid=SECOND_ACTION_RESOURCE_TEMPLATE_UUID,
        target_handle_uuid=SECOND_ACTION_VALUE_TARGET_UUID,
        source_handle_uuid=SECOND_ACTION_VALUE_SOURCE_UUID,
        ready_target_uuid=SECOND_ACTION_READY_TARGET_UUID,
        ready_source_uuid=SECOND_ACTION_READY_SOURCE_UUID,
    )
    placeholder = WorkflowContractFixture(
        source=PublishedWorkflowSource(
            workflow_uuid=CHILD_WORKFLOW_UUID,
            definition_fqid="tests.c1_r2.pending",
            module="tests.c1_r2.pending",
            symbol="pending",
            package_catalog_digest=_digest("pending-package"),
            definition_content_hash=_digest("pending-definition"),
        ),
        template_uuid=CHILD_TEMPLATE_UUID,
        handles={},
        contract_pin={},
    )
    imports = {SECOND_ACTION_TEMPLATE_UUID: nested_action}
    if leaf_group:
        imports[GROUP_TEMPLATE_UUID] = presentation_group_import()
    world = ExpansionWorld(
        store=store,
        catalog=catalog,
        resolver=resolver,
        imports=imports,
        child=placeholder,
        contracts={},
    )
    world.publish()
    leaf = create_applied_workflow(
        world,
        workflow_uuid=LEAF_WORKFLOW_UUID,
        module="tests.c1_r2.leaf",
        symbol="leaf",
        template_uuid=LEAF_TEMPLATE_UUID,
        boundary_handle_uuids={
            ("value", "target"): LEAF_VALUE_TARGET_UUID,
            ("result", "source"): ACTION_VALUE_SOURCE_UUID,
            ("ready", "target"): LEAF_READY_TARGET_UUID,
            ("ready", "source"): LEAF_READY_SOURCE_UUID,
        },
        nodes=(
            [
                group_node(node_uuid=GROUP_NODE_UUID),
                action_node(
                    node_uuid=GRANDCHILD_NODE_UUID,
                    template_uuid=SECOND_ACTION_TEMPLATE_UUID,
                    target_handle_uuid=SECOND_ACTION_VALUE_TARGET_UUID,
                    parent_uuid=GROUP_NODE_UUID,
                ),
            ]
            if leaf_group
            else [
                action_node(
                    node_uuid=GRANDCHILD_NODE_UUID,
                    template_uuid=SECOND_ACTION_TEMPLATE_UUID,
                    target_handle_uuid=SECOND_ACTION_VALUE_TARGET_UUID,
                )
            ]
        ),
        edges=[],
        meta_data=workflow_meta(
            input_schema=scalar_schema(),
            output_schema=scalar_schema(),
            output_binding={
                "kind": "node_output",
                "workflow_node_uuid": GRANDCHILD_NODE_UUID,
                "source_handle_uuid": SECOND_ACTION_VALUE_SOURCE_UUID,
            },
        ),
    )
    world.contracts[leaf.source.workflow_uuid] = leaf
    nested = workflow_node(
        node_uuid=CHILD_NODE_UUID,
        template_uuid=LEAF_TEMPLATE_UUID,
        input_handle_uuid=LEAF_VALUE_TARGET_UUID,
        parameter="value",
        composite=composite_metadata(
            leaf,
            target_node_uuid=STORED_EXPANDED_GRANDCHILD_UUID,
            target_handle_uuid=SECOND_ACTION_VALUE_TARGET_UUID,
            source_node_uuid=STORED_EXPANDED_GRANDCHILD_UUID,
            source_handle_uuid=SECOND_ACTION_VALUE_SOURCE_UUID,
            entry_node_uuid=STORED_EXPANDED_GRANDCHILD_UUID,
            entry_handle_uuid=SECOND_ACTION_READY_TARGET_UUID,
            completion_node_uuid=STORED_EXPANDED_GRANDCHILD_UUID,
            completion_handle_uuid=SECOND_ACTION_READY_SOURCE_UUID,
        ),
    )
    nested_internal = action_node(
        node_uuid=STORED_EXPANDED_GRANDCHILD_UUID,
        template_uuid=SECOND_ACTION_TEMPLATE_UUID,
        target_handle_uuid=SECOND_ACTION_VALUE_TARGET_UUID,
        parameter=None,
        parent_uuid=STORED_EXPANDED_GROUP_UUID if leaf_group else CHILD_NODE_UUID,
    )
    nested_group = (
        group_node(
            node_uuid=STORED_EXPANDED_GROUP_UUID,
            parent_uuid=CHILD_NODE_UUID,
        )
        if leaf_group
        else None
    )
    child = create_applied_workflow(
        world,
        workflow_uuid=CHILD_WORKFLOW_UUID,
        module="tests.c1_r2.child",
        symbol="child",
        template_uuid=CHILD_TEMPLATE_UUID,
        boundary_handle_uuids={
            ("value", "target"): CHILD_VALUE_TARGET_UUID,
            ("result", "source"): CHILD_VALUE_SOURCE_UUID,
            ("ready", "target"): CHILD_READY_TARGET_UUID,
            ("ready", "source"): CHILD_READY_SOURCE_UUID,
        },
        nodes=[
            nested,
            *([nested_group] if nested_group is not None else []),
            nested_internal,
        ],
        edges=[
            authoring_edge(
                edge_uuid=STORED_NESTED_EDGE_UUID,
                source_node_uuid=CHILD_NODE_UUID,
                source_handle_uuid=ACTION_VALUE_SOURCE_UUID,
                target_node_uuid=STORED_EXPANDED_GRANDCHILD_UUID,
                target_handle_uuid=SECOND_ACTION_VALUE_TARGET_UUID,
                description=edge_description,
                meta_data=edge_meta_data,
            )
        ],
        meta_data=workflow_meta(
            input_schema=scalar_schema(),
            output_schema=scalar_schema(),
            output_binding={
                "kind": "node_output",
                "workflow_node_uuid": STORED_EXPANDED_GRANDCHILD_UUID,
                "source_handle_uuid": SECOND_ACTION_VALUE_SOURCE_UUID,
            },
        ),
        hierarchical_fixture=True,
    )
    world.child = child
    world.contracts[child.source.workflow_uuid] = child
    world.store.create_workflow(
        workflow_uuid=PARENT_WORKFLOW_UUID,
        name="parent",
        tags=[],
        description=None,
        meta_data=workflow_meta(
            input_schema=scalar_schema(),
            output_schema=None,
            output_binding=None,
        ),
    )
    return world


def make_nested_resource_world(
    tmp_path: Path,
    *,
    parent_schema: dict[str, Any],
    direct_schema: dict[str, Any],
    leaf_schema: dict[str, Any],
) -> ExpansionWorld:
    """使用真实 I/O binding 发布 parent -> direct Composite -> leaf Action。

    嵌套 provider 有意存放在 ``meta_data.unilab.input_bindings``；``param``
    保持为空，因为它专用于 literal value。
    """

    store = WorkflowStore(tmp_path / "workflow.db")
    catalog = TemplateCatalog(store)
    resolver = MemoryPublishedWorkflowResolver()
    nested_action = action_import(
        template_uuid=SECOND_ACTION_TEMPLATE_UUID,
        resource_template_uuid=SECOND_ACTION_RESOURCE_TEMPLATE_UUID,
        target_handle_uuid=SECOND_ACTION_VALUE_TARGET_UUID,
        source_handle_uuid=SECOND_ACTION_VALUE_SOURCE_UUID,
        ready_target_uuid=SECOND_ACTION_READY_TARGET_UUID,
        ready_source_uuid=SECOND_ACTION_READY_SOURCE_UUID,
        value_schema=leaf_schema,
    )
    placeholder = WorkflowContractFixture(
        source=PublishedWorkflowSource(
            workflow_uuid=CHILD_WORKFLOW_UUID,
            definition_fqid="tests.c1_r2.pending",
            module="tests.c1_r2.pending",
            symbol="pending",
            package_catalog_digest=_digest("pending-package"),
            definition_content_hash=_digest("pending-definition"),
        ),
        template_uuid=CHILD_TEMPLATE_UUID,
        handles={},
        contract_pin={},
    )
    world = ExpansionWorld(
        store=store,
        catalog=catalog,
        resolver=resolver,
        imports={SECOND_ACTION_TEMPLATE_UUID: nested_action},
        child=placeholder,
        contracts={},
    )
    try:
        world.publish()
        leaf = create_applied_workflow(
            world,
            workflow_uuid=LEAF_WORKFLOW_UUID,
            module="tests.c1_r2.resource_leaf",
            symbol="resource_leaf",
            template_uuid=LEAF_TEMPLATE_UUID,
            boundary_handle_uuids={
                ("value", "target"): LEAF_VALUE_TARGET_UUID,
                ("value", "source"): LEAF_VALUE_SOURCE_UUID,
                ("ready", "target"): LEAF_READY_TARGET_UUID,
                ("ready", "source"): LEAF_READY_SOURCE_UUID,
            },
            nodes=[
                action_node(
                    node_uuid=GRANDCHILD_NODE_UUID,
                    template_uuid=SECOND_ACTION_TEMPLATE_UUID,
                    target_handle_uuid=SECOND_ACTION_VALUE_TARGET_UUID,
                )
            ],
            edges=[],
            meta_data=workflow_meta(
                input_schema=leaf_schema,
                output_schema=leaf_schema,
                output_binding={"kind": "workflow_input", "parameter": "value"},
                output_implicit=True,
            ),
        )
        world.contracts[leaf.source.workflow_uuid] = leaf
        nested = workflow_node(
            node_uuid=CHILD_NODE_UUID,
            template_uuid=LEAF_TEMPLATE_UUID,
            input_handle_uuid=LEAF_VALUE_TARGET_UUID,
            parameter="value",
            composite=composite_metadata(
                leaf,
                target_node_uuid=STORED_EXPANDED_GRANDCHILD_UUID,
                target_handle_uuid=SECOND_ACTION_VALUE_TARGET_UUID,
                source_node_uuid=None,
                source_handle_uuid=None,
                entry_node_uuid=STORED_EXPANDED_GRANDCHILD_UUID,
                entry_handle_uuid=SECOND_ACTION_READY_TARGET_UUID,
                completion_node_uuid=STORED_EXPANDED_GRANDCHILD_UUID,
                completion_handle_uuid=SECOND_ACTION_READY_SOURCE_UUID,
            ),
        )
        assert nested.param == {}
        assert nested.meta_data["unilab"]["input_bindings"] == {
            LEAF_VALUE_TARGET_UUID: {"parameter": "value"}
        }
        nested_internal = action_node(
            node_uuid=STORED_EXPANDED_GRANDCHILD_UUID,
            template_uuid=SECOND_ACTION_TEMPLATE_UUID,
            target_handle_uuid=SECOND_ACTION_VALUE_TARGET_UUID,
            parameter=None,
            parent_uuid=CHILD_NODE_UUID,
        )
        child = create_applied_workflow(
            world,
            workflow_uuid=CHILD_WORKFLOW_UUID,
            module="tests.c1_r2.resource_child",
            symbol="resource_child",
            template_uuid=CHILD_TEMPLATE_UUID,
            boundary_handle_uuids={
                ("value", "target"): CHILD_VALUE_TARGET_UUID,
                ("value", "source"): CHILD_VALUE_SOURCE_UUID,
                ("ready", "target"): CHILD_READY_TARGET_UUID,
                ("ready", "source"): CHILD_READY_SOURCE_UUID,
            },
            nodes=[nested, nested_internal],
            edges=[],
            meta_data=workflow_meta(
                input_schema=direct_schema,
                output_schema=direct_schema,
                output_binding={"kind": "workflow_input", "parameter": "value"},
                output_implicit=True,
            ),
            hierarchical_fixture=True,
        )
        world.child = child
        world.contracts[child.source.workflow_uuid] = child
        world.store.create_workflow(
            workflow_uuid=PARENT_WORKFLOW_UUID,
            name="resource-parent",
            tags=[],
            description=None,
            meta_data=workflow_meta(
                input_schema=parent_schema,
                output_schema=None,
                output_binding=None,
            ),
        )
        return world
    except BaseException:
        world.close()
        raise


def plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return plain(value.model_dump(exclude_none=True))
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value
