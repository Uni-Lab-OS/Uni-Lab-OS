"""C1 R1 review findings 的生命周期与原子性 public RED。"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from typing import Any

import pytest

from unilabos.package_manager.consumers import (
    PackageCatalogPublishedWorkflowResolver,
)
from unilabos.registry.catalog_consumer import (
    workflow_template_imports_from_registry_snapshot,
)
from unilabos.workflow import composition
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.catalog import (
    TemplateCatalog,
    TemplateCatalogUnavailable,
)
from unilabos.workflow.composite import PublishedWorkflowCatalogPublisher
from unilabos.workflow.models import WorkflowEdgeWrite, WorkflowNodeWrite
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

from .test_c1_catalog_publication_lifecycle import (
    AUTHORITY,
    WORKFLOW_TEMPLATE_NAME,
    _apply_child,
    _catalog_identities,
    _compose,
    _prepare_child_apply,
    _registry_snapshot,
    _StaticResourceTemplateIdentityIndex,
)
from .test_c1_published_workflow_contract import (
    HOST_RESOURCE_TEMPLATE_UUID,
    WORKFLOW_UUID,
    _package_catalog,
)


@pytest.fixture(autouse=True)
def _clean_composition() -> Iterator[None]:
    composition.reset_workflow_service_for_test()
    try:
        yield
    finally:
        composition.reset_workflow_service_for_test()


def test_public_save_graph_removes_now_stale_child_from_complete_catalog(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "authority"
    service = _compose(working_dir, include_host=True)
    _apply_child(service, working_dir)
    assert WORKFLOW_TEMPLATE_NAME in _catalog_identities(service)
    graph = service.get_graph(WORKFLOW_UUID)

    saved = service.save_graph(
        WORKFLOW_UUID,
        revision=graph["workflow"]["revision"],
        nodes=graph["nodes"],
        edges=graph["edges"],
    )

    assert saved["workflow"]["revision"] == graph["workflow"]["revision"] + 1
    assert WORKFLOW_TEMPLATE_NAME not in _catalog_identities(service)


def test_public_soft_delete_removes_child_from_complete_catalog(tmp_path: Path) -> None:
    working_dir = tmp_path / "authority"
    service = _compose(working_dir, include_host=True)
    _apply_child(service, working_dir)
    assert WORKFLOW_TEMPLATE_NAME in _catalog_identities(service)

    service.delete_workflow(WORKFLOW_UUID)

    assert WORKFLOW_TEMPLATE_NAME not in _catalog_identities(service)


class _PublishedSnapshotRaceSpy(WorkflowStore):
    """冻结 Publisher 唯一 coherent Applied snapshot 的 public Store seam。"""

    def __init__(self, database_path: Path) -> None:
        super().__init__(database_path)
        self.read_started = Event()
        self.release_read = Event()
        self._state_lock = Lock()
        self._armed = False
        self.coherent_snapshot_reads = 0
        self.split_graph_reads = 0

    def arm_publication_race(self) -> None:
        with self._state_lock:
            self._armed = True

    def _pause_armed_read(self) -> None:
        should_pause = False
        with self._state_lock:
            if self._armed:
                self._armed = False
                should_pause = True
        if should_pause:
            self.read_started.set()
            if not self.release_read.wait(timeout=3):
                raise TimeoutError("publication snapshot race was not released")

    def get_published_workflow_snapshot(
        self,
        workflow_uuid: str,
    ) -> dict[str, Any]:
        """返回一个 graph 与 applied_source 已一起冻结的 publication value。"""

        graph = super().get_graph(workflow_uuid)
        record = super().get_authoring_record(workflow_uuid)
        self.coherent_snapshot_reads += 1
        self._pause_armed_read()
        return {**graph, "applied_source": record.get("applied_source")}

    def get_graph(
        self,
        workflow_uuid: str,
        *,
        conn: Any | None = None,
    ) -> dict[str, Any]:
        """侦测旧的 split-read 路径，并在两次 public read 之间制造 revision race。"""

        graph = super().get_graph(workflow_uuid, conn=conn)
        self.split_graph_reads += 1
        self._pause_armed_read()
        return graph


def test_publisher_reads_graph_and_applied_source_through_one_public_store_snapshot(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "authority"
    service = _compose(working_dir, include_host=True)
    _apply_child(service, working_dir)
    composition.reset_workflow_service_for_test()

    database_path = working_dir / "workflow.db"
    store = _PublishedSnapshotRaceSpy(database_path)
    writer = WorkflowStore(database_path)
    identity_index = _StaticResourceTemplateIdentityIndex(include_host=True)
    resolver = PackageCatalogPublishedWorkflowResolver((_package_catalog(),))
    catalog = TemplateCatalog(store)
    publisher = PublishedWorkflowCatalogPublisher(
        catalog=catalog,
        authority=AUTHORITY,
        store=store,
        sources=resolver.sources,
        base_templates=workflow_template_imports_from_registry_snapshot(
            _registry_snapshot(include_host=True),
            authority_id=AUTHORITY.authority_id,
            resource_template_identity_resolver=identity_index.resolve_symbol,
        ),
        host_node_resource_template_uuid=HOST_RESOURCE_TEMPLATE_UUID,
    )
    initial_graph = writer.get_graph(WORKFLOW_UUID)
    nodes = [WorkflowNodeWrite.model_validate(item) for item in initial_graph["nodes"]]
    edges = [WorkflowEdgeWrite.model_validate(item) for item in initial_graph["edges"]]
    store.arm_publication_race()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(publisher.publish)
            assert store.read_started.wait(timeout=1)
            writer.save_graph(
                WORKFLOW_UUID,
                revision=initial_graph["workflow"]["revision"],
                nodes=nodes,
                edges=edges,
            )
            store.release_read.set()
            future.result(timeout=3)

        assert store.coherent_snapshot_reads == 1
        assert store.split_graph_reads == 0
    finally:
        store.release_read.set()
        writer.close()
        store.close()


class _BlockRedundantInvalidateCatalog(TemplateCatalog):
    """仅在 public invalidate 被调用时安装第二个真实 DELETE fault。"""

    def __init__(self, store: WorkflowStore, database_path: Path) -> None:
        super().__init__(store)
        self._database_path = database_path

    def invalidate(self, authority: Any) -> None:
        fault_store = WorkflowStore(self._database_path)
        try:
            with fault_store.transaction() as connection:
                connection.execute(
                    """
                    CREATE TRIGGER c1_fail_redundant_catalog_invalidate
                    BEFORE DELETE ON workflow_template_catalog
                    BEGIN
                        SELECT RAISE(ABORT, 'injected redundant invalidate failure');
                    END
                    """
                )
        finally:
            fault_store.close()
        super().invalidate(authority)


def _drop_atomicity_faults(database_path: Path) -> None:
    cleanup = WorkflowStore(database_path)
    try:
        with cleanup.transaction() as connection:
            connection.execute(
                "DROP TRIGGER IF EXISTS c1_fail_workflow_template_insert"
            )
            connection.execute(
                "DROP TRIGGER IF EXISTS c1_fail_redundant_catalog_invalidate"
            )
    finally:
        cleanup.close()


def test_apply_commit_atomically_deletes_marker_before_replace_failure_cleanup(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "authority"
    database_path = working_dir / "workflow.db"
    store = WorkflowStore(database_path)
    catalog = _BlockRedundantInvalidateCatalog(store, database_path)
    identity_index = _StaticResourceTemplateIdentityIndex(include_host=True)
    resolver = PackageCatalogPublishedWorkflowResolver((_package_catalog(),))
    publisher = PublishedWorkflowCatalogPublisher(
        catalog=catalog,
        authority=AUTHORITY,
        store=store,
        sources=resolver.sources,
        base_templates=workflow_template_imports_from_registry_snapshot(
            _registry_snapshot(include_host=True),
            authority_id=AUTHORITY.authority_id,
            resource_template_identity_resolver=identity_index.resolve_symbol,
        ),
        host_node_resource_template_uuid=HOST_RESOURCE_TEMPLATE_UUID,
    )
    publisher.publish()
    service = WorkflowService(
        store,
        compiler=WorkflowAuthoringEngine(
            catalog=catalog,
            authority=AUTHORITY,
            resource_template_identity_index=identity_index,
        ),
        catalog_publisher=publisher,
    )
    service.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="Lifecycle child",
        tags=[],
        description=None,
        meta_data={},
    )
    candidate_hash = _prepare_child_apply(service, working_dir)
    previous_revision = service.get_workflow(WORKFLOW_UUID)["revision"]
    fault_store = WorkflowStore(database_path)
    try:
        with fault_store.transaction() as connection:
            connection.execute(
                f"""
                CREATE TRIGGER c1_fail_workflow_template_insert
                BEFORE INSERT ON workflow_node_template
                WHEN NEW.name = '{WORKFLOW_TEMPLATE_NAME}'
                BEGIN
                    SELECT RAISE(ABORT, 'injected C1 catalog replace failure');
                END
                """
            )

        with pytest.raises(WorkflowError) as caught:
            service.apply_authoring(
                WORKFLOW_UUID,
                candidate_hash=candidate_hash,
            )

        assert caught.value.code == "template_catalog_unavailable"
        assert service.get_workflow(WORKFLOW_UUID)["revision"] == previous_revision + 1
        with pytest.raises(TemplateCatalogUnavailable), catalog.snapshot(AUTHORITY):
            pass
    finally:
        fault_store.close()
        service.close()
        _drop_atomicity_faults(database_path)

    restarted = _compose(working_dir, include_host=True)
    assert WORKFLOW_TEMPLATE_NAME in _catalog_identities(restarted)
