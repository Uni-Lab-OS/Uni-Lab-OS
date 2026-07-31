"""Phase 01 Round14：Apply 提交后的陈旧 writeback 不得污染新 Draft。"""

from __future__ import annotations

import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
CATALOG_FINGERPRINT = f"sha256:{'f' * 64}"
OLD_SOURCE = "old_draft()"
NEW_SOURCE = "new_draft()"


class SourceOnlyCompiler:
    compiler_version = "phase-01-risk-round14-postcommit-v1"
    template_catalog_fingerprint = CATALOG_FINGERPRINT

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> dict[str, Any]:
        del workflow_uuid, workflow_revision, source_uri
        return {
            "diagnostics": [],
            "graph": deepcopy(applied_graph),
            "normalized_python_source": (
                python_source if python_source.endswith("\n") else python_source + "\n"
            ),
            "source_map": [],
            "changeset": {
                "kind": "source_only",
                "created_node_uuids": [],
                "updated_node_uuids": [],
                "deleted_node_uuids": [],
                "created_edge_uuids": [],
                "updated_edge_uuids": [],
                "deleted_edge_uuids": [],
                "reserved_metadata_changed": False,
            },
            "compiler_version": self.compiler_version,
            "template_catalog_fingerprint": self.template_catalog_fingerprint,
        }


class ApplyCommittedBarrierStore(WorkflowStore):
    """暂停已提交的旧 Apply，放行另一个连接保存新 Draft。"""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.apply_committed = threading.Event()
        self.release_post_commit = threading.Event()

    def apply_authoring_candidate(self, **kwargs: Any) -> tuple[int, str]:
        resulting_revision = super().apply_authoring_candidate(**kwargs)
        self.apply_committed.set()
        if not self.release_post_commit.wait(timeout=3):
            raise TimeoutError("test did not release post-commit Apply")
        return resulting_revision


class SettleWritebackBarrierStore(WorkflowStore):
    """暂停旧 Apply 的 settle，使新 Draft 先更新 marker。"""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.settle_entered = threading.Event()
        self.release_settle = threading.Event()

    def settle_writeback(self, **kwargs: Any) -> None:
        self.settle_entered.set()
        if not self.release_settle.wait(timeout=3):
            raise TimeoutError("test did not release stale settle_writeback")
        super().settle_writeback(**kwargs)


def _seed_authoring(
    store: WorkflowStore,
    tmp_path: Path,
) -> tuple[WorkflowService, Path]:
    service = WorkflowService(store, compiler=SourceOnlyCompiler())
    service.create_workflow(
        name="phase 01 risk round 14 postcommit",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase_01_risk_round_14_postcommit",
        package_root=package_root,
        relative_path="workflows/review.py",
    )
    return service, package_root / "workflows" / "review.py"


def _save_old_candidate(service: WorkflowService) -> dict[str, Any]:
    saved = service.save_draft(
        WORKFLOW_UUID,
        python_source=OLD_SOURCE,
        expected_draft_hash=None,
        expected_workflow_revision=1,
    )
    assert saved["candidate"] is not None
    return saved


def _start_old_apply(
    service: WorkflowService,
    saved: dict[str, Any],
) -> tuple[threading.Thread, dict[str, Any]]:
    outcome: dict[str, Any] = {}

    def apply() -> None:
        try:
            outcome["result"] = service.apply_authoring(
                WORKFLOW_UUID,
                expected_draft_hash=saved["draft"]["draft_hash"],
                expected_workflow_revision=1,
                expected_candidate_hash=saved["candidate"]["candidate_hash"],
            )
        except WorkflowError as error:
            outcome["error"] = {
                "status": error.status,
                "code": error.code,
            }
        except Exception as error:  # noqa: BLE001 - 暴露线程异常泄漏
            outcome["unexpected"] = type(error).__name__

    thread = threading.Thread(target=apply, name="round14-postcommit-old-apply")
    thread.start()
    return thread, outcome


def _marker(store: WorkflowStore) -> dict[str, Any]:
    record = store.get_authoring_record(WORKFLOW_UUID)
    candidate = record["candidate"]
    return {
        "observed_draft_hash": record["observed_draft_hash"],
        "candidate_hash": record["candidate_hash"],
        "candidate_draft_hash": (
            candidate["draft_hash"] if candidate is not None else None
        ),
        "writeback_status": record["writeback_status"],
        "writeback_source": record["writeback_source"],
        "writeback_expected_hash": record["writeback_expected_hash"],
    }


