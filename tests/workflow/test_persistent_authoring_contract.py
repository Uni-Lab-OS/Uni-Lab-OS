"""Round 02G persistent Candidate、Apply 与旧库迁移合同。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import (
    create_authoring_transform_app,
    create_workflow_app,
)
from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import StoreConflict, WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
OTHER_WORKFLOW_UUID = "22222222-2222-4222-8222-222222222222"
NODE_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OUTSIDE_NODE_UUID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
RESOURCE_TEMPLATE_UUID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
NODE_TEMPLATE_UUID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
FINGERPRINT = f"sha256:{'2' * 64}"
SOURCE = "result = build()\n"


def _node() -> dict[str, Any]:
    return {
        "uuid": NODE_UUID,
        "workflow_node_template_uuid": None,
        "parent_uuid": None,
        "material_uuid": None,
        "name": "candidate node",
        "status": "idle",
        "type": "compute",
        "icon": None,
        "pose": {},
        "param": {},
        "footer": None,
        "action_name": None,
        "action_type": None,
        "execution_policy": {},
        "disabled": False,
        "minimized": False,
        "script": None,
        "description": None,
        "meta_data": {},
    }


def _changeset(*, kind: str = "source_only", reserved: bool = False) -> dict[str, Any]:
    return {
        "kind": kind,
        "created_node_uuids": [],
        "updated_node_uuids": [],
        "deleted_node_uuids": [],
        "created_edge_uuids": [],
        "updated_edge_uuids": [],
        "deleted_edge_uuids": [],
        "reserved_metadata_changed": reserved,
    }


class AdversarialCompiler:
    compiler_version = "round-02g-adversarial/v1"
    template_catalog_fingerprint = FINGERPRINT

    def __init__(self, case: str) -> None:
        self.case = case

    def compile(self, **values: Any) -> CandidateCompilation:
        graph = deepcopy(values["applied_graph"])
        source_map: list[dict[str, Any]] = []
        changeset = _changeset()
        if self.case == "private-field":
            graph["workflow"]["private_bundle"] = "must not be dropped"
        elif self.case == "workflow-identity":
            graph["workflow"]["uuid"] = OTHER_WORKFLOW_UUID
        elif self.case == "duplicate-node":
            graph["nodes"].append(deepcopy(graph["nodes"][0]))
        elif self.case == "foreign-source-map":
            source_map = [
                {
                    "workflow_node_uuid": OUTSIDE_NODE_UUID,
                    "start_line": 1,
                    "start_column": 1,
                    "end_line": 1,
                    "end_column": 2,
                }
            ]
        elif self.case == "false-changeset":
            changeset["updated_node_uuids"] = [NODE_UUID]
            changeset["kind"] = "graph"
        elif self.case == "non-authoring-metadata":
            graph["workflow"]["meta_data"]["owner"] = "compiler-overwrite"
        elif self.case == "catalog-projection":
            graph["node_templates"] = [
                {
                    "uuid": NODE_TEMPLATE_UUID,
                    "create_time": "2026-08-01T00:00:00Z",
                    "update_time": "2026-08-01T00:00:00Z",
                    "meta_data": {},
                    "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
                    "name": "unreferenced",
                    "display_name": "Unreferenced",
                    "goal": {},
                    "goal_default": {},
                    "feedback": {},
                    "result": {},
                    "type": "action",
                    "node_type": "compute",
                }
            ]
        return CandidateCompilation(
            diagnostics=[],
            graph=graph,
            normalized_python_source=values["python_source"],
            source_map=source_map,
            changeset=changeset,
            compiler_version=self.compiler_version,
            template_catalog_fingerprint=self.template_catalog_fingerprint,
        )


@contextmanager
def _service_context(
    tmp_path: Path,
    compiler: Any,
    *,
    with_node: bool,
) -> Iterator[tuple[WorkflowService, Path]]:
    store = WorkflowStore(tmp_path / "unilabos_data" / "workflow.db")
    service = WorkflowService(store, compiler=compiler)
    service.create_workflow(
        name="original name",
        tags=["keep"],
        description="original description",
        meta_data={"owner": "human"},
        workflow_uuid=WORKFLOW_UUID,
    )
    if with_node:
        service.save_graph(
            WORKFLOW_UUID,
            revision=1,
            nodes=[_node()],
            edges=[],
        )
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="round_02g",
        package_root=package_root,
        relative_path="workflows/demo.py",
    )
    try:
        yield service, package_root / "workflows" / "demo.py"
    finally:
        store.close()


@pytest.mark.parametrize(
    "case",
    [
        "private-field",
        "workflow-identity",
        "duplicate-node",
        "foreign-source-map",
        "false-changeset",
        "non-authoring-metadata",
        "catalog-projection",
    ],
)
def test_pure_and_persistent_paths_reject_the_same_untrusted_bundle(
    tmp_path: Path,
    case: str,
) -> None:
    compiler = AdversarialCompiler(case)
    with _service_context(tmp_path, compiler, with_node=True) as (service, _path):
        graph = service.get_graph(WORKFLOW_UUID)
        with TestClient(create_authoring_transform_app(compiler)) as pure_client:
            pure = pure_client.post(
                "/api/v1/authoring/compile",
                json={
                    "workflow_uuid": WORKFLOW_UUID,
                    "revision": graph["workflow"]["revision"],
                    "source_uri": "package://round_02g/workflows/demo.py",
                    "python_source": SOURCE,
                    "applied_graph": graph,
                },
            )
        with TestClient(create_workflow_app(service)) as persistent_client:
            saved = persistent_client.put(
                f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
                json={
                    "python_source": SOURCE,
                    "expected_draft_hash": None,
                    "expected_workflow_revision": graph["workflow"]["revision"],
                },
            )

    assert pure.status_code == 500
    assert pure.json()["error"]["code"] == "internal_error"
    assert saved.status_code == 200
    aggregate = saved.json()["data"]
    assert aggregate["candidate"] is None
    assert [item["code"] for item in aggregate["draft"]["diagnostics"]] == [
        "candidate_invalid"
    ]


class ApplyCompiler:
    compiler_version = "round-02g-apply/v1"
    template_catalog_fingerprint = FINGERPRINT

    def __init__(self, *, semantic: bool) -> None:
        self.semantic = semantic
        self.compile_count = 0
        self.guard_entries = 0

    @contextmanager
    def catalog_snapshot(self) -> Iterator[str]:
        self.guard_entries += 1
        yield self.template_catalog_fingerprint

    def compile(self, **values: Any) -> CandidateCompilation:
        self.compile_count += 1
        graph = deepcopy(values["applied_graph"])
        changeset = _changeset()
        if self.semantic:
            graph["workflow"]["name"] = "source-owned name"
            graph["workflow"]["description"] = "source-owned description"
            graph["workflow"]["meta_data"]["unilab"] = {
                "input_contract": {"version": 1, "parameters": []},
                "output_contract": {"version": 1, "outputs": []},
                "output_bindings": {},
            }
            changeset = _changeset(kind="graph", reserved=True)
        return CandidateCompilation(
            diagnostics=[],
            graph=graph,
            normalized_python_source=values["python_source"],
            source_map=[],
            changeset=changeset,
            compiler_version=self.compiler_version,
            template_catalog_fingerprint=self.template_catalog_fingerprint,
        )


@pytest.mark.parametrize(
    ("semantic", "expected_revision"),
    [(False, 1), (True, 2)],
    ids=["source-only", "semantic"],
)
def test_apply_recompiles_under_catalog_guard_and_never_writes_source(
    tmp_path: Path,
    semantic: bool,
    expected_revision: int,
) -> None:
    compiler = ApplyCompiler(semantic=semantic)
    with (
        _service_context(tmp_path, compiler, with_node=False) as (
            service,
            source_path,
        ),
        TestClient(create_workflow_app(service)) as client,
    ):
        saved_response = client.put(
            f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
            json={
                "python_source": SOURCE,
                "expected_draft_hash": None,
                "expected_workflow_revision": 1,
            },
        )
        assert saved_response.status_code == 200
        saved = saved_response.json()["data"]
        assert saved["candidate"] is not None
        source_before = source_path.read_bytes()

        legacy = client.post(
            f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
            json={
                "candidate_hash": saved["candidate"]["candidate_hash"],
                "expected_draft_hash": saved["draft"]["draft_hash"],
                "expected_workflow_revision": 1,
                "graph": saved["candidate"]["graph"],
            },
        )
        applied = client.post(
            f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
            json={"candidate_hash": saved["candidate"]["candidate_hash"]},
        )
        after = client.get(f"/api/v1/workflows/{WORKFLOW_UUID}").json()["data"]
        events = service.list_events(after_id=0)["items"]
        tasks = service.list_workflow_tasks()["items"]

    assert legacy.status_code == 400
    assert legacy.json()["error"]["code"] == "invalid_input"
    assert applied.status_code == 200, applied.text
    result = applied.json()["data"]
    assert result["apply_result"] == {
        "kind": "graph" if semantic else "source_only",
        "previous_workflow_revision": 1,
        "workflow_revision": expected_revision,
        "applied_candidate_hash": saved["candidate"]["candidate_hash"],
        "applied_source_hash": saved["draft"]["draft_hash"],
        "warnings": [],
    }
    assert source_path.read_bytes() == source_before
    assert compiler.compile_count == 2
    assert compiler.guard_entries == 1
    assert after["revision"] == expected_revision
    assert after["name"] == ("source-owned name" if semantic else "original name")
    assert after["description"] == (
        "source-owned description" if semantic else "original description"
    )
    if semantic:
        assert after["meta_data"]["unilab"] == {
            "input_contract": {"version": 1, "parameters": []},
            "output_contract": {"version": 1, "outputs": []},
            "output_bindings": {},
        }
    assert events[-1]["event"] == "workflow.authoring.changed"
    assert events[-1]["data"] == {
        "workflow_uuid": WORKFLOW_UUID,
        "cause": "applied",
        "workflow_revision": expected_revision,
        "draft_hash": saved["draft"]["draft_hash"],
        "candidate_hash": None,
    }
    assert tasks == []


_LEGACY_NODE_TABLE = """
CREATE TABLE workflow_node_template (
    uuid TEXT PRIMARY KEY, create_time TEXT NOT NULL, update_time TEXT NOT NULL,
    deleted_at TEXT, description TEXT, meta_data TEXT NOT NULL,
    authority_id TEXT NOT NULL, resource_template_uuid TEXT NOT NULL,
    name TEXT NOT NULL, display_name TEXT NOT NULL, class TEXT,
    goal TEXT NOT NULL, goal_default TEXT NOT NULL, feedback TEXT NOT NULL,
    result TEXT NOT NULL, schema TEXT, type TEXT NOT NULL, icon TEXT,
    header TEXT, footer TEXT, node_type TEXT NOT NULL
)
"""
_LEGACY_HANDLE_TABLE = """
CREATE TABLE workflow_handle_template (
    uuid TEXT PRIMARY KEY, create_time TEXT NOT NULL, update_time TEXT NOT NULL,
    deleted_at TEXT, description TEXT, meta_data TEXT NOT NULL,
    authority_id TEXT NOT NULL, workflow_node_template_uuid TEXT NOT NULL,
    handle_key TEXT NOT NULL, io_type TEXT NOT NULL, display_name TEXT NOT NULL,
    type TEXT NOT NULL, required INTEGER NOT NULL, data_source TEXT, data_key TEXT
)
"""


def _legacy_catalog_database(path: Path, conflict: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(_LEGACY_NODE_TABLE)
        connection.execute(_LEGACY_HANDLE_TABLE)
        for index in range(2 if conflict == "node" else 1):
            connection.execute(
                "INSERT INTO workflow_node_template VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"30000000-0000-4000-8000-00000000000{index}",
                    "2026-08-01T00:00:00Z",
                    "2026-08-01T00:00:00Z",
                    None,
                    None,
                    "{}",
                    "legacy-authority",
                    RESOURCE_TEMPLATE_UUID,
                    "\tÄction\u2003" if index == 0 else " äction\n",
                    "Duplicate",
                    None,
                    "{}",
                    "{}",
                    "{}",
                    "{}",
                    None,
                    "action",
                    None,
                    None,
                    None,
                    "compute",
                ),
            )
        if conflict == "handle":
            for index in range(2):
                connection.execute(
                    "INSERT INTO workflow_handle_template VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"40000000-0000-4000-8000-00000000000{index}",
                        "2026-08-01T00:00:00Z",
                        "2026-08-01T00:00:00Z",
                        None,
                        None,
                        "{}",
                        "legacy-authority",
                        "30000000-0000-4000-8000-000000000000",
                        "\tÄesult\u2003" if index == 0 else " äesult\n",
                        "source",
                        "Result",
                        "string",
                        0,
                        "result",
                        "result",
                    ),
                )
        connection.commit()
    finally:
        connection.close()


def _legacy_database_snapshot(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(path)
    try:
        return {
            "schema": connection.execute(
                """
                SELECT type, name, tbl_name, sql
                FROM sqlite_master
                ORDER BY type, name
                """
            ).fetchall(),
            "node_rows": connection.execute(
                "SELECT * FROM workflow_node_template ORDER BY uuid"
            ).fetchall(),
            "handle_rows": connection.execute(
                "SELECT * FROM workflow_handle_template ORDER BY uuid"
            ).fetchall(),
        }
    finally:
        connection.close()


@pytest.mark.parametrize("conflict", ["node", "handle"])
def test_legacy_unicode_duplicate_business_key_has_stable_zero_change_error(
    tmp_path: Path,
    conflict: str,
) -> None:
    database_path = tmp_path / f"legacy-{conflict}.db"
    _legacy_catalog_database(database_path, conflict)
    before = _legacy_database_snapshot(database_path)

    try:
        opened = WorkflowStore(database_path)
    except BaseException as error:  # noqa: BLE001 - public migration error audit
        captured = error
    else:  # pragma: no cover - RED branch must not silently select a duplicate
        opened.close()
        pytest.fail("legacy duplicate active Catalog key must fail startup")

    assert isinstance(captured, StoreConflict)
    assert not isinstance(captured, sqlite3.IntegrityError)
    assert str(captured) == "legacy_catalog_business_key_conflict"
    assert _legacy_database_snapshot(database_path) == before
    connection = sqlite3.connect(database_path)
    try:
        table = f"workflow_{conflict}_template"
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 2
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        index_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
    finally:
        connection.close()
    assert "ux_workflow_node_template_authority_key_active" not in index_names
    assert "ux_workflow_handle_template_authority_key_active" not in index_names
