"""A1 reviewer round-2 regressions for backend parity and ownership seams."""

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
from tests.workflow.test_authoring_engine import (
    ANALYZE_NODE_UUID,
    FINAL_REPORT_TARGET,
    FINAL_TEMPLATE_UUID,
    PREPARE_NODE_UUID,
    _catalog_imports,
    _source,
)
from tests.workflow.test_authoring_engine import (
    AUTHORITY as ENGINE_AUTHORITY,
)
from tests.workflow.test_authoring_engine import (
    RESOURCE_TEMPLATE_UUID as ENGINE_RESOURCE_TEMPLATE_UUID,
)
from tests.workflow.test_authoring_engine import (
    WORKFLOW_UUID as ENGINE_WORKFLOW_UUID,
)
from unilabos.package_manager import WorkspaceSource, compile_package_source
from unilabos.registry.catalog_consumer import RegistryTemplateProjectionError
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.catalog import TemplateCatalog
from unilabos.workflow.composition import (
    compose_workflow_runtime,
    get_workflow_service,
    reset_workflow_service_for_test,
)
from unilabos.workflow.models import WorkflowNodeWrite
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_input import ResolvedResourceSlot

WORKFLOW_UUID = "b0000000-0000-4000-8000-000000000001"
NODE_UUID = "b1000000-0000-4000-8000-000000000001"
MATERIAL_UUID = "b2000000-0000-4000-8000-000000000001"
UNKNOWN_RESOURCE_TEMPLATE_UUID = "b3000000-0000-4000-8000-000000000001"
NIL_UUID = "00000000-0000-0000-0000-000000000000"


def _assert_invalid_input(response: Any) -> None:
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_input"


