"""A1 Registry snapshot -> TemplateCatalog -> HTTP 的原子公开链合同。"""

from __future__ import annotations

import copy
import importlib
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.package_manager.test_a1_canonical_action_catalog import (
    RESOURCE_TEMPLATE_UUID,
    _register,
    _write_package,
)
from unilabos.package_manager import WorkspaceSource, compile_package_source
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.catalog import (
    CatalogAuthority,
    TemplateCatalog,
)
from unilabos.workflow.composition import (
    compose_workflow_runtime,
    get_workflow_service,
    reset_workflow_service_for_test,
)
from unilabos.workflow.store import WorkflowStore

AUTHORITY = CatalogAuthority(authority_id="os-local", kind="local")


def _public(module_name: str, member: str) -> Any:
    module = importlib.import_module(module_name)
    if not hasattr(module, member):
        pytest.fail(
            f"A1 缺少公共 Interface: {module_name}.{member}",
            pytrace=False,
        )
    return getattr(module, member)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _registry_snapshot(registry: Any) -> Mapping[str, Any]:
    return MappingProxyType(copy.deepcopy(registry.device_type_registry))


def _imports(snapshot: Mapping[str, Any]) -> tuple[Any, ...]:
    adapter = _public(
        "unilabos.registry.catalog_consumer",
        "workflow_template_imports_from_registry_snapshot",
    )
    return tuple(
        adapter(
            snapshot,
            authority_id=AUTHORITY.authority_id,
            resource_template_identity_resolver=lambda source_identity: {
                "community.a1_contract_lab.pump": RESOURCE_TEMPLATE_UUID,
                "a1_contract_lab.resources:plate_96": RESOURCE_TEMPLATE_UUID,
            }[source_identity],
        )
    )


def _http_client(catalog: TemplateCatalog) -> TestClient:
    create_router = _public(
        "unilabos.app.workflow_api",
        "create_workflow_template_catalog_router",
    )
    app = FastAPI()
    app.include_router(create_router(catalog, AUTHORITY))
    return TestClient(app)


def _catalog_state(catalog: TemplateCatalog) -> dict[str, Any]:
    with catalog.snapshot(AUTHORITY) as snapshot:
        return {
            "fingerprint": snapshot.fingerprint,
            "nodes": [_plain(item) for item in snapshot.node_templates],
            "handles": [_plain(item) for item in snapshot.handle_templates],
        }


def test_registry_projection_publishes_one_complete_stable_catalog_and_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "package"
    _write_package(workspace)
    package_catalog = compile_package_source(WorkspaceSource(workspace))
    registry = _register(package_catalog, monkeypatch)

    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        catalog = TemplateCatalog(store)
        imports = _imports(_registry_snapshot(registry))
        first = catalog.replace(AUTHORITY, imports)
        second = catalog.replace(AUTHORITY, imports)

        assert first.fingerprint == second.fingerprint
        assert [item["uuid"] for item in first.node_templates] == [
            item["uuid"] for item in second.node_templates
        ]
        assert [item["uuid"] for item in first.handle_templates] == [
            item["uuid"] for item in second.handle_templates
        ]

        transfer = next(
            item for item in first.node_templates if item["name"] == "transfer"
        )
        transfer_handles = [
            item
            for item in first.handle_templates
            if item["workflow_node_template_uuid"] == transfer["uuid"]
        ]
        assert transfer["resource_template_uuid"] == RESOURCE_TEMPLATE_UUID
        assert transfer["schema"]["x-unilabos-action-contract"]["version"] == 1
        sample_target = next(
            item
            for item in transfer_handles
            if item["handle_key"] == "sample" and item["io_type"] == "target"
        )
        assert sample_target["required"] is True
        assert sample_target["type"] == "ResourceSlot"
        assert _plain(sample_target["meta_data"]["unilab"]) == {
            "value_schema": {"$slot": "ResourceSlot"},
            "editor_control": "material_port",
            "allowed_resource_template_uuids": [RESOURCE_TEMPLATE_UUID],
            "implicit_passthrough": False,
        }
        mode_target = next(
            item
            for item in transfer_handles
            if item["handle_key"] == "mode" and item["io_type"] == "target"
        )
        assert _plain(mode_target["meta_data"]["unilab"]) == {
            "value_schema": {
                "type": "string",
                "enum": ["safe", "fast"],
                "default": "safe",
            },
            "editor_control": "variable_selector",
            "allowed_resource_template_uuids": None,
            "implicit_passthrough": False,
        }

        select_target = next(
            item for item in first.node_templates if item["name"] == "select_target"
        )
        device_target = next(
            item
            for item in first.handle_templates
            if item["workflow_node_template_uuid"] == select_target["uuid"]
            and item["handle_key"] == "target"
            and item["io_type"] == "target"
        )
        assert device_target["meta_data"]["unilab"]["editor_control"] == (
            "site_selector"
        )

        consume = next(
            item for item in first.node_templates if item["name"] == "consume"
        )
        implicit = next(
            item
            for item in first.handle_templates
            if item["workflow_node_template_uuid"] == consume["uuid"]
            and item["handle_key"] == "sample"
            and item["io_type"] == "source"
        )
        assert implicit["meta_data"]["unilab"]["implicit_passthrough"] is True
        assert implicit["data_source"] == "result"
        assert implicit["data_key"] == "sample"
    finally:
        store.close()


