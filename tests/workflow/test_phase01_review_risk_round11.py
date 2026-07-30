"""Phase 01 第十一轮 monitor pending recovery 组合风险测试。"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.source_monitor import WorkflowSourceMonitor
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
CATALOG_FINGERPRINT = "sha256:" + ("b" * 64)
ORIGINAL_SOURCE = "value = 'candidate'"
NORMALIZED_SOURCE = "value = 'candidate'\n"
WRITEBACK_WARNING = {
    "code": "draft_writeback_pending",
    "message": "工作流已应用，但本地源码同步失败；OS 已保留可恢复的源码记录。",
}


def _hash(source: str) -> str:
    return f"sha256:{hashlib.sha256(source.encode()).hexdigest()}"


class SourceOnlyCompiler:
    compiler_version = "phase-01-risk-round11-v1"
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
        name="round-11-risk-workflow",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase01_round11_package",
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


def _apply_with_pending_writeback(
    service: WorkflowService,
    saved: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], Callable[..., None]]:
    original_atomic_write = service._atomic_write

    def fail_before_publish(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise OSError("deterministic Apply pre-publish writeback failure")

    monkeypatch.setattr(service, "_atomic_write", fail_before_publish)
    applied = _apply_saved(service, saved)
    monkeypatch.setattr(service, "_atomic_write", original_atomic_write)
    return applied, original_atomic_write


def _record_marker(store: WorkflowStore) -> dict[str, Any]:
    # Authoring aggregate 尚不公开 recovery marker；这是验证持久性的最小 seam。
    record = store.get_authoring_record(WORKFLOW_UUID)
    return {
        "observed_draft_hash": record["observed_draft_hash"],
        "writeback_status": record["writeback_status"],
        "writeback_source": record["writeback_source"],
        "writeback_expected_hash": record["writeback_expected_hash"],
    }


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        result = path.stat()
    except FileNotFoundError:
        return None
    return result.st_dev, result.st_ino


def _canonical_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _bounded_observe(
    observation: Callable[[], Any],
    predicate: Callable[[Any], bool],
    *,
    timeout: float = 1.0,
) -> tuple[bool, Any]:
    """用有界轮询等待 monitor tick，不使用不受控的 sleep。"""

    deadline = time.monotonic() + timeout
    value = observation()
    while time.monotonic() < deadline:
        if predicate(value):
            return True, value
        threading.Event().wait(0.005)
        value = observation()
    return predicate(value), value


def _public_recovery_state(
    store: WorkflowStore,
    service: WorkflowService,
    source_path: Path,
    *,
    event_cursor: int,
) -> dict[str, Any]:
    aggregate = service.get_authoring(WORKFLOW_UUID)
    events = service.list_events(after_id=event_cursor)["items"]
    return {
        "state": aggregate["state"],
        "draft": (
            aggregate["draft"]["python_source"]
            if aggregate["draft"] is not None
            else None
        ),
        "candidate": aggregate["candidate"],
        "marker": _record_marker(store),
        "canonical": _canonical_bytes(source_path),
        "identity": _identity(source_path),
        "event_causes": [event["data"]["cause"] for event in events],
    }


@pytest.mark.parametrize("canonical_start", ["missing", "expected"])
def test_monitor_retries_pending_recovery_when_signature_does_not_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canonical_start: str,
) -> None:
    store, service, source_path, saved = _seed_candidate(tmp_path)
    monitor: WorkflowSourceMonitor | None = None
    try:
        applied, original_atomic_write = _apply_with_pending_writeback(
            service,
            saved,
            monkeypatch,
        )
        if canonical_start == "missing":
            source_path.unlink()
            _fsync_parent(source_path)
        else:
            assert source_path.read_text(encoding="utf-8") == ORIGINAL_SOURCE
        event_cursor = service.list_events(after_id=0)["items"][-1]["id"]
        first_failure = threading.Event()
        successful_publish = threading.Event()
        write_lock = threading.Lock()
        write_attempts = 0
        published_identity: tuple[int, int] | None = None

        def fail_once_then_publish(
            *args: Any,
            **kwargs: Any,
        ) -> None:
            nonlocal write_attempts, published_identity
            with write_lock:
                write_attempts += 1
                attempt = write_attempts
            if attempt == 1:
                first_failure.set()
                raise OSError("deterministic monitor pre-publish recovery failure")
            original_atomic_write(*args, **kwargs)
            published_identity = _identity(source_path)
            successful_publish.set()

        monkeypatch.setattr(service, "_atomic_write", fail_once_then_publish)
        monitor = WorkflowSourceMonitor(
            service,
            interval_seconds=0.005,
            settle_seconds=0.0,
        )
        monitor.start()
        first_attempt_observed = first_failure.wait(timeout=1.0)
        recovered, public_state = _bounded_observe(
            lambda: _public_recovery_state(
                store,
                service,
                source_path,
                event_cursor=event_cursor,
            ),
            lambda value: (
                successful_publish.is_set()
                and value["marker"]["writeback_status"] == "settled"
            ),
        )
        monitor.stop()
        monitor = None
        with write_lock:
            final_write_attempts = write_attempts
        final_state = _public_recovery_state(
            store,
            service,
            source_path,
            event_cursor=event_cursor,
        )
    finally:
        if monitor is not None:
            monitor.stop()
        store.close()

    assert {
        "warnings": applied["apply_result"]["warnings"],
        "first_attempt_observed": first_attempt_observed,
        "recovered": recovered,
        "write_attempts": final_write_attempts,
        "published_identity": published_identity,
        "bounded_state": public_state,
        "final_state": final_state,
    } == {
        "warnings": [WRITEBACK_WARNING],
        "first_attempt_observed": True,
        "recovered": True,
        "write_attempts": 2,
        "published_identity": final_state["identity"],
        "bounded_state": {
            "state": "applied",
            "draft": NORMALIZED_SOURCE,
            "candidate": None,
            "marker": {
                "observed_draft_hash": _hash(NORMALIZED_SOURCE),
                "writeback_status": "settled",
                "writeback_source": None,
                "writeback_expected_hash": None,
            },
            "canonical": NORMALIZED_SOURCE.encode(),
            "identity": final_state["identity"],
            "event_causes": ["recovered"],
        },
        "final_state": {
            "state": "applied",
            "draft": NORMALIZED_SOURCE,
            "candidate": None,
            "marker": {
                "observed_draft_hash": _hash(NORMALIZED_SOURCE),
                "writeback_status": "settled",
                "writeback_source": None,
                "writeback_expected_hash": None,
            },
            "canonical": NORMALIZED_SOURCE.encode(),
            "identity": final_state["identity"],
            "event_causes": ["recovered"],
        },
    }


def test_monitor_settles_post_publish_wrapper_failure_without_duplicate_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, service, source_path, saved = _seed_candidate(tmp_path)
    monitor: WorkflowSourceMonitor | None = None
    try:
        applied, original_atomic_write = _apply_with_pending_writeback(
            service,
            saved,
            monkeypatch,
        )
        source_path.unlink()
        _fsync_parent(source_path)
        event_cursor = service.list_events(after_id=0)["items"][-1]["id"]
        published = threading.Event()
        write_lock = threading.Lock()
        write_attempts = 0
        published_identity: tuple[int, int] | None = None

        def publish_then_fail(
            *args: Any,
            **kwargs: Any,
        ) -> None:
            nonlocal write_attempts, published_identity
            with write_lock:
                write_attempts += 1
            original_atomic_write(*args, **kwargs)
            published_identity = _identity(source_path)
            published.set()
            raise OSError("deterministic monitor post-publish wrapper failure")

        monkeypatch.setattr(service, "_atomic_write", publish_then_fail)
        monitor = WorkflowSourceMonitor(
            service,
            interval_seconds=0.005,
            settle_seconds=0.0,
        )
        monitor.start()
        publish_observed = published.wait(timeout=1.0)
        recovered, public_state = _bounded_observe(
            lambda: _public_recovery_state(
                store,
                service,
                source_path,
                event_cursor=event_cursor,
            ),
            lambda value: value["marker"]["writeback_status"] == "settled",
        )
        monitor.stop()
        monitor = None
        with write_lock:
            attempts_after_monitor = write_attempts
        identity_after_monitor = _identity(source_path)
        events_after_monitor = service.list_events(after_id=event_cursor)["items"]

        repeated = service.reconcile_registered_source(WORKFLOW_UUID)
        with write_lock:
            attempts_after_repeat = write_attempts
        identity_after_repeat = _identity(source_path)
        events_after_repeat = service.list_events(after_id=event_cursor)["items"]
    finally:
        if monitor is not None:
            monitor.stop()
        store.close()

    assert {
        "warnings": applied["apply_result"]["warnings"],
        "publish_observed": publish_observed,
        "recovered": recovered,
        "public_state": public_state,
        "write_attempts": (attempts_after_monitor, attempts_after_repeat),
        "published_identity": published_identity,
        "identities": (identity_after_monitor, identity_after_repeat),
        "event_causes": [event["data"]["cause"] for event in events_after_monitor],
        "event_causes_after_repeat": [
            event["data"]["cause"] for event in events_after_repeat
        ],
        "repeated_state": repeated["state"],
        "repeated_candidate": repeated["candidate"],
    } == {
        "warnings": [WRITEBACK_WARNING],
        "publish_observed": True,
        "recovered": True,
        "public_state": {
            "state": "applied",
            "draft": NORMALIZED_SOURCE,
            "candidate": None,
            "marker": {
                "observed_draft_hash": _hash(NORMALIZED_SOURCE),
                "writeback_status": "settled",
                "writeback_source": None,
                "writeback_expected_hash": None,
            },
            "canonical": NORMALIZED_SOURCE.encode(),
            "identity": published_identity,
            "event_causes": ["recovered"],
        },
        "write_attempts": (1, 1),
        "published_identity": published_identity,
        "identities": (published_identity, published_identity),
        "event_causes": ["recovered"],
        "event_causes_after_repeat": ["recovered"],
        "repeated_state": "applied",
        "repeated_candidate": None,
    }