def test_template_http_matches_backend_optional_filters_bounds_and_ordering(
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
        catalog.replace(AUTHORITY, _imports(_registry_snapshot(registry)))
        with catalog.snapshot(AUTHORITY) as snapshot:
            template_uuids = sorted(item["uuid"] for item in snapshot.node_templates)
        with store.transaction() as connection:
            for index, template_uuid in enumerate(template_uuids):
                create_time = (
                    "2026-08-02T00:00:01Z" if index < 2 else "2026-08-02T00:00:00Z"
                )
                connection.execute(
                    "UPDATE workflow_node_template SET create_time = ? WHERE uuid = ?",
                    (create_time, template_uuid),
                )
        with catalog.snapshot(AUTHORITY) as snapshot:
            expected_order = [
                item["uuid"]
                for item in sorted(
                    snapshot.node_templates,
                    key=lambda item: (item["create_time"], item["uuid"]),
                    reverse=True,
                )
            ]
            measure = next(
                item for item in snapshot.node_templates if item["name"] == "measure"
            )
        client = _http_client(catalog)
        endpoint = "/api/v1/workflow-node-templates"

        absent = client.get(endpoint, params={"page_size": 100})
        empty_uuid = client.get(
            endpoint,
            params={"page_size": 100, "resource_template_uuid": ""},
        )
        assert absent.status_code == empty_uuid.status_code == 200
        assert empty_uuid.json()["data"] == absent.json()["data"]
        assert [item["uuid"] for item in absent.json()["data"]["items"]] == (
            expected_order
        )

        for params in (
            {"resource_template_uuid": RESOURCE_TEMPLATE_UUID},
            {"name": "  MEAS  "},
            {"type": f"  {measure['type']}  "},
            {"node_type": f"  {measure['node_type']}  "},
        ):
            response = client.get(endpoint, params=params)
            assert response.status_code == 200
            assert response.json()["data"]["total"] > 0

        assert (
            client.get(
                endpoint,
                params={"resource_template_uuid": UNKNOWN_RESOURCE_TEMPLATE_UUID},
            ).json()["data"]["total"]
            == 0
        )
        for name in ("name", "type", "node_type"):
            whitespace = client.get(endpoint, params={name: "   ", "page_size": 100})
            assert whitespace.status_code == 200
            assert whitespace.json()["data"] == absent.json()["data"]

        for params in (
            {"page": 0, "page_size": 0},
            {"page": -7, "page_size": -9},
        ):
            normalized = client.get(endpoint, params=params)
            assert normalized.status_code == 200
            assert normalized.json()["data"]["page"] == 1
            assert normalized.json()["data"]["page_size"] == 20
        capped = client.get(endpoint, params={"page_size": 101})
        assert capped.status_code == 200
        assert capped.json()["data"]["page_size"] == 100

        too_large = str(2**100)
        _assert_invalid_input(client.get(endpoint, params={"page": too_large}))
        _assert_invalid_input(client.get(endpoint, params={"page_size": too_large}))
    finally:
        store.close()


def test_template_http_rejects_malformed_and_nil_uuid_filters_and_paths(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        catalog = TemplateCatalog(store)
        catalog.replace(AUTHORITY, [])
        client = _http_client(catalog)
        endpoint = "/api/v1/workflow-node-templates"

        for value in ("not-a-uuid", " ", NIL_UUID):
            _assert_invalid_input(
                client.get(endpoint, params={"resource_template_uuid": value})
            )
        for identity in ("not-a-uuid", NIL_UUID):
            for path in (
                f"/api/v1/workflow-node-templates/{identity}",
                f"/api/v1/workflow-node-templates/{identity}/handles",
                f"/api/v1/workflow-handle-templates/{identity}",
            ):
                _assert_invalid_input(client.get(path))
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


def _seed_production_catalog(
    working_dir: Path,
    *,
    device_snapshot: dict[str, Any],
    resource_snapshot: dict[str, Any],
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    compose_workflow_runtime(
        working_dir,
        authority=AUTHORITY,
        registry_snapshot=MappingProxyType(copy.deepcopy(device_snapshot)),
        resource_registry_snapshot=MappingProxyType(copy.deepcopy(resource_snapshot)),
    )
    reset_workflow_service_for_test()
    reader = WorkflowStore(working_dir / "workflow.db")
    try:
        return _catalog_state(TemplateCatalog(reader)), _identity_rows(reader)
    finally:
        reader.close()


def test_explicit_device_snapshot_without_resource_snapshot_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "package"
    _write_package(workspace)
    registry = _register(
        compile_package_source(WorkspaceSource(workspace)),
        monkeypatch,
    )
    devices = copy.deepcopy(registry.device_type_registry)
    resources = copy.deepcopy(registry.resource_type_registry)
    working_dir = tmp_path / "unilabos_data"

    reset_workflow_service_for_test()
    try:
        before_catalog, before_identities = _seed_production_catalog(
            working_dir,
            device_snapshot=devices,
            resource_snapshot=resources,
        )
        with pytest.raises(RegistryTemplateProjectionError) as failure:
            compose_workflow_runtime(
                working_dir,
                authority=AUTHORITY,
                registry_snapshot=MappingProxyType(copy.deepcopy(devices)),
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
                registry_snapshot=MappingProxyType(copy.deepcopy(devices)),
            )
        )
        assert client.get("/api/v1/workflow-node-templates").status_code == 404
        assert get_workflow_service() is None

        reader = WorkflowStore(working_dir / "workflow.db")
        try:
            assert _catalog_state(TemplateCatalog(reader)) == before_catalog
            assert _identity_rows(reader) == before_identities
        finally:
            reader.close()
    finally:
        reset_workflow_service_for_test()


def test_explicit_strict_resource_identity_resolver_remains_compatible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "package"
    _write_package(workspace)
    registry = _register(
        compile_package_source(WorkspaceSource(workspace)),
        monkeypatch,
    )
    reset_workflow_service_for_test()
    try:
        service = compose_workflow_runtime(
            tmp_path / "unilabos_data",
            authority=AUTHORITY,
            registry_snapshot=_registry_snapshot(registry),
            resource_template_identity_resolver=lambda source_identity: {
                "community.a1_contract_lab.pump": RESOURCE_TEMPLATE_UUID,
                "a1_contract_lab.resources:plate_96": RESOURCE_TEMPLATE_UUID,
            }[source_identity],
        )
        assert get_workflow_service() is service
        assert service.compiler.template_catalog_fingerprint.startswith("sha256:")
    finally:
        reset_workflow_service_for_test()


def _ordinary_service(
    tmp_path: Path,
    *,
    input_contract: dict[str, Any] | None = None,
) -> tuple[WorkflowStore, WorkflowService]:
    store = WorkflowStore(tmp_path / "workflow.db")
    catalog = TemplateCatalog(store)
    catalog.replace(ENGINE_AUTHORITY, _catalog_imports())
    meta_data = (
        {}
        if input_contract is None
        else {"unilab": {"input_contract": copy.deepcopy(input_contract)}}
    )
    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="Ordinary save",
        tags=[],
        description=None,
        meta_data=meta_data,
    )
    return store, WorkflowService(store)


def _final_node(*, meta_data: dict[str, Any]) -> WorkflowNodeWrite:
    return WorkflowNodeWrite(
        uuid=NODE_UUID,
        workflow_node_template_uuid=FINAL_TEMPLATE_UUID,
        name="finalize",
        status="idle",
        type="compute",
        param={},
        action_name="finalize",
        meta_data=meta_data,
    )


@pytest.mark.parametrize(
    "executor_binding",
    [
        {"mode": "fixed", "device_id": "reactor-1"},
        {"mode": "fixed"},
        {"mode": "fixed", "device_id": ""},
        {"mode": "automatic", "device_id": "reactor-1"},
        {"mode": "fixed", "device_id": "reactor-1", "extra": True},
        "reactor-1",
    ],
    ids=[
        "legal-fixed",
        "missing-device-id",
        "empty-device-id",
        "wrong-mode",
        "extra-field",
        "wrong-shape",
    ],
)
def test_ordinary_save_cannot_create_executor_binding(
    tmp_path: Path,
    executor_binding: Any,
) -> None:
    store, service = _ordinary_service(tmp_path)
    try:
        saved = service.save_graph(
            WORKFLOW_UUID,
            revision=1,
            nodes=[
                _final_node(
                    meta_data={
                        "caller": "preserved",
                        "unilab": {"executor_binding": executor_binding},
                    }
                )
            ],
            edges=[],
        )

        assert saved["nodes"][0]["meta_data"] == {"caller": "preserved"}
    finally:
        store.close()


def _input_contract(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "parameters": [
            {
                "name": name,
                "schema": schema,
                "required": True,
            }
        ],
    }


def test_ordinary_binding_cannot_invent_workflow_input_contract(
    tmp_path: Path,
) -> None:
    store, service = _ordinary_service(tmp_path)
    try:
        with pytest.raises(WorkflowError) as failure:
            service.save_graph(
                WORKFLOW_UUID,
                revision=1,
                nodes=[
                    _final_node(
                        meta_data={
                            "unilab": {
                                "input_bindings": {
                                    FINAL_REPORT_TARGET: {"parameter": "report"}
                                }
                            }
                        }
                    )
                ],
                edges=[],
            )
        assert failure.value.code == "invalid_input"
        graph = service.get_graph(WORKFLOW_UUID)
        assert graph["nodes"] == []
        assert "unilab" not in graph["workflow"]["meta_data"]
    finally:
        store.close()


def test_ordinary_binding_rejects_extra_fields_at_save(
    tmp_path: Path,
) -> None:
    store, service = _ordinary_service(
        tmp_path,
        input_contract=_input_contract("report", {"type": "string"}),
    )
    try:
        with pytest.raises(WorkflowError) as failure:
            service.save_graph(
                WORKFLOW_UUID,
                revision=1,
                nodes=[
                    _final_node(
                        meta_data={
                            "unilab": {
                                "input_bindings": {
                                    FINAL_REPORT_TARGET: {
                                        "parameter": "report",
                                        "extra": "not-owned",
                                    }
                                }
                            }
                        }
                    )
                ],
                edges=[],
            )
        assert failure.value.code == "invalid_input"
        assert service.get_graph(WORKFLOW_UUID)["nodes"] == []
    finally:
        store.close()


def test_ordinary_binding_rejects_contract_handle_type_mismatch_at_save(
    tmp_path: Path,
) -> None:
    store, service = _ordinary_service(
        tmp_path,
        input_contract=_input_contract("amount", {"type": "integer"}),
    )
    try:
        with pytest.raises(WorkflowError) as failure:
            service.save_graph(
                WORKFLOW_UUID,
                revision=1,
                nodes=[
                    _final_node(
                        meta_data={
                            "unilab": {
                                "input_bindings": {
                                    FINAL_REPORT_TARGET: {"parameter": "amount"}
                                }
                            }
                        }
                    )
                ],
                edges=[],
            )
        assert failure.value.code == "invalid_input"
        assert service.get_graph(WORKFLOW_UUID)["nodes"] == []
    finally:
        store.close()


class _EngineMaterialResolver:
    def resolve(
        self,
        *,
        material_uuid: str,
        allowed_resource_template_uuids: tuple[str, ...] | None,
    ) -> ResolvedResourceSlot:
        assert material_uuid == MATERIAL_UUID
        assert allowed_resource_template_uuids is None
        return ResolvedResourceSlot(material_uuid, ENGINE_RESOURCE_TEMPLATE_UUID)


def test_compiler_candidate_apply_keeps_fixed_selector_contract_and_task_defaults(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        catalog = TemplateCatalog(store)
        catalog.replace(ENGINE_AUTHORITY, _catalog_imports())
        engine = WorkflowAuthoringEngine(catalog=catalog, authority=ENGINE_AUTHORITY)
        service = WorkflowService(
            store,
            compiler=engine,
            resource_resolver=_EngineMaterialResolver(),
        )
        service.create_workflow(
            workflow_uuid=ENGINE_WORKFLOW_UUID,
            name="Candidate fixed selector",
            tags=[],
            description=None,
            meta_data={},
        )
        package_root = tmp_path / "package"
        package_root.mkdir()
        service.register_editable_source(
            workflow_uuid=ENGINE_WORKFLOW_UUID,
            package_id="lab",
            package_root=package_root,
            relative_path="workflows/sample.py",
        )
        draft = service.save_draft(
            ENGINE_WORKFLOW_UUID,
            python_source=_source(fixed_device_id="reactor-1"),
            expected_draft_hash=None,
            expected_workflow_revision=1,
        )
        candidate = draft["candidate"]
        assert candidate is not None
        if draft["draft"]["python_source"] != candidate["normalized_python_source"]:
            draft = service.save_draft(
                ENGINE_WORKFLOW_UUID,
                python_source=candidate["normalized_python_source"],
                expected_draft_hash=draft["draft"]["draft_hash"],
                expected_workflow_revision=1,
            )
            candidate = draft["candidate"]
            assert candidate is not None
        service.apply_authoring(
            ENGINE_WORKFLOW_UUID,
            candidate_hash=candidate["candidate_hash"],
        )
        graph = service.get_graph(ENGINE_WORKFLOW_UUID)

        parameters = graph["workflow"]["meta_data"]["unilab"]["input_contract"][
            "parameters"
        ]
        assert parameters == [
            {
                "name": "sample",
                "schema": {"$slot": "ResourceSlot"},
                "required": True,
            },
            {
                "name": "cycles",
                "schema": {"type": "integer", "minimum": 1, "maximum": 10},
                "required": False,
                "default": 3,
            },
            {
                "name": "mode",
                "schema": {"type": "string", "enum": ["fast", "safe"]},
                "required": False,
                "default": "safe",
            },
            {
                "name": "note",
                "schema": {
                    "anyOf": [
                        {"type": "string", "maxLength": 200},
                        {"type": "null"},
                    ]
                },
                "required": False,
                "default": None,
            },
        ]
        nodes = {node["uuid"]: node for node in graph["nodes"]}
        for node_uuid in (PREPARE_NODE_UUID, ANALYZE_NODE_UUID):
            assert nodes[node_uuid]["meta_data"]["unilab"]["executor_binding"] == {
                "mode": "fixed",
                "device_id": "reactor-1",
            }

        task = service.create_workflow_task(
            workflow_uuid=ENGINE_WORKFLOW_UUID,
            run_mode="normal",
            target_node_uuid=None,
            input_value={"sample": {"uuid": MATERIAL_UUID}},
            description=None,
            meta_data={},
        )
        assert task["input"] == {
            "sample": {
                "uuid": MATERIAL_UUID,
                "resource_template_uuid": ENGINE_RESOURCE_TEMPLATE_UUID,
            },
            "cycles": 3,
            "mode": "safe",
            "note": None,
        }
        jobs = {
            job["workflow_node_uuid"]: job
            for job in service.list_workflow_node_jobs(task["uuid"])
        }
        assert jobs[PREPARE_NODE_UUID]["param"]["cycles"] == 3
        assert jobs[PREPARE_NODE_UUID]["param"]["note"] is None
        assert jobs[ANALYZE_NODE_UUID]["param"]["label"] == "safe"
    finally:
        store.close()
