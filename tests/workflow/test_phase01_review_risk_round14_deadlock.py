"""Phase 01 Round14：Authoring Apply 跨 authority 锁反转风险测试。"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
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
SOURCE = "build()"


class ApplyTransactionProbeStore(WorkflowStore):
    """只观测当前线程是否正在 Authoring Apply 的 Store 事务中。"""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.apply_transaction_active = threading.Event()
        self._apply_call = threading.local()

    def apply_authoring_candidate(self, **kwargs: Any) -> int:
        self._apply_call.active = True
        try:
            return super().apply_authoring_candidate(**kwargs)
        finally:
            self._apply_call.active = False

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with super().transaction() as connection:
            is_apply = bool(getattr(self._apply_call, "active", False))
            if is_apply:
                self.apply_transaction_active.set()
            try:
                yield connection
            finally:
                if is_apply:
                    self.apply_transaction_active.clear()


class CatalogGateCompiler:
    compiler_version = "phase-01-risk-round14-deadlock-v1"

    def __init__(self, store: ApplyTransactionProbeStore) -> None:
        self.store = store
        self.catalog_gate = threading.Semaphore(1)
        self.catalog_requested_inside_store_transaction = threading.Event()

    @property
    def template_catalog_fingerprint(self) -> str:
        if self.store.apply_transaction_active.is_set():
            self.catalog_requested_inside_store_transaction.set()
            if not self.catalog_gate.acquire(timeout=3):
                raise TimeoutError("test did not release the catalog gate")
            self.catalog_gate.release()
        return CATALOG_FINGERPRINT

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
            "template_catalog_fingerprint": CATALOG_FINGERPRINT,
        }


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


def _open_authoring(
    tmp_path: Path,
) -> tuple[ApplyTransactionProbeStore, WorkflowService, CatalogGateCompiler]:
    store = ApplyTransactionProbeStore(tmp_path / "workflow.db")
    compiler = CatalogGateCompiler(store)
    service = WorkflowService(store, compiler=compiler)
    service.create_workflow(
        name="phase 01 risk round 14 deadlock",
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
        package_id="phase_01_risk_round_14_deadlock",
        package_root=package_root,
        relative_path="workflows/review.py",
    )
    return store, service, compiler


def test_apply_store_transaction_never_calls_catalog_authority_guard(
    tmp_path: Path,
) -> None:
    store, service, compiler = _open_authoring(tmp_path)
    saved = service.save_draft(
        WORKFLOW_UUID,
        python_source=SOURCE,
        expected_draft_hash=None,
        expected_workflow_revision=2,
    )
    assert saved["candidate"] is not None

    begin_store_read = threading.Event()
    catalog_held = threading.Event()
    store_read_started = threading.Event()
    store_read_finished = threading.Event()
    forced_catalog_release = threading.Event()
    apply_finished = threading.Event()
    apply_outcome: dict[str, Any] = {}
    store_read_outcome: dict[str, Any] = {}

    def catalog_then_store() -> None:
        acquired = compiler.catalog_gate.acquire(timeout=3)
        if not acquired:
            store_read_outcome["unexpected"] = "catalog_gate_timeout"
            store_read_finished.set()
            return
        catalog_held.set()
        if not begin_store_read.wait(timeout=3):
            store_read_outcome["unexpected"] = "store_read_not_started"
        else:
            store_read_started.set()
            try:
                store_read_outcome["workflow"] = store.get_workflow(WORKFLOW_UUID)
            except Exception as error:  # noqa: BLE001 - 暴露线程异常
                store_read_outcome["unexpected"] = type(error).__name__
        if not forced_catalog_release.is_set():
            compiler.catalog_gate.release()
        store_read_finished.set()

    def apply() -> None:
        try:
            apply_outcome["result"] = service.apply_authoring(
                WORKFLOW_UUID,
                expected_draft_hash=saved["draft"]["draft_hash"],
                expected_workflow_revision=2,
                expected_candidate_hash=saved["candidate"]["candidate_hash"],
            )
        except WorkflowError as error:
            apply_outcome["error"] = {
                "status": error.status,
                "code": error.code,
            }
        except Exception as error:  # noqa: BLE001 - 暴露线程异常
            apply_outcome["unexpected"] = type(error).__name__
        finally:
            apply_finished.set()

    catalog_thread = threading.Thread(
        target=catalog_then_store,
        name="round14-catalog-then-store",
        daemon=True,
    )
    apply_thread = threading.Thread(
        target=apply,
        name="round14-store-then-catalog",
        daemon=True,
    )

    try:
        catalog_thread.start()
        assert catalog_held.wait(timeout=2)
        apply_thread.start()

        catalog_called_under_store_lock = (
            compiler.catalog_requested_inside_store_transaction.wait(timeout=2)
        )
        apply_completed_before_store_read = apply_finished.is_set()

        begin_store_read.set()
        assert store_read_started.wait(timeout=2)
        store_read_completed_while_catalog_held = store_read_finished.wait(timeout=0.2)

        if not store_read_completed_while_catalog_held:
            forced_catalog_release.set()
            compiler.catalog_gate.release()

        apply_thread.join(timeout=3)
        catalog_thread.join(timeout=3)
        workers_cleaned_up = (
            not apply_thread.is_alive() and not catalog_thread.is_alive()
        )
    finally:
        begin_store_read.set()
        if apply_thread.is_alive() or catalog_thread.is_alive():
            forced_catalog_release.set()
            compiler.catalog_gate.release()
        apply_thread.join(timeout=3)
        catalog_thread.join(timeout=3)
        if not apply_thread.is_alive() and not catalog_thread.is_alive():
            store.close()

    assert {
        "catalog_called_under_store_lock": catalog_called_under_store_lock,
        "apply_completed_before_store_read": apply_completed_before_store_read,
        "store_read_completed_while_catalog_held": (
            store_read_completed_while_catalog_held
        ),
        "workers_cleaned_up": workers_cleaned_up,
        "apply_error": apply_outcome.get("error") or apply_outcome.get("unexpected"),
        "store_read_error": store_read_outcome.get("unexpected"),
    } == {
        "catalog_called_under_store_lock": False,
        "apply_completed_before_store_read": True,
        "store_read_completed_while_catalog_held": True,
        "workers_cleaned_up": True,
        "apply_error": None,
        "store_read_error": None,
    }
