"""M2A MaterialSource 首条 Python↔Graph 纵向合同。

该 RED 只冻结一个合法 ``existing`` selector 的公开端到端通路：
AST-only Authoring Engine 编译、生成、再编译，以及真实 WorkflowService /
WorkflowStore / TemplateCatalog 的保存与读回。不在本 slice 扩展
Site 范围、mode matrix、fan-out 或 runtime 物料分配。
"""

from __future__ import annotations

from pathlib import Path

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.catalog import (
    CatalogAuthority,
    NodeTemplateImport,
    TemplateCatalog,
)
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "10000000-0000-4000-8000-000000000001"
MATERIAL_SOURCE_NODE_UUID = "20000000-0000-4000-8000-000000000001"
PREPARE_NODE_UUID = "20000000-0000-4000-8000-000000000002"
MATERIAL_SOURCE_TEMPLATE_UUID = "30000000-0000-4000-8000-000000000001"
PREPARE_TEMPLATE_UUID = "30000000-0000-4000-8000-000000000002"
MATERIAL_HANDLE_UUID = "40000000-0000-4000-8000-000000000001"
SAMPLE_HANDLE_UUID = "40000000-0000-4000-8000-000000000002"
HOST_RESOURCE_TEMPLATE_UUID = "31000000-0000-4000-8000-000000000001"
PLATE_RESOURCE_TEMPLATE_UUID = "32000000-0000-4000-8000-000000000001"
MOUNT_MATERIAL_UUID = "50000000-0000-4000-8000-000000000001"
PLATE_SOURCE_IDENTITY = "lab.resources:corning_96_well_plate"

AUTHORITY = CatalogAuthority(authority_id="m2a-test-authority", kind="backend")

EXPECTED_SELECTOR = {
    "mode": "existing",
    "resource_template_uuid": PLATE_RESOURCE_TEMPLATE_UUID,
    "mount": {"uuid": MOUNT_MATERIAL_UUID},
    "material_uuid": None,
    "site": None,
    "slot_range": None,
    "flow_role": "primary_sample",
}


class _StaticResourceTemplateIdentityIndex:
    """唯一允许的窄 fake：authority-owned ResourceTemplate 身份边界。"""

    def resolve_symbol(self, qualified_name: str) -> str:
        if qualified_name != PLATE_SOURCE_IDENTITY:
            raise KeyError(qualified_name)
        return PLATE_RESOURCE_TEMPLATE_UUID

    def identify_uuid(self, resource_template_uuid: str) -> str:
        if resource_template_uuid != PLATE_RESOURCE_TEMPLATE_UUID:
            raise KeyError(resource_template_uuid)
        return PLATE_SOURCE_IDENTITY


def _handle(
    handle_uuid: str,
    *,
    key: str,
    io_type: str,
    required: bool,
) -> dict[str, object]:
    return {
        "uuid": handle_uuid,
        "description": f"{key} contract",
        "meta_data": {"contract": "m2a"},
        "handle_key": key,
        "io_type": io_type,
        "display_name": key.replace("_", " ").title(),
        "type": "ResourceSlot",
        "required": required,
        "data_source": "executor",
        "data_key": key,
    }


def _catalog_imports() -> list[NodeTemplateImport]:
    return [
        NodeTemplateImport(
            template={
                "uuid": MATERIAL_SOURCE_TEMPLATE_UUID,
                "description": "Framework-owned MaterialSource",
                "meta_data": {"framework": "material_source"},
                "resource_template_uuid": HOST_RESOURCE_TEMPLATE_UUID,
                "name": "material_source",
                "display_name": "Material Source",
                "class": "unilabos.workflow.authoring:material_source",
                "goal": {},
                "goal_default": {},
                "feedback": {},
                "result": {},
                "schema": None,
                "type": "material_source",
                "icon": None,
                "header": None,
                "footer": None,
                "node_type": "material_source",
            },
            handles=[
                _handle(
                    MATERIAL_HANDLE_UUID,
                    key="material",
                    io_type="source",
                    required=False,
                )
            ],
        ),
        NodeTemplateImport(
            template={
                "uuid": PREPARE_TEMPLATE_UUID,
                "description": "Prepare one plate",
                "meta_data": {"contract": "m2a"},
                "resource_template_uuid": HOST_RESOURCE_TEMPLATE_UUID,
                "name": "prepare",
                "display_name": "Prepare",
                "class": "lab.devices:Reactor",
                "goal": {},
                "goal_default": {},
                "feedback": {},
                "result": {},
                "schema": None,
                "type": "action",
                "icon": None,
                "header": None,
                "footer": None,
                "node_type": "compute",
            },
            handles=[
                _handle(
                    SAMPLE_HANDLE_UUID,
                    key="sample",
                    io_type="target",
                    required=True,
                )
            ],
        ),
    ]


