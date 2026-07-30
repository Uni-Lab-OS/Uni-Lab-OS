"""Phase 01 第四轮 Authoring CAS 与 Apply 降级风险回归测试。"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
CATALOG_FINGERPRINT = "sha256:" + ("f" * 64)
WRITEBACK_WARNING = {
    "code": "draft_writeback_pending",
    "message": ("工作流已应用，但本地源码同步失败；OS 已保留可恢复的源码记录。"),
}
POST_COMMIT_RESERVED = {
    "input_contract": {"version": 1, "parameters": []},
    "output_contract": {"version": 1, "outputs": []},
    "output_bindings": {},
}


class SourceOnlyCompiler:
    compiler_version = "phase-01-risk-round4-source-v1"
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


class GraphCompiler:
    compiler_version = "phase-01-risk-round4-graph-v1"
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
        del source_uri
        return CandidateCompilation(
            diagnostics=[],
            graph={
                "workflow": {
                    **applied_graph["workflow"],
                    "uuid": workflow_uuid,
                    "revision": workflow_revision,
                    "meta_data": {
                        "color": "candidate-only",
                        "unilab": POST_COMMIT_RESERVED,
                    },
                },
                "nodes": [],
                "edges": [],
                "node_templates": [],
                "handle_templates": [],
            },
            normalized_python_source=(
                python_source if python_source.endswith("\n") else python_source + "\n"
            ),
            source_map=[],
            changeset={
                "kind": "graph",
                "created_node_uuids": [],
                "updated_node_uuids": [],
                "deleted_node_uuids": [],
                "created_edge_uuids": [],
                "updated_edge_uuids": [],
                "deleted_edge_uuids": [],
                "reserved_metadata_changed": True,
            },
            compiler_version=self.compiler_version,
            template_catalog_fingerprint=self.template_catalog_fingerprint,
        )


def _create_workflow(
    service: WorkflowService,
    *,
    meta_data: dict[str, Any] | None = None,
) -> None:
    service.create_workflow(
        name="round-4-risk-workflow",
        tags=[],
        description=None,
        meta_data={} if meta_data is None else meta_data,
        workflow_uuid=WORKFLOW_UUID,
    )


def _save_candidate(
    service: WorkflowService,
    package_root: Path,
    *,
    source: str,
    meta_data: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    _create_workflow(service, meta_data=meta_data)
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase01_round4_package",
        package_root=package_root,
        relative_path="workflows/demo.py",
    )
    source_path = package_root / "workflows" / "demo.py"
    saved = service.save_draft(
        WORKFLOW_UUID,
        python_source=source,
        expected_draft_hash=None,
        expected_workflow_revision=1,
    )
    assert saved["candidate"] is not None
    return source_path, saved


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


def _directory_for_fd(value: object) -> Path | None:
    if not isinstance(value, int):
        return None
    try:
        return Path(os.readlink(f"/proc/self/fd/{value}")).resolve()
    except OSError:
        return None


def _dirfd_path(
    value: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    *,
    directory_fd: object,
) -> Path:
    path = Path(os.fsdecode(value))
    parent = _directory_for_fd(directory_fd)
    return parent / path if parent is not None else path


def test_apply_preserves_old_fd_write_at_cas_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store, compiler=SourceOnlyCompiler())
    package_root = tmp_path / "package"
    package_root.mkdir()
    source_path, saved = _save_candidate(
        service,
        package_root,
        source="value = 'candidate'",
    )
    finalization_entered = threading.Event()
    allow_finalization = threading.Event()
    original_unlink = os.unlink
    outcome: dict[str, Any] = {}

    def gated_unlink(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        absolute_path = _dirfd_path(
            path,
            directory_fd=kwargs.get("dir_fd"),
        )
        if (
            absolute_path.parent == source_path.parent
            and absolute_path.suffix == ".cas"
        ):
            finalization_entered.set()
            if not allow_finalization.wait(timeout=2):
                raise TimeoutError("test did not release CAS finalization")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(
        "unilabos.workflow.service.os.unlink",
        gated_unlink,
    )
    descriptor = os.open(source_path, os.O_RDWR)

    def apply() -> None:
        try:
            outcome["result"] = _apply_saved(service, saved)
        except Exception as exc:  # noqa: BLE001 - 后台异常由主测试断言
            outcome["error"] = exc

    thread = threading.Thread(target=apply, name="apply-cas-finalization")
    thread.start()
    external_source = "value = 'external write during finalization'\n"
    try:
        assert finalization_entered.wait(timeout=1)
        external_bytes = external_source.encode("utf-8")
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, external_bytes)
        os.ftruncate(descriptor, len(external_bytes))
        os.fsync(descriptor)
        allow_finalization.set()
        thread.join(timeout=2)
    finally:
        allow_finalization.set()
        os.close(descriptor)
        thread.join(timeout=2)

    try:
        assert not thread.is_alive()
        assert "error" not in outcome
        result = outcome["result"]
        aggregate = service.get_authoring(WORKFLOW_UUID)
        record = store.get_authoring_record(WORKFLOW_UUID)
    finally:
        store.close()

    assert result["apply_result"]["warnings"] == [WRITEBACK_WARNING]
    assert aggregate["draft"]["python_source"] == external_source
    assert record["writeback_status"] == "pending"


def test_cas_restore_failure_retains_the_only_original_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store, compiler=SourceOnlyCompiler())
    package_root = tmp_path / "package"
    package_root.mkdir()
    source_path, saved = _save_candidate(
        service,
        package_root,
        source="value = 'original draft'\n",
    )
    original_bytes = source_path.read_bytes()
    original_link = os.link
    install_conflicts = 0
    restore_failures = 0

    def conflicting_link(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        nonlocal install_conflicts, restore_failures
        source_name = Path(os.fsdecode(source))
        destination_path = _dirfd_path(
            destination,
            directory_fd=kwargs.get("dst_dir_fd"),
        )
        if destination_path == source_path and source_name.suffix == ".tmp":
            install_conflicts += 1
            destination_fd = kwargs.get("dst_dir_fd")
            assert isinstance(destination_fd, int)
            intruder = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=destination_fd,
            )
            os.close(intruder)
            try:
                original_link(source, destination, *args, **kwargs)
            finally:
                os.unlink(destination, dir_fd=destination_fd)
        if destination_path == source_path and source_name.suffix == ".cas":
            restore_failures += 1
            raise OSError("deterministic backup restore failure")
        original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(
        "unilabos.workflow.service.os.link",
        conflicting_link,
    )
    try:
        with pytest.raises(WorkflowError) as error:
            service.save_draft(
                WORKFLOW_UUID,
                python_source="value = 'replacement draft'\n",
                expected_draft_hash=saved["draft"]["draft_hash"],
                expected_workflow_revision=1,
            )
    finally:
        store.close()

    backup_paths = sorted(source_path.parent.glob(f".{source_path.name}.*.cas"))
    surviving_contents = [
        path.read_bytes()
        for path in ([source_path] if source_path.exists() else []) + backup_paths
    ]

    assert error.value.code == "internal_error"
    assert install_conflicts == 1
    assert restore_failures >= 1
    assert original_bytes in surviving_contents
    assert source_path.exists() or backup_paths


def test_graph_apply_fallback_uses_post_commit_facts_without_writeback_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store, compiler=GraphCompiler())
    package_root = tmp_path / "package"
    package_root.mkdir()
    source_path, saved = _save_candidate(
        service,
        package_root,
        source="build()",
        meta_data={"color": "before"},
    )

    def fail_authoring_hydration(
        workflow_uuid: str,
    ) -> dict[str, Any]:
        del workflow_uuid
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        service,
        "get_authoring",
        fail_authoring_hydration,
    )
    try:
        result = _apply_saved(service, saved)
        persisted_graph = service.get_graph(WORKFLOW_UUID)
        record = store.get_authoring_record(WORKFLOW_UUID)
    finally:
        store.close()

    authoring = result["authoring"]
    applied_graph = authoring["applied_graph"]
    observed = {
        "warnings": result["apply_result"]["warnings"],
        "revisions": (
            result["apply_result"]["workflow_revision"],
            authoring["workflow_revision"],
            applied_graph["workflow"]["revision"],
        ),
        "applied_graph_is_persisted": applied_graph == persisted_graph,
        "meta_data": applied_graph["workflow"]["meta_data"],
        "writeback_status": record["writeback_status"],
        "draft_source": source_path.read_text(encoding="utf-8"),
    }
    assert observed == {
        "warnings": [],
        "revisions": (2, 2, 2),
        "applied_graph_is_persisted": True,
        "meta_data": {
            "color": "before",
            "unilab": POST_COMMIT_RESERVED,
        },
        "writeback_status": "settled",
        "draft_source": "build()\n",
    }
