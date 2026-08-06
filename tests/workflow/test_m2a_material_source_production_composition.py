"""M2A production Registry 与独立 Inventory composition 纵向合同。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from unilabos.app.scheduler.inventory import (
    InventoryService,
    ResourceTemplateIdentity,
)
from unilabos.registry.catalog_consumer import (
    workflow_template_imports_from_registry_snapshot,
)
from unilabos.workflow.catalog import (
    CatalogAuthority,
    LocalResourceTemplateIdentityIndex,
    TemplateCatalog,
)
from unilabos.workflow.composition import (
    compose_workflow_runtime,
    reset_workflow_service_for_test,
)
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

AUTHORITY = CatalogAuthority(authority_id="m2a-production", kind="local")

HOST_SOURCE_IDENTITY = "host_node"
PLATE_SOURCE_IDENTITY = "lab.resources:plate_96"
WAREHOUSE_SOURCE_IDENTITY = "lab.resources:warehouse"

WORKFLOW_UUID = "91000000-0000-4000-8000-000000000001"
MATERIAL_SOURCE_NODE_UUID = "92000000-0000-4000-8000-000000000001"
MOUNT_MATERIAL_UUID = "93000000-0000-4000-8000-000000000001"
SITE_UUID = "94000000-0000-4000-8000-000000000001"
MISSING_MOUNT_UUID = "93000000-0000-4000-8000-000000000099"


def _registry_snapshot(*, include_host: bool = True) -> Mapping[str, object]:
    devices: dict[str, object] = {}
    if include_host:
        devices["host_node"] = {
            "class": {
                "module": "unilabos.ros.nodes.presets.host_node:HostNode",
                "action_value_mappings": {},
            },
            "display_name": "Host Node",
        }
    else:
        devices["custom_device"] = {
            "class": {
                "module": "lab.devices:CustomDevice",
                "action_value_mappings": {},
            },
            "display_name": "Custom Device",
        }
    return MappingProxyType(deepcopy(devices))


def _resource_registry_snapshot() -> Mapping[str, object]:
    return MappingProxyType(
        {
            "plate_96": {"class": {"module": PLATE_SOURCE_IDENTITY}},
            "warehouse": {"class": {"module": WAREHOUSE_SOURCE_IDENTITY}},
        }
    )


@dataclass(frozen=True)
class _SeededIdentities:
    host_uuid: str
    plate_uuid: str
    warehouse_uuid: str

    @property
    def by_source(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                HOST_SOURCE_IDENTITY: self.host_uuid,
                PLATE_SOURCE_IDENTITY: self.plate_uuid,
                WAREHOUSE_SOURCE_IDENTITY: self.warehouse_uuid,
            }
        )


def _seed_material_authority(working_dir: Path) -> _SeededIdentities:
    working_dir.mkdir(parents=True, exist_ok=True)
    store = WorkflowStore(working_dir / "workflow.db")
    try:
        index = LocalResourceTemplateIdentityIndex(
            store,
            AUTHORITY,
            (
                HOST_SOURCE_IDENTITY,
                PLATE_SOURCE_IDENTITY,
                WAREHOUSE_SOURCE_IDENTITY,
            ),
        )
        identities = _SeededIdentities(
            host_uuid=index.resolve_symbol(HOST_SOURCE_IDENTITY),
            plate_uuid=index.resolve_symbol(PLATE_SOURCE_IDENTITY),
            warehouse_uuid=index.resolve_symbol(WAREHOUSE_SOURCE_IDENTITY),
        )
        TemplateCatalog(store).replace(
            AUTHORITY,
            [],
            resource_template_identities=index.assignments,
        )
    finally:
        store.close()

    resource_templates = {
        identities.host_uuid: ResourceTemplateIdentity(
            uuid=identities.host_uuid,
            material_class="HostNode",
        ),
        identities.plate_uuid: ResourceTemplateIdentity(
            uuid=identities.plate_uuid,
            material_class="Plate96",
        ),
        identities.warehouse_uuid: ResourceTemplateIdentity(
            uuid=identities.warehouse_uuid,
            material_class="Warehouse",
        ),
    }
    inventory = InventoryService.open(
        working_dir=working_dir,
        resource_templates=MappingProxyType(resource_templates),
    )
    try:
        inventory.create_material(
            material_uuid=MOUNT_MATERIAL_UUID,
            resource_template_uuid=identities.warehouse_uuid,
            barcode="M2A-WAREHOUSE-1",
            name="M2A warehouse",
        )
        inventory.create_site(
            site_uuid=SITE_UUID,
            description="M2A direct compatible Site",
            meta_data={"slot": "A1"},
            material_uuid=MOUNT_MATERIAL_UUID,
            name="A1",
            sort_order=0,
            allowed_resource_template_uuids=[identities.plate_uuid],
            occupied_material_uuid=None,
            position_x=0.0,
            position_y=0.0,
            position_z=0.0,
            depth=1.0,
            length=1.0,
            width=1.0,
        )
        return identities
    finally:
        inventory.close()


@contextmanager
def _composed_service(
    working_dir: Path,
) -> Iterator[tuple[WorkflowService, _SeededIdentities]]:
    identities = _seed_material_authority(working_dir)
    reset_workflow_service_for_test()
    try:
        service = compose_workflow_runtime(
            working_dir,
            authority=AUTHORITY,
            registry_snapshot=_registry_snapshot(),
            resource_registry_snapshot=_resource_registry_snapshot(),
        )
        yield service, identities
    finally:
        reset_workflow_service_for_test()


def _source() -> str:
    return f'''from lab.resources import plate_96
from unilabos.workflow.authoring import (
    MaterialFlowRole,
    material_source,
    resource_ref,
    workflow_definition,
)


@workflow_definition(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="Production MaterialSource",
)
def production_material_source():
    # unilab:node_uuid={MATERIAL_SOURCE_NODE_UUID}
    sample = material_source(
        resource_template=plate_96,
        mode="create_new",
        mount=resource_ref("{MOUNT_MATERIAL_UUID}"),
        material_uuid=None,
        site="{SITE_UUID}",
        slot_range=None,
        flow_role=MaterialFlowRole.PRIMARY_SAMPLE,
    )
'''


def _create_workflow(service: WorkflowService) -> dict[str, Any]:
    service.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="Production MaterialSource",
        tags=[],
        description=None,
        meta_data={},
    )
    return service.get_graph(WORKFLOW_UUID)


def _compile(service: WorkflowService, applied_graph: dict[str, Any]) -> Any:
    assert service.compiler is not None
    return service.compiler.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=1,
        python_source=_source(),
        source_uri="package://lab/workflows/production_material_source.py",
        applied_graph=applied_graph,
    )


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def test_registry_adapter_publishes_one_host_owned_material_source_aggregate(
    tmp_path: Path,
) -> None:
    identities = _seed_material_authority(tmp_path / "seed")
    imports = workflow_template_imports_from_registry_snapshot(
        _registry_snapshot(),
        authority_id=AUTHORITY.authority_id,
        resource_template_identity_resolver=identities.by_source.__getitem__,
    )

    assert {item.template["name"] for item in imports} == {
        "group",
        "material_source",
    }
    framework = next(
        item for item in imports if item.template["name"] == "material_source"
    )
    assert {
        "class": framework.template["class"],
        "name": framework.template["name"],
        "type": framework.template["type"],
        "node_type": framework.template["node_type"],
        "resource_template_uuid": framework.template["resource_template_uuid"],
    } == {
        "class": "unilabos.workflow.authoring:material_source",
        "name": "material_source",
        "type": "material_source",
        "node_type": "material_source",
        "resource_template_uuid": identities.host_uuid,
    }
    assert framework.template["meta_data"]["unilab"]["authority_id"] == (
        AUTHORITY.authority_id
    )
    assert len(framework.handles) == 1
    handle = _plain(framework.handles[0])
    assert {
        "handle_key": handle["handle_key"],
        "io_type": handle["io_type"],
        "type": handle["type"],
        "required": handle["required"],
        "data_source": handle["data_source"],
        "data_key": handle["data_key"],
    } == {
        "handle_key": "material",
        "io_type": "source",
        "type": "ResourceSlot",
        "required": False,
        "data_source": "executor",
        "data_key": "material",
    }

    without_host = workflow_template_imports_from_registry_snapshot(
        _registry_snapshot(include_host=False),
        authority_id=AUTHORITY.authority_id,
        resource_template_identity_resolver=identities.by_source.__getitem__,
    )
    assert without_host == ()


def test_production_composition_compiles_material_source_with_inventory_authority(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    with _composed_service(working_dir) as (service, identities):
        reader = WorkflowStore(working_dir / "workflow.db")
        try:
            with TemplateCatalog(reader).snapshot(AUTHORITY) as snapshot:
                assert {item["name"] for item in snapshot.node_templates} == {
                    "group",
                    "material_source",
                }
                assert len(snapshot.handle_templates) == 1
                material_source_template = next(
                    item
                    for item in snapshot.node_templates
                    if item["name"] == "material_source"
                )
                assert material_source_template["resource_template_uuid"] == (
                    identities.host_uuid
                )
        finally:
            reader.close()

        applied = _create_workflow(service)
        compiled = _compile(service, applied)
        assert compiled.valid, compiled.diagnostics
        assert compiled.graph is not None
        material_source = next(
            item
            for item in compiled.graph["nodes"]
            if item["uuid"] == MATERIAL_SOURCE_NODE_UUID
        )
        assert material_source["param"]["resource_template_uuid"] == (
            identities.plate_uuid
        )

        assert service.compiler is not None
        generated = service.compiler.generate_python(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=1,
            graph=compiled.graph,
            source_uri="package://lab/workflows/production_material_source.py",
        )
        assert generated.valid, generated.diagnostics
        assert generated.normalized_python_source is not None
        assert "from lab.resources import plate_96" in (
            generated.normalized_python_source
        )
        recompiled = service.compiler.compile(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=1,
            python_source=generated.normalized_python_source,
            source_uri="package://lab/workflows/production_material_source.py",
            applied_graph=compiled.graph,
        )

    assert recompiled.valid, recompiled.diagnostics
    assert recompiled.graph == compiled.graph


def test_production_service_rejects_missing_mount_without_graph_change(
    tmp_path: Path,
) -> None:
    with _composed_service(tmp_path / "unilabos_data") as (service, _):
        applied = _create_workflow(service)
        compiled = _compile(service, applied)
        assert compiled.valid, compiled.diagnostics
        assert compiled.graph is not None
        graph = deepcopy(compiled.graph)
        material_source = next(
            item for item in graph["nodes"] if item["uuid"] == MATERIAL_SOURCE_NODE_UUID
        )
        material_source["param"]["mount"] = {"uuid": MISSING_MOUNT_UUID}
        before = service.get_graph(WORKFLOW_UUID)

        with pytest.raises(WorkflowError) as caught:
            service.save_graph(
                WORKFLOW_UUID,
                revision=1,
                nodes=graph["nodes"],
                edges=graph["edges"],
            )

        assert caught.value.code == "not_found"
        assert service.get_graph(WORKFLOW_UUID) == before
        assert before == applied
        assert before["workflow"]["revision"] == 1
