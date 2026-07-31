"""Round 02G production Authoring 组合根的公共 HTTP 合同。"""

from __future__ import annotations

import importlib
import warnings
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.workflow.test_authoring_engine import (
    WORKFLOW_UUID,
    _catalog_imports,
    _source,
)
from unilabos.config.config import BasicConfig
from unilabos.workflow import composition
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.catalog import CatalogAuthority, TemplateCatalog
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

LOCAL_AUTHORITY = CatalogAuthority(authority_id="production-local", kind="local")
BACKEND_AUTHORITY = CatalogAuthority(
    authority_id="production-backend",
    kind="backend",
)
AUTHORING_ROUTES = {
    "/api/v1/authoring/compile": "post",
    "/api/v1/authoring/generate-python": "post",
    "/api/v1/authoring/validate": "post",
    "/api/v1/workflows/{workflow_uuid}/authoring": "get",
    "/api/v1/workflows/{workflow_uuid}/authoring/draft": "put",
    "/api/v1/workflows/{workflow_uuid}/authoring/apply": "post",
    "/api/v1/events": "get",
}
_HTTP_METHODS = {"get", "put", "post", "delete", "patch", "options", "head"}


@pytest.fixture(autouse=True)
def _clean_production_composition() -> Any:
    composition.reset_workflow_service_for_test()
    try:
        yield
    finally:
        composition.reset_workflow_service_for_test()


