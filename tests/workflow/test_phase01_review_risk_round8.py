"""Phase 01 第八轮 reconcile recovery reread 风险测试。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
CATALOG_FINGERPRINT = "sha256:" + ("8" * 64)
ORIGINAL_SOURCE = "value = 'candidate'"
NORMALIZED_SOURCE = "value = 'candidate'\n"
WRITEBACK_WARNING = {
    "code": "draft_writeback_pending",
    "message": "工作流已应用，但本地源码同步失败；OS 已保留可恢复的源码记录。",
}


def _hash(source: str) -> str:
    return f"sha256:{hashlib.sha256(source.encode()).hexdigest()}"


class SourceOnlyCompiler:
    compiler_version = "phase-01-risk-round8-v1"
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


def _seed_candidate(
    tmp_path: Path,
) -> tuple[WorkflowStore, WorkflowService, Path, dict[str, Any]]:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store, compiler=SourceOnlyCompiler())
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.create_workflow(
        name="round-8-risk-workflow",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase01_round8_package",
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
    return store, service, package_root / "workflows" / "demo.py", saved


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


def _record_marker(store: WorkflowStore) -> dict[str, Any]:
    # Authoring aggregate 尚不公开 recovery marker；这是验证其持久性的最小 seam。
    record = store.get_authoring_record(WORKFLOW_UUID)
    return {
        "observed_draft_hash": record["observed_draft_hash"],
        "writeback_status": record["writeback_status"],
        "writeback_source": record["writeback_source"],
        "writeback_expected_hash": record["writeback_expected_hash"],
    }


def _fail_apply_writeback_before_publish(
    service: WorkflowService,
    saved: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], Any]:
    original_atomic_write = service._atomic_write

    def fail_before_publish(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise OSError("deterministic pre-publish writeback failure")

    monkeypatch.setattr(service, "_atomic_write", fail_before_publish)
    applied = _apply_saved(service, saved)
    monkeypatch.setattr(service, "_atomic_write", original_atomic_write)
    return applied, original_atomic_write


@pytest.mark.parametrize("recovery_start", ["expected", "missing"])
def test_recovery_reread_preserves_external_third_inode_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recovery_start: str,
) -> None:
    external_source = f"value = 'external after {recovery_start} recovery'\n"
    store, service, source_path, saved = _seed_candidate(tmp_path)
    original_identity = _identity(source_path)
    try:
        applied, original_atomic_write = _fail_apply_writeback_before_publish(
            service,
            saved,
            monkeypatch,
        )
        marker_after_apply = _record_marker(store)
        canonical_after_apply = source_path.read_bytes()
        if recovery_start == "missing":
            source_path.unlink()
            _fsync_parent(source_path)
        canonical_missing_before_reconcile = not source_path.exists()

        staged_external = source_path.with_name(
            f".round8-{recovery_start}-external.tmp"
        )
        staged_external.write_text(external_source, encoding="utf-8")
        _fsync_file(staged_external)
        staged_identity = _identity(staged_external)
        scenario: dict[str, Any] = {}

        def recover_normalized_then_replace_external(
            *args: Any,
            **kwargs: Any,
        ) -> None:
            original_atomic_write(*args, **kwargs)
            scenario["normalized_bytes_before_external"] = source_path.read_bytes()
            scenario["normalized_identity"] = _identity(source_path)
            os.replace(staged_external, source_path)
            _fsync_parent(source_path)
            scenario["external_identity"] = _identity(source_path)

        monkeypatch.setattr(
            service,
            "_atomic_write",
            recover_normalized_then_replace_external,
        )
        event_cursor = service.list_events(after_id=0)["items"][-1]["id"]
        first_reconcile = service.reconcile_registered_source(WORKFLOW_UUID)
        marker_after_first_reconcile = _record_marker(store)
        canonical_after_first_reconcile = source_path.read_text(encoding="utf-8")
        first_identity = _identity(source_path)
        first_events = service.list_events(after_id=event_cursor)["items"]

        monkeypatch.setattr(service, "_atomic_write", original_atomic_write)
        second_reconcile = service.reconcile_registered_source(WORKFLOW_UUID)
        marker_after_second_reconcile = _record_marker(store)
        canonical_after_second_reconcile = source_path.read_text(encoding="utf-8")
        second_identity = _identity(source_path)
        all_events = service.list_events(after_id=event_cursor)["items"]
    finally:
        store.close()

    first_candidate = first_reconcile["candidate"]
    second_candidate = second_reconcile["candidate"]
    assert {
        "warnings": applied["apply_result"]["warnings"],
        "marker_after_apply": marker_after_apply,
        "canonical_after_apply": canonical_after_apply,
        "canonical_missing_before_reconcile": (canonical_missing_before_reconcile),
        "normalized_bytes_before_external": scenario.get(
            "normalized_bytes_before_external"
        ),
        "normalized_replaced_expected_inode": (
            recovery_start == "missing"
            or scenario.get("normalized_identity") != original_identity
        ),
        "external_was_staged_third_inode": (
            scenario.get("external_identity") == staged_identity
            and scenario.get("external_identity") != scenario.get("normalized_identity")
        ),
        "first_state": first_reconcile["state"],
        "first_draft": first_reconcile["draft"]["python_source"],
        "first_diagnostics": first_reconcile["draft"]["diagnostics"],
        "first_candidate_source": (
            first_candidate["normalized_python_source"]
            if first_candidate is not None
            else None
        ),
        "marker_after_first_reconcile": marker_after_first_reconcile,
        "first_event_causes": [event["data"]["cause"] for event in first_events],
        "first_event_draft_hashes": [
            event["data"]["draft_hash"] for event in first_events
        ],
        "canonical_after_first_reconcile": canonical_after_first_reconcile,
        "second_state": second_reconcile["state"],
        "second_candidate_preserved": (
            first_candidate is not None
            and second_candidate is not None
            and second_candidate["candidate_hash"] == first_candidate["candidate_hash"]
        ),
        "marker_after_second_reconcile": marker_after_second_reconcile,
        "second_reconcile_was_read_only": (
            canonical_after_second_reconcile == canonical_after_first_reconcile
            and second_identity == first_identity
        ),
        "event_causes_after_second_reconcile": [
            event["data"]["cause"] for event in all_events
        ],
    } == {
        "warnings": [WRITEBACK_WARNING],
        "marker_after_apply": {
            "observed_draft_hash": saved["draft"]["draft_hash"],
            "writeback_status": "pending",
            "writeback_source": NORMALIZED_SOURCE,
            "writeback_expected_hash": saved["draft"]["draft_hash"],
        },
        "canonical_after_apply": ORIGINAL_SOURCE.encode(),
        "canonical_missing_before_reconcile": recovery_start == "missing",
        "normalized_bytes_before_external": NORMALIZED_SOURCE.encode(),
        "normalized_replaced_expected_inode": True,
        "external_was_staged_third_inode": True,
        "first_state": "unapplied_source_only",
        "first_draft": external_source,
        "first_diagnostics": [],
        "first_candidate_source": external_source,
        "marker_after_first_reconcile": {
            "observed_draft_hash": _hash(external_source),
            "writeback_status": "settled",
            "writeback_source": None,
            "writeback_expected_hash": None,
        },
        "first_event_causes": ["external_draft_changed"],
        "first_event_draft_hashes": [_hash(external_source)],
        "canonical_after_first_reconcile": external_source,
        "second_state": "unapplied_source_only",
        "second_candidate_preserved": True,
        "marker_after_second_reconcile": {
            "observed_draft_hash": _hash(external_source),
            "writeback_status": "settled",
            "writeback_source": None,
            "writeback_expected_hash": None,
        },
        "second_reconcile_was_read_only": True,
        "event_causes_after_second_reconcile": ["external_draft_changed"],
    }


def test_recovery_without_external_writer_settles_applied_source_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, service, source_path, saved = _seed_candidate(tmp_path)
    original_identity = _identity(source_path)
    try:
        applied, _original_atomic_write = _fail_apply_writeback_before_publish(
            service,
            saved,
            monkeypatch,
        )
        marker_after_apply = _record_marker(store)
        event_cursor = service.list_events(after_id=0)["items"][-1]["id"]

        first_reconcile = service.reconcile_registered_source(WORKFLOW_UUID)
        marker_after_first_reconcile = _record_marker(store)
        canonical_after_first_reconcile = source_path.read_bytes()
        identity_after_first_reconcile = _identity(source_path)
        first_events = service.list_events(after_id=event_cursor)["items"]

        second_reconcile = service.reconcile_registered_source(WORKFLOW_UUID)
        marker_after_second_reconcile = _record_marker(store)
        canonical_after_second_reconcile = source_path.read_bytes()
        identity_after_second_reconcile = _identity(source_path)
        all_events = service.list_events(after_id=event_cursor)["items"]
    finally:
        store.close()

    assert {
        "warnings": applied["apply_result"]["warnings"],
        "marker_after_apply": marker_after_apply,
        "first_state": first_reconcile["state"],
        "first_candidate": first_reconcile["candidate"],
        "first_diagnostics": first_reconcile["draft"]["diagnostics"],
        "normalized_was_published": (
            canonical_after_first_reconcile == NORMALIZED_SOURCE.encode()
            and identity_after_first_reconcile != original_identity
        ),
        "marker_after_first_reconcile": marker_after_first_reconcile,
        "first_event_causes": [event["data"]["cause"] for event in first_events],
        "second_state": second_reconcile["state"],
        "second_candidate": second_reconcile["candidate"],
        "marker_after_second_reconcile": marker_after_second_reconcile,
        "second_reconcile_was_read_only": (
            canonical_after_second_reconcile == canonical_after_first_reconcile
            and identity_after_second_reconcile == identity_after_first_reconcile
        ),
        "event_causes_after_second_reconcile": [
            event["data"]["cause"] for event in all_events
        ],
    } == {
        "warnings": [WRITEBACK_WARNING],
        "marker_after_apply": {
            "observed_draft_hash": saved["draft"]["draft_hash"],
            "writeback_status": "pending",
            "writeback_source": NORMALIZED_SOURCE,
            "writeback_expected_hash": saved["draft"]["draft_hash"],
        },
        "first_state": "applied",
        "first_candidate": None,
        "first_diagnostics": [],
        "normalized_was_published": True,
        "marker_after_first_reconcile": {
            "observed_draft_hash": _hash(NORMALIZED_SOURCE),
            "writeback_status": "settled",
            "writeback_source": None,
            "writeback_expected_hash": None,
        },
        "first_event_causes": ["recovered"],
        "second_state": "applied",
        "second_candidate": None,
        "marker_after_second_reconcile": {
            "observed_draft_hash": _hash(NORMALIZED_SOURCE),
            "writeback_status": "settled",
            "writeback_source": None,
            "writeback_expected_hash": None,
        },
        "second_reconcile_was_read_only": True,
        "event_causes_after_second_reconcile": ["recovered"],
    }