def _authority(
    store: WorkflowStore,
    service: WorkflowService,
    source_path: Path,
) -> dict[str, Any]:
    authoring = service.get_authoring(WORKFLOW_UUID)
    return {
        "state": authoring["state"],
        "draft_hash": authoring["draft"]["draft_hash"],
        "candidate_hash": (
            authoring["candidate"]["candidate_hash"]
            if authoring["candidate"] is not None
            else None
        ),
        "marker": _marker(store),
        "reconciliation_pending": service.source_reconciliation_pending(WORKFLOW_UUID),
        "canonical": source_path.read_bytes(),
    }


def test_stale_mark_pending_cannot_pollute_new_draft_across_restart(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    old_store = ApplyCommittedBarrierStore(database_path)
    new_store: WorkflowStore | None = None
    restart_store: WorkflowStore | None = None
    apply_thread: threading.Thread | None = None
    try:
        old_service, source_path = _seed_authoring(old_store, tmp_path)
        old_saved = _save_old_candidate(old_service)
        new_store = WorkflowStore(database_path)
        new_service = WorkflowService(new_store, compiler=SourceOnlyCompiler())

        apply_thread, old_outcome = _start_old_apply(old_service, old_saved)
        apply_commit_observed = old_store.apply_committed.wait(timeout=2)
        assert apply_commit_observed

        new_saved = new_service.save_draft(
            WORKFLOW_UUID,
            python_source=NEW_SOURCE,
            expected_draft_hash=old_saved["draft"]["draft_hash"],
            expected_workflow_revision=1,
        )
        assert new_saved["candidate"] is not None
        new_authority = _authority(new_store, new_service, source_path)

        old_store.release_post_commit.set()
        apply_thread.join(timeout=3)
        old_apply_finished = not apply_thread.is_alive()
        apply_thread = None
        after_old_apply = _authority(new_store, new_service, source_path)

        old_store.close()
        new_store.close()
        new_store = None
        restart_store = WorkflowStore(database_path)
        restart_service = WorkflowService(
            restart_store,
            compiler=SourceOnlyCompiler(),
        )
        restarted = restart_service.reconcile_registered_source(WORKFLOW_UUID)
        after_restart = _authority(
            restart_store,
            restart_service,
            source_path,
        )
    finally:
        old_store.release_post_commit.set()
        if apply_thread is not None:
            apply_thread.join(timeout=3)
        if new_store is not None:
            new_store.close()
        if restart_store is not None:
            restart_store.close()
        else:
            old_store.close()

    expected_marker = {
        "observed_draft_hash": new_saved["draft"]["draft_hash"],
        "candidate_hash": new_saved["candidate"]["candidate_hash"],
        "candidate_draft_hash": new_saved["draft"]["draft_hash"],
        "writeback_status": "settled",
        "writeback_source": None,
        "writeback_expected_hash": None,
    }
    expected_authority = {
        "state": "unapplied_source_only",
        "draft_hash": new_saved["draft"]["draft_hash"],
        "candidate_hash": new_saved["candidate"]["candidate_hash"],
        "marker": expected_marker,
        "reconciliation_pending": False,
        "canonical": NEW_SOURCE.encode(),
    }
    assert {
        "apply_commit_observed": apply_commit_observed,
        "old_apply_finished": old_apply_finished,
        "old_apply_warning_codes": [
            item["code"] for item in old_outcome["result"]["apply_result"]["warnings"]
        ],
        "new_authority": new_authority,
        "after_old_apply": after_old_apply,
        "after_restart": after_restart,
        "restart_state": restarted["state"],
    } == {
        "apply_commit_observed": True,
        "old_apply_finished": True,
        "old_apply_warning_codes": ["draft_writeback_pending"],
        "new_authority": expected_authority,
        "after_old_apply": expected_authority,
        "after_restart": expected_authority,
        "restart_state": "unapplied_source_only",
    }


def test_stale_settle_cannot_replace_new_draft_marker(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    old_store = SettleWritebackBarrierStore(database_path)
    new_store: WorkflowStore | None = None
    restart_store: WorkflowStore | None = None
    apply_thread: threading.Thread | None = None
    try:
        old_service, source_path = _seed_authoring(old_store, tmp_path)
        old_saved = _save_old_candidate(old_service)
        new_store = WorkflowStore(database_path)
        new_service = WorkflowService(new_store, compiler=SourceOnlyCompiler())

        apply_thread, old_outcome = _start_old_apply(old_service, old_saved)
        settle_observed = old_store.settle_entered.wait(timeout=2)
        assert settle_observed

        written_old = new_service.get_authoring(WORKFLOW_UUID)
        new_saved = new_service.save_draft(
            WORKFLOW_UUID,
            python_source=NEW_SOURCE,
            expected_draft_hash=written_old["draft"]["draft_hash"],
            expected_workflow_revision=1,
        )
        assert new_saved["candidate"] is not None
        new_authority = _authority(new_store, new_service, source_path)

        old_store.release_settle.set()
        apply_thread.join(timeout=3)
        old_apply_finished = not apply_thread.is_alive()
        apply_thread = None
        after_old_apply = _authority(new_store, new_service, source_path)

        old_store.close()
        new_store.close()
        new_store = None
        restart_store = WorkflowStore(database_path)
        restart_service = WorkflowService(
            restart_store,
            compiler=SourceOnlyCompiler(),
        )
        restarted = restart_service.reconcile_registered_source(WORKFLOW_UUID)
        after_restart = _authority(
            restart_store,
            restart_service,
            source_path,
        )
    finally:
        old_store.release_settle.set()
        if apply_thread is not None:
            apply_thread.join(timeout=3)
        if new_store is not None:
            new_store.close()
        if restart_store is not None:
            restart_store.close()
        else:
            old_store.close()

    expected_marker = {
        "observed_draft_hash": new_saved["draft"]["draft_hash"],
        "candidate_hash": new_saved["candidate"]["candidate_hash"],
        "candidate_draft_hash": new_saved["draft"]["draft_hash"],
        "writeback_status": "settled",
        "writeback_source": None,
        "writeback_expected_hash": None,
    }
    expected_authority = {
        "state": "unapplied_source_only",
        "draft_hash": new_saved["draft"]["draft_hash"],
        "candidate_hash": new_saved["candidate"]["candidate_hash"],
        "marker": expected_marker,
        "reconciliation_pending": False,
        "canonical": NEW_SOURCE.encode(),
    }
    assert {
        "settle_observed": settle_observed,
        "old_apply_finished": old_apply_finished,
        "old_apply_failed": "error" in old_outcome or "unexpected" in old_outcome,
        "new_authority": new_authority,
        "after_old_apply": after_old_apply,
        "after_restart": after_restart,
        "restart_state": restarted["state"],
    } == {
        "settle_observed": True,
        "old_apply_finished": True,
        "old_apply_failed": False,
        "new_authority": expected_authority,
        "after_old_apply": expected_authority,
        "after_restart": expected_authority,
        "restart_state": "unapplied_source_only",
    }


def test_reconcile_clears_malformed_pending_for_already_projected_new_draft(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    seed_store: WorkflowStore | None = WorkflowStore(database_path)
    restart_store: WorkflowStore | None = None
    try:
        seed_service, source_path = _seed_authoring(seed_store, tmp_path)
        saved = seed_service.save_draft(
            WORKFLOW_UUID,
            python_source=NEW_SOURCE,
            expected_draft_hash=None,
            expected_workflow_revision=1,
        )
        assert saved["candidate"] is not None
        settled_authority = _authority(seed_store, seed_service, source_path)

        with seed_store.transaction() as connection:
            connection.execute(
                """
                UPDATE workflow_authoring
                SET writeback_status = 'pending',
                    writeback_source = NULL,
                    writeback_expected_hash = NULL
                WHERE workflow_uuid = ?
                """,
                (WORKFLOW_UUID,),
            )
        malformed_authority = _authority(seed_store, seed_service, source_path)

        seed_store.close()
        seed_store = None
        restart_store = WorkflowStore(database_path)
        restart_service = WorkflowService(
            restart_store,
            compiler=SourceOnlyCompiler(),
        )
        reconciled = restart_service.reconcile_registered_source(WORKFLOW_UUID)
        after_reconcile = _authority(
            restart_store,
            restart_service,
            source_path,
        )
    finally:
        if seed_store is not None:
            seed_store.close()
        if restart_store is not None:
            restart_store.close()

    expected_marker = {
        "observed_draft_hash": saved["draft"]["draft_hash"],
        "candidate_hash": saved["candidate"]["candidate_hash"],
        "candidate_draft_hash": saved["draft"]["draft_hash"],
        "writeback_status": "settled",
        "writeback_source": None,
        "writeback_expected_hash": None,
    }
    expected_authority = {
        "state": "unapplied_source_only",
        "draft_hash": saved["draft"]["draft_hash"],
        "candidate_hash": saved["candidate"]["candidate_hash"],
        "marker": expected_marker,
        "reconciliation_pending": False,
        "canonical": NEW_SOURCE.encode(),
    }
    malformed_marker = {
        **expected_marker,
        "writeback_status": "pending",
    }
    assert {
        "settled_authority": settled_authority,
        "malformed_authority": malformed_authority,
        "reconciled_state": reconciled["state"],
        "after_reconcile": after_reconcile,
    } == {
        "settled_authority": expected_authority,
        "malformed_authority": {
            **expected_authority,
            "marker": malformed_marker,
            "reconciliation_pending": True,
        },
        "reconciled_state": "unapplied_source_only",
        "after_reconcile": expected_authority,
    }
