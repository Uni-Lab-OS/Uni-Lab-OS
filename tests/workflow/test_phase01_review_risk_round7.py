"""Phase 01 第七轮 Apply writeback authority 风险测试。"""

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
CATALOG_FINGERPRINT = "sha256:" + ("7" * 64)
ORIGINAL_SOURCE = "value = 'candidate'"
NORMALIZED_SOURCE = "value = 'candidate'\n"
WRITEBACK_WARNING = {
    "code": "draft_writeback_pending",
    "message": "工作流已应用，但本地源码同步失败；OS 已保留可恢复的源码记录。",
}


def _hash(source: str) -> str:
    return f"sha256:{hashlib.sha256(source.encode()).hexdigest()}"


class SourceOnlyCompiler:
    compiler_version = "phase-01-risk-round7-v1"
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
        name="round-7-risk-workflow",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase01_round7_package",
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


def test_apply_does_not_settle_external_replace_after_normalized_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external_source = "value = 'external authority after apply'\n"
    store, service, source_path, saved = _seed_candidate(tmp_path)
    original_identity = _identity(source_path)
    staged_external = source_path.with_name(".round7-external.tmp")
    staged_external.write_text(external_source, encoding="utf-8")
    _fsync_file(staged_external)
    original_atomic_write = service._atomic_write
    scenario: dict[str, Any] = {}

    def write_normalized_then_replace_external(
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
        write_normalized_then_replace_external,
    )
    try:
        applied = _apply_saved(service, saved)
        marker_after_apply = _record_marker(store)
        canonical_after_apply = source_path.read_text(encoding="utf-8")
        event_cursor = service.list_events(after_id=0)["items"][-1]["id"]

        monkeypatch.setattr(service, "_atomic_write", original_atomic_write)
        reconciled = service.reconcile_registered_source(WORKFLOW_UUID)
        marker_after_reconcile = _record_marker(store)
        events = service.list_events(after_id=event_cursor)["items"]
        canonical_after_reconcile = source_path.read_text(encoding="utf-8")
    finally:
        store.close()

    assert {
        "normalized_bytes_before_external": scenario.get(
            "normalized_bytes_before_external"
        ),
        "normalized_was_second_inode": (
            scenario.get("normalized_identity") != original_identity
        ),
        "external_was_third_inode": (
            scenario.get("external_identity")
            not in {original_identity, scenario.get("normalized_identity")}
        ),
        "warnings": applied["apply_result"]["warnings"],
        "apply_state": applied["authoring"]["state"],
        "apply_draft": applied["authoring"]["draft"]["python_source"],
        "canonical_after_apply": canonical_after_apply,
        "marker_after_apply": marker_after_apply,
        "reconcile_state": reconciled["state"],
        "reconcile_draft": reconciled["draft"]["python_source"],
        "reconcile_diagnostics": reconciled["draft"]["diagnostics"],
        "reconcile_candidate_source": (
            reconciled["candidate"]["normalized_python_source"]
            if reconciled["candidate"] is not None
            else None
        ),
        "marker_after_reconcile": marker_after_reconcile,
        "event_causes": [event["data"]["cause"] for event in events],
        "event_draft_hashes": [event["data"]["draft_hash"] for event in events],
        "canonical_after_reconcile": canonical_after_reconcile,
    } == {
        "normalized_bytes_before_external": NORMALIZED_SOURCE.encode(),
        "normalized_was_second_inode": True,
        "external_was_third_inode": True,
        "warnings": [WRITEBACK_WARNING],
        "apply_state": "applied_source_stale",
        "apply_draft": external_source,
        "canonical_after_apply": external_source,
        "marker_after_apply": {
            "observed_draft_hash": saved["draft"]["draft_hash"],
            "writeback_status": "pending",
            "writeback_source": NORMALIZED_SOURCE,
            "writeback_expected_hash": saved["draft"]["draft_hash"],
        },
        "reconcile_state": "unapplied_source_only",
        "reconcile_draft": external_source,
        "reconcile_diagnostics": [],
        "reconcile_candidate_source": external_source,
        "marker_after_reconcile": {
            "observed_draft_hash": _hash(external_source),
            "writeback_status": "settled",
            "writeback_source": None,
            "writeback_expected_hash": None,
        },
        "event_causes": ["external_draft_changed"],
        "event_draft_hashes": [_hash(external_source)],
        "canonical_after_reconcile": external_source,
    }


