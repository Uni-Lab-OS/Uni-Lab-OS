"""Phase 01 第九轮 reconcile marker 原子性风险测试。"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
CATALOG_FINGERPRINT = "sha256:" + ("9" * 64)
ORIGINAL_SOURCE = "value = 'candidate'"
NORMALIZED_SOURCE = "value = 'candidate'\n"
WRITEBACK_WARNING = {
    "code": "draft_writeback_pending",
    "message": "工作流已应用，但本地源码同步失败；OS 已保留可恢复的源码记录。",
}


def _hash(source: str) -> str:
    return f"sha256:{hashlib.sha256(source.encode()).hexdigest()}"


class TransientCompiler:
    compiler_version = "phase-01-risk-round9-v1"
    template_catalog_fingerprint = CATALOG_FINGERPRINT

    def __init__(self) -> None:
        self._fail_next = False
        self.failure_count = 0

    def fail_next(self) -> None:
        self._fail_next = True

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
        if self._fail_next:
            self._fail_next = False
            self.failure_count += 1
            raise RuntimeError("deterministic transient compiler failure")
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


def _seed_candidate(
    tmp_path: Path,
) -> tuple[
    WorkflowStore,
    WorkflowService,
    TransientCompiler,
    Path,
    dict[str, Any],
]:
    store = WorkflowStore(tmp_path / "workflow.db")
    compiler = TransientCompiler()
    service = WorkflowService(store, compiler=compiler)
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.create_workflow(
        name="round-9-risk-workflow",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase01_round9_package",
        package_root=package_root,
        relative_path="workflows/demo.py",
    )
    saved = service.save_draft(
        WORKFLOW_UUID,
        python_source=ORIGINAL_SOURCE,
        expected_draft_hash=None,
        expected_workflow_revision=1,
    )
    assert saved["candidate"] is not None
    assert saved["candidate"]["normalized_python_source"] == NORMALIZED_SOURCE
    return (
        store,
        service,
        compiler,
        package_root / "workflows" / "demo.py",
        saved,
    )


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


def _identity(path: Path) -> tuple[int, int]:
    result = path.stat()
    return result.st_dev, result.st_ino


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_replace_source(path: Path, source: str, *, label: str) -> tuple[int, int]:
    staged = path.with_name(f".round9-{label}.tmp")
    staged.write_text(source, encoding="utf-8")
    _fsync_file(staged)
    staged_identity = _identity(staged)
    os.replace(staged, path)
    _fsync_parent(path)
    assert _identity(path) == staged_identity
    return staged_identity


def _record_marker(store: WorkflowStore) -> dict[str, Any]:
    # Authoring aggregate 尚不公开 recovery marker；这是验证其持久性的最小 seam。
    record = store.get_authoring_record(WORKFLOW_UUID)
    return {
        "observed_draft_hash": record["observed_draft_hash"],
        "writeback_status": record["writeback_status"],
        "writeback_source": record["writeback_source"],
        "writeback_expected_hash": record["writeback_expected_hash"],
    }


def _apply_with_pending_writeback(
    service: WorkflowService,
    saved: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    original_atomic_write = service._atomic_write

    def fail_before_publish(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise OSError("deterministic pre-publish writeback failure")

    monkeypatch.setattr(service, "_atomic_write", fail_before_publish)
    applied = _apply_saved(service, saved)
    monkeypatch.setattr(service, "_atomic_write", original_atomic_write)
    return applied


def test_compile_failure_keeps_recovery_for_restored_expected_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external_source = "value = 'external before transient compile'\n"
    store, service, compiler, source_path, saved = _seed_candidate(tmp_path)
    original_identity = _identity(source_path)
    try:
        applied = _apply_with_pending_writeback(service, saved, monkeypatch)
        marker_after_apply = _record_marker(store)
        external_identity = _atomic_replace_source(
            source_path,
            external_source,
            label="external-before-compile",
        )
        event_cursor = service.list_events(after_id=0)["items"][-1]["id"]

        compiler.fail_next()
        with pytest.raises(WorkflowError) as failure:
            service.reconcile_registered_source(WORKFLOW_UUID)
        marker_after_failure = _record_marker(store)
        aggregate_after_failure = service.get_authoring(WORKFLOW_UUID)
        events_after_failure = service.list_events(after_id=event_cursor)["items"]
        canonical_after_failure = source_path.read_text(encoding="utf-8")

        restored_identity = _atomic_replace_source(
            source_path,
            ORIGINAL_SOURCE,
            label="restored-expected",
        )
        recovered = service.reconcile_registered_source(WORKFLOW_UUID)
        marker_after_recovery = _record_marker(store)
        canonical_after_recovery = source_path.read_text(encoding="utf-8")
        recovered_identity = _identity(source_path)
        events_after_recovery = service.list_events(after_id=event_cursor)["items"]

        second_reconcile = service.reconcile_registered_source(WORKFLOW_UUID)
        marker_after_second_reconcile = _record_marker(store)
        canonical_after_second_reconcile = source_path.read_text(encoding="utf-8")
        second_identity = _identity(source_path)
        events_after_second_reconcile = service.list_events(after_id=event_cursor)[
            "items"
        ]
    finally:
        store.close()

    assert {
        "warnings": applied["apply_result"]["warnings"],
        "marker_after_apply": marker_after_apply,
        "external_was_new_inode": external_identity != original_identity,
        "compiler_error": failure.value.code,
        "compiler_failure_count": compiler.failure_count,
        "marker_after_failure": marker_after_failure,
        "aggregate_after_failure_state": aggregate_after_failure["state"],
        "aggregate_after_failure_candidate": aggregate_after_failure["candidate"],
        "canonical_after_failure": canonical_after_failure,
        "events_after_failure": events_after_failure,
        "expected_was_restored_atomically": restored_identity != external_identity,
        "recovered_state": recovered["state"],
        "recovered_candidate": recovered["candidate"],
        "recovered_diagnostics": recovered["draft"]["diagnostics"],
        "marker_after_recovery": marker_after_recovery,
        "canonical_after_recovery": canonical_after_recovery,
        "recovery_published_new_inode": recovered_identity != restored_identity,
        "event_causes_after_recovery": [
            event["data"]["cause"] for event in events_after_recovery
        ],
        "second_state": second_reconcile["state"],
        "marker_after_second_reconcile": marker_after_second_reconcile,
        "second_reconcile_was_read_only": (
            canonical_after_second_reconcile == canonical_after_recovery
            and second_identity == recovered_identity
        ),
        "event_causes_after_second_reconcile": [
            event["data"]["cause"] for event in events_after_second_reconcile
        ],
    } == {
        "warnings": [WRITEBACK_WARNING],
        "marker_after_apply": {
            "observed_draft_hash": saved["draft"]["draft_hash"],
            "writeback_status": "pending",
            "writeback_source": NORMALIZED_SOURCE,
            "writeback_expected_hash": saved["draft"]["draft_hash"],
        },
        "external_was_new_inode": True,
        "compiler_error": "internal_error",
        "compiler_failure_count": 1,
        "marker_after_failure": {
            "observed_draft_hash": saved["draft"]["draft_hash"],
            "writeback_status": "pending",
            "writeback_source": NORMALIZED_SOURCE,
            "writeback_expected_hash": saved["draft"]["draft_hash"],
        },
        "aggregate_after_failure_state": "applied_source_stale",
        "aggregate_after_failure_candidate": None,
        "canonical_after_failure": external_source,
        "events_after_failure": [],
        "expected_was_restored_atomically": True,
        "recovered_state": "applied",
        "recovered_candidate": None,
        "recovered_diagnostics": [],
        "marker_after_recovery": {
            "observed_draft_hash": _hash(NORMALIZED_SOURCE),
            "writeback_status": "settled",
            "writeback_source": None,
            "writeback_expected_hash": None,
        },
        "canonical_after_recovery": NORMALIZED_SOURCE,
        "recovery_published_new_inode": True,
        "event_causes_after_recovery": ["recovered"],
        "second_state": "applied",
        "marker_after_second_reconcile": {
            "observed_draft_hash": _hash(NORMALIZED_SOURCE),
            "writeback_status": "settled",
            "writeback_source": None,
            "writeback_expected_hash": None,
        },
        "second_reconcile_was_read_only": True,
        "event_causes_after_second_reconcile": ["recovered"],
    }


def test_record_failure_keeps_pending_until_external_candidate_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external_source = "value = 'external survives record retry'\n"
    store, service, _compiler, source_path, saved = _seed_candidate(tmp_path)
    original_identity = _identity(source_path)
    try:
        applied = _apply_with_pending_writeback(service, saved, monkeypatch)
        marker_after_apply = _record_marker(store)
        external_identity = _atomic_replace_source(
            source_path,
            external_source,
            label="external-before-record",
        )
        event_cursor = service.list_events(after_id=0)["items"][-1]["id"]
        original_record_compilation = store.record_draft_compilation
        record_failures = 0

        def fail_record_compilation(*args: Any, **kwargs: Any) -> int:
            nonlocal record_failures
            del args, kwargs
            record_failures += 1
            raise sqlite3.OperationalError("deterministic record transaction failure")

        monkeypatch.setattr(
            store,
            "record_draft_compilation",
            fail_record_compilation,
        )
        with pytest.raises(sqlite3.OperationalError):
            service.reconcile_registered_source(WORKFLOW_UUID)
        marker_after_failure = _record_marker(store)
        aggregate_after_failure = service.get_authoring(WORKFLOW_UUID)
        events_after_failure = service.list_events(after_id=event_cursor)["items"]
        canonical_after_failure = source_path.read_text(encoding="utf-8")
        identity_after_failure = _identity(source_path)

        monkeypatch.setattr(
            store,
            "record_draft_compilation",
            original_record_compilation,
        )
        reconciled = service.reconcile_registered_source(WORKFLOW_UUID)
        marker_after_success = _record_marker(store)
        canonical_after_success = source_path.read_text(encoding="utf-8")
        identity_after_success = _identity(source_path)
        events_after_success = service.list_events(after_id=event_cursor)["items"]

        second_reconcile = service.reconcile_registered_source(WORKFLOW_UUID)
        marker_after_second_reconcile = _record_marker(store)
        canonical_after_second_reconcile = source_path.read_text(encoding="utf-8")
        identity_after_second_reconcile = _identity(source_path)
        events_after_second_reconcile = service.list_events(after_id=event_cursor)[
            "items"
        ]
    finally:
        store.close()

    candidate = reconciled["candidate"]
    second_candidate = second_reconcile["candidate"]
    assert {
        "warnings": applied["apply_result"]["warnings"],
        "marker_after_apply": marker_after_apply,
        "external_was_new_inode": external_identity != original_identity,
        "record_failures": record_failures,
        "marker_after_failure": marker_after_failure,
        "aggregate_after_failure_state": aggregate_after_failure["state"],
        "aggregate_after_failure_candidate": aggregate_after_failure["candidate"],
        "events_after_failure": events_after_failure,
        "failure_was_filesystem_read_only": (
            canonical_after_failure == external_source
            and identity_after_failure == external_identity
        ),
        "success_state": reconciled["state"],
        "success_draft": reconciled["draft"]["python_source"],
        "success_diagnostics": reconciled["draft"]["diagnostics"],
        "success_candidate_source": (
            candidate["normalized_python_source"] if candidate is not None else None
        ),
        "marker_after_success": marker_after_success,
        "success_was_filesystem_read_only": (
            canonical_after_success == external_source
            and identity_after_success == external_identity
        ),
        "event_causes_after_success": [
            event["data"]["cause"] for event in events_after_success
        ],
        "event_draft_hashes_after_success": [
            event["data"]["draft_hash"] for event in events_after_success
        ],
        "second_state": second_reconcile["state"],
        "candidate_preserved": (
            candidate is not None
            and second_candidate is not None
            and second_candidate["candidate_hash"] == candidate["candidate_hash"]
        ),
        "marker_after_second_reconcile": marker_after_second_reconcile,
        "second_reconcile_was_read_only": (
            canonical_after_second_reconcile == external_source
            and identity_after_second_reconcile == external_identity
        ),
        "event_causes_after_second_reconcile": [
            event["data"]["cause"] for event in events_after_second_reconcile
        ],
    } == {
        "warnings": [WRITEBACK_WARNING],
        "marker_after_apply": {
            "observed_draft_hash": saved["draft"]["draft_hash"],
            "writeback_status": "pending",
            "writeback_source": NORMALIZED_SOURCE,
            "writeback_expected_hash": saved["draft"]["draft_hash"],
        },
        "external_was_new_inode": True,
        "record_failures": 1,
        "marker_after_failure": {
            "observed_draft_hash": saved["draft"]["draft_hash"],
            "writeback_status": "pending",
            "writeback_source": NORMALIZED_SOURCE,
            "writeback_expected_hash": saved["draft"]["draft_hash"],
        },
        "aggregate_after_failure_state": "applied_source_stale",
        "aggregate_after_failure_candidate": None,
        "events_after_failure": [],
        "failure_was_filesystem_read_only": True,
        "success_state": "unapplied_source_only",
        "success_draft": external_source,
        "success_diagnostics": [],
        "success_candidate_source": external_source,
        "marker_after_success": {
            "observed_draft_hash": _hash(external_source),
            "writeback_status": "settled",
            "writeback_source": None,
            "writeback_expected_hash": None,
        },
        "success_was_filesystem_read_only": True,
        "event_causes_after_success": ["external_draft_changed"],
        "event_draft_hashes_after_success": [_hash(external_source)],
        "second_state": "unapplied_source_only",
        "candidate_preserved": True,
        "marker_after_second_reconcile": {
            "observed_draft_hash": _hash(external_source),
            "writeback_status": "settled",
            "writeback_source": None,
            "writeback_expected_hash": None,
        },
        "second_reconcile_was_read_only": True,
        "event_causes_after_second_reconcile": ["external_draft_changed"],
    }
