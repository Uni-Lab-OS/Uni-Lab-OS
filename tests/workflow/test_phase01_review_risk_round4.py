"""Phase 01 第四轮 Authoring CAS 与 Apply 降级风险回归测试。"""

from __future__ import annotations

import fcntl
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
CATALOG_FINGERPRINT = "sha256:" + ("f" * 64)
WRITEBACK_WARNING = {
    "code": "draft_writeback_pending",
    "message": ("工作流已应用，但本地源码同步失败；OS 已保留可恢复的源码记录。"),
}
POST_COMMIT_RESERVED = {
    "input_contract": {"version": 1, "parameters": []},
    "output_contract": {"version": 1, "outputs": []},
    "output_bindings": {},
}


class SourceOnlyCompiler:
    compiler_version = "phase-01-risk-round4-source-v1"
    template_catalog_fingerprint = CATALOG_FINGERPRINT

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
        normalized = (
            python_source if python_source.endswith("\n") else python_source + "\n"
        )
        return CandidateCompilation(
            diagnostics=[],
            graph=applied_graph,
            normalized_python_source=normalized,
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


class GraphCompiler:
    compiler_version = "phase-01-risk-round4-graph-v1"
    template_catalog_fingerprint = CATALOG_FINGERPRINT

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> CandidateCompilation:
        del source_uri
        return CandidateCompilation(
            diagnostics=[],
            graph={
                "workflow": {
                    **applied_graph["workflow"],
                    "uuid": workflow_uuid,
                    "revision": workflow_revision,
                    "meta_data": {
                        **applied_graph["workflow"]["meta_data"],
                        "unilab": POST_COMMIT_RESERVED,
                    },
                },
                "nodes": [],
                "edges": [],
                "node_templates": applied_graph["node_templates"],
                "handle_templates": applied_graph["handle_templates"],
            },
            normalized_python_source=(
                python_source if python_source.endswith("\n") else python_source + "\n"
            ),
            source_map=[],
            changeset={
                "kind": "graph",
                "created_node_uuids": [],
                "updated_node_uuids": [],
                "deleted_node_uuids": [],
                "created_edge_uuids": [],
                "updated_edge_uuids": [],
                "deleted_edge_uuids": [],
                "reserved_metadata_changed": True,
            },
            compiler_version=self.compiler_version,
            template_catalog_fingerprint=self.template_catalog_fingerprint,
        )


def _create_workflow(
    service: WorkflowService,
    *,
    meta_data: dict[str, Any] | None = None,
) -> None:
    service.create_workflow(
        name="round-4-risk-workflow",
        tags=[],
        description=None,
        meta_data={} if meta_data is None else meta_data,
        workflow_uuid=WORKFLOW_UUID,
    )


def _save_candidate(
    service: WorkflowService,
    package_root: Path,
    *,
    source: str,
    meta_data: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    _create_workflow(service, meta_data=meta_data)
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase01_round4_package",
        package_root=package_root,
        relative_path="workflows/demo.py",
    )
    source_path = package_root / "workflows" / "demo.py"
    saved = service.save_draft(
        WORKFLOW_UUID,
        python_source=source,
        expected_draft_hash=None,
        expected_workflow_revision=1,
    )
    assert saved["candidate"] is not None
    return source_path, saved


def _apply_saved(
    service: WorkflowService,
    saved: dict[str, Any],
) -> dict[str, Any]:
    return service.apply_authoring(
        WORKFLOW_UUID,
        expected_draft_hash=saved["draft"]["draft_hash"],
        expected_workflow_revision=1,
        expected_candidate_hash=saved["candidate"]["candidate_hash"],
    )


def test_apply_preserves_old_fd_write_at_cas_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store, compiler=SourceOnlyCompiler())
    package_root = tmp_path / "package"
    package_root.mkdir()
    source_path, saved = _save_candidate(
        service,
        package_root,
        source="value = 'candidate'",
    )
    lease_entered = threading.Event()
    allow_lease = threading.Event()
    original_fcntl = fcntl.fcntl
    outcome: dict[str, Any] = {}

    def gated_fcntl(
        descriptor: int,
        command: int,
        argument: int = 0,
    ) -> int:
        if command == fcntl.F_SETLEASE and argument == fcntl.F_WRLCK:
            lease_entered.set()
            if not allow_lease.wait(timeout=2):
                raise TimeoutError("test did not release lease acquisition")
        return original_fcntl(descriptor, command, argument)

    monkeypatch.setattr(
        "unilabos.workflow.service.fcntl.fcntl",
        gated_fcntl,
    )
    descriptor = os.open(source_path, os.O_RDWR)

    def apply() -> None:
        try:
            outcome["result"] = _apply_saved(service, saved)
        except Exception as exc:  # noqa: BLE001 - 后台异常由主测试断言
            outcome["error"] = exc

    thread = threading.Thread(target=apply, name="apply-cas-finalization")
    thread.start()
    external_source = "value = 'external write during finalization'\n"
    try:
        assert lease_entered.wait(timeout=1)
        external_bytes = external_source.encode("utf-8")
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, external_bytes)
        os.ftruncate(descriptor, len(external_bytes))
        os.fsync(descriptor)
        allow_lease.set()
        thread.join(timeout=2)
    finally:
        allow_lease.set()
        os.close(descriptor)
        thread.join(timeout=2)

    try:
        assert not thread.is_alive()
        assert "error" not in outcome
        result = outcome["result"]
        aggregate = service.get_authoring(WORKFLOW_UUID)
        record = store.get_authoring_record(WORKFLOW_UUID)
    finally:
        store.close()

    assert result["apply_result"]["warnings"] == [WRITEBACK_WARNING]
    assert aggregate["draft"]["python_source"] == external_source
    assert record["writeback_status"] == "pending"


