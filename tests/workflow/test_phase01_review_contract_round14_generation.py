"""Phase 01 Round14：writeback CAS 必须区分 Apply generation。"""

from __future__ import annotations

import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
SOURCE = "same_source()"
NORMALIZED_SOURCE = "normalized_source()\n"
BLOCKING_SOURCE = "concurrent_edit()"
FIRST_CATALOG = f"sha256:{'a' * 64}"
SECOND_CATALOG = f"sha256:{'b' * 64}"


class FixedSourceOnlyCompiler:
    def __init__(self, *, compiler_version: str, catalog_fingerprint: str) -> None:
        self.compiler_version = compiler_version
        self.template_catalog_fingerprint = catalog_fingerprint

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> dict[str, Any]:
        del workflow_uuid, workflow_revision, python_source, source_uri
        return {
            "diagnostics": [],
            "graph": deepcopy(applied_graph),
            "normalized_python_source": NORMALIZED_SOURCE,
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


class FirstSettleBarrierStore(WorkflowStore):
    """暂停第一次 Apply 的 post-commit settle。"""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.settle_entered = threading.Event()
        self.release_settle = threading.Event()

    def settle_writeback(self, **kwargs: Any) -> bool:
        self.settle_entered.set()
        if not self.release_settle.wait(timeout=5):
            raise TimeoutError("test did not release first Apply settle")
        return super().settle_writeback(**kwargs)


class SecondCommitBarrierStore(WorkflowStore):
    """暂停第二次已提交的 Apply，使其文件写回确定性失败。"""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.apply_committed = threading.Event()
        self.release_post_commit = threading.Event()

    def apply_authoring_candidate(self, **kwargs: Any) -> int:
        resulting_revision = super().apply_authoring_candidate(**kwargs)
        self.apply_committed.set()
        if not self.release_post_commit.wait(timeout=5):
            raise TimeoutError("test did not release second post-commit Apply")
        return resulting_revision


def _compiler(*, generation: str) -> FixedSourceOnlyCompiler:
    if generation == "first":
        return FixedSourceOnlyCompiler(
            compiler_version="round14-generation-first",
            catalog_fingerprint=FIRST_CATALOG,
        )
    return FixedSourceOnlyCompiler(
        compiler_version="round14-generation-second",
        catalog_fingerprint=SECOND_CATALOG,
    )


def _seed_authoring(
    store: WorkflowStore,
    tmp_path: Path,
) -> tuple[WorkflowService, Path]:
    service = WorkflowService(store, compiler=_compiler(generation="first"))
    service.create_workflow(
        name="phase 01 round 14 generation",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase_01_round_14_generation",
        package_root=package_root,
        relative_path="workflows/review.py",
    )
    return service, package_root / "workflows" / "review.py"


def _start_apply(
    *,
    name: str,
    service: WorkflowService,
    candidate: dict[str, Any],
) -> tuple[threading.Thread, dict[str, Any]]:
    outcome: dict[str, Any] = {}

    def apply() -> None:
        try:
            outcome["result"] = service.apply_authoring(
                WORKFLOW_UUID,
                expected_draft_hash=candidate["draft"]["draft_hash"],
                expected_workflow_revision=1,
                expected_candidate_hash=candidate["candidate"]["candidate_hash"],
            )
        except WorkflowError as error:
            outcome["error"] = {
                "status": error.status,
                "code": error.code,
            }
        except Exception as error:  # noqa: BLE001 - 暴露线程异常泄漏
            outcome["unexpected"] = type(error).__name__

    thread = threading.Thread(target=apply, name=name)
    thread.start()
    return thread, outcome


def _authoring_projection(
    service: WorkflowService,
    source_path: Path,
) -> dict[str, Any]:
    authoring = service.get_authoring(WORKFLOW_UUID)
    applied_source = authoring["applied_source"]
    return {
        "state": authoring["state"],
        "draft_source": authoring["draft"]["python_source"],
        "candidate_hash": (
            authoring["candidate"]["candidate_hash"]
            if authoring["candidate"] is not None
            else None
        ),
        "applied_compiler": (
            applied_source["compiler_version"] if applied_source is not None else None
        ),
        "applied_catalog": (
            applied_source["template_catalog_fingerprint"]
            if applied_source is not None
            else None
        ),
        "reconciliation_pending": service.source_reconciliation_pending(WORKFLOW_UUID),
        "canonical": source_path.read_text(encoding="utf-8"),
    }


def test_stale_settle_cannot_clear_new_apply_generation_with_same_source_pair(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    first_store = FirstSettleBarrierStore(database_path)
    second_store: SecondCommitBarrierStore | None = None
    restart_store: WorkflowStore | None = None
    first_thread: threading.Thread | None = None
    second_thread: threading.Thread | None = None
    try:
        first_service, source_path = _seed_authoring(first_store, tmp_path)
        first_candidate = first_service.save_draft(
            WORKFLOW_UUID,
            python_source=SOURCE,
            expected_draft_hash=None,
            expected_workflow_revision=1,
        )
        assert first_candidate["candidate"] is not None

        second_store = SecondCommitBarrierStore(database_path)
        second_service = WorkflowService(
            second_store,
            compiler=_compiler(generation="second"),
        )

        first_thread, first_outcome = _start_apply(
            name="round14-generation-first-apply",
            service=first_service,
            candidate=first_candidate,
        )
        first_settle_observed = first_store.settle_entered.wait(timeout=2)
        assert first_settle_observed

        normalized_draft = second_service.get_authoring(WORKFLOW_UUID)["draft"]
        assert normalized_draft["python_source"] == NORMALIZED_SOURCE
        second_candidate = second_service.save_draft(
            WORKFLOW_UUID,
            python_source=SOURCE,
            expected_draft_hash=normalized_draft["draft_hash"],
            expected_workflow_revision=1,
        )
        assert second_candidate["candidate"] is not None
        assert (
            second_candidate["draft"]["draft_hash"]
            == first_candidate["draft"]["draft_hash"]
        )
        assert (
            second_candidate["candidate"]["normalized_python_source"]
            == first_candidate["candidate"]["normalized_python_source"]
            == NORMALIZED_SOURCE
        )
        assert (
            second_candidate["candidate"]["candidate_hash"]
            != first_candidate["candidate"]["candidate_hash"]
        )
        assert {
            first_candidate["candidate"]["template_catalog_fingerprint"],
            second_candidate["candidate"]["template_catalog_fingerprint"],
        } == {FIRST_CATALOG, SECOND_CATALOG}
        assert (
            first_candidate["candidate"]["compiler_version"]
            != second_candidate["candidate"]["compiler_version"]
        )

        second_thread, second_outcome = _start_apply(
            name="round14-generation-second-apply",
            service=second_service,
            candidate=second_candidate,
        )
        second_commit_observed = second_store.apply_committed.wait(timeout=2)
        assert second_commit_observed

        source_path.write_text(BLOCKING_SOURCE, encoding="utf-8")
        second_store.release_post_commit.set()
        second_thread.join(timeout=3)
        second_apply_finished = not second_thread.is_alive()
        second_thread = None
        source_path.write_text(SOURCE, encoding="utf-8")
        pending_before_stale_settle = second_service.source_reconciliation_pending(
            WORKFLOW_UUID
        )

        first_store.release_settle.set()
        first_thread.join(timeout=3)
        first_apply_finished = not first_thread.is_alive()
        first_thread = None
        after_stale_settle = _authoring_projection(
            second_service,
            source_path,
        )

        first_store.close()
        second_store.close()
        second_store = None
        restart_store = WorkflowStore(database_path)
        restart_service = WorkflowService(
            restart_store,
            compiler=_compiler(generation="second"),
        )
        restarted = restart_service.reconcile_registered_source(WORKFLOW_UUID)
        after_restart = _authoring_projection(
            restart_service,
            source_path,
        )
    finally:
        first_store.release_settle.set()
        if second_store is not None:
            second_store.release_post_commit.set()
        if first_thread is not None:
            first_thread.join(timeout=3)
        if second_thread is not None:
            second_thread.join(timeout=3)
        if second_store is not None:
            second_store.close()
        if restart_store is not None:
            restart_store.close()
        else:
            first_store.close()

    assert {
        "first_settle_observed": first_settle_observed,
        "second_commit_observed": second_commit_observed,
        "first_apply_finished": first_apply_finished,
        "second_apply_finished": second_apply_finished,
        "first_apply_failed": "error" in first_outcome or "unexpected" in first_outcome,
        "second_apply_warning_codes": [
            warning["code"]
            for warning in second_outcome["result"]["apply_result"]["warnings"]
        ],
        "pending_before_stale_settle": pending_before_stale_settle,
        "after_stale_settle": after_stale_settle,
        "restart_state": restarted["state"],
        "after_restart": after_restart,
    } == {
        "first_settle_observed": True,
        "second_commit_observed": True,
        "first_apply_finished": True,
        "second_apply_finished": True,
        "first_apply_failed": False,
        "second_apply_warning_codes": ["draft_writeback_pending"],
        "pending_before_stale_settle": True,
        "after_stale_settle": {
            "state": "applied_source_stale",
            "draft_source": SOURCE,
            "candidate_hash": None,
            "applied_compiler": "round14-generation-second",
            "applied_catalog": SECOND_CATALOG,
            "reconciliation_pending": True,
            "canonical": SOURCE,
        },
        "restart_state": "applied",
        "after_restart": {
            "state": "applied",
            "draft_source": NORMALIZED_SOURCE,
            "candidate_hash": None,
            "applied_compiler": "round14-generation-second",
            "applied_catalog": SECOND_CATALOG,
            "reconciliation_pending": False,
            "canonical": NORMALIZED_SOURCE,
        },
    }