def test_reconcile_settles_published_normalized_source_without_rewriting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, service, source_path, saved = _seed_candidate(tmp_path)
    original_bytes = source_path.read_bytes()
    original_replace = os.replace
    canonical_install_failures = 0

    def publish_then_fail_finalization(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        nonlocal canonical_install_failures
        source_name = Path(os.fsdecode(source)).name
        destination_name = Path(os.fsdecode(destination)).name
        if (
            destination_name == source_path.name
            and not source_name.endswith(".cas")
            and canonical_install_failures == 0
        ):
            original_replace(source, destination, *args, **kwargs)
            canonical_install_failures += 1
            raise OSError("deterministic normalized install finalization failure")
        original_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", publish_then_fail_finalization)
    try:
        applied = _apply_saved(service, saved)
        marker_after_apply = _record_marker(store)
        canonical_snapshot = source_path.read_bytes()
        canonical_identity = _identity(source_path)
        artifacts_snapshot = {
            path.name: path.read_bytes()
            for path in source_path.parent.glob(f".{source_path.name}.*.cas")
        }
        event_cursor = service.list_events(after_id=0)["items"][-1]["id"]

        monkeypatch.setattr(os, "replace", original_replace)
        aggregate_from_get = service.get_authoring(WORKFLOW_UUID)
        marker_after_get = _record_marker(store)
        events_after_get = service.list_events(after_id=event_cursor)["items"]
        canonical_after_get = source_path.read_bytes()
        identity_after_get = _identity(source_path)
        artifacts_after_get = {
            path.name: path.read_bytes()
            for path in source_path.parent.glob(f".{source_path.name}.*.cas")
        }

        reconciled = service.reconcile_registered_source(WORKFLOW_UUID)
        marker_after_reconcile = _record_marker(store)
        events_after_reconcile = service.list_events(after_id=event_cursor)["items"]
        canonical_after_reconcile = source_path.read_bytes()
        identity_after_reconcile = _identity(source_path)
        artifacts_after_reconcile = {
            path.name: path.read_bytes()
            for path in source_path.parent.glob(f".{source_path.name}.*.cas")
        }
    finally:
        store.close()

    assert {
        "canonical_install_failures": canonical_install_failures,
        "warnings": applied["apply_result"]["warnings"],
        "apply_state": applied["authoring"]["state"],
        "apply_candidate": applied["authoring"]["candidate"],
        "canonical_snapshot": canonical_snapshot,
        "original_artifact_count": list(artifacts_snapshot.values()).count(
            original_bytes
        ),
        "marker_after_apply": marker_after_apply,
        "get_state": aggregate_from_get["state"],
        "get_candidate": aggregate_from_get["candidate"],
        "get_draft": aggregate_from_get["draft"]["python_source"],
        "get_was_read_only": (
            canonical_after_get == canonical_snapshot
            and identity_after_get == canonical_identity
            and artifacts_after_get == artifacts_snapshot
            and marker_after_get == marker_after_apply
            and events_after_get == []
        ),
        "reconcile_state": reconciled["state"],
        "reconcile_candidate": reconciled["candidate"],
        "reconcile_diagnostics": reconciled["draft"]["diagnostics"],
        "marker_after_reconcile": marker_after_reconcile,
        "reconcile_did_not_rewrite": (
            canonical_after_reconcile == canonical_snapshot
            and identity_after_reconcile == canonical_identity
            and artifacts_after_reconcile == artifacts_snapshot
        ),
        "reconcile_event_causes": [
            event["data"]["cause"] for event in events_after_reconcile
        ],
    } == {
        "canonical_install_failures": 1,
        "warnings": [WRITEBACK_WARNING],
        "apply_state": "applied",
        "apply_candidate": None,
        "canonical_snapshot": NORMALIZED_SOURCE.encode(),
        "original_artifact_count": 1,
        "marker_after_apply": {
            "observed_draft_hash": saved["draft"]["draft_hash"],
            "writeback_status": "pending",
            "writeback_source": NORMALIZED_SOURCE,
            "writeback_expected_hash": saved["draft"]["draft_hash"],
        },
        "get_state": "applied",
        "get_candidate": None,
        "get_draft": NORMALIZED_SOURCE,
        "get_was_read_only": True,
        "reconcile_state": "applied",
        "reconcile_candidate": None,
        "reconcile_diagnostics": [],
        "marker_after_reconcile": {
            "observed_draft_hash": _hash(NORMALIZED_SOURCE),
            "writeback_status": "settled",
            "writeback_source": None,
            "writeback_expected_hash": None,
        },
        "reconcile_did_not_rewrite": True,
        "reconcile_event_causes": ["recovered"],
    }
