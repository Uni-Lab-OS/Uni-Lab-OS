"""Phase 01 第三轮 Authoring 独立风险回归测试。"""

from __future__ import annotations

import fcntl
import hashlib
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow import composition
from unilabos.workflow import service as workflow_service_module
from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowConflict, WorkflowError, WorkflowService
from unilabos.workflow.source_monitor import WorkflowSourceMonitor
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
CATALOG_FINGERPRINT = "sha256:" + ("e" * 64)


def _hash(source: str) -> str:
    return f"sha256:{hashlib.sha256(source.encode('utf-8')).hexdigest()}"


def _compilation(
    *,
    source: str,
    graph: dict[str, Any],
    compiler_version: str,
    catalog_fingerprint: str,
) -> CandidateCompilation:
    normalized = source if source.endswith("\n") else source + "\n"
    return CandidateCompilation(
        diagnostics=[],
        graph=graph,
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
        compiler_version=compiler_version,
        template_catalog_fingerprint=catalog_fingerprint,
    )


class DeterministicCompiler:
    compiler_version = "phase-01-risk-round3-v1"
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
        return _compilation(
            source=python_source,
            graph=applied_graph,
            compiler_version=self.compiler_version,
            catalog_fingerprint=self.template_catalog_fingerprint,
        )


class TransientCompiler:
    compiler_version = "phase-01-risk-round3-transient-v1"

    def __init__(self, failure_point: str):
        self.failure_point = failure_point
        self.armed = False
        self.failed = False
        self.call_times: list[float] = []
        self.failure_observed = threading.Event()
        self.recovery_observed = threading.Event()
        self._lock = threading.Lock()

    @property
    def template_catalog_fingerprint(self) -> str:
        if self.armed and self.failure_point == "catalog" and not self.failed:
            self.failed = True
            self.failure_observed.set()
            raise RuntimeError("transient catalog failure")
        return CATALOG_FINGERPRINT

    def arm(self) -> None:
        with self._lock:
            self.armed = True
            self.failed = False
            self.call_times.clear()
        self.failure_observed.clear()
        self.recovery_observed.clear()

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
            self.call_times.append(time.monotonic())
        if self.armed and self.failure_point == "compiler" and not self.failed:
            self.failed = True
            self.failure_observed.set()
            raise RuntimeError("transient compiler failure")
        fingerprint = self.template_catalog_fingerprint
        if self.armed:
            self.recovery_observed.set()
        return _compilation(
            source=python_source,
            graph=applied_graph,
            compiler_version=self.compiler_version,
            catalog_fingerprint=fingerprint,
        )

    def times(self) -> list[float]:
        with self._lock:
            return list(self.call_times)


@pytest.fixture(autouse=True)
def clean_composition():
    composition.reset_workflow_service_for_test()
    try:
        yield
    finally:
        composition.reset_workflow_service_for_test()


@pytest.fixture()
def service(tmp_path: Path):
    opened = WorkflowStore(tmp_path / "workflow.db")
    workflow_service = WorkflowService(
        opened,
        compiler=DeterministicCompiler(),
    )
    try:
        yield workflow_service
    finally:
        opened.close()


def _create_workflow(service: WorkflowService) -> None:
    service.create_workflow(
        name="round-3-risk-workflow",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )


def _register_source(
    service: WorkflowService,
    package_root: Path,
    *,
    filename: str = "demo.py",
) -> Path:
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase01_round3_package",
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


def _seed_registered_source(
    *,
    working_dir: Path,
    package_root: Path,
    source: str,
) -> Path:
    seed_store = WorkflowStore(working_dir / "workflow.db")
    seed = WorkflowService(seed_store, compiler=DeterministicCompiler())
    try:
        _create_workflow(seed)
        source_path = _register_source(seed, package_root)
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(source, encoding="utf-8")
        return source_path
    finally:
        seed_store.close()


def _save_candidate(
    service: WorkflowService,
    package_root: Path,
    *,
    source: str = "value = 'candidate'",
) -> tuple[Path, dict[str, Any]]:
    _create_workflow(service)
    source_path = _register_source(service, package_root)
    saved = service.save_draft(
        WORKFLOW_UUID,
        python_source=source,
        expected_draft_hash=None,
        expected_workflow_revision=1,
    )
    assert saved["candidate"] is not None
    return source_path, saved


