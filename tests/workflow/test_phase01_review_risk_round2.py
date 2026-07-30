"""Phase 01 第二轮 Authoring monitor 与 writeback 并发风险回归测试。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow import composition
from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.source_monitor import WorkflowSourceMonitor
from unilabos.workflow.store import WorkflowStore

WORKFLOW_A_UUID = "11111111-1111-4111-8111-111111111111"
WORKFLOW_B_UUID = "22222222-2222-4222-8222-222222222222"
CATALOG_FINGERPRINT = "sha256:" + ("d" * 64)


class RecordingCompiler:
    compiler_version = "phase-01-risk-round2-v1"
    template_catalog_fingerprint = CATALOG_FINGERPRINT

    def __init__(self, *, failing_marker: str | None = None):
        self.failing_marker = failing_marker
        self.compiled_sources: list[str] = []
        self._lock = threading.Lock()

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
        with self._lock:
            self.compiled_sources.append(python_source)
        if self.failing_marker and self.failing_marker in python_source:
            raise ValueError("deterministic compiler failure")
        normalized = (
            python_source
            if python_source.endswith("\n")
            else python_source + "\n"
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

    def clear(self) -> None:
        with self._lock:
            self.compiled_sources.clear()

    def sources(self) -> list[str]:
        with self._lock:
            return list(self.compiled_sources)


@pytest.fixture(autouse=True)
def clean_composition():
    composition.reset_workflow_service_for_test()
    try:
        yield
    finally:
        composition.reset_workflow_service_for_test()


@pytest.fixture()
def service(tmp_path: Path):
    compiler = RecordingCompiler()
    opened = WorkflowStore(tmp_path / "workflow.db")
    workflow_service = WorkflowService(opened, compiler=compiler)
    try:
        yield workflow_service, compiler
    finally:
        opened.close()


def _create_workflow(service: WorkflowService, workflow_uuid: str) -> None:
    service.create_workflow(
        name=f"workflow-{workflow_uuid[:8]}",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=workflow_uuid,
    )


def _register_source(
    service: WorkflowService,
    *,
    workflow_uuid: str,
    package_root: Path,
    filename: str,
) -> Path:
    service.register_editable_source(
        workflow_uuid=workflow_uuid,
        package_id="phase01_round2_package",
        package_root=package_root,
        relative_path=f"workflows/{filename}",
    )
    return package_root / "workflows" / filename


def _wait_for(
    observation: Callable[[], Any],
    predicate: Callable[[Any], bool],
    *,
    timeout: float = 3.0,
) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = observation()
        if predicate(value):
            return value
        threading.Event().wait(0.01)
    value = observation()
    assert predicate(value), f"bounded wait timed out; last value: {value!r}"
    return value


def test_monitor_waits_for_segmented_write_to_stabilize(
    service,
    tmp_path: Path,
) -> None:
    workflow_service, compiler = service
    package_root = tmp_path / "package"
    package_root.mkdir()
    _create_workflow(workflow_service, WORKFLOW_A_UUID)
    source_path = _register_source(
        workflow_service,
        workflow_uuid=WORKFLOW_A_UUID,
        package_root=package_root,
        filename="segmented.py",
    )
    initial = workflow_service.save_draft(
        WORKFLOW_A_UUID,
        python_source="value = 'baseline'\n",
        expected_draft_hash=None,
        expected_workflow_revision=1,
    )
    cursor = workflow_service.list_events(after_id=0)["items"][-1]["id"]
    assert initial["candidate"] is not None
    compiler.clear()

    segments = [b"value", b" = ", b"'final", b" content", b"'\n"]
    final_source = b"".join(segments).decode("utf-8")
    write_next = threading.Barrier(2)
    segment_flushed = threading.Barrier(2)
    writer_ready = threading.Event()
    writer_errors: list[BaseException] = []

    def segmented_writer() -> None:
        try:
            with source_path.open("wb") as stream:
                writer_ready.set()
                for segment in segments:
                    write_next.wait(timeout=2)
                    stream.write(segment)
                    stream.flush()
                    segment_flushed.wait(timeout=2)
        except Exception as exc:  # noqa: BLE001 - 后台异常由主测试断言
            writer_errors.append(exc)
            writer_ready.set()

    writer = threading.Thread(
        target=segmented_writer,
        name="segmented-draft-writer",
    )
    monitor = WorkflowSourceMonitor(
        workflow_service,
        interval_seconds=0.005,
    )
    writer.start()
    assert writer_ready.wait(timeout=1)
    monitor.start()
    try:
        for _segment in segments:
            write_next.wait(timeout=2)
            segment_flushed.wait(timeout=2)
            threading.Event().wait(0.015)
        writer.join(timeout=2)
        assert not writer.is_alive()
        assert writer_errors == []

        aggregate = _wait_for(
            lambda: workflow_service.get_authoring(WORKFLOW_A_UUID),
            lambda value: (
                value["draft"]["python_source"] == final_source
                and value["candidate"] is not None
                and value["candidate"]["draft_hash"]
                == value["draft"]["draft_hash"]
            ),
        )
    finally:
        monitor.stop()
        if writer.is_alive():
            try:
                write_next.abort()
                segment_flushed.abort()
            finally:
                writer.join(timeout=2)

    assert compiler.sources() == [final_source]
    events = workflow_service.list_events(after_id=cursor)["items"]
    assert len(events) == 1
    assert events[0]["data"] == {
        "workflow_uuid": WORKFLOW_A_UUID,
        "cause": "external_draft_changed",
        "workflow_revision": 1,
        "draft_hash": aggregate["draft"]["draft_hash"],
        "candidate_hash": aggregate["candidate"]["candidate_hash"],
    }


@pytest.mark.parametrize(
    "failure_mode",
    ["compiler_none", "compiler_failure", "invalid_utf8"],
)
def test_composition_isolates_bad_registered_drafts_and_keeps_monitoring(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    package_root = tmp_path / "package"
    package_root.mkdir()
    seed_store = WorkflowStore(working_dir / "workflow.db")
    seed = WorkflowService(seed_store)
    for workflow_uuid, filename in [
        (WORKFLOW_A_UUID, "bad.py"),
        (WORKFLOW_B_UUID, "healthy.py"),
    ]:
        _create_workflow(seed, workflow_uuid)
        path = _register_source(
            seed,
            workflow_uuid=workflow_uuid,
            package_root=package_root,
            filename=filename,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
    bad_path = package_root / "workflows" / "bad.py"
    healthy_path = package_root / "workflows" / "healthy.py"
    bad_path.write_text("value = 'compiler failure'\n", encoding="utf-8")
    healthy_path.write_text("value = 'healthy'\n", encoding="utf-8")
    if failure_mode == "invalid_utf8":
        bad_path.write_bytes(b"\xff\xfe")
    seed_store.close()

    compiler: RecordingCompiler | None = RecordingCompiler()
    if failure_mode == "compiler_none":
        compiler = None
    elif failure_mode == "compiler_failure":
        compiler = RecordingCompiler(failing_marker="compiler failure")

    runtime_service = composition.compose_workflow_runtime(
        working_dir,
        compiler=compiler,
    )
    assert runtime_service is composition.get_workflow_service()
    assert {
        item["uuid"] for item in runtime_service.list_workflows()["items"]
    } == {WORKFLOW_A_UUID, WORKFLOW_B_UUID}

    if compiler is None:
        compiler = RecordingCompiler()
        runtime_service.compiler = compiler
    else:
        healthy = _wait_for(
            lambda: runtime_service.get_authoring(WORKFLOW_B_UUID),
            lambda value: value["candidate"] is not None,
        )
        assert healthy["draft"]["python_source"] == "value = 'healthy'\n"
        compiler.failing_marker = None

    repaired_source = "value = 'repaired'\n"
    bad_path.write_text(repaired_source, encoding="utf-8")
    repaired = _wait_for(
        lambda: runtime_service.get_authoring(WORKFLOW_A_UUID),
        lambda value: (
            value["draft"]["python_source"] == repaired_source
            and value["candidate"] is not None
        ),
    )
    assert repaired["candidate"]["draft_hash"] == repaired["draft"]["draft_hash"]


def test_apply_writeback_rechecks_after_final_compare_before_replace(
    service,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_service, _compiler = service
    package_root = tmp_path / "package"
    package_root.mkdir()
    _create_workflow(workflow_service, WORKFLOW_A_UUID)
    source_path = _register_source(
        workflow_service,
        workflow_uuid=WORKFLOW_A_UUID,
        package_root=package_root,
        filename="apply-race.py",
    )
    saved = workflow_service.save_draft(
        WORKFLOW_A_UUID,
        python_source="value = 'candidate'",
        expected_draft_hash=None,
        expected_workflow_revision=1,
    )
    original_atomic_write = workflow_service._atomic_write
    writeback_entered = threading.Event()
    allow_writeback = threading.Event()
    outcome: dict[str, Any] = {}

    def blocked_atomic_write(*args, **kwargs):
        writeback_entered.set()
        if not allow_writeback.wait(timeout=2):
            raise TimeoutError("test did not release writeback")
        return original_atomic_write(*args, **kwargs)

    monkeypatch.setattr(
        workflow_service,
        "_atomic_write",
        blocked_atomic_write,
    )

    def apply_candidate() -> None:
        try:
            outcome["result"] = workflow_service.apply_authoring(
                WORKFLOW_A_UUID,
                expected_draft_hash=saved["draft"]["draft_hash"],
                expected_workflow_revision=1,
                expected_candidate_hash=saved["candidate"]["candidate_hash"],
            )
        except Exception as exc:  # noqa: BLE001 - 后台异常由主测试断言
            outcome["error"] = exc

    apply_thread = threading.Thread(
        target=apply_candidate,
        name="apply-writeback-race",
    )
    apply_thread.start()
    try:
        assert writeback_entered.wait(timeout=1)
        external_source = "value = 'newer external draft'\n"
        source_path.write_text(external_source, encoding="utf-8")
        allow_writeback.set()
        apply_thread.join(timeout=2)
    finally:
        allow_writeback.set()
        apply_thread.join(timeout=2)

    assert not apply_thread.is_alive()
    assert "error" not in outcome
    result = outcome["result"]
    assert source_path.read_text(encoding="utf-8") == external_source
    assert result["apply_result"]["warnings"] == [
        {
            "code": "draft_writeback_pending",
            "message": (
                "工作流已应用，但本地源码同步失败；"
                "OS 已保留可恢复的源码记录。"
            ),
        }
    ]
    assert result["authoring"]["draft"]["python_source"] == external_source
    assert result["authoring"]["state"] == "applied_source_stale"


def test_reset_waits_for_monitor_exit_before_closing_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    package_root = tmp_path / "package"
    package_root.mkdir()
    runtime_service = composition.compose_workflow_runtime(
        working_dir,
        compiler=RecordingCompiler(),
    )
    original_reconcile = runtime_service.reconcile_registered_source
    reconcile_entered = threading.Event()
    allow_reconcile = threading.Event()
    reconcile_finished = threading.Event()
    worker_errors: list[BaseException] = []

    def blocked_reconcile(workflow_uuid: str):
        reconcile_entered.set()
        if not allow_reconcile.wait(timeout=5):
            worker_errors.append(TimeoutError("test did not release monitor"))
            reconcile_finished.set()
            return None
        try:
            return original_reconcile(workflow_uuid)
        except Exception as exc:  # noqa: BLE001 - 避免泄漏线程异常
            worker_errors.append(exc)
            return None
        finally:
            reconcile_finished.set()

    monkeypatch.setattr(
        runtime_service,
        "reconcile_registered_source",
        blocked_reconcile,
    )
    _create_workflow(runtime_service, WORKFLOW_A_UUID)
    _register_source(
        runtime_service,
        workflow_uuid=WORKFLOW_A_UUID,
        package_root=package_root,
        filename="blocked.py",
    )
    assert reconcile_entered.wait(timeout=1)

    reset_done = threading.Event()

    def reset_runtime() -> None:
        composition.reset_workflow_service_for_test()
        reset_done.set()

    reset_thread = threading.Thread(
        target=reset_runtime,
        name="reset-workflow-runtime",
    )
    reset_thread.start()
    returned_while_monitor_blocked = reset_done.wait(timeout=2.25)
    allow_reconcile.set()
    try:
        assert reconcile_finished.wait(timeout=2)
        reset_thread.join(timeout=2)
    finally:
        allow_reconcile.set()
        reset_thread.join(timeout=2)

    assert not returned_while_monitor_blocked
    assert not reset_thread.is_alive()
    assert reset_done.is_set()
    assert worker_errors == []
    assert composition.get_workflow_service() is None
    assert not any(
        thread.name == "workflow-source-monitor" and thread.is_alive()
        for thread in threading.enumerate()
    )
