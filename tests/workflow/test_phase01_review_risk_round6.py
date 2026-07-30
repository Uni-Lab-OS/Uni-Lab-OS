"""Phase 01 第六轮 Authoring 外部 canonical authority 风险测试。"""

from __future__ import annotations

import multiprocessing
import os
import queue
import signal
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
CATALOG_FINGERPRINT = "sha256:" + ("6" * 64)


class SourceOnlyCompiler:
    compiler_version = "phase-01-risk-round6-v1"
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


def _seed_existing_draft(
    tmp_path: Path,
    *,
    source: str,
) -> tuple[WorkflowStore, WorkflowService, Path, dict[str, Any]]:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store, compiler=SourceOnlyCompiler())
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.create_workflow(
        name="round-6-risk-workflow",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase01_round6_package",
        package_root=package_root,
        relative_path="workflows/demo.py",
    )
    saved = service.save_draft(
        WORKFLOW_UUID,
        python_source=source,
        expected_draft_hash=None,
        expected_workflow_revision=1,
    )
    assert saved["candidate"] is not None
    return store, service, package_root / "workflows" / "demo.py", saved


def _resolved_dirfd_path(
    value: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    *,
    directory_fd: object,
) -> Path:
    path = Path(os.fsdecode(value))
    if not isinstance(directory_fd, int):
        return path.resolve()
    parent = Path(os.readlink(f"/proc/self/fd/{directory_fd}")).resolve()
    return parent / path


def _file_identity(path: Path) -> tuple[int, int]:
    result = path.stat()
    return result.st_dev, result.st_ino


def _open_canonical(
    source_path: str,
    attempted: Any,
    outcome: Any,
) -> None:
    attempted.set()
    try:
        descriptor = os.open(source_path, os.O_RDONLY | os.O_CLOEXEC)
        try:
            content = os.read(descriptor, 1024)
        finally:
            os.close(descriptor)
    except Exception as exc:  # noqa: BLE001 - 子进程结果显式回传
        outcome.put(("error", type(exc).__name__))
    else:
        outcome.put(("opened", content))


def _write_canonical_in_place(
    source_path: str,
    external_source: bytes,
    attempted: Any,
    outcome: Any,
) -> None:
    attempted.set()
    try:
        path = Path(source_path)
        before = _file_identity(path)
        with path.open("wb") as stream:
            stream.write(external_source)
            stream.flush()
            os.fsync(stream.fileno())
        after = _file_identity(path)
    except Exception as exc:  # noqa: BLE001 - 子进程结果显式回传
        outcome.put(("error", type(exc).__name__))
    else:
        outcome.put(("written", before, after))


