"""Phase 01 第五轮 Authoring 跨进程与 crash consistency 风险测试。"""

from __future__ import annotations

import multiprocessing
import os
import queue
import signal
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
CATALOG_FINGERPRINT = "sha256:" + ("a" * 64)
CRASH_EXIT_CODE = 86


class SourceOnlyCompiler:
    compiler_version = "phase-01-risk-round5-v1"
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
) -> tuple[Path, Path, dict[str, Any]]:
    database_path = tmp_path / "workflow.db"
    package_root = tmp_path / "package"
    package_root.mkdir()
    store = WorkflowStore(database_path)
    service = WorkflowService(store, compiler=SourceOnlyCompiler())
    try:
        service.create_workflow(
            name="round-5-risk-workflow",
            tags=[],
            description=None,
            meta_data={},
            workflow_uuid=WORKFLOW_UUID,
        )
        service.register_editable_source(
            workflow_uuid=WORKFLOW_UUID,
            package_id="phase01_round5_package",
            package_root=package_root,
            relative_path="workflows/demo.py",
        )
        saved = service.save_draft(
            WORKFLOW_UUID,
            python_source=source,
            expected_draft_hash=None,
            expected_workflow_revision=1,
        )
    finally:
        store.close()
    assert saved["candidate"] is not None
    return database_path, package_root / "workflows" / "demo.py", saved


def _holder_save_draft(
    database_path: str,
    expected_draft_hash: str,
    lease_acquired_sender: Any,
    release_holder_receiver: Any,
    outcome: Any,
) -> None:
    import unilabos.workflow.service as service_module

    signal.signal(signal.SIGIO, signal.SIG_DFL)
    original_fcntl = service_module.fcntl.fcntl

    def pause_after_lease(
        descriptor: int,
        command: int,
        argument: int = 0,
    ) -> int:
        result = original_fcntl(descriptor, command, argument)
        if (
            command == service_module.fcntl.F_SETLEASE
            and argument == service_module.fcntl.F_WRLCK
        ):
            lease_acquired_sender.send("lease_acquired")
            if not release_holder_receiver.poll(timeout=5):
                raise TimeoutError("test did not release lease holder")
            assert release_holder_receiver.recv() == "release"
        return result

    service_module.fcntl.fcntl = pause_after_lease
    store = WorkflowStore(Path(database_path))
    service = WorkflowService(store, compiler=SourceOnlyCompiler())
    try:
        try:
            service.save_draft(
                WORKFLOW_UUID,
                python_source="value = 'holder replacement'\n",
                expected_draft_hash=expected_draft_hash,
                expected_workflow_revision=1,
            )
        except WorkflowError as exc:
            outcome.put(("error", exc.code))
        except Exception as exc:  # noqa: BLE001 - 子进程结果显式回传
            outcome.put(("unexpected", type(exc).__name__))
        else:
            outcome.put(("success", None))
    finally:
        store.close()


