"""M1R production composition 的独立 Inventory authority RED。"""

from __future__ import annotations

from pathlib import Path

import pytest

import unilabos.workflow.composition as composition_module
import unilabos.workflow.material_resolver as material_resolver_module
import unilabos.workflow.runtime as runtime_module
from tests.workflow.test_m2a_material_source_production_composition import (
    AUTHORITY,
    MOUNT_MATERIAL_UUID,
    SITE_UUID,
    _compile,
    _create_workflow,
    _registry_snapshot,
    _resource_registry_snapshot,
)
from unilabos.app.scheduler.inventory import (
    InventoryService,
    ResourceTemplateIdentity,
)
from unilabos.workflow.composition import (
    compose_workflow_runtime,
    reset_workflow_service_for_test,
)

HOST_TEMPLATE_UUID = "81000000-0000-4000-8000-000000000214"
PLATE_TEMPLATE_UUID = "82000000-0000-4000-8000-000000000214"
WAREHOUSE_TEMPLATE_UUID = "83000000-0000-4000-8000-000000000214"


class _FixedResourceTemplateIdentityIndex:
    """测试 Registry snapshot 的稳定双向 identity port。"""

    def __init__(self) -> None:
        self._by_source = {
            "host_node": HOST_TEMPLATE_UUID,
            "lab.resources:plate_96": PLATE_TEMPLATE_UUID,
            "lab.resources:warehouse": WAREHOUSE_TEMPLATE_UUID,
        }
        self._by_uuid = {value: key for key, value in self._by_source.items()}

    def resolve_symbol(self, qualified_name: str) -> str:
        return self._by_source[qualified_name]

    def identify_uuid(self, resource_template_uuid: str) -> str:
        return self._by_uuid[resource_template_uuid]


@pytest.fixture(autouse=True)
def _clean_production_composition() -> None:
    reset_workflow_service_for_test()
    try:
        yield
    finally:
        reset_workflow_service_for_test()


def _seed_public_inventory(working_dir: Path) -> None:
    resource_templates = {
        PLATE_TEMPLATE_UUID: ResourceTemplateIdentity(
            uuid=PLATE_TEMPLATE_UUID,
            material_class="Plate96",
        ),
        WAREHOUSE_TEMPLATE_UUID: ResourceTemplateIdentity(
            uuid=WAREHOUSE_TEMPLATE_UUID,
            material_class="Warehouse",
        ),
    }
    inventory = InventoryService.open(
        working_dir=working_dir,
        resource_templates=resource_templates,
    )
    try:
        inventory.create_material(
            material_uuid=MOUNT_MATERIAL_UUID,
            resource_template_uuid=WAREHOUSE_TEMPLATE_UUID,
            barcode="M1R-PRODUCTION-WAREHOUSE",
            name="M1R production warehouse",
        )
        inventory.create_site(
            site_uuid=SITE_UUID,
            description="M1R production compatible Site",
            meta_data={"slot": "A1"},
            material_uuid=MOUNT_MATERIAL_UUID,
            name="A1",
            sort_order=0,
            allowed_resource_template_uuids=[PLATE_TEMPLATE_UUID],
            occupied_material_uuid=None,
            position_x=0.0,
            position_y=0.0,
            position_z=0.0,
            depth=1.0,
            length=1.0,
            width=1.0,
        )
    finally:
        inventory.close()


def test_production_composition_opens_workflow_and_inventory_databases(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "unilabos_data"

    service = compose_workflow_runtime(working_dir)

    assert service is composition_module.get_workflow_service()
    assert (working_dir / "workflow.db").is_file()
    assert (working_dir / "inventory.db").is_file()

    reset_workflow_service_for_test()
    reopened_inventory = InventoryService.open(
        working_dir=working_dir,
        resource_templates={},
    )
    try:
        assert reopened_inventory.get_acknowledged_sequence() == 0
    finally:
        reopened_inventory.close()


def test_production_material_source_reads_inventory_without_workflow_uow(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    _seed_public_inventory(working_dir)
    service = compose_workflow_runtime(
        working_dir,
        authority=AUTHORITY,
        registry_snapshot=_registry_snapshot(),
        resource_registry_snapshot=_resource_registry_snapshot(),
        resource_template_identity_resolver=_FixedResourceTemplateIdentityIndex(),
    )
    applied = _create_workflow(service)

    compiled = _compile(service, applied)

    assert compiled.valid, compiled.diagnostics
    assert compiled.graph is not None
    saved = service.save_graph(
        applied["workflow"]["uuid"],
        revision=applied["workflow"]["revision"],
        nodes=compiled.graph["nodes"],
        edges=compiled.graph["edges"],
    )
    assert saved["workflow"]["revision"] == 2


def test_production_wiring_has_no_retired_or_borrowed_material_authority() -> None:
    production_sources = {
        "composition.py": Path(composition_module.__file__).read_text(encoding="utf-8"),
        "runtime.py": Path(runtime_module.__file__).read_text(encoding="utf-8"),
        "material_resolver.py": Path(material_resolver_module.__file__).read_text(
            encoding="utf-8"
        ),
    }
    forbidden = (
        "unilabos.resources.authority",
        "MaterialModule",
        "SQLiteMaterialAdapter",
        "RuntimeAuthorityUnitOfWork",
        "from_runtime_authority",
        "InventoryStore",
    )
    violations = sorted(
        (filename, marker)
        for filename, source in production_sources.items()
        for marker in forbidden
        if marker in source
    )

    assert {
        "opens_public_inventory": (
            "InventoryService.open(" in production_sources["composition.py"]
        ),
        "forbidden_references": violations,
    } == {
        "opens_public_inventory": True,
        "forbidden_references": [],
    }