def _source() -> str:
    return f'''from lab.devices import Reactor
from lab.resources import corning_96_well_plate
from unilabos.workflow.authoring import (
    MaterialFlowRole,
    device,
    material_source,
    resource_ref,
    workflow_definition,
)


reactor: Reactor = device()


@workflow_definition(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="Assay",
)
def assay_workflow():
    # unilab:node_uuid={MATERIAL_SOURCE_NODE_UUID}
    assay_plate = material_source(
        resource_template=corning_96_well_plate,
        mode="existing",
        mount=resource_ref("{MOUNT_MATERIAL_UUID}"),
        material_uuid=None,
        site=None,
        slot_range=None,
        flow_role=MaterialFlowRole.PRIMARY_SAMPLE,
    )
    # unilab:node_uuid={PREPARE_NODE_UUID}
    prepared = reactor.prepare(sample=assay_plate)
'''


def _node(graph: dict[str, object], node_uuid: str) -> dict[str, object]:
    nodes = graph["nodes"]
    assert isinstance(nodes, list)
    return next(item for item in nodes if item["uuid"] == node_uuid)


def test_canonical_material_source_round_trips_and_persists_selector(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        catalog = TemplateCatalog(store)
        catalog.replace(AUTHORITY, _catalog_imports())
        engine = WorkflowAuthoringEngine(
            catalog=catalog,
            authority=AUTHORITY,
            resource_template_identity_index=(_StaticResourceTemplateIdentityIndex()),
        )
        service = WorkflowService(store, compiler=engine)
        service.create_workflow(
            workflow_uuid=WORKFLOW_UUID,
            name="Assay",
            tags=[],
            description=None,
            meta_data={},
        )
        applied = service.get_graph(WORKFLOW_UUID)

        compiled = engine.compile(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=1,
            python_source=_source(),
            source_uri="package://lab/workflows/m2a_assay.py",
            applied_graph=applied,
        )

        assert compiled.valid, compiled.diagnostics
        assert compiled.graph is not None
        candidate = compiled.graph
        material_source_node = _node(candidate, MATERIAL_SOURCE_NODE_UUID)
        assert material_source_node["name"] == "assay_plate"
        assert material_source_node["type"] == "material_source"
        assert material_source_node["workflow_node_template_uuid"] == (
            MATERIAL_SOURCE_TEMPLATE_UUID
        )
        assert material_source_node.get("material_uuid") is None
        assert material_source_node["param"] == EXPECTED_SELECTOR

        source_handles = [
            handle
            for handle in candidate["handle_templates"]
            if handle["workflow_node_template_uuid"] == MATERIAL_SOURCE_TEMPLATE_UUID
        ]
        assert len(source_handles) == 1
        assert {
            "uuid": source_handles[0]["uuid"],
            "handle_key": source_handles[0]["handle_key"],
            "io_type": source_handles[0]["io_type"],
            "type": source_handles[0]["type"],
        } == {
            "uuid": MATERIAL_HANDLE_UUID,
            "handle_key": "material",
            "io_type": "source",
            "type": "ResourceSlot",
        }
        assert len(candidate["edges"]) == 1
        assert {
            "source_node_uuid": candidate["edges"][0]["source_node_uuid"],
            "target_node_uuid": candidate["edges"][0]["target_node_uuid"],
            "source_handle_uuid": candidate["edges"][0]["source_handle_uuid"],
            "target_handle_uuid": candidate["edges"][0]["target_handle_uuid"],
        } == {
            "source_node_uuid": MATERIAL_SOURCE_NODE_UUID,
            "target_node_uuid": PREPARE_NODE_UUID,
            "source_handle_uuid": MATERIAL_HANDLE_UUID,
            "target_handle_uuid": SAMPLE_HANDLE_UUID,
        }

        generated = engine.generate_python(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=1,
            graph=candidate,
            source_uri="package://lab/workflows/m2a_assay.py",
        )
        assert generated.valid, generated.diagnostics
        assert generated.normalized_python_source is not None
        assert "MaterialFlowRole.PRIMARY_SAMPLE" in generated.normalized_python_source
        assert "resource_template=corning_96_well_plate" in (
            generated.normalized_python_source
        )

        recompiled = engine.compile(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=1,
            python_source=generated.normalized_python_source,
            source_uri="package://lab/workflows/m2a_assay.py",
            applied_graph=candidate,
        )
        assert recompiled.valid, recompiled.diagnostics
        assert recompiled.graph == candidate

        service.save_graph(
            WORKFLOW_UUID,
            revision=1,
            nodes=candidate["nodes"],
            edges=candidate["edges"],
        )
        persisted = service.get_graph(WORKFLOW_UUID)
        assert _node(persisted, MATERIAL_SOURCE_NODE_UUID)["param"] == (
            EXPECTED_SELECTOR
        )
        assert persisted["edges"] == candidate["edges"]
    finally:
        store.close()
