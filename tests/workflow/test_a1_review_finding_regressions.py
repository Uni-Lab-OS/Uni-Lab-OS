"""A1 independent-review regressions across HTTP, authoring, and startup."""

from __future__ import annotations

import copy
import importlib
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.package_manager.test_a1_canonical_action_catalog import (
    RESOURCE_TEMPLATE_UUID,
    _register,
    _write_package,
)
from tests.workflow.test_a1_registry_template_http_chain import (
    AUTHORITY,
    _catalog_state,
    _http_client,
    _imports,
    _registry_snapshot,
)
from unilabos.package_manager import WorkspaceSource, compile_package_source
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.catalog import TemplateCatalog
from unilabos.workflow.composition import (
    compose_workflow_runtime,
    get_workflow_service,
    reset_workflow_service_for_test,
)
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_input import ResolvedResourceSlot

WORKFLOW_UUID = "a0000000-0000-4000-8000-000000000001"
MATERIAL_UUID = "a1000000-0000-4000-8000-000000000001"


def _assert_invalid_input(response: Any) -> None:
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_input"


def test_backend_shaped_template_list_filters_paginates_and_omits_null_icon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "package"
    _write_package(workspace)
    registry = _register(
        compile_package_source(WorkspaceSource(workspace)),
        monkeypatch,
    )
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        catalog = TemplateCatalog(store)
        imports = copy.deepcopy(list(_imports(_registry_snapshot(registry))))
        measure_import = next(
            item for item in imports if item.template["name"] == "measure"
        )
        measure_import.template["icon"] = "activity"
        snapshot = catalog.replace(AUTHORITY, imports)
        client = _http_client(catalog)

        first_page = client.get(
            "/api/v1/workflow-node-templates",
            params={"page": 1, "page_size": 2},
        )
        assert first_page.status_code == 200
        first_data = first_page.json()["data"]
        assert first_data["page"] == 1
        assert first_data["page_size"] == 2
        assert first_data["total"] == len(snapshot.node_templates)
        assert len(first_data["items"]) == 2

        second_data = client.get(
            "/api/v1/workflow-node-templates",
            params={"page": 2, "page_size": 2},
        ).json()["data"]
        assert second_data["page"] == 2
        assert second_data["page_size"] == 2
        assert len(second_data["items"]) == 2
        assert {item["uuid"] for item in first_data["items"]}.isdisjoint(
            item["uuid"] for item in second_data["items"]
        )

        measure = next(
            item for item in snapshot.node_templates if item["name"] == "measure"
        )
        filters = {
            "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
            "name": "  MEAS  ",
            "type": measure["type"],
            "node_type": measure["node_type"],
        }
        filtered = client.get(
            "/api/v1/workflow-node-templates",
            params=filters,
        )
        assert filtered.status_code == 200
        filtered_data = filtered.json()["data"]
        assert filtered_data["total"] == 1
        assert filtered_data["items"] == [
            {
                "uuid": measure["uuid"],
                "name": "measure",
                "display_name": measure["display_name"],
                "type": measure["type"],
                "node_type": measure["node_type"],
                "icon": "activity",
                "resource_template": {
                    "uuid": RESOURCE_TEMPLATE_UUID,
                    "name": "community.a1_contract_lab.pump",
                    "display_name": "A1 泵",
                },
            }
        ]

        all_items = client.get(
            "/api/v1/workflow-node-templates",
            params={"page_size": 100},
        ).json()["data"]["items"]
        assert all(
            "icon" not in item for item in all_items if item["name"] != "measure"
        )
    finally:
        store.close()


