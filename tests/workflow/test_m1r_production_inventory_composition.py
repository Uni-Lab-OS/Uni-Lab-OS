"""M1R production composition 的独立 Inventory authority RED。"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import unilabos.workflow.composition as composition_module
import unilabos.workflow.material_resolver as material_resolver_module
import unilabos.workflow.runtime as runtime_module
from tests.workflow.test_m2a_material_source_production_composition import (
    AUTHORITY,
    MOUNT_MATERIAL_UUID,
    SITE_UUID,
    WORKFLOW_UUID,
    _compile,
    _create_workflow,
    _registry_snapshot,
    _resource_registry_snapshot,
)
from unilabos.app.scheduler.inventory import (
    InventoryService,
    ResourceTemplateIdentity,
)
from unilabos.app.workflow_api import install_composed_workflow_authoring_api
from unilabos.workflow.composition import (
    compose_workflow_runtime,
    get_workflow_inventory_service,
    reset_workflow_service_for_test,
)
from unilabos.workflow.service import WorkflowError, WorkflowService

HOST_TEMPLATE_UUID = "81000000-0000-4000-8000-000000000214"
PLATE_TEMPLATE_UUID = "82000000-0000-4000-8000-000000000214"
WAREHOUSE_TEMPLATE_UUID = "83000000-0000-4000-8000-000000000214"
SAMPLE_MATERIAL_UUID = "93000000-0000-4000-8000-000000000214"


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
        inventory.create_material(
            material_uuid=SAMPLE_MATERIAL_UUID,
            resource_template_uuid=PLATE_TEMPLATE_UUID,
            barcode="M1R-PRODUCTION-SAMPLE",
            name="M1R production sample",
        )
        inventory.create_site(
            site_uuid=SITE_UUID,
            description="M1R production compatible Site",
            meta_data={"slot": "A1"},
            material_uuid=MOUNT_MATERIAL_UUID,
            name="A1",
            sort_order=0,
            allowed_resource_template_uuids=[PLATE_TEMPLATE_UUID],
            occupied_material_uuid=SAMPLE_MATERIAL_UUID,
            position_x=0.0,
            position_y=0.0,
            position_z=0.0,
            depth=1.0,
            length=1.0,
            width=1.0,
        )
    finally:
        inventory.close()


def _create_existing_material_task(service: WorkflowService) -> dict[str, Any]:
    applied = _create_workflow(service)
    compiled = _compile(service, applied)
    assert compiled.valid, compiled.diagnostics
    assert compiled.graph is not None
    nodes = [dict(node) for node in compiled.graph["nodes"]]
    material_source = next(node for node in nodes if node["type"] == "material_source")
    material_source["param"] = {
        **material_source["param"],
        "mode": "existing",
        "material_uuid": SAMPLE_MATERIAL_UUID,
    }
    service.save_graph(
        applied["workflow"]["uuid"],
        revision=applied["workflow"]["revision"],
        nodes=nodes,
        edges=compiled.graph["edges"],
    )
    return service.create_workflow_task(
        workflow_uuid=WORKFLOW_UUID,
        run_mode="normal",
        target_node_uuid=None,
        input_value={},
        description=None,
        meta_data={},
    )


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(
                "timed out waiting for production runtime reconciliation"
            )
        time.sleep(0.02)


def test_production_composition_opens_workflow_and_inventory_databases(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "unilabos_data"

    service = compose_workflow_runtime(working_dir)

    assert service is composition_module.get_workflow_service()
    assert get_workflow_inventory_service() is not None
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


def test_edge_scheduler_reuses_composed_workflow_and_inventory_authorities(
    tmp_path: Path,
) -> None:
    from unilabos.app.scheduler import integration

    working_dir = tmp_path / "unilabos_data"
    service = compose_workflow_runtime(working_dir)
    inventory = get_workflow_inventory_service()
    assert inventory is not None
    try:
        scheduler, _backend = integration.setup_edge_scheduler(
            inventory_service=inventory,
            workflow_tasks=service,
            host_node_getter=lambda: None,
            device_state_db_path="off",
            workflow_history_db_path="off",
        )

        assert integration.get_inventory_service() is inventory
        with pytest.raises(WorkflowError, match="请求的资源不存在"):
            scheduler.reconcile_task_admission("70000000-0000-4000-8000-000000000214")
        assert sorted(path.name for path in working_dir.glob("*.db")) == [
            "inventory.db",
            "workflow.db",
        ]
    finally:
        integration.reset_for_test()


def test_production_task_post_coordinates_admission_after_task_commit(
    tmp_path: Path,
) -> None:
    service = compose_workflow_runtime(tmp_path / "unilabos_data")
    service.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="M1R task ingress",
        tags=[],
        description=None,
        meta_data={},
    )
    coordinated: list[str] = []

    def coordinate(task_uuid: str) -> None:
        assert service.get_workflow_task(task_uuid)["uuid"] == task_uuid
        coordinated.append(task_uuid)

    app = FastAPI()
    install_composed_workflow_authoring_api(
        app,
        service,
        object(),
        task_admission_coordinator=coordinate,
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/workflow-tasks",
            json={
                "workflow_uuid": WORKFLOW_UUID,
                "run_mode": "normal",
                "input": {},
                "meta_data": {},
            },
        )

    assert response.status_code == 201
    assert coordinated == [response.json()["data"]["uuid"]]


def test_edge_scheduler_startup_reconciles_pending_material_source_tasks(
    tmp_path: Path,
) -> None:
    from unilabos.app.scheduler import integration

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
    service.save_graph(
        applied["workflow"]["uuid"],
        revision=applied["workflow"]["revision"],
        nodes=compiled.graph["nodes"],
        edges=compiled.graph["edges"],
    )
    task = service.create_workflow_task(
        workflow_uuid=WORKFLOW_UUID,
        run_mode="normal",
        target_node_uuid=None,
        input_value={},
        description=None,
        meta_data={},
    )
    assert service.get_material_admission(task["uuid"]) is None
    inventory = get_workflow_inventory_service()
    assert inventory is not None

    try:
        integration.setup_edge_scheduler(
            inventory_service=inventory,
            workflow_tasks=service,
            host_node_getter=lambda: None,
            device_state_db_path="off",
            workflow_history_db_path="off",
        )

        projection = service.get_material_admission(task["uuid"])
        assert projection is not None
        assert projection["status"] == "rejected"
    finally:
        integration.reset_for_test()


def test_production_cancel_command_triggers_terminal_material_release(
    tmp_path: Path,
) -> None:
    from unilabos.app.scheduler import integration

    working_dir = tmp_path / "unilabos_data"
    _seed_public_inventory(working_dir)
    service = compose_workflow_runtime(
        working_dir,
        authority=AUTHORITY,
        registry_snapshot=_registry_snapshot(),
        resource_registry_snapshot=_resource_registry_snapshot(),
        resource_template_identity_resolver=_FixedResourceTemplateIdentityIndex(),
    )
    task = _create_existing_material_task(service)
    inventory = get_workflow_inventory_service()
    assert inventory is not None

    try:
        integration.setup_edge_scheduler(
            inventory_service=inventory,
            workflow_tasks=service,
            host_node_getter=lambda: None,
            device_state_db_path="off",
            workflow_history_db_path="off",
        )
        admission = service.get_material_admission(task["uuid"])
        assert admission is not None
        assert admission["status"] == "admitted"
        reservation_uuid = admission["reservation_uuid"]
        assert isinstance(reservation_uuid, str)

        service.create_workflow_task_command(
            task["uuid"],
            command_type="cancel",
            target_node_uuid=None,
            idempotency_key="m1r-production-terminal-release",
            description=None,
            meta_data={},
        )
        _wait_until(
            lambda: (
                service.get_workflow_task(task["uuid"])["status"] == "canceled"
                and service.get_material_release(task["uuid"]) is not None
            )
        )

        release = service.get_material_release(task["uuid"])
        assert release is not None
        assert release["status"] == "released"
        assert release["reservation_uuid"] == reservation_uuid
        assert not inventory.has_active_task_reservation(
            task["uuid"],
            reservation_uuid,
        )
    finally:
        integration.reset_for_test()


def test_edge_scheduler_startup_recovers_missed_terminal_release(
    tmp_path: Path,
) -> None:
    from unilabos.app.scheduler import integration

    working_dir = tmp_path / "unilabos_data"
    _seed_public_inventory(working_dir)
    service = compose_workflow_runtime(
        working_dir,
        authority=AUTHORITY,
        registry_snapshot=_registry_snapshot(),
        resource_registry_snapshot=_resource_registry_snapshot(),
        resource_template_identity_resolver=_FixedResourceTemplateIdentityIndex(),
    )
    task = _create_existing_material_task(service)
    inventory = get_workflow_inventory_service()
    assert inventory is not None

    try:
        integration.setup_edge_scheduler(
            inventory_service=inventory,
            workflow_tasks=service,
            host_node_getter=lambda: None,
            device_state_db_path="off",
            workflow_history_db_path="off",
        )
        admission = service.get_material_admission(task["uuid"])
        assert admission is not None
        reservation_uuid = admission["reservation_uuid"]
        assert isinstance(reservation_uuid, str)
        integration.reset_for_test()

        service.create_workflow_task_command(
            task["uuid"],
            command_type="cancel",
            target_node_uuid=None,
            idempotency_key="m1r-production-terminal-recovery",
            description=None,
            meta_data={},
        )
        _wait_until(
            lambda: service.get_workflow_task(task["uuid"])["status"] == "canceled"
        )
        assert service.get_material_release(task["uuid"]) is None
        assert inventory.has_active_task_reservation(
            task["uuid"],
            reservation_uuid,
        )

        integration.setup_edge_scheduler(
            inventory_service=inventory,
            workflow_tasks=service,
            host_node_getter=lambda: None,
            device_state_db_path="off",
            workflow_history_db_path="off",
        )

        release = service.get_material_release(task["uuid"])
        assert release is not None
        assert release["status"] == "released"
        assert release["reservation_uuid"] == reservation_uuid
        assert not inventory.has_active_task_reservation(
            task["uuid"],
            reservation_uuid,
        )
    finally:
        integration.reset_for_test()
