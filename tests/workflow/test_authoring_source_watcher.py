"""Round 02F package Draft watcher 的去抖、去重与固定路径合同。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.source_monitor import WorkflowSourceMonitor
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
CATALOG_FINGERPRINT = f"sha256:{'a' * 64}"


class FakeSourceService:
    def __init__(self) -> None:
        self.registration = {"workflow_uuid": WORKFLOW_UUID}
        self.signature: tuple[Any, ...] = ("file", 1)
        self.calls: list[tuple[Any, ...]] = []
        self.reconciled = threading.Event()

    def list_registered_sources(self) -> list[dict[str, str]]:
        return [self.registration]

    def source_signature(self, registration: dict[str, str]) -> tuple[Any, ...]:
        assert registration is self.registration
        return self.signature

    def reconcile_registered_source(self, workflow_uuid: str) -> None:
        assert workflow_uuid == WORKFLOW_UUID
        self.calls.append(self.signature)
        self.reconciled.set()


class SourceOnlyCompiler:
    compiler_version = "round-02f-watcher-v1"
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
        return CandidateCompilation(
            diagnostics=[],
            graph=applied_graph,
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


def _wait_for(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.005)
    assert predicate(), "bounded wait timed out"


def _service_with_source(tmp_path: Path) -> tuple[WorkflowService, WorkflowStore, Path]:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store, compiler=SourceOnlyCompiler())
    service.create_workflow(
        name="watcher contract",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="watcher_contract",
        package_root=package_root,
        relative_path="workflows/demo.py",
    )
    source = package_root / "workflows" / "demo.py"
    saved = service.save_draft(
        WORKFLOW_UUID,
        python_source="value = 'baseline'\n",
        expected_draft_hash=None,
        expected_workflow_revision=1,
    )
    assert saved["candidate"] is not None
    return service, store, source


def test_monitor_waits_for_stable_signature_and_coalesces_burst() -> None:
    service = FakeSourceService()
    monitor = WorkflowSourceMonitor(
        service,  # type: ignore[arg-type]
        interval_seconds=0.003,
        settle_seconds=0.03,
    )
    monitor.start()
    try:
        service.signature = ("file", 2)
        threading.Event().wait(0.012)
        service.signature = ("file", 3)
        threading.Event().wait(0.012)
        service.signature = ("file", 4)
        _wait_for(lambda: service.calls == [("file", 4)])
        threading.Event().wait(0.05)
    finally:
        monitor.stop()

    assert service.calls == [("file", 4)]


def test_monitor_does_not_repeat_processed_signature() -> None:
    service = FakeSourceService()
    monitor = WorkflowSourceMonitor(
        service,  # type: ignore[arg-type]
        interval_seconds=0.003,
        settle_seconds=0.01,
    )
    monitor.start()
    try:
        assert service.reconciled.wait(timeout=1)
        threading.Event().wait(0.05)
    finally:
        monitor.stop()

    assert service.calls == [("file", 1)]


def test_same_hash_external_rewrite_does_not_emit_duplicate_event(
    tmp_path: Path,
) -> None:
    service, store, source = _service_with_source(tmp_path)
    cursor = service.list_events(after_id=0)["items"][-1]["id"]
    original = source.read_bytes()
    monitor = WorkflowSourceMonitor(
        service,
        interval_seconds=0.003,
        settle_seconds=0.01,
    )
    monitor.start()
    try:
        source.write_bytes(original)
        _wait_for(
            lambda: bool(monitor._processed),
        )
        threading.Event().wait(0.04)
        events = service.list_events(after_id=cursor)["items"]
    finally:
        monitor.stop()
        store.close()

    assert events == []


def test_delete_rename_and_restore_stay_bound_to_canonical_path(tmp_path: Path) -> None:
    service, store, source = _service_with_source(tmp_path)
    cursor = service.list_events(after_id=0)["items"][-1]["id"]
    renamed = source.with_name("renamed.py")
    monitor = WorkflowSourceMonitor(
        service,
        interval_seconds=0.003,
        settle_seconds=0.01,
    )
    monitor.start()
    try:
        source.rename(renamed)
        _wait_for(
            lambda: len(service.list_events(after_id=cursor)["items"]) >= 1,
        )
        assert service.get_authoring(WORKFLOW_UUID)["state"] == "draft_missing"
        assert renamed.exists()
        assert service.get_graph(WORKFLOW_UUID)["workflow"]["uuid"] == WORKFLOW_UUID

        source.write_text("value = 'restored canonical'\n", encoding="utf-8")
        _wait_for(
            lambda: (
                service.get_authoring(WORKFLOW_UUID)["draft"] is not None
                and service.get_authoring(WORKFLOW_UUID)["draft"]["python_source"]
                == "value = 'restored canonical'\n"
            )
        )
        _wait_for(
            lambda: len(service.list_events(after_id=cursor)["items"]) >= 2,
        )
    finally:
        monitor.stop()
        events = service.list_events(after_id=cursor)["items"]
        store.close()

    assert renamed.read_text(encoding="utf-8") == "value = 'baseline'\n"
    assert [event["data"]["cause"] for event in events] == [
        "external_draft_changed",
        "recovered",
    ]