def test_template_http_rejects_malformed_query_and_path_identities_with_400(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        catalog = TemplateCatalog(store)
        catalog.replace(AUTHORITY, [])
        client = _http_client(catalog)

        for params in (
            {"page": "not-an-int"},
            {"page_size": "not-an-int"},
            {"resource_template_uuid": "not-a-uuid"},
        ):
            _assert_invalid_input(
                client.get("/api/v1/workflow-node-templates", params=params)
            )
        for path in (
            "/api/v1/workflow-node-templates/not-a-uuid",
            "/api/v1/workflow-node-templates/not-a-uuid/handles",
            "/api/v1/workflow-handle-templates/not-a-uuid",
        ):
            _assert_invalid_input(client.get(path))
    finally:
        store.close()


class _KnownMaterial:
    def resolve(
        self,
        *,
        material_uuid: str,
        allowed_resource_template_uuids: tuple[str, ...] | None,
    ) -> ResolvedResourceSlot:
        assert material_uuid == MATERIAL_UUID
        assert allowed_resource_template_uuids == (RESOURCE_TEMPLATE_UUID,)
        return ResolvedResourceSlot(material_uuid, RESOURCE_TEMPLATE_UUID)


def _add_report_consumer(workspace: Path) -> None:
    device_path = workspace / "a1_contract_lab" / "device.py"
    source = device_path.read_text(encoding="utf-8")
    marker = '    def health(self) -> str:\n        return "ok"'
    assert marker in source
    device_path.write_text(
        source.replace(
            marker,
            '    @action(description="required named result consumer")\n'
            "    def archive(self, report: str) -> None:\n"
            "        return None\n\n" + marker,
        ),
        encoding="utf-8",
    )


def test_named_and_implicit_results_remain_business_edges_through_task_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "package"
    _write_package(workspace)
    _add_report_consumer(workspace)
    registry = _register(
        compile_package_source(WorkspaceSource(workspace)),
        monkeypatch,
    )
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        catalog = TemplateCatalog(store)
        catalog.replace(AUTHORITY, _imports(_registry_snapshot(registry)))
        engine = WorkflowAuthoringEngine(catalog=catalog, authority=AUTHORITY)
        service = WorkflowService(
            store,
            compiler=engine,
            resource_resolver=_KnownMaterial(),
        )
        applied = service.create_workflow(
            workflow_uuid=WORKFLOW_UUID,
            name="A1 result flow",
            tags=[],
            description=None,
            meta_data={},
        )
        assert applied["revision"] == 1
        compiled = engine.compile(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=1,
            source_uri="package://a1_contract_lab/workflows/results.py",
            applied_graph=service.get_graph(WORKFLOW_UUID),
            python_source=f'''from a1_contract_lab.device import Pump
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, workflow_definition

pump: Pump = device()

@workflow_definition(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="A1 result flow",
    description="Named and implicit result flow",
)
def result_flow(*, sample: ResourceSlot):
    # unilab:node_uuid=a2000000-0000-4000-8000-000000000001
    transferred = pump.transfer(sample=sample)
    # unilab:node_uuid=a2000000-0000-4000-8000-000000000002
    archived = pump.archive(report=transferred.report)
    # unilab:node_uuid=a2000000-0000-4000-8000-000000000003
    held = pump.consume(sample=transferred.sample)
    # unilab:node_uuid=a2000000-0000-4000-8000-000000000004
    finished = pump.consume(sample=held.sample)
''',
        )
        assert compiled.valid, compiled.diagnostics
        assert compiled.graph is not None
        graph = service.save_graph(
            WORKFLOW_UUID,
            revision=1,
            nodes=compiled.graph["nodes"],
            edges=compiled.graph["edges"],
        )
        task = service.create_workflow_task(
            workflow_uuid=WORKFLOW_UUID,
            run_mode="normal",
            target_node_uuid=None,
            input_value={"sample": {"uuid": MATERIAL_UUID}},
            description=None,
            meta_data={},
        )

        business_edges = [
            edge
            for edge in task["execution_plan"]["edges"]
            if edge["source_data_key"] in {"report", "sample"}
        ]
        assert len(business_edges) == 3
        assert all(edge.get("dependency_only") is not True for edge in business_edges)
        assert len(service.list_workflow_node_jobs(task["uuid"])) == 4
        assert len(graph["edges"]) >= len(business_edges)
    finally:
        store.close()


def _identity_rows(store: WorkflowStore) -> list[tuple[str, str]]:
    with store.transaction() as connection:
        return [
            (str(row["source_identity"]), str(row["resource_template_uuid"]))
            for row in connection.execute(
                """
                SELECT source_identity, resource_template_uuid
                FROM workflow_resource_template_identity
                ORDER BY source_identity
                """
            ).fetchall()
        ]


@pytest.mark.parametrize("missing_kind", ["stale", "unknown"])
def test_production_registry_identity_resolution_fails_closed_before_http_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_kind: str,
) -> None:
    workspace = tmp_path / "package"
    _write_package(workspace)
    registry = _register(
        compile_package_source(WorkspaceSource(workspace)),
        monkeypatch,
    )
    device_snapshot = copy.deepcopy(registry.device_type_registry)
    resource_snapshot = copy.deepcopy(registry.resource_type_registry)
    working_dir = tmp_path / "unilabos_data"

    reset_workflow_service_for_test()
    try:
        compose_workflow_runtime(
            working_dir,
            authority=AUTHORITY,
            registry_snapshot=MappingProxyType(copy.deepcopy(device_snapshot)),
            resource_registry_snapshot=MappingProxyType(
                copy.deepcopy(resource_snapshot)
            ),
        )
        reset_workflow_service_for_test()
        reader = WorkflowStore(working_dir / "workflow.db")
        try:
            before_catalog = _catalog_state(TemplateCatalog(reader))
            before_identities = _identity_rows(reader)
        finally:
            reader.close()

        broken_devices = copy.deepcopy(device_snapshot)
        broken_resources = copy.deepcopy(resource_snapshot)
        if missing_kind == "stale":
            broken_resources.clear()
        else:
            extension = broken_devices["community.a1_contract_lab.pump"]["class"][
                "action_value_mappings"
            ]["transfer"]["schema"]["x-unilabos-action-contract"]
            extension["resource_template_symbols"]["goal"]["sample"] = [
                "a1_contract_lab.resources:unknown_plate"
            ]

        projection_error = importlib.import_module(
            "unilabos.registry.catalog_consumer"
        ).RegistryTemplateProjectionError
        with pytest.raises(projection_error) as failure:
            compose_workflow_runtime(
                working_dir,
                authority=AUTHORITY,
                registry_snapshot=MappingProxyType(broken_devices),
                resource_registry_snapshot=MappingProxyType(broken_resources),
            )
        assert failure.value.code == "template_catalog_mismatch"
        assert get_workflow_service() is None

        reader = WorkflowStore(working_dir / "workflow.db")
        try:
            assert _catalog_state(TemplateCatalog(reader)) == before_catalog
            assert _identity_rows(reader) == before_identities
        finally:
            reader.close()

        from unilabos.config.config import BasicConfig

        monkeypatch.setattr(BasicConfig, "working_dir", str(working_dir))
        monkeypatch.setattr(BasicConfig, "workflow_graph_authority", AUTHORITY)
        monkeypatch.setattr(BasicConfig, "workflow_editable_package_roots", ())
        server = importlib.reload(importlib.import_module("unilabos.app.web.server"))
        client = TestClient(
            server.setup_server(
                registry_snapshot=MappingProxyType(broken_devices),
                resource_registry_snapshot=MappingProxyType(broken_resources),
            )
        )
        assert client.get("/api/v1/workflow-node-templates").status_code == 404
    finally:
        reset_workflow_service_for_test()