def test_contract_change_updates_fingerprint_but_preserves_business_uuids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "package"
    _write_package(workspace)
    first_registry = _register(
        compile_package_source(WorkspaceSource(workspace)),
        monkeypatch,
    )
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        catalog = TemplateCatalog(store)
        first = catalog.replace(AUTHORITY, _imports(_registry_snapshot(first_registry)))

        source_path = workspace / "a1_contract_lab" / "device.py"
        changed = source_path.read_text(encoding="utf-8").replace(
            "= 1.25,",
            "= 2.5,",
        )
        source_path.write_text(changed, encoding="utf-8")
        second_registry = _register(
            compile_package_source(WorkspaceSource(workspace)),
            monkeypatch,
        )
        second = catalog.replace(
            AUTHORITY,
            _imports(_registry_snapshot(second_registry)),
        )

        assert second.fingerprint != first.fingerprint
        first_node_ids = {item["name"]: item["uuid"] for item in first.node_templates}
        second_node_ids = {item["name"]: item["uuid"] for item in second.node_templates}
        assert second_node_ids == first_node_ids
        first_handle_ids = {
            (
                item["workflow_node_template_uuid"],
                item["handle_key"],
                item["io_type"],
            ): item["uuid"]
            for item in first.handle_templates
        }
        second_handle_ids = {
            (
                item["workflow_node_template_uuid"],
                item["handle_key"],
                item["io_type"],
            ): item["uuid"]
            for item in second.handle_templates
        }
        assert second_handle_ids == first_handle_ids
    finally:
        store.close()


