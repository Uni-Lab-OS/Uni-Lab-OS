"""Phase 01A7：Catalog snapshot guard 退出异常的公共合同。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
CATALOG_FINGERPRINT = f"sha256:{'a' * 64}"
OTHER_CATALOG_FINGERPRINT = f"sha256:{'b' * 64}"
SOURCE = "result = build()\n"


class ExitFailingCatalogCompiler:
    """模拟进入成功、退出失败的可变 Catalog Adapter。"""

    compiler_version = "phase-01a7-catalog-guard-exit-v1"
    template_catalog_fingerprint = CATALOG_FINGERPRINT

    def __init__(
        self,
        *,
        snapshot_fingerprint: str = CATALOG_FINGERPRINT,
        fail_enter: bool = False,
    ) -> None:
        self.snapshot_fingerprint = snapshot_fingerprint
        self.fail_enter = fail_enter

    @contextmanager
    def catalog_snapshot(self) -> Iterator[str]:
        if self.fail_enter:
            raise OSError("catalog guard enter failed")
        try:
            yield self.snapshot_fingerprint
        finally:
            raise RuntimeError("catalog guard release failed")

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> CandidateCompilation:
        del workflow_uuid, workflow_revision, source_uri
        return CandidateCompilation(
            diagnostics=[],
            graph=deepcopy(applied_graph),
            normalized_python_source=python_source,
            source_map=[],
            changeset={
                "kind": "source_only",
                "created_node_uuids": [],
                "updated_node_uuids": [],
                "deleted_node_uuids": [],
                "created_edge_uuids": [],
                "updated_edge_uuids": [],
                "deleted_edge_uuids": [],
                "reserved_metadata_changed": False,
            },
            compiler_version=self.compiler_version,
            template_catalog_fingerprint=self.template_catalog_fingerprint,
        )


@contextmanager
def _authoring_client(
    tmp_path: Path,
    compiler: ExitFailingCatalogCompiler,
) -> Iterator[tuple[TestClient, Path]]:
    database_path = tmp_path / "unilabos_data" / "workflow.db"
    store = WorkflowStore(database_path)
    service = WorkflowService(store, compiler=compiler)
    service.create_workflow(
        name="Phase 01A7 Catalog guard 退出合同",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase_01a7_catalog_guard_exit",
        package_root=package_root,
        relative_path="workflows/demo.py",
    )
    try:
        with TestClient(
            create_workflow_app(service),
            raise_server_exceptions=False,
        ) as client:
            yield client, database_path
    finally:
        store.close()


def _save_candidate(client: TestClient) -> dict[str, Any]:
    response = client.put(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": SOURCE,
            "expected_draft_hash": None,
            "expected_workflow_revision": 1,
        },
    )
    assert response.status_code == 200
    aggregate = response.json()["data"]
    assert aggregate["candidate"] is not None
    return aggregate


def _get_authoring(client: TestClient) -> dict[str, Any]:
    response = client.get(f"/api/v1/workflows/{WORKFLOW_UUID}/authoring")
    assert response.status_code == 200
    return response.json()["data"]


def _response_body(response: Any) -> Any:
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    return response.text


def _recover_authoring(
    database_path: Path,
    compiler: ExitFailingCatalogCompiler,
) -> dict[str, Any]:
    store = WorkflowStore(database_path)
    try:
        return WorkflowService(store, compiler=compiler).get_authoring(WORKFLOW_UUID)
    finally:
        store.close()


def _persisted_projection(aggregate: dict[str, Any]) -> dict[str, Any]:
    return {
        "workflow_revision": aggregate["workflow_revision"],
        "applied_graph": aggregate["applied_graph"],
        "applied_source": aggregate["applied_source"],
        "candidate": aggregate["candidate"],
        "state": aggregate["state"],
    }


def test_guard_exit_failure_does_not_hide_catalog_conflict(tmp_path: Path) -> None:
    compiler = ExitFailingCatalogCompiler(
        snapshot_fingerprint=OTHER_CATALOG_FINGERPRINT
    )
    with _authoring_client(tmp_path, compiler) as (client, _database_path):
        saved = _save_candidate(client)
        before = _get_authoring(client)

        response = client.post(
            f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
            json={"candidate_hash": saved["candidate"]["candidate_hash"]},
        )
        after = _get_authoring(client)

    assert {
        "status": response.status_code,
        "body": _response_body(response),
        "authority_unchanged": after == before,
    } == {
        "status": 409,
        "body": {
            "code": 409,
            "error": {
                "code": "template_catalog_conflict",
                "message": "设备动作模板已更新，请重新编译并检查工作流",
            },
        },
        "authority_unchanged": True,
    }


def test_guard_exit_failure_after_commit_preserves_success_envelope(
    tmp_path: Path,
) -> None:
    compiler = ExitFailingCatalogCompiler()
    with _authoring_client(tmp_path, compiler) as (client, database_path):
        saved = _save_candidate(client)

        response = client.post(
            f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
            json={"candidate_hash": saved["candidate"]["candidate_hash"]},
        )
        committed = _get_authoring(client)

    recovered = _recover_authoring(database_path, compiler)

    assert {
        "status": response.status_code,
        "content_type": response.headers.get("content-type", "").split(";", 1)[0],
        "body": _response_body(response),
        "committed_candidate": committed["candidate"],
        "committed_source": committed["applied_source"],
        "recovered_candidate": recovered["candidate"],
        "recovered_source": recovered["applied_source"],
        "persisted_aggregate_recovered": (
            _persisted_projection(recovered) == _persisted_projection(committed)
        ),
    } == {
        "status": 200,
        "content_type": "application/json",
        "body": {
            "code": 0,
            "data": {
                "apply_result": {
                    "kind": "source_only",
                    "previous_workflow_revision": 1,
                    "workflow_revision": 1,
                    "applied_candidate_hash": saved["candidate"]["candidate_hash"],
                    "applied_source_hash": saved["draft"]["draft_hash"],
                    "warnings": [],
                },
                "authoring": committed,
            },
        },
        "committed_candidate": None,
        "committed_source": recovered["applied_source"],
        "recovered_candidate": None,
        "recovered_source": {
            **committed["applied_source"],
        },
        "persisted_aggregate_recovered": True,
    }


def test_guard_enter_failure_is_503_and_does_not_change_authority(
    tmp_path: Path,
) -> None:
    compiler = ExitFailingCatalogCompiler(fail_enter=True)
    with _authoring_client(tmp_path, compiler) as (client, _database_path):
        saved = _save_candidate(client)
        before = _get_authoring(client)

        response = client.post(
            f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
            json={"candidate_hash": saved["candidate"]["candidate_hash"]},
        )
        after = _get_authoring(client)

    assert response.status_code == 503
    assert response.json() == {
        "code": 503,
        "error": {
            "code": "template_catalog_unavailable",
            "message": "设备动作模板暂不可用，请稍后重试",
        },
    }
    assert after == before
