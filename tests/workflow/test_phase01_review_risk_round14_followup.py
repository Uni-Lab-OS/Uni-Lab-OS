"""Phase 01 Round14 follow-up：Authoring token 事务竞态风险测试。"""

from __future__ import annotations

import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from unilabos.workflow.models import WorkflowNodeWrite
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
NODE_UUID = "20000000-0000-4000-8000-000000000001"
NODE_TEMPLATE_UUID = "40000000-0000-4000-8000-000000000001"
RESOURCE_TEMPLATE_UUID = "50000000-0000-4000-8000-000000000001"
SOURCE_HANDLE_UUID = "60000000-0000-4000-8000-000000000001"
TARGET_HANDLE_UUID = "60000000-0000-4000-8000-000000000002"
CATALOG_FINGERPRINT = f"sha256:{'e' * 64}"
OLD_SOURCE = "old_draft()"
NEW_SOURCE = "new_draft()"


class SourceOnlyCompiler:
    compiler_version = "phase-01-risk-round14-followup-v1"
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
            "source_map": [
                {
                    "workflow_node_uuid": NODE_UUID,
                    "start_line": 1,
                    "start_column": 1,
                    "end_line": 1,
                    "end_column": 1,
                }
            ],
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


class ApplyEntryBarrierStore(WorkflowStore):
    """在 final checks 后、SQLite Apply 事务前提供确定性 barrier。"""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.apply_transaction_entered = threading.Event()
        self.release_apply_transaction = threading.Event()

    def apply_authoring_candidate(self, **kwargs: Any) -> tuple[int, str]:
        self.apply_transaction_entered.set()
        if not self.release_apply_transaction.wait(timeout=3):
            raise TimeoutError("test did not release Authoring Apply transaction")
        return super().apply_authoring_candidate(**kwargs)


def _node() -> WorkflowNodeWrite:
    return WorkflowNodeWrite(
        uuid=NODE_UUID,
        workflow_node_template_uuid=NODE_TEMPLATE_UUID,
        name="applied node",
        status="idle",
        type="compute",
        pose={},
        param={},
        execution_policy={},
        disabled=False,
        minimized=False,
        meta_data={},
    )