def test_invalid_action_projection_preserves_previous_complete_snapshot(
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
        before = _catalog_state(catalog)

        broken = copy.deepcopy(registry.device_type_registry)
        transfer = broken["community.a1_contract_lab.pump"]["class"][
            "action_value_mappings"
        ]["transfer"]
        transfer["schema"]["x-unilabos-action-contract"]["version"] = 2
        projection_error = _public(
            "unilabos.registry.catalog_consumer",
            "RegistryTemplateProjectionError",
        )
        with pytest.raises(projection_error) as caught:
            _imports(MappingProxyType(broken))

        assert caught.value.code == "invalid_action_contract"
        assert caught.value.path.endswith(
            "/actions/transfer/schema/x-unilabos-action-contract/version"
        )
        assert _catalog_state(catalog) == before
    finally:
        store.close()


def test_backend_shaped_template_http_reads_the_persisted_compiler_snapshot(
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
        snapshot = catalog.replace(AUTHORITY, _imports(_registry_snapshot(registry)))
        engine = WorkflowAuthoringEngine(catalog=catalog, authority=AUTHORITY)
        client = _http_client(catalog)

        listed = client.get("/api/v1/workflow-node-templates")
        assert listed.status_code == 200
        data = listed.json()["data"]
        assert listed.json()["code"] == 0
        assert data["authority"] == {
            "authority_id": AUTHORITY.authority_id,
            "kind": AUTHORITY.kind,
        }
        assert data["catalog_fingerprint"] == snapshot.fingerprint
        assert data["catalog_fingerprint"] == engine.template_catalog_fingerprint
        assert data["total"] == len(snapshot.node_templates)
        assert {item["uuid"] for item in data["items"]} == {
            item["uuid"] for item in snapshot.node_templates
        }

        transfer = next(
            item for item in snapshot.node_templates if item["name"] == "transfer"
        )
        transfer_summary = next(
            item for item in data["items"] if item["uuid"] == transfer["uuid"]
        )
        assert transfer_summary == {
            "uuid": transfer["uuid"],
            "name": "transfer",
            "display_name": transfer["display_name"],
            "type": transfer["type"],
            "node_type": transfer["node_type"],
            "resource_template": {
                "uuid": RESOURCE_TEMPLATE_UUID,
                "name": "community.a1_contract_lab.pump",
                "display_name": "A1 泵",
            },
        }
        detail = client.get(f"/api/v1/workflow-node-templates/{transfer['uuid']}")
        assert detail.status_code == 200
        detail_data = detail.json()["data"]
        assert detail_data["catalog_fingerprint"] == snapshot.fingerprint
        assert detail_data["template"]["uuid"] == transfer["uuid"]
        assert detail_data["handles"]
        assert all(
            item["workflow_node_template_uuid"] == transfer["uuid"]
            for item in detail_data["handles"]
        )

        handles = client.get(
            f"/api/v1/workflow-node-templates/{transfer['uuid']}/handles"
        )
        assert handles.status_code == 200
        handle_data = handles.json()["data"]
        assert handle_data["catalog_fingerprint"] == snapshot.fingerprint
        assert handle_data["items"] == detail_data["handles"]

        handle_uuid = detail_data["handles"][0]["uuid"]
        handle = client.get(f"/api/v1/workflow-handle-templates/{handle_uuid}")
        assert handle.status_code == 200
        assert handle.json()["data"] == {
            "authority": data["authority"],
            "catalog_fingerprint": snapshot.fingerprint,
            "handle": detail_data["handles"][0],
        }

        registry.device_type_registry["community.a1_contract_lab.pump"]["class"][
            "action_value_mappings"
        ]["transfer"]["busy"] = True
        after_live_mutation = client.get("/api/v1/workflow-node-templates").json()[
            "data"
        ]
        assert after_live_mutation == data
    finally:
        store.close()


def test_production_composition_publishes_catalog_before_authoring_is_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "package"
    _write_package(workspace)
    registry = _register(
        compile_package_source(WorkspaceSource(workspace)),
        monkeypatch,
    )
    working_dir = tmp_path / "unilabos_data"
    reset_workflow_service_for_test()
    try:
        service = compose_workflow_runtime(
            working_dir,
            authority=AUTHORITY,
            registry_snapshot=_registry_snapshot(registry),
            resource_template_identity_resolver=lambda source_identity: {
                "community.a1_contract_lab.pump": RESOURCE_TEMPLATE_UUID,
                "a1_contract_lab.resources:plate_96": RESOURCE_TEMPLATE_UUID,
            }[source_identity],
        )

        assert get_workflow_service() is service
        assert isinstance(service.compiler, WorkflowAuthoringEngine)
        assert service.compiler.template_catalog_fingerprint.startswith("sha256:")
        reader = WorkflowStore(working_dir / "workflow.db")
        try:
            state = _catalog_state(TemplateCatalog(reader))
        finally:
            reader.close()
        assert state["fingerprint"] == service.compiler.template_catalog_fingerprint
        assert {item["name"] for item in state["nodes"]} >= {
            "transfer",
            "measure",
        }
    finally:
        reset_workflow_service_for_test()


def test_failed_production_projection_keeps_old_catalog_and_never_becomes_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "package"
    _write_package(workspace)
    registry = _register(
        compile_package_source(WorkspaceSource(workspace)),
        monkeypatch,
    )
    working_dir = tmp_path / "unilabos_data"
    seed_store = WorkflowStore(working_dir / "workflow.db")
    try:
        seed_catalog = TemplateCatalog(seed_store)
        seed_catalog.replace(AUTHORITY, _imports(_registry_snapshot(registry)))
        before = _catalog_state(seed_catalog)
    finally:
        seed_store.close()

    broken = copy.deepcopy(registry.device_type_registry)
    broken["community.a1_contract_lab.pump"]["class"]["action_value_mappings"][
        "transfer"
    ]["schema"]["x-unilabos-action-contract"]["version"] = 2
    projection_error = _public(
        "unilabos.registry.catalog_consumer",
        "RegistryTemplateProjectionError",
    )

    reset_workflow_service_for_test()
    try:
        with pytest.raises(projection_error):
            compose_workflow_runtime(
                working_dir,
                authority=AUTHORITY,
                registry_snapshot=MappingProxyType(broken),
                resource_template_identity_resolver=lambda source_identity: {
                    "community.a1_contract_lab.pump": RESOURCE_TEMPLATE_UUID,
                    "a1_contract_lab.resources:plate_96": RESOURCE_TEMPLATE_UUID,
                }[source_identity],
            )
        assert get_workflow_service() is None

        reader = WorkflowStore(working_dir / "workflow.db")
        try:
            assert _catalog_state(TemplateCatalog(reader)) == before
        finally:
            reader.close()
    finally:
        reset_workflow_service_for_test()


def test_template_http_missing_identity_uses_backend_error_envelope(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        catalog = TemplateCatalog(store)
        catalog.replace(AUTHORITY, [])
        client = _http_client(catalog)
        unknown = "40000000-0000-4000-8000-000000000001"

        for path in (
            f"/api/v1/workflow-node-templates/{unknown}",
            f"/api/v1/workflow-node-templates/{unknown}/handles",
            f"/api/v1/workflow-handle-templates/{unknown}",
        ):
            response = client.get(path)
            assert response.status_code == 404
            assert response.json() == {
                "code": 404,
                "error": {
                    "code": "not_found",
                    "message": "资源不存在",
                },
            }
    finally:
        store.close()