def _write_declared_package(selected_root: Path, source: str) -> Path:
    package_root = selected_root / "production_lab"
    source_path = package_root / "workflows" / "demo.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(source, encoding="utf-8")
    (selected_root / "package.yaml").write_text(
        "\n".join(
            [
                "package:",
                "  name: production_lab",
                "",
                "workflows:",
                f"  - workflow_uuid: {WORKFLOW_UUID}",
                "    source: production_lab/workflows/demo.py",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return source_path


def _seed_production_authority(working_dir: Path) -> None:
    store = WorkflowStore(working_dir / "workflow.db")
    try:
        service = WorkflowService(store)
        service.create_workflow(
            name="Persisted workflow",
            tags=["keep"],
            description="Persisted description",
            meta_data={"owner": "keep"},
            workflow_uuid=WORKFLOW_UUID,
        )
        local_imports = _catalog_imports()
        for item in local_imports:
            item.template.pop("uuid")
            for handle in item.handles:
                handle.pop("uuid")
        TemplateCatalog(store).replace(LOCAL_AUTHORITY, local_imports)
    finally:
        store.close()


def _configure_production(
    monkeypatch: pytest.MonkeyPatch,
    *,
    working_dir: Path,
    authority: Any,
    roots: tuple[Path, ...],
) -> None:
    monkeypatch.setattr(BasicConfig, "working_dir", str(working_dir))
    monkeypatch.setattr(
        BasicConfig,
        "workflow_graph_authority",
        authority,
        raising=False,
    )
    monkeypatch.setattr(
        BasicConfig,
        "workflow_editable_package_roots",
        roots,
        raising=False,
    )


def _reload_server() -> Any:
    return importlib.reload(importlib.import_module("unilabos.app.web.server"))


def _assert_authoring_routes_absent(schema: dict[str, Any]) -> None:
    assert not set(AUTHORING_ROUTES) & set(schema["paths"])


def _assert_authoring_routes_unique(app: Any) -> None:
    with warnings.catch_warnings(record=True) as openapi_warnings:
        warnings.simplefilter("always")
        schema = app.openapi()
    operations = []
    for path, expected_method in AUTHORING_ROUTES.items():
        path_item = schema["paths"][path]
        assert set(path_item) & _HTTP_METHODS == {expected_method}
        operations.append(path_item[expected_method]["operationId"])
    assert len(operations) == 7
    assert len(set(operations)) == 7
    assert not [
        item
        for item in openapi_warnings
        if "Duplicate Operation ID" in str(item.message)
        and any(operation in str(item.message) for operation in operations)
    ]


@pytest.mark.parametrize(
    "authority",
    [
        None,
        {"authority_id": "production-local", "kind": "guessed-local"},
        BACKEND_AUTHORITY,
    ],
    ids=["missing", "invalid-kind", "backend"],
)
def test_production_authoring_is_not_mounted_without_valid_explicit_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority: Any,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    _configure_production(
        monkeypatch,
        working_dir=working_dir,
        authority=authority,
        roots=(),
    )
    server = _reload_server()

    schema = server.setup_server().openapi()

    _assert_authoring_routes_absent(schema)
    assert composition.get_workflow_service() is None


def test_direct_backend_authority_rejection_does_not_publish_or_retain_lease(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "unilabos_data"

    with pytest.raises((TypeError, ValueError, RuntimeError)):
        composition.compose_workflow_runtime(
            working_dir,
            authority=BACKEND_AUTHORITY,
        )
    assert composition.get_workflow_service() is None

    replacement = composition.compose_workflow_runtime(
        working_dir,
        authority=LOCAL_AUTHORITY,
    )
    assert replacement is composition.get_workflow_service()


def test_authoring_routes_install_atomically_and_retry_after_pure_router_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unilabos.app import workflow_api

    working_dir = tmp_path / "unilabos_data"
    _configure_production(
        monkeypatch,
        working_dir=working_dir,
        authority=LOCAL_AUTHORITY,
        roots=(),
    )
    original_create_pure_router = workflow_api.create_authoring_transform_router

    def fail_pure_router(_engine: Any) -> Any:
        raise RuntimeError("injected pure router construction failure")

    monkeypatch.setattr(
        workflow_api,
        "create_authoring_transform_router",
        fail_pure_router,
    )
    server = _reload_server()
    app = server.setup_server()
    _assert_authoring_routes_absent(app.openapi())

    monkeypatch.setattr(
        workflow_api,
        "create_authoring_transform_router",
        original_create_pure_router,
    )
    assert server.setup_server() is app
    _assert_authoring_routes_unique(app)


def test_real_server_uses_one_real_engine_for_all_authoring_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    selected_root = tmp_path / "editable"
    selected_root.mkdir()
    source_path = _write_declared_package(selected_root, _source())
    _seed_production_authority(working_dir)
    _configure_production(
        monkeypatch,
        working_dir=working_dir,
        authority=LOCAL_AUTHORITY,
        roots=(selected_root,),
    )
    server = _reload_server()
    app = server.setup_server()
    assert server.setup_server() is app

    _assert_authoring_routes_unique(app)

    service = composition.get_workflow_service()
    assert service is not None
    assert isinstance(service.compiler, WorkflowAuthoringEngine)
    engine = service.compiler

    with TestClient(app) as client:
        startup = client.get(f"/api/v1/workflows/{WORKFLOW_UUID}/authoring").json()[
            "data"
        ]
        assert startup["candidate"] is not None
        assert startup["candidate"]["compiler_version"] == engine.compiler_version
        assert (
            startup["candidate"]["template_catalog_fingerprint"]
            == engine.template_catalog_fingerprint
        )
        assert (
            client.get("/api/v1/events", headers={"Last-Event-ID": "-1"}).status_code
            == 400
        )

        pure = client.post(
            "/api/v1/authoring/compile",
            json={
                "workflow_uuid": WORKFLOW_UUID,
                "revision": startup["workflow_revision"],
                "source_uri": startup["draft"]["source_uri"],
                "python_source": startup["draft"]["python_source"],
                "applied_graph": startup["applied_graph"],
            },
        )
        assert pure.status_code == 200, pure.text
        pure_data = pure.json()["data"]
        assert pure_data["compiler_version"] == engine.compiler_version
        assert (
            pure_data["template_catalog_fingerprint"]
            == engine.template_catalog_fingerprint
        )

        normalized_source = startup["candidate"]["normalized_python_source"]
        saved_response = client.put(
            f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
            json={
                "python_source": normalized_source,
                "expected_draft_hash": startup["draft"]["draft_hash"],
                "expected_workflow_revision": startup["workflow_revision"],
            },
        )
        assert saved_response.status_code == 200, saved_response.text
        saved = saved_response.json()["data"]
        assert saved["candidate"] is not None
        assert saved["candidate"]["compiler_version"] == engine.compiler_version

        before_apply = source_path.read_bytes()
        applied_response = client.post(
            f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
            json={"candidate_hash": saved["candidate"]["candidate_hash"]},
        )
        assert applied_response.status_code == 200, applied_response.text
        applied = applied_response.json()["data"]
        assert applied["apply_result"]["warnings"] == []
        assert source_path.read_bytes() == before_apply
        assert client.get("/api/v1/workflow-tasks").json()["data"]["items"] == []

        externally_edited = source_path.read_text(encoding="utf-8") + "\n# external\n"
        source_path.write_text(externally_edited, encoding="utf-8")
        reconciled = service.reconcile_registered_source(WORKFLOW_UUID)
        assert reconciled["draft"]["python_source"] == externally_edited
        assert reconciled["candidate"] is not None
        assert reconciled["candidate"]["compiler_version"] == engine.compiler_version

    assert composition.get_workflow_service() is service
    assert service.compiler is engine


def test_persistent_candidate_round_trips_through_production_pure_generate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    selected_root = tmp_path / "editable"
    selected_root.mkdir()
    _write_declared_package(selected_root, _source().replace("= 3,", "= 4,"))
    _seed_production_authority(working_dir)
    _configure_production(
        monkeypatch,
        working_dir=working_dir,
        authority=LOCAL_AUTHORITY,
        roots=(selected_root,),
    )
    app = _reload_server().setup_server()

    with TestClient(app) as client:
        aggregate_response = client.get(f"/api/v1/workflows/{WORKFLOW_UUID}/authoring")
        assert aggregate_response.status_code == 200, aggregate_response.text
        aggregate = aggregate_response.json()["data"]
        assert aggregate["candidate"] is not None

        candidate_graph = aggregate["candidate"]["graph"]
        template_uuids = {item["uuid"] for item in candidate_graph["node_templates"]}
        handle_uuids = {item["uuid"] for item in candidate_graph["handle_templates"]}
        caller_template_uuids = {item.template["uuid"] for item in _catalog_imports()}
        caller_handle_uuids = {
            handle["uuid"] for item in _catalog_imports() for handle in item.handles
        }
        assert template_uuids.isdisjoint(caller_template_uuids)
        assert handle_uuids.isdisjoint(caller_handle_uuids)
        assert all(
            edge["source_handle_uuid"] in handle_uuids
            and edge["target_handle_uuid"] in handle_uuids
            for edge in candidate_graph["edges"]
        )
        unilab = candidate_graph["workflow"]["meta_data"]["unilab"]
        assert all(
            binding["source_handle_uuid"] in handle_uuids
            for binding in unilab["output_bindings"].values()
            if binding["kind"] == "node_output"
        )
        assert all(
            handle_uuid in handle_uuids
            for node in candidate_graph["nodes"]
            for handle_uuid in node["meta_data"]["unilab"]["input_bindings"]
        )
        candidate_cycles = next(
            item
            for item in unilab["input_contract"]["parameters"]
            if item["name"] == "cycles"
        )
        assert candidate_cycles["default"] == 4
        generated_response = client.post(
            "/api/v1/authoring/generate-python",
            json={
                "workflow_uuid": WORKFLOW_UUID,
                "revision": aggregate["workflow_revision"],
                "source_uri": aggregate["draft"]["source_uri"],
                "graph": candidate_graph,
            },
        )

    assert generated_response.status_code == 200, generated_response.text
    generated = generated_response.json()["data"]
    assert generated["diagnostics"] == []
    assert generated["graph"] is not None
    assert generated["graph"] == candidate_graph
    assert generated["changeset"]["kind"] == "source_only"
    cycles = next(
        item
        for item in generated["graph"]["workflow"]["meta_data"]["unilab"][
            "input_contract"
        ]["parameters"]
        if item["name"] == "cycles"
    )
    assert cycles["default"] == 4