def _replace_canonical(
    source_path: str,
    staged_path: str,
    attempted: Any,
    outcome: Any,
) -> None:
    attempted.set()
    try:
        os.replace(staged_path, source_path)
        parent_descriptor = os.open(
            str(Path(source_path).parent),
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
        identity = _file_identity(Path(source_path))
    except Exception as exc:  # noqa: BLE001 - 子进程结果显式回传
        outcome.put(("error", type(exc).__name__))
    else:
        outcome.put(("replaced", identity))


def _queue_result(result_queue: Any) -> tuple[Any, ...] | None:
    try:
        return result_queue.get(timeout=1)
    except queue.Empty:
        return None


def _terminate_if_alive(process: multiprocessing.Process) -> None:
    if not process.is_alive():
        return
    process.terminate()
    process.join(timeout=2)


def test_failed_finalization_preserves_new_inode_external_open_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_source = b"value = 'registered original'\n"
    candidate_source = b"value = 'candidate install'\n"
    external_source = b"value = 'external new-inode write'\n"
    store, service, source_path, saved = _seed_existing_draft(
        tmp_path,
        source=original_source.decode(),
    )
    original_identity = _file_identity(source_path)
    context = multiprocessing.get_context("spawn")
    old_open_attempted = context.Event()
    external_write_attempted = context.Event()
    old_open_outcome = context.Queue()
    external_write_outcome = context.Queue()
    old_opener = context.Process(
        target=_open_canonical,
        args=(
            str(source_path),
            old_open_attempted,
            old_open_outcome,
        ),
        name="old-canonical-lease-breaker",
    )
    external_writer = context.Process(
        target=_write_canonical_in_place,
        args=(
            str(source_path),
            external_source,
            external_write_attempted,
            external_write_outcome,
        ),
        name="new-canonical-external-writer",
    )
    original_replace = os.replace
    scenario: dict[str, Any] = {}

    def publish_then_fail(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        destination_path = _resolved_dirfd_path(
            destination,
            directory_fd=kwargs.get("dst_dir_fd"),
        )
        if destination_path != source_path or scenario.get("injected"):
            original_replace(source, destination, *args, **kwargs)
            return

        pending_before_open = signal.sigpending()
        old_opener.start()
        scenario["old_open_attempted"] = old_open_attempted.wait(timeout=2)
        deadline = time.monotonic() + 2
        while signal.sigpending() == pending_before_open:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.001)
        scenario["old_lease_break_pending"] = signal.sigpending() != pending_before_open

        original_replace(source, destination, *args, **kwargs)
        scenario["candidate_identity"] = _file_identity(source_path)
        scenario["candidate_was_new_inode"] = (
            scenario["candidate_identity"] != original_identity
        )

        external_writer.start()
        scenario["external_write_attempted"] = external_write_attempted.wait(timeout=2)
        external_writer.join(timeout=3)
        scenario["external_writer_exitcode"] = external_writer.exitcode
        scenario["injected"] = True
        raise OSError("deterministic post-install finalization failure")

    monkeypatch.setattr(os, "replace", publish_then_fail)
    try:
        with pytest.raises(WorkflowError) as error:
            service.save_draft(
                WORKFLOW_UUID,
                python_source=candidate_source.decode(),
                expected_draft_hash=saved["draft"]["draft_hash"],
                expected_workflow_revision=1,
            )
    finally:
        store.close()
        old_opener.join(timeout=5)
        _terminate_if_alive(old_opener)
        _terminate_if_alive(external_writer)

    old_open_result = _queue_result(old_open_outcome)
    external_write_result = _queue_result(external_write_outcome)
    old_open_outcome.close()
    external_write_outcome.close()

    assert {
        "error": error.value.code,
        "old_open_attempted": scenario.get("old_open_attempted"),
        "old_lease_break_pending": scenario.get("old_lease_break_pending"),
        "old_opener_exitcode": old_opener.exitcode,
        "old_open_result_kind": (
            old_open_result[0] if old_open_result is not None else None
        ),
        "candidate_was_new_inode": scenario.get("candidate_was_new_inode"),
        "external_write_attempted": scenario.get("external_write_attempted"),
        "external_writer_exitcode": scenario.get("external_writer_exitcode"),
        "external_write_result": external_write_result,
        "canonical_source": source_path.read_bytes(),
    } == {
        "error": "internal_error",
        "old_open_attempted": True,
        "old_lease_break_pending": True,
        "old_opener_exitcode": 0,
        "old_open_result_kind": "opened",
        "candidate_was_new_inode": True,
        "external_write_attempted": True,
        "external_writer_exitcode": 0,
        "external_write_result": (
            "written",
            scenario["candidate_identity"],
            scenario["candidate_identity"],
        ),
        "canonical_source": external_source,
    }


def test_directory_fsync_failure_preserves_external_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_source = b"value = 'registered original'\n"
    candidate_source = b"value = 'candidate before fsync'\n"
    external_source = b"value = 'external atomic replace'\n"
    store, service, source_path, saved = _seed_existing_draft(
        tmp_path,
        source=original_source.decode(),
    )
    original_identity = _file_identity(source_path)
    staged_external_path = source_path.with_name(".external-round6.tmp")
    staged_external_path.write_bytes(external_source)
    staged_descriptor = os.open(
        staged_external_path,
        os.O_RDONLY | os.O_CLOEXEC,
    )
    try:
        os.fsync(staged_descriptor)
    finally:
        os.close(staged_descriptor)

    context = multiprocessing.get_context("spawn")
    replace_attempted = context.Event()
    replace_outcome = context.Queue()
    external_replacer = context.Process(
        target=_replace_canonical,
        args=(
            str(source_path),
            str(staged_external_path),
            replace_attempted,
            replace_outcome,
        ),
        name="external-canonical-replacer",
    )
    original_fsync = os.fsync
    scenario: dict[str, Any] = {}

    def replace_external_during_directory_fsync(descriptor: int) -> None:
        descriptor_stat = os.fstat(descriptor)
        current_identity = _file_identity(source_path) if source_path.exists() else None
        if (
            not scenario.get("injected")
            and stat.S_ISDIR(descriptor_stat.st_mode)
            and current_identity != original_identity
            and source_path.read_bytes() == candidate_source
        ):
            scenario["candidate_identity"] = current_identity
            external_replacer.start()
            scenario["external_replace_attempted"] = replace_attempted.wait(timeout=2)
            external_replacer.join(timeout=3)
            scenario["external_replacer_exitcode"] = external_replacer.exitcode
            scenario["injected"] = True
            raise OSError("deterministic directory fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", replace_external_during_directory_fsync)
    try:
        with pytest.raises(WorkflowError) as error:
            service.save_draft(
                WORKFLOW_UUID,
                python_source=candidate_source.decode(),
                expected_draft_hash=saved["draft"]["draft_hash"],
                expected_workflow_revision=1,
            )
    finally:
        store.close()
        _terminate_if_alive(external_replacer)

    external_replace_result = _queue_result(replace_outcome)
    replace_outcome.close()
    external_replace_result_kind = (
        external_replace_result[0] if external_replace_result is not None else None
    )
    external_replace_identity = (
        external_replace_result[1] if external_replace_result is not None else None
    )

    assert {
        "error": error.value.code,
        "fsync_hook_injected": scenario.get("injected"),
        "external_replace_attempted": scenario.get("external_replace_attempted"),
        "external_replacer_exitcode": scenario.get("external_replacer_exitcode"),
        "external_replace_result_kind": external_replace_result_kind,
        "external_replaced_candidate_inode": (
            external_replace_identity != scenario.get("candidate_identity")
        ),
        "canonical_source": source_path.read_bytes(),
    } == {
        "error": "internal_error",
        "fsync_hook_injected": True,
        "external_replace_attempted": True,
        "external_replacer_exitcode": 0,
        "external_replace_result_kind": "replaced",
        "external_replaced_candidate_inode": True,
        "canonical_source": external_source,
    }