def test_monitor_reconciles_change_between_startup_scan_and_monitor_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    package_root = tmp_path / "package"
    package_root.mkdir()
    source_path = _seed_registered_source(
        working_dir=working_dir,
        package_root=package_root,
        source="value = 'startup scan'\n",
    )
    start_entered = threading.Event()
    allow_start = threading.Event()
    original_start = WorkflowSourceMonitor.start
    outcome: dict[str, Any] = {}

    def gated_start(monitor: WorkflowSourceMonitor) -> None:
        start_entered.set()
        if not allow_start.wait(timeout=2):
            raise TimeoutError("test did not release monitor start")
        original_start(monitor)

    monkeypatch.setattr(WorkflowSourceMonitor, "start", gated_start)

    def compose() -> None:
        try:
            outcome["service"] = composition.compose_workflow_runtime(
                working_dir,
                compiler=DeterministicCompiler(),
            )
        except Exception as exc:  # noqa: BLE001 - 后台异常由主测试断言
            outcome["error"] = exc

    thread = threading.Thread(target=compose, name="compose-before-monitor")
    thread.start()
    try:
        assert start_entered.wait(timeout=1)
        startup_service = composition.get_workflow_service()
        assert startup_service is not None
        startup_events = startup_service.list_events(after_id=0)["items"]
        assert len(startup_events) == 1
        assert startup_events[0]["data"]["cause"] == "recovered"
        cursor = startup_events[0]["id"]

        changed_source = "value = 'changed before monitor start'\n"
        source_path.write_text(changed_source, encoding="utf-8")
        allow_start.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert "error" not in outcome
        runtime_service = outcome["service"]

        events = _wait_for(
            lambda: runtime_service.list_events(after_id=cursor)["items"],
            lambda items: any(
                item["data"].get("draft_hash") == _hash(changed_source)
                and item["data"].get("cause") == "external_draft_changed"
                for item in items
            ),
        )
        aggregate = runtime_service.get_authoring(WORKFLOW_UUID)
    finally:
        allow_start.set()
        thread.join(timeout=2)

    assert len(events) == 1
    assert aggregate["candidate"] is not None
    assert aggregate["candidate"]["draft_hash"] == _hash(changed_source)


