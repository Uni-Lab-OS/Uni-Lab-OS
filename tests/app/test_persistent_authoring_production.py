"""Round 02G production Authoring 组合根的公共 HTTP 合同。"""

from __future__ import annotations

import importlib
import warnings
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.workflow.test_authoring_engine import (
    AUTHORITY,
    WORKFLOW_UUID,
    _catalog_imports,
    _source,
)
from unilabos.config.config import BasicConfig
from unilabos.workflow import composition
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.catalog import TemplateCatalog
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore


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
        TemplateCatalog(store).replace(AUTHORITY, _catalog_imports())
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


@pytest.mark.parametrize(
    "authority",
    [
        None,
        {"authority_id": "production-local", "kind": "guessed-local"},
    ],
    ids=["missing", "invalid-kind"],
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

    paths = server.setup_server().openapi()["paths"]

    assert "/api/v1/authoring/compile" not in paths
    assert "/api/v1/authoring/generate-python" not in paths
    assert "/api/v1/authoring/validate" not in paths
    assert "/api/v1/workflows/{workflow_uuid}/authoring" not in paths
    assert "/api/v1/workflows/{workflow_uuid}/authoring/draft" not in paths
    assert "/api/v1/workflows/{workflow_uuid}/authoring/apply" not in paths


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
        authority=AUTHORITY,
        roots=(selected_root,),
    )
    server = _reload_server()
    app = server.setup_server()
    assert server.setup_server() is app

    with warnings.catch_warnings(record=True) as openapi_warnings:
        warnings.simplefilter("always")
        schema = app.openapi()
    expected_routes = {
        "/api/v1/authoring/compile": "post",
        "/api/v1/authoring/generate-python": "post",
        "/api/v1/authoring/validate": "post",
        "/api/v1/workflows/{workflow_uuid}/authoring": "get",
        "/api/v1/workflows/{workflow_uuid}/authoring/draft": "put",
        "/api/v1/workflows/{workflow_uuid}/authoring/apply": "post",
        "/api/v1/events": "get",
    }
    http_methods = {"get", "put", "post", "delete", "patch", "options", "head"}
    operations = []
    for path, expected_method in expected_routes.items():
        path_item = schema["paths"][path]
        assert set(path_item) & http_methods == {expected_method}
        operations.append(path_item[expected_method]["operationId"])
    assert len(operations) == 7
    assert len(set(operations)) == 7
    assert not [
        item
        for item in openapi_warnings
        if "Duplicate Operation ID" in str(item.message)
        and any(operation in str(item.message) for operation in operations)
    ]

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
