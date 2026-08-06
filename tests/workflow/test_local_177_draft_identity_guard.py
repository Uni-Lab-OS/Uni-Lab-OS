"""LOCAL-177 跨工作流 Draft 导入的持久源码身份守护回归。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.package_manager import WorkspaceSource, compile_package_source
from unilabos.package_manager.catalog import PackageCompileError
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.catalog import CatalogAuthority, TemplateCatalog
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

S04_WORKFLOW_UUID = "1bc5a151-445a-5a53-b24a-7a4b521ac60c"
S06_WORKFLOW_UUID = "0b4e6fce-14bc-5866-a373-16ad25c7f8cf"
LOCAL_AUTHORITY = CatalogAuthority(authority_id="local-177", kind="local")


def _workflow_source(*, workflow_uuid: str, name: str, symbol: str) -> str:
    return f'''from unilabos.workflow.authoring import workflow_definition


@workflow_definition(
    workflow_uuid="{workflow_uuid}",
    displayname="{name}",
)
def {symbol}() -> None:
    pass
'''


def _write_registered_s04_package(workspace: Path) -> tuple[Path, str]:
    package_root = workspace / "szlab_poly_studio"
    source_path = package_root / "workflows" / "s04_robot_stirring.py"
    source_path.parent.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (workspace / "pyproject.toml").write_text(
        '''[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "szlab-poly-studio"
version = "1.0.0"

[tool.setuptools.packages.find]
include = ["szlab_poly_studio*"]
''',
        encoding="utf-8",
    )
    (workspace / "package.yaml").write_text(
        f'''package:
  name: szlab_poly_studio

workflows:
  - workflow_uuid: {S04_WORKFLOW_UUID}
    source: szlab_poly_studio/workflows/s04_robot_stirring.py
''',
        encoding="utf-8",
    )
    source = _workflow_source(
        workflow_uuid=S04_WORKFLOW_UUID,
        name="S04 机械臂与磁搅联调",
        symbol="s04_robot_stirring",
    )
    source_path.write_text(source, encoding="utf-8")
    return source_path, source


@pytest.fixture()
def registered_s04(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, Path, Path, dict[str, Any]]]:
    workspace = tmp_path / "editable"
    source_path, _source = _write_registered_s04_package(workspace)

    initial_catalog = compile_package_source(WorkspaceSource(workspace))
    assert [
        workflow.details["workflow_uuid"]
        for workflow in initial_catalog.definitions.workflows
    ] == [S04_WORKFLOW_UUID]

    store = WorkflowStore(tmp_path / "unilabos_data" / "workflow.db")
    service = WorkflowService(
        store,
        compiler=WorkflowAuthoringEngine(
            catalog=TemplateCatalog(store),
            authority=LOCAL_AUTHORITY,
        ),
    )
    TemplateCatalog(store).replace(LOCAL_AUTHORITY, [])
    service.create_workflow(
        name="S04 机械臂与磁搅联调",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=S04_WORKFLOW_UUID,
    )
    service.register_editable_source(
        workflow_uuid=S04_WORKFLOW_UUID,
        package_id="szlab_poly_studio",
        package_root=workspace / "szlab_poly_studio",
        relative_path="workflows/s04_robot_stirring.py",
    )
    aggregate = service.reconcile_registered_source(S04_WORKFLOW_UUID)

    with TestClient(create_workflow_app(service)) as client:
        yield client, workspace, source_path, aggregate
    store.close()


def test_draft_put_rejects_s06_source_without_breaking_registered_s04(
    registered_s04: tuple[TestClient, Path, Path, dict[str, Any]],
) -> None:
    """S06 源码不得覆盖 S04 Draft 并导致下次启动失败。"""

    client, workspace, source_path, aggregate = registered_s04
    original_s04 = source_path.read_bytes()
    imported_s06 = _workflow_source(
        workflow_uuid=S06_WORKFLOW_UUID,
        name="S06 机械臂与加液联调",
        symbol="s06_robot_liquid_handling",
    )

    response = client.put(
        f"/api/v1/workflows/{S04_WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": imported_s06,
            "expected_draft_hash": aggregate["draft"]["draft_hash"],
            "expected_workflow_revision": aggregate["workflow_revision"],
        },
    )

    try:
        restarted_catalog = compile_package_source(WorkspaceSource(workspace))
    except PackageCompileError as error:
        assert [diagnostic.code for diagnostic in error.diagnostics] == [
            "WORKFLOW_UUID_MISMATCH",
            "WORKFLOW_SOURCE_DECLARATION_MISSING",
        ]
        pytest.fail(
            "跨工作流 Draft 导入已破坏已登记源码："
            "启动编译依次触发 WORKFLOW_UUID_MISMATCH 与 "
            "WORKFLOW_SOURCE_DECLARATION_MISSING",
            pytrace=False,
        )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "workflow_identity_mismatch"
    assert source_path.read_bytes() == original_s04
    assert [
        workflow.details["workflow_uuid"]
        for workflow in restarted_catalog.definitions.workflows
    ] == [S04_WORKFLOW_UUID]


def test_draft_put_still_saves_complete_source_for_the_same_workflow_identity(
    registered_s04: tuple[TestClient, Path, Path, dict[str, Any]],
) -> None:
    """同 UUID 的完整 Python 仍可以替换当前 Workflow Draft。"""

    client, workspace, source_path, aggregate = registered_s04
    updated_s04 = _workflow_source(
        workflow_uuid=S04_WORKFLOW_UUID,
        name="S04 机械臂与磁搅联调（已更新）",
        symbol="s04_robot_stirring",
    )

    response = client.put(
        f"/api/v1/workflows/{S04_WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": updated_s04,
            "expected_draft_hash": aggregate["draft"]["draft_hash"],
            "expected_workflow_revision": aggregate["workflow_revision"],
        },
    )

    assert response.status_code == 200, response.text
    saved = response.json()["data"]
    assert saved["draft"]["python_source"] == updated_s04
    assert saved["draft"]["diagnostics"] == []
    assert saved["candidate"] is not None
    assert source_path.read_text(encoding="utf-8") == updated_s04
    restarted_catalog = compile_package_source(WorkspaceSource(workspace))
    assert [
        workflow.details["workflow_uuid"]
        for workflow in restarted_catalog.definitions.workflows
    ] == [S04_WORKFLOW_UUID]