def test_cas_restore_failure_retains_the_only_original_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store, compiler=SourceOnlyCompiler())
    package_root = tmp_path / "package"
    package_root.mkdir()
    source_path, saved = _save_candidate(
        service,
        package_root,
        source="value = 'original draft'\n",
    )
    original_bytes = source_path.read_bytes()
    replacement_source = "value = 'replacement draft'\n"
    replacement_bytes = replacement_source.encode()
    original_replace = os.replace
    canonical_install_failures = 0
    rollback_failures = 0

    def fail_canonical_install_and_rollback(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        nonlocal canonical_install_failures, rollback_failures
        source_name = Path(os.fsdecode(source)).name
        destination_name = Path(os.fsdecode(destination)).name
        if destination_name == source_path.name and source_name.endswith(".cas"):
            rollback_failures += 1
            raise OSError("deterministic backup rollback failure")
        if destination_name == source_path.name and canonical_install_failures == 0:
            original_replace(source, destination, *args, **kwargs)
            canonical_install_failures += 1
            raise OSError("deterministic canonical install finalization failure")
        original_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fail_canonical_install_and_rollback)
    try:
        with pytest.raises(WorkflowError) as error:
            service.save_draft(
                WORKFLOW_UUID,
                python_source=replacement_source,
                expected_draft_hash=saved["draft"]["draft_hash"],
                expected_workflow_revision=1,
            )
    finally:
        store.close()

    backup_paths = sorted(source_path.parent.glob(f".{source_path.name}.*.cas"))
    backup_snapshot = {path.name: path.read_bytes() for path in backup_paths}
    canonical_snapshot = source_path.read_bytes() if source_path.exists() else None
    rollback_failures_after_save = rollback_failures

    assert error.value.code == "internal_error"
    assert canonical_install_failures == 1
    assert rollback_failures_after_save == 0
    assert canonical_snapshot == replacement_bytes
    assert list(backup_snapshot.values()).count(original_bytes) == 1

    recovered_store = WorkflowStore(tmp_path / "workflow.db")
    recovered = WorkflowService(
        recovered_store,
        compiler=SourceOnlyCompiler(),
    )
    try:
        recovered_aggregate = recovered.get_authoring(WORKFLOW_UUID)
        canonical_after_get = source_path.read_bytes()
        artifacts_after_get = {
            path.name: path.read_bytes()
            for path in source_path.parent.glob(f".{source_path.name}.*.cas")
        }
        reconciled = recovered.reconcile_registered_source(WORKFLOW_UUID)
    finally:
        recovered_store.close()

    assert recovered_aggregate["state"] == "candidate_stale"
    assert recovered_aggregate["draft"]["python_source"] == replacement_source
    assert recovered_aggregate["candidate"] is None
    assert canonical_after_get == canonical_snapshot
    assert artifacts_after_get == backup_snapshot
    assert reconciled["state"] == "unapplied_source_only"
    assert reconciled["draft"]["python_source"] == replacement_source
    assert reconciled["candidate"] is not None
    assert (
        source_path.read_bytes() if source_path.exists() else None
    ) == canonical_snapshot
    assert rollback_failures == rollback_failures_after_save
    assert {
        path.name: path.read_bytes()
        for path in source_path.parent.glob(f".{source_path.name}.*.cas")
    } == backup_snapshot


def test_graph_apply_fallback_uses_post_commit_facts_without_writeback_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store, compiler=GraphCompiler())
    package_root = tmp_path / "package"
    package_root.mkdir()
    source_path, saved = _save_candidate(
        service,
        package_root,
        source="build()",
        meta_data={"color": "before"},
    )

    def fail_authoring_hydration(
        workflow_uuid: str,
    ) -> dict[str, Any]:
        del workflow_uuid
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        service,
        "get_authoring",
        fail_authoring_hydration,
    )
    try:
        result = _apply_saved(service, saved)
        persisted_graph = service.get_graph(WORKFLOW_UUID)
        record = store.get_authoring_record(WORKFLOW_UUID)
    finally:
        store.close()

    authoring = result["authoring"]
    applied_graph = authoring["applied_graph"]
    observed = {
        "warnings": result["apply_result"]["warnings"],
        "revisions": (
            result["apply_result"]["workflow_revision"],
            authoring["workflow_revision"],
            applied_graph["workflow"]["revision"],
        ),
        "applied_graph_is_persisted": applied_graph == persisted_graph,
        "meta_data": applied_graph["workflow"]["meta_data"],
        "writeback_status": record["writeback_status"],
        "draft_source": source_path.read_text(encoding="utf-8"),
    }
    assert observed == {
        "warnings": [],
        "revisions": (2, 2, 2),
        "applied_graph_is_persisted": True,
        "meta_data": {
            "color": "before",
            "unilab": POST_COMMIT_RESERVED,
        },
        "writeback_status": "settled",
        "draft_source": "build()\n",
    }