def test_monitor_positive_control_observes_change_after_start(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    package_root = tmp_path / "package"
    package_root.mkdir()
    source_path = _seed_registered_source(
        working_dir=working_dir,
        package_root=package_root,
        source="value = 'startup'\n",
    )
    runtime_service = composition.compose_workflow_runtime(
        working_dir,
        compiler=DeterministicCompiler(),
    )
    cursor = runtime_service.list_events(after_id=0)["items"][-1]["id"]

    changed_source = "value = 'changed after monitor start'\n"
    source_path.write_text(changed_source, encoding="utf-8")
    aggregate = _wait_for(
        lambda: runtime_service.get_authoring(WORKFLOW_UUID),
        lambda value: (
            value["candidate"] is not None
            and value["candidate"]["draft_hash"] == _hash(changed_source)
        ),
    )

    assert aggregate["draft"]["python_source"] == changed_source
    events = runtime_service.list_events(after_id=cursor)["items"]
    assert [event["data"]["cause"] for event in events] == ["external_draft_changed"]


def test_draft_put_detects_old_file_descriptor_write_during_cas_install(
    service: WorkflowService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "package"
    package_root.mkdir()
    source_path, saved = _save_candidate(service, package_root)
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

    def save_replacement() -> None:
        try:
            outcome["result"] = service.save_draft(
                WORKFLOW_UUID,
                python_source="value = 'replacement'\n",
                expected_draft_hash=saved["draft"]["draft_hash"],
                expected_workflow_revision=1,
            )
        except Exception as exc:  # noqa: BLE001 - 后台异常由主测试断言
            outcome["error"] = exc

    thread = threading.Thread(target=save_replacement, name="draft-old-file-descriptor")
    thread.start()
    external_source = "value = 'external old fd wins'\n"
    try:
        assert lease_entered.wait(timeout=1)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, external_source.encode("utf-8"))
        os.ftruncate(descriptor, len(external_source.encode("utf-8")))
        os.fsync(descriptor)
        allow_lease.set()
        thread.join(timeout=2)
    finally:
        allow_lease.set()
        os.close(descriptor)
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert isinstance(outcome.get("error"), WorkflowConflict)
    assert outcome["error"].code == "draft_hash_conflict"
    assert source_path.read_text(encoding="utf-8") == external_source


@pytest.mark.parametrize("failure_point", ["compiler", "catalog"])
def test_monitor_retries_unchanged_source_after_transient_failure_with_backoff(
    tmp_path: Path,
    failure_point: str,
) -> None:
    compiler = TransientCompiler(failure_point)
    store = WorkflowStore(tmp_path / f"{failure_point}.db")
    workflow_service = WorkflowService(store, compiler=compiler)
    package_root = tmp_path / f"package-{failure_point}"
    package_root.mkdir()
    monitor = WorkflowSourceMonitor(
        workflow_service,
        interval_seconds=0.005,
        settle_seconds=0.01,
    )
    try:
        source_path, _saved = _save_candidate(
            workflow_service,
            package_root,
            source="value = 'baseline'\n",
        )
        cursor = workflow_service.list_events(after_id=0)["items"][-1]["id"]
        monitor.start()
        compiler.arm()

        changed_source = f"value = 'retry {failure_point}'\n"
        source_path.write_text(changed_source, encoding="utf-8")
        assert compiler.failure_observed.wait(timeout=1)
        assert compiler.recovery_observed.wait(timeout=2)
        aggregate = _wait_for(
            lambda: workflow_service.get_authoring(WORKFLOW_UUID),
            lambda value: (
                value["candidate"] is not None
                and value["candidate"]["draft_hash"] == _hash(changed_source)
            ),
        )
        events = workflow_service.list_events(after_id=cursor)["items"]
    finally:
        monitor.stop()
        store.close()

    call_times = compiler.times()
    assert len(call_times) == 2
    assert call_times[1] - call_times[0] >= 0.02
    assert aggregate["draft"]["python_source"] == changed_source
    assert len(events) == 1
    assert events[0]["data"]["cause"] == "external_draft_changed"


@pytest.mark.parametrize("read_operation", ["aggregate", "signature"])
@pytest.mark.parametrize(
    "directory_fd_paths_supported",
    [True, False],
    ids=["directory-fd", "windows-path-fallback"],
)
def test_source_read_fails_closed_when_parent_becomes_symlink_after_check(
    service: WorkflowService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_operation: str,
    directory_fd_paths_supported: bool,
) -> None:
    monkeypatch.setattr(
        workflow_service_module,
        "_DIRECTORY_FD_PATHS_SUPPORTED",
        directory_fd_paths_supported,
    )
    package_root = tmp_path / "package"
    package_root.mkdir()
    _create_workflow(service)
    source_path = _register_source(service, package_root)
    source_path.parent.mkdir()
    source_path.write_text("value = 'inside'\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_source = outside / source_path.name
    outside_content = "value = 'outside secret'\n"
    outside_source.write_text(outside_content, encoding="utf-8")
    saved_parent = package_root / "workflows-before-symlink"
    original_check = service._assert_contained_regular_target
    swapped = False

    def check_then_swap(
        root: Path,
        target: Path,
        *,
        allow_missing: bool,
    ) -> None:
        nonlocal swapped
        original_check(root, target, allow_missing=allow_missing)
        if target == source_path and not swapped:
            source_path.parent.rename(saved_parent)
            source_path.parent.symlink_to(outside, target_is_directory=True)
            swapped = True

    monkeypatch.setattr(
        service,
        "_assert_contained_regular_target",
        check_then_swap,
    )
    registration = service._store.get_source_registration(WORKFLOW_UUID)

    with pytest.raises(WorkflowError) as error:
        if read_operation == "aggregate":
            service.get_authoring(WORKFLOW_UUID)
        else:
            service.source_signature(registration)

    assert error.value.code == "invalid_input"
    assert outside_source.read_text(encoding="utf-8") == outside_content
    assert {path.name for path in outside.iterdir()} == {source_path.name}


def test_source_write_fails_closed_when_parent_becomes_symlink_after_check(
    service: WorkflowService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "package"
    package_root.mkdir()
    _create_workflow(service)
    source_path = _register_source(service, package_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    saved_parent = package_root / "workflows-before-symlink"
    original_check = service._assert_contained_regular_target
    target_checks = 0

    def check_then_swap(
        root: Path,
        target: Path,
        *,
        allow_missing: bool,
    ) -> None:
        nonlocal target_checks
        original_check(root, target, allow_missing=allow_missing)
        if target != source_path:
            return
        target_checks += 1
        if target_checks == 3:
            source_path.parent.rename(saved_parent)
            source_path.parent.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(
        service,
        "_assert_contained_regular_target",
        check_then_swap,
    )

    with pytest.raises(WorkflowError) as error:
        service.save_draft(
            WORKFLOW_UUID,
            python_source="value = 'must stay inside'\n",
            expected_draft_hash=None,
            expected_workflow_revision=1,
        )

    assert error.value.code in {"invalid_input", "internal_error"}
    assert list(outside.iterdir()) == []
    assert not (outside / source_path.name).exists()