def _external_truncate(
    source_path: str,
    external_source: str,
    open_attempted: Any,
    outcome: Any,
) -> None:
    open_attempted.set()
    try:
        with Path(source_path).open("wb") as stream:
            stream.write(external_source.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
    except Exception as exc:  # noqa: BLE001 - 子进程结果显式回传
        outcome.put(("error", type(exc).__name__))
    else:
        outcome.put(("written", None))


def _resolved_dirfd_path(
    value: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    *,
    directory_fd: object,
) -> Path:
    path = Path(os.fsdecode(value))
    if not isinstance(directory_fd, int):
        return path
    parent = Path(os.readlink(f"/proc/self/fd/{directory_fd}")).resolve()
    return parent / path


def _crash_save_before_atomic_replace(
    database_path: str,
    source_path: str,
    expected_draft_hash: str,
    crash_injected: Any,
) -> None:
    import unilabos.workflow.service as service_module

    canonical_path = Path(source_path)
    original_replace = service_module.os.replace
    original_write = service_module.WorkflowService._write_regular_fd

    def crash_before_replace(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        destination_path = _resolved_dirfd_path(
            destination,
            directory_fd=kwargs.get("dst_dir_fd"),
        )
        if destination_path == canonical_path:
            crash_injected.set()
            os._exit(CRASH_EXIT_CODE)
        original_replace(source, destination, *args, **kwargs)

    def expose_in_place_write(
        descriptor: int,
        content: bytes,
    ) -> None:
        descriptor_stat = os.fstat(descriptor)
        canonical_stat = canonical_path.stat()
        if (
            descriptor_stat.st_dev == canonical_stat.st_dev
            and descriptor_stat.st_ino == canonical_stat.st_ino
        ):
            prefix = content[: max(1, len(content) // 2)]
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, prefix)
            os.fsync(descriptor)
            crash_injected.set()
            os._exit(CRASH_EXIT_CODE)
        original_write(descriptor, content)

    service_module.os.replace = crash_before_replace
    service_module.WorkflowService._write_regular_fd = staticmethod(
        expose_in_place_write
    )
    store = WorkflowStore(Path(database_path))
    service = WorkflowService(store, compiler=SourceOnlyCompiler())
    try:
        service.save_draft(
            WORKFLOW_UUID,
            python_source="value = 'replacement that must stay hidden'\n",
            expected_draft_hash=expected_draft_hash,
            expected_workflow_revision=1,
        )
    finally:
        store.close()


def _queue_result(result_queue: Any) -> tuple[str, str | None] | None:
    try:
        return result_queue.get(timeout=1)
    except queue.Empty:
        return None


def _terminate_if_alive(process: multiprocessing.Process) -> None:
    if not process.is_alive():
        return
    process.terminate()
    process.join(timeout=2)


def _release_holder(release_sender: Any) -> None:
    try:
        release_sender.send("release")
    except (BrokenPipeError, EOFError, OSError):
        pass


def test_cross_process_lease_break_survives_and_preserves_external_draft(
    tmp_path: Path,
) -> None:
    database_path, source_path, saved = _seed_existing_draft(
        tmp_path,
        source="value = 'original'\n",
    )
    external_source = "value = 'external process wins'\n"
    context = multiprocessing.get_context("spawn")
    lease_acquired_receiver, lease_acquired_sender = context.Pipe(duplex=False)
    release_holder_receiver, release_holder_sender = context.Pipe(duplex=False)
    writer_open_attempted = context.Event()
    holder_outcome = context.Queue()
    writer_outcome = context.Queue()
    holder = context.Process(
        target=_holder_save_draft,
        args=(
            str(database_path),
            saved["draft"]["draft_hash"],
            lease_acquired_sender,
            release_holder_receiver,
            holder_outcome,
        ),
        name="workflow-lease-holder",
    )
    writer = context.Process(
        target=_external_truncate,
        args=(
            str(source_path),
            external_source,
            writer_open_attempted,
            writer_outcome,
        ),
        name="external-draft-writer",
    )

    holder.start()
    lease_acquired_sender.close()
    release_holder_receiver.close()
    try:
        assert lease_acquired_receiver.poll(timeout=2)
        assert lease_acquired_receiver.recv() == "lease_acquired"
        writer.start()
        assert writer_open_attempted.wait(timeout=2)
        holder.join(timeout=1)
        survived_lease_break = holder.is_alive()
        _release_holder(release_holder_sender)
        holder.join(timeout=5)
        writer.join(timeout=5)
        holder_result = _queue_result(holder_outcome) if holder.exitcode == 0 else None
        writer_result = _queue_result(writer_outcome) if writer.exitcode == 0 else None
    finally:
        _release_holder(release_holder_sender)
        _terminate_if_alive(holder)
        _terminate_if_alive(writer)
        lease_acquired_receiver.close()
        release_holder_sender.close()
        holder_outcome.close()
        writer_outcome.close()

    assert {
        "holder_survived_break": survived_lease_break,
        "holder_exitcode": holder.exitcode,
        "holder_result": holder_result,
        "writer_exitcode": writer.exitcode,
        "writer_result": writer_result,
        "canonical_source": source_path.read_text(encoding="utf-8"),
    } == {
        "holder_survived_break": True,
        "holder_exitcode": 0,
        "holder_result": ("error", "draft_hash_conflict"),
        "writer_exitcode": 0,
        "writer_result": ("written", None),
        "canonical_source": external_source,
    }


def test_existing_draft_crash_before_replace_keeps_complete_original(
    tmp_path: Path,
) -> None:
    original_source = "value = 'complete original'\n"
    database_path, source_path, saved = _seed_existing_draft(
        tmp_path,
        source=original_source,
    )
    original_bytes = source_path.read_bytes()
    context = multiprocessing.get_context("spawn")
    crash_injected = context.Event()
    worker = context.Process(
        target=_crash_save_before_atomic_replace,
        args=(
            str(database_path),
            str(source_path),
            saved["draft"]["draft_hash"],
            crash_injected,
        ),
        name="workflow-crash-before-replace",
    )

    worker.start()
    try:
        assert crash_injected.wait(timeout=3)
        worker.join(timeout=3)
    finally:
        _terminate_if_alive(worker)

    assert worker.exitcode == CRASH_EXIT_CODE
    assert source_path.read_bytes() == original_bytes
    assert source_path.read_text(encoding="utf-8") == original_source


@pytest.mark.parametrize("operation", ["get", "reconcile"])
def test_missing_draft_does_not_revive_untrusted_cas_artifacts(
    tmp_path: Path,
    operation: str,
) -> None:
    database_path, source_path, _saved = _seed_existing_draft(
        tmp_path,
        source="value = 'registered original'\n",
    )
    source_path.unlink()
    artifacts = {
        f".{source_path.name}.arbitrary.cas": (b"value = 'arbitrary artifact'\n"),
        f".{source_path.name}.residual.cas": (
            b"value = 'residual original-shaped artifact'\n"
        ),
    }
    for name, content in artifacts.items():
        (source_path.parent / name).write_bytes(content)

    store = WorkflowStore(database_path)
    service = WorkflowService(store, compiler=SourceOnlyCompiler())
    try:
        if operation == "get":
            aggregate = service.get_authoring(WORKFLOW_UUID)
        else:
            aggregate = service.reconcile_registered_source(WORKFLOW_UUID)
    finally:
        store.close()

    observed_artifacts = {
        path.name: path.read_bytes()
        for path in source_path.parent.glob(f".{source_path.name}.*.cas")
    }
    assert {
        "state": aggregate["state"],
        "draft": aggregate["draft"],
        "candidate": aggregate["candidate"],
        "canonical_exists": source_path.exists(),
        "artifacts": observed_artifacts,
    } == {
        "state": "draft_missing",
        "draft": None,
        "candidate": None,
        "canonical_exists": False,
        "artifacts": artifacts,
    }
