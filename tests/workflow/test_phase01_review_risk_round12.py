"""Phase 01 第十二轮常驻 monitor processed fast-path 风险测试。"""

from __future__ import annotations

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
CATALOG_FINGERPRINT = "sha256:" + ("c" * 64)
ORIGINAL_SOURCE = "value = 'candidate'"
NORMALIZED_SOURCE = "value = 'candidate'\n"
WRITEBACK_WARNING = {
    "code": "draft_writeback_pending",
    "message": "工作流已应用，但本地源码同步失败；OS 已保留可恢复的源码记录。",
}


class SourceOnlyCompiler:
    compiler_version = "phase-01-risk-round12-v1"
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
) -> tuple[
    WorkflowStore,
    WorkflowService,
    Path,
    dict[str, Any],
    dict[str, Any],
]:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store, compiler=SourceOnlyCompiler())
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.create_workflow(
        name="round-12-risk-workflow",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase01_round12_package",
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
    registration = store.list_source_registrations()[0]
    return (
        store,
        service,
        package_root / "workflows" / "demo.py",
        registration,
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


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        result = path.stat()
    except FileNotFoundError:
        return None
    return result.st_dev, result.st_ino


def _bounded_observe(
    observation: Callable[[], Any],
    predicate: Callable[[Any], bool],
    *,
    timeout: float = 1.5,
) -> tuple[bool, Any]:
    """以有界 monitor tick 轮询取代宽松 sleep。"""

    deadline = time.monotonic() + timeout
    value = observation()
    while time.monotonic() < deadline:
        if predicate(value):
            return True, value
        threading.Event().wait(0.005)
        value = observation()
    return predicate(value), value


def _public_state(
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
        "reconciliation_pending": service.source_reconciliation_pending(WORKFLOW_UUID),
        "canonical": source_path.read_bytes(),
        "identity": _identity(source_path),
        "event_causes": [event["data"]["cause"] for event in events],
    }


def test_running_monitor_retries_apply_writeback_after_processed_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, service, source_path, registration, saved = _seed_candidate(tmp_path)
    monitor = WorkflowSourceMonitor(
        service,
        interval_seconds=0.005,
        settle_seconds=0.0,
    )
    monitor_started = False
    try:
        expected_signature = service.source_signature(registration)
        expected_identity = _identity(source_path)
        assert source_path.read_text(encoding="utf-8") == ORIGINAL_SOURCE

        monitor.start()
        monitor_started = True
        baseline_processed, processed_signature = _bounded_observe(
            lambda: monitor._processed.get(WORKFLOW_UUID),
            lambda value: value == expected_signature,
        )
        assert baseline_processed, (
            "测试前提失败：旧 expected Draft signature 未进入 monitor._processed；"
            f"last value: {processed_signature!r}"
        )

        original_atomic_write = service._atomic_write
        publish_observed = threading.Event()
        write_lock = threading.Lock()
        attempt_times: list[float] = []
        attempt_threads: list[str] = []
        published_identity: tuple[int, int] | None = None

        def fail_apply_once_then_publish(
            *args: Any,
            **kwargs: Any,
        ) -> None:
            nonlocal published_identity
            with write_lock:
                attempt_times.append(time.monotonic())
                attempt_threads.append(threading.current_thread().name)
                attempt = len(attempt_times)
            if attempt == 1:
                raise OSError("deterministic Apply pre-publish writeback failure")
            original_atomic_write(*args, **kwargs)
            published_identity = _identity(source_path)
            publish_observed.set()

        monkeypatch.setattr(
            service,
            "_atomic_write",
            fail_apply_once_then_publish,
        )
        caller_thread = threading.current_thread().name
        applied = _apply_saved(service, saved)
        signature_after_apply = service.source_signature(registration)
        identity_after_apply = _identity(source_path)
        pending_after_apply = service.source_reconciliation_pending(WORKFLOW_UUID)
        event_cursor = service.list_events(after_id=0)["items"][-1]["id"]

        recovered, bounded_state = _bounded_observe(
            lambda: _public_state(
                service,
                source_path,
                event_cursor=event_cursor,
            ),
            lambda value: (
                publish_observed.is_set() and not value["reconciliation_pending"]
            ),
        )
        monitor.stop()
        monitor_started = False

        with write_lock:
            attempts_after_monitor = len(attempt_times)
            threads_after_monitor = list(attempt_threads)
            retry_interval = (
                attempt_times[1] - attempt_times[0] if len(attempt_times) >= 2 else None
            )
        final_state = _public_state(
            service,
            source_path,
            event_cursor=event_cursor,
        )

        if recovered:
            repeated = service.reconcile_registered_source(WORKFLOW_UUID)
        else:
            # 不以手工 reconcile 掩盖常驻 monitor 未自动恢复的 RED。
            repeated = service.get_authoring(WORKFLOW_UUID)
        with write_lock:
            attempts_after_repeat = len(attempt_times)
        repeated_state = _public_state(
            service,
            source_path,
            event_cursor=event_cursor,
        )
    finally:
        if monitor_started:
            monitor.stop()
        store.close()

    assert {
        "baseline_processed": baseline_processed,
        "processed_signature": processed_signature,
        "signature_unchanged_after_apply": (
            signature_after_apply == expected_signature
        ),
        "identity_unchanged_after_apply": identity_after_apply == expected_identity,
        "warnings": applied["apply_result"]["warnings"],
        "pending_after_apply": pending_after_apply,
        "recovered": recovered,
        "write_attempts": (attempts_after_monitor, attempts_after_repeat),
        "attempt_threads": threads_after_monitor,
        "retry_used_bounded_backoff": (
            retry_interval is not None and 0.015 <= retry_interval <= 1.5
        ),
        "bounded_state": bounded_state,
        "published_identity": published_identity,
        "final_state": final_state,
        "repeated_state": repeated_state,
        "repeated_result": (repeated["state"], repeated["candidate"]),
    } == {
        "baseline_processed": True,
        "processed_signature": expected_signature,
        "signature_unchanged_after_apply": True,
        "identity_unchanged_after_apply": True,
        "warnings": [WRITEBACK_WARNING],
        "pending_after_apply": True,
        "recovered": True,
        "write_attempts": (2, 2),
        "attempt_threads": [caller_thread, "workflow-source-monitor"],
        "retry_used_bounded_backoff": True,
        "bounded_state": {
            "state": "applied",
            "draft": NORMALIZED_SOURCE,
            "candidate": None,
            "reconciliation_pending": False,
            "canonical": NORMALIZED_SOURCE.encode(),
            "identity": published_identity,
            "event_causes": ["recovered"],
        },
        "published_identity": published_identity,
        "final_state": {
            "state": "applied",
            "draft": NORMALIZED_SOURCE,
            "candidate": None,
            "reconciliation_pending": False,
            "canonical": NORMALIZED_SOURCE.encode(),
            "identity": published_identity,
            "event_causes": ["recovered"],
        },
        "repeated_state": {
            "state": "applied",
            "draft": NORMALIZED_SOURCE,
            "candidate": None,
            "reconciliation_pending": False,
            "canonical": NORMALIZED_SOURCE.encode(),
            "identity": published_identity,
            "event_causes": ["recovered"],
        },
        "repeated_result": ("applied", None),
    }
