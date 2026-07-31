"""Phase 01A6：Authoring Apply 的 Catalog → Store 锁序合同。"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
CATALOG_FINGERPRINT = f"sha256:{'e' * 64}"
SOURCE = "build()\n"


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


class MutableCatalogCompiler:
    """用 guard 模拟可变 Catalog，并暴露错误锁序的确定性观测点。"""

    compiler_version = "phase-01a6-catalog-store-lock-order-v1"

    def __init__(self, store: ApplyTransactionProbeStore) -> None:
        self.store = store
        self.catalog_gate = threading.Semaphore(1)
        self.catalog_access_requested = threading.Event()
        self.catalog_snapshot_requested = threading.Event()
        self.catalog_snapshot_requested_inside_store = threading.Event()
        self.fingerprint_requested_inside_store = threading.Event()

    @property
    def template_catalog_fingerprint(self) -> str:
        # 旧实现仅在 Store 回调中的最终读取形成反向 Catalog 获取。
        if self.store.apply_transaction_active.is_set():
            self.fingerprint_requested_inside_store.set()
            self.catalog_access_requested.set()
            if not self.catalog_gate.acquire(timeout=3):
                raise TimeoutError("测试未释放 Catalog gate")
            self.catalog_gate.release()
        return CATALOG_FINGERPRINT

    @contextmanager
    def catalog_snapshot(self) -> Iterator[str]:
        """在整个调用方上下文内保持 Catalog 不变。"""

        self.catalog_snapshot_requested.set()
        self.catalog_access_requested.set()
        if self.store.apply_transaction_active.is_set():
            self.catalog_snapshot_requested_inside_store.set()
        if not self.catalog_gate.acquire(timeout=3):
            raise TimeoutError("测试未释放 Catalog snapshot gate")
        try:
            yield CATALOG_FINGERPRINT
        finally:
            self.catalog_gate.release()

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
            "normalized_python_source": python_source,
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
            "template_catalog_fingerprint": CATALOG_FINGERPRINT,
        }


def _open_authoring(
    tmp_path: Path,
) -> tuple[ApplyTransactionProbeStore, WorkflowService, MutableCatalogCompiler]:
    store = ApplyTransactionProbeStore(tmp_path / "workflow.db")
    compiler = MutableCatalogCompiler(store)
    service = WorkflowService(store, compiler=compiler)
    service.create_workflow(
        name="phase 01A6 catalog/store lock order",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase_01a6_catalog_store_lock_order",
        package_root=package_root,
        relative_path="workflows/review.py",
    )
    return store, service, compiler


def test_apply_acquires_catalog_snapshot_before_store_transaction(
    tmp_path: Path,
) -> None:
    store, service, compiler = _open_authoring(tmp_path)
    saved = service.save_draft(
        WORKFLOW_UUID,
        python_source=SOURCE,
        expected_draft_hash=None,
        expected_workflow_revision=1,
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
        try:
            if not begin_store_read.wait(timeout=3):
                store_read_outcome["unexpected"] = "store_read_not_started"
                return
            store_read_started.set()
            store_read_outcome["workflow"] = store.get_workflow(WORKFLOW_UUID)
        except Exception as error:  # noqa: BLE001 - 暴露线程异常
            store_read_outcome["unexpected"] = type(error).__name__
        finally:
            if not forced_catalog_release.is_set():
                compiler.catalog_gate.release()
            store_read_finished.set()

    def apply() -> None:
        try:
            apply_outcome["result"] = service.apply_authoring(
                WORKFLOW_UUID,
                candidate_hash=saved["candidate"]["candidate_hash"],
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
        name="phase01a6-catalog-then-store",
        daemon=True,
    )
    apply_thread = threading.Thread(
        target=apply,
        name="phase01a6-apply",
        daemon=True,
    )

    store_read_completed_while_catalog_held = False
    try:
        catalog_thread.start()
        catalog_was_held = catalog_held.wait(timeout=2)
        apply_thread.start()
        catalog_access_was_requested = compiler.catalog_access_requested.wait(timeout=2)

        begin_store_read.set()
        store_read_was_started = store_read_started.wait(timeout=2)
        store_read_completed_while_catalog_held = store_read_finished.wait(timeout=0.5)
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
        if (
            apply_thread.is_alive() or catalog_thread.is_alive()
        ) and not forced_catalog_release.is_set():
            forced_catalog_release.set()
            compiler.catalog_gate.release()
        apply_thread.join(timeout=3)
        catalog_thread.join(timeout=3)
        if not apply_thread.is_alive() and not catalog_thread.is_alive():
            store.close()

    assert {
        "catalog_was_held": catalog_was_held,
        "catalog_access_was_requested": catalog_access_was_requested,
        "catalog_snapshot_requested": compiler.catalog_snapshot_requested.is_set(),
        "snapshot_requested_inside_store": (
            compiler.catalog_snapshot_requested_inside_store.is_set()
        ),
        "fingerprint_requested_inside_store": (
            compiler.fingerprint_requested_inside_store.is_set()
        ),
        "store_read_was_started": store_read_was_started,
        "store_read_completed_while_catalog_held": (
            store_read_completed_while_catalog_held
        ),
        "workers_cleaned_up": workers_cleaned_up,
        "apply_finished": apply_finished.is_set(),
        "apply_error": apply_outcome.get("error") or apply_outcome.get("unexpected"),
        "store_read_error": store_read_outcome.get("unexpected"),
    } == {
        "catalog_was_held": True,
        "catalog_access_was_requested": True,
        "catalog_snapshot_requested": True,
        "snapshot_requested_inside_store": False,
        "fingerprint_requested_inside_store": False,
        "store_read_was_started": True,
        "store_read_completed_while_catalog_held": True,
        "workers_cleaned_up": True,
        "apply_finished": True,
        "apply_error": None,
        "store_read_error": None,
    }