def _seed_template_catalog(store: WorkflowStore) -> None:
    timestamp = "2026-07-31T00:00:00Z"
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workflow_node_template(
                uuid, create_time, update_time, meta_data, authority_id,
                resource_template_uuid, name, display_name, class, goal,
                goal_default, feedback, result, schema, type, icon, header,
                footer, node_type
            ) VALUES (?, ?, ?, '{}', 'os-local', ?, 'source', 'Source', NULL,
                      '{}', '{}', '{}', '{}', NULL, 'action', NULL, NULL, NULL,
                      'compute')
            """,
            (
                NODE_TEMPLATE_UUID,
                timestamp,
                timestamp,
                RESOURCE_TEMPLATE_UUID,
            ),
        )
        for handle_uuid, handle_key, io_type, display_name in (
            (SOURCE_HANDLE_UUID, "result", "source", "Result"),
            (TARGET_HANDLE_UUID, "input", "target", "Input"),
        ):
            connection.execute(
                """
                INSERT INTO workflow_handle_template(
                    uuid, create_time, update_time, meta_data, authority_id,
                    workflow_node_template_uuid, handle_key, io_type,
                    display_name, type, required, data_source, data_key
                ) VALUES (?, ?, ?, '{}', 'os-local', ?, ?, ?, ?, 'number', 0,
                          NULL, NULL)
                """,
                (
                    handle_uuid,
                    timestamp,
                    timestamp,
                    NODE_TEMPLATE_UUID,
                    handle_key,
                    io_type,
                    display_name,
                ),
            )


def _seed_authoring(
    store: WorkflowStore,
    tmp_path: Path,
) -> tuple[WorkflowService, Path]:
    service = WorkflowService(store, compiler=SourceOnlyCompiler())
    service.create_workflow(
        name="phase 01 risk round 14 follow-up",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    _seed_template_catalog(store)
    service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[_node()],
        edges=[],
    )
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase_01_risk_round_14_followup",
        package_root=package_root,
        relative_path="workflows/review.py",
    )
    return service, package_root / "workflows" / "review.py"


def _snapshot(
    store: WorkflowStore,
    service: WorkflowService,
    source_path: Path,
) -> dict[str, Any]:
    return {
        "graph": service.get_graph(WORKFLOW_UUID),
        "record": store.get_authoring_record(WORKFLOW_UUID),
        "events": service.list_events(after_id=0)["items"],
        "canonical": source_path.read_bytes(),
    }


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
                expected_workflow_revision=2,
                expected_candidate_hash=saved["candidate"]["candidate_hash"],
            )
        except WorkflowError as error:
            outcome["error"] = {
                "status": error.status,
                "code": error.code,
            }
        except Exception as error:  # noqa: BLE001 - 暴露线程异常泄漏
            outcome["unexpected"] = type(error).__name__

    thread = threading.Thread(target=apply, name="round14-followup-old-apply")
    thread.start()
    return thread, outcome


def test_new_draft_wins_transaction_race_against_old_apply_and_restart(
    tmp_path: Path,
) -> None:
    store = ApplyEntryBarrierStore(tmp_path / "workflow.db")
    restart_store: WorkflowStore | None = None
    old_apply_thread: threading.Thread | None = None
    try:
        old_service, source_path = _seed_authoring(store, tmp_path)
        other_service = WorkflowService(store, compiler=SourceOnlyCompiler())
        old_saved = old_service.save_draft(
            WORKFLOW_UUID,
            python_source=OLD_SOURCE,
            expected_draft_hash=None,
            expected_workflow_revision=2,
        )
        assert old_saved["candidate"] is not None

        old_apply_thread, old_outcome = _start_old_apply(
            old_service,
            old_saved,
        )
        transaction_entry_observed = store.apply_transaction_entered.wait(timeout=2)
        assert transaction_entry_observed

        new_saved = other_service.save_draft(
            WORKFLOW_UUID,
            python_source=NEW_SOURCE,
            expected_draft_hash=old_saved["draft"]["draft_hash"],
            expected_workflow_revision=2,
        )
        assert new_saved["candidate"] is not None
        new_authority = _snapshot(store, other_service, source_path)

        store.release_apply_transaction.set()
        old_apply_thread.join(timeout=3)
        old_apply_finished = not old_apply_thread.is_alive()
        old_apply_thread = None
        after_old_apply = _snapshot(store, other_service, source_path)

        store.close()
        restart_store = WorkflowStore(tmp_path / "workflow.db")
        restart_service = WorkflowService(
            restart_store,
            compiler=SourceOnlyCompiler(),
        )
        restarted = restart_service.reconcile_registered_source(WORKFLOW_UUID)
        after_restart = _snapshot(
            restart_store,
            restart_service,
            source_path,
        )
    finally:
        store.release_apply_transaction.set()
        if old_apply_thread is not None:
            old_apply_thread.join(timeout=3)
        if restart_store is not None:
            restart_store.close()
        else:
            store.close()

    assert {
        "transaction_entry_observed": transaction_entry_observed,
        "old_apply_finished": old_apply_finished,
        "old_outcome": old_outcome,
        "new_state": new_saved["state"],
        "new_candidate_hash": new_saved["candidate"]["candidate_hash"],
        "old_apply_was_read_only": after_old_apply == new_authority,
        "restart_was_read_only": after_restart == new_authority,
        "restart_state": restarted["state"],
        "restart_candidate_hash": (
            restarted["candidate"]["candidate_hash"]
            if restarted["candidate"] is not None
            else None
        ),
        "canonical_after_restart": after_restart["canonical"],
    } == {
        "transaction_entry_observed": True,
        "old_apply_finished": True,
        "old_outcome": {
            "error": {
                "status": 409,
                "code": "draft_hash_conflict",
            }
        },
        "new_state": "unapplied_source_only",
        "new_candidate_hash": new_saved["candidate"]["candidate_hash"],
        "old_apply_was_read_only": True,
        "restart_was_read_only": True,
        "restart_state": "unapplied_source_only",
        "restart_candidate_hash": new_saved["candidate"]["candidate_hash"],
        "canonical_after_restart": NEW_SOURCE.encode(),
    }
