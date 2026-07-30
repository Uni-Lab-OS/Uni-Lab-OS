"""Phase 01 第十轮 missing Draft recovery 瞬时失败风险测试。"""

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
CATALOG_FINGERPRINT = "sha256:" + ("0" * 64)
ORIGINAL_SOURCE = "value = 'candidate'"
NORMALIZED_SOURCE = "value = 'candidate'\n"
WRITEBACK_WARNING = {
    "code": "draft_writeback_pending",
    "message": "工作流已应用，但本地源码同步失败；OS 已保留可恢复的源码记录。",
}


def _hash(source: str) -> str:
    return f"sha256:{hashlib.sha256(source.encode()).hexdigest()}"


class SourceOnlyCompiler:
    compiler_version = "phase-01-risk-round10-v1"
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
        name="round-10-risk-workflow",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase01_round10_package",
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


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        result = path.stat()
    except FileNotFoundError:
        return None
    return result.st_dev, result.st_ino


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


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
) -> tuple[dict[str, Any], Any]:
    original_atomic_write = service._atomic_write

    def fail_before_publish(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise OSError("deterministic Apply pre-publish writeback failure")

    monkeypatch.setattr(service, "_atomic_write", fail_before_publish)
    applied = _apply_saved(service, saved)
    monkeypatch.setattr(service, "_atomic_write", original_atomic_write)
    return applied, original_atomic_write


@pytest.mark.parametrize("failure_mode", ["pre_publish", "post_publish"])
def test_missing_recovery_failure_keeps_pending_until_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    store, service, source_path, saved = _seed_candidate(tmp_path)
    try:
        applied, original_atomic_write = _apply_with_pending_writeback(
            service,
            saved,
            monkeypatch,
        )
        marker_after_apply = _record_marker(store)
        source_path.unlink()
        _fsync_parent(source_path)
        event_cursor = service.list_events(after_id=0)["items"][-1]["id"]
        recovery_failures = 0
        scenario: dict[str, Any] = {}

        def fail_missing_recovery(
            *args: Any,
            **kwargs: Any,
        ) -> None:
            nonlocal recovery_failures
            recovery_failures += 1
            if failure_mode == "post_publish":
                original_atomic_write(*args, **kwargs)
                scenario["published_bytes"] = source_path.read_bytes()
                scenario["published_identity"] = _identity(source_path)
            raise OSError(f"deterministic {failure_mode} recovery failure")

        monkeypatch.setattr(service, "_atomic_write", fail_missing_recovery)
        first_reconcile = service.reconcile_registered_source(WORKFLOW_UUID)
        marker_after_first_reconcile = _record_marker(store)
        canonical_after_first_reconcile = _canonical_bytes(source_path)
        identity_after_first_reconcile = _identity(source_path)
        events_after_first_reconcile = service.list_events(after_id=event_cursor)[
            "items"
        ]

        aggregate_from_get = service.get_authoring(WORKFLOW_UUID)
        marker_after_get = _record_marker(store)
        canonical_after_get = _canonical_bytes(source_path)
        identity_after_get = _identity(source_path)
        events_after_get = service.list_events(after_id=event_cursor)["items"]

        monkeypatch.setattr(service, "_atomic_write", original_atomic_write)
        retry_reconcile = service.reconcile_registered_source(WORKFLOW_UUID)
        marker_after_retry = _record_marker(store)
        canonical_after_retry = _canonical_bytes(source_path)
        identity_after_retry = _identity(source_path)
        events_after_retry = service.list_events(after_id=event_cursor)["items"]

        second_reconcile = service.reconcile_registered_source(WORKFLOW_UUID)
        marker_after_second_reconcile = _record_marker(store)
        canonical_after_second_reconcile = _canonical_bytes(source_path)
        identity_after_second_reconcile = _identity(source_path)
        events_after_second_reconcile = service.list_events(after_id=event_cursor)[
            "items"
        ]
    finally:
        store.close()

    expected_first_state = (
        "draft_missing" if failure_mode == "pre_publish" else "applied"
    )
    expected_first_draft = None if failure_mode == "pre_publish" else NORMALIZED_SOURCE
    assert {
        "warnings": applied["apply_result"]["warnings"],
        "marker_after_apply": marker_after_apply,
        "recovery_failures": recovery_failures,
        "post_publish_bytes": scenario.get("published_bytes"),
        "first_state": first_reconcile["state"],
        "first_draft": (
            first_reconcile["draft"]["python_source"]
            if first_reconcile["draft"] is not None
            else None
        ),
        "first_candidate": first_reconcile["candidate"],
        "marker_after_first_reconcile": marker_after_first_reconcile,
        "canonical_after_first_reconcile": canonical_after_first_reconcile,
        "events_after_first_reconcile": events_after_first_reconcile,
        "get_state": aggregate_from_get["state"],
        "get_draft": (
            aggregate_from_get["draft"]["python_source"]
            if aggregate_from_get["draft"] is not None
            else None
        ),
        "get_candidate": aggregate_from_get["candidate"],
        "get_was_read_only": (
            marker_after_get == marker_after_first_reconcile
            and canonical_after_get == canonical_after_first_reconcile
            and identity_after_get == identity_after_first_reconcile
            and events_after_get == events_after_first_reconcile
        ),
        "retry_state": retry_reconcile["state"],
        "retry_candidate": retry_reconcile["candidate"],
        "retry_diagnostics": (
            retry_reconcile["draft"]["diagnostics"]
            if retry_reconcile["draft"] is not None
            else None
        ),
        "marker_after_retry": marker_after_retry,
        "canonical_after_retry": canonical_after_retry,
        "retry_publish_behavior": (
            identity_after_retry is not None
            and (
                failure_mode == "pre_publish"
                or identity_after_retry == identity_after_first_reconcile
            )
        ),
        "event_causes_after_retry": [
            event["data"]["cause"] for event in events_after_retry
        ],
        "second_state": second_reconcile["state"],
        "second_candidate": second_reconcile["candidate"],
        "marker_after_second_reconcile": marker_after_second_reconcile,
        "second_reconcile_was_read_only": (
            canonical_after_second_reconcile == canonical_after_retry
            and identity_after_second_reconcile == identity_after_retry
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
        "recovery_failures": 1,
        "post_publish_bytes": (
            None if failure_mode == "pre_publish" else NORMALIZED_SOURCE.encode()
        ),
        "first_state": expected_first_state,
        "first_draft": expected_first_draft,
        "first_candidate": None,
        "marker_after_first_reconcile": {
            "observed_draft_hash": saved["draft"]["draft_hash"],
            "writeback_status": "pending",
            "writeback_source": NORMALIZED_SOURCE,
            "writeback_expected_hash": saved["draft"]["draft_hash"],
        },
        "canonical_after_first_reconcile": (
            None if failure_mode == "pre_publish" else NORMALIZED_SOURCE.encode()
        ),
        "events_after_first_reconcile": [],
        "get_state": expected_first_state,
        "get_draft": expected_first_draft,
        "get_candidate": None,
        "get_was_read_only": True,
        "retry_state": "applied",
        "retry_candidate": None,
        "retry_diagnostics": [],
        "marker_after_retry": {
            "observed_draft_hash": _hash(NORMALIZED_SOURCE),
            "writeback_status": "settled",
            "writeback_source": None,
            "writeback_expected_hash": None,
        },
        "canonical_after_retry": NORMALIZED_SOURCE.encode(),
        "retry_publish_behavior": True,
        "event_causes_after_retry": ["recovered"],
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
