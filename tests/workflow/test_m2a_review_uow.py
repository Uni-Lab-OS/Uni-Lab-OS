"""M2A review：MaterialSource 写事务 authority UoW。"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from unilabos.resources.authority import (
    MaterialNotFound,
    MaterialRecord,
    SiteRecord,
)
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.catalog import TemplateCatalog
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

from .m2a_material_source_authority_fixture import (
    MOUNT_MATERIAL_UUID,
    SITE_A_UUID,
    default_material_source_authority,
)
from .test_m2a_material_source_site_authority import _source
from .test_m2a_material_source_stale_apply import _save_materialized_candidate
from .test_m2a_material_source_vertical_slice import (
    AUTHORITY,
    MATERIAL_SOURCE_NODE_UUID,
    WORKFLOW_UUID,
    _catalog_imports,
    _StaticResourceTemplateIdentityIndex,
)

SECOND_MATERIAL_SOURCE_NODE_UUID = "20000000-0000-4000-8000-000000000003"


class _UowAwareMaterialSourceAuthority:
    """仅在调用者借入 Store UoW 时变化的 static-authority fake。"""

    def __init__(self, store: WorkflowStore, *, fail_in_uow: bool = False) -> None:
        self._store = store
        self._delegate = default_material_source_authority()
        self._fail_in_uow = fail_in_uow
        self.calls: list[tuple[str, object | None, bool]] = []

    def reset_calls(self) -> None:
        self.calls.clear()

    def _record(self, method: str, uow: object | None) -> None:
        owned = uow is not None and self._store.owns_unit_of_work(uow)
        self.calls.append((method, uow, owned))

    def get_material(
        self,
        material_uuid: str,
        *,
        uow: object | None = None,
    ) -> MaterialRecord:
        self._record("get_material", uow)
        if (
            self._fail_in_uow
            and uow is not None
            and material_uuid == MOUNT_MATERIAL_UUID
        ):
            raise MaterialNotFound(f"material {material_uuid} not found")
        return self._delegate.get_material(material_uuid)

    def get_site(
        self,
        site_uuid: str,
        *,
        uow: object | None = None,
    ) -> SiteRecord:
        self._record("get_site", uow)
        return self._delegate.get_site(site_uuid)

    def list_sites(
        self,
        material_uuid: str,
        *,
        uow: object | None = None,
    ) -> Sequence[SiteRecord]:
        self._record("list_sites", uow)
        return self._delegate.list_sites(material_uuid)


@dataclass
class _ServiceContext:
    store: WorkflowStore
    engine: WorkflowAuthoringEngine
    service: WorkflowService
    authority: _UowAwareMaterialSourceAuthority


@contextmanager
def _opened_service(
    database_path: Path,
    *,
    fail_in_uow: bool,
) -> Iterator[_ServiceContext]:
    store = WorkflowStore(database_path)
    try:
        catalog = TemplateCatalog(store)
        catalog.replace(AUTHORITY, _catalog_imports())
        authority = _UowAwareMaterialSourceAuthority(
            store,
            fail_in_uow=fail_in_uow,
        )
        engine = WorkflowAuthoringEngine(
            catalog=catalog,
            authority=AUTHORITY,
            resource_template_identity_index=_StaticResourceTemplateIdentityIndex(),
            material_source_authority=authority,
        )
        service = WorkflowService(
            store,
            compiler=engine,
            material_source_authority=authority,
        )
        service.create_workflow(
            workflow_uuid=WORKFLOW_UUID,
            name="M2A UoW review",
            tags=[],
            description=None,
            meta_data={},
        )
        yield _ServiceContext(store, engine, service, authority)
    finally:
        store.close()


def _compiled_graph(context: _ServiceContext) -> dict[str, Any]:
    applied = context.service.get_graph(WORKFLOW_UUID)
    compiled = context.engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=1,
        python_source=_source(site=SITE_A_UUID),
        source_uri="package://lab/workflows/m2a_uow.py",
        applied_graph=applied,
    )
    assert compiled.valid, compiled.diagnostics
    assert compiled.graph is not None
    return compiled.graph


def _graph_covering_exact_and_all_sites(
    context: _ServiceContext,
) -> dict[str, Any]:
    graph = deepcopy(_compiled_graph(context))
    first = next(
        node for node in graph["nodes"] if node["uuid"] == MATERIAL_SOURCE_NODE_UUID
    )
    second = deepcopy(first)
    second["uuid"] = SECOND_MATERIAL_SOURCE_NODE_UUID
    second["name"] = "all_site_sample"
    second["param"]["site"] = None
    graph["nodes"].append(second)
    return graph


def _assert_one_active_store_uow(
    calls: list[tuple[str, object | None, bool]],
) -> None:
    assert {method for method, _, _ in calls} == {
        "get_material",
        "get_site",
        "list_sites",
    }
    uows = [uow for _, uow, _ in calls]
    assert uows
    assert uows[0] is not None
    assert all(uow is uows[0] for uow in uows)
    assert all(owned for _, _, owned in calls)


def test_direct_save_validates_all_material_source_facts_in_one_store_uow(
    tmp_path: Path,
) -> None:
    with _opened_service(
        tmp_path / "workflow.db",
        fail_in_uow=False,
    ) as context:
        graph = _graph_covering_exact_and_all_sites(context)
        context.authority.reset_calls()

        saved = context.service.save_graph(
            WORKFLOW_UUID,
            revision=1,
            nodes=graph["nodes"],
            edges=graph["edges"],
        )

        assert saved["workflow"]["revision"] == 2
        _assert_one_active_store_uow(context.authority.calls)


def test_direct_save_rolls_back_when_mount_disappears_inside_store_uow(
    tmp_path: Path,
) -> None:
    with _opened_service(
        tmp_path / "workflow.db",
        fail_in_uow=True,
    ) as context:
        graph = _compiled_graph(context)
        context.authority.reset_calls()
        graph_before = context.service.get_graph(WORKFLOW_UUID)

        with pytest.raises(WorkflowError) as caught:
            context.service.save_graph(
                WORKFLOW_UUID,
                revision=1,
                nodes=graph["nodes"],
                edges=graph["edges"],
            )

        assert caught.value.code == "not_found"
        assert context.authority.calls
        assert context.authority.calls[0][1] is not None
        assert context.authority.calls[0][2] is True
        assert context.service.get_graph(WORKFLOW_UUID) == graph_before
        assert context.service.get_workflow(WORKFLOW_UUID)["revision"] == 1


def test_apply_rolls_back_when_mount_disappears_inside_commit_uow(
    tmp_path: Path,
) -> None:
    with _opened_service(
        tmp_path / "workflow.db",
        fail_in_uow=True,
    ) as context:
        package_root = tmp_path / "package"
        package_root.mkdir()
        context.service.register_editable_source(
            workflow_uuid=WORKFLOW_UUID,
            package_id="m2a_uow_review",
            package_root=package_root,
            relative_path="workflows/assay.py",
        )
        source_path = package_root / "workflows" / "assay.py"
        saved = _save_materialized_candidate(context.service)
        candidate = saved["candidate"]
        assert isinstance(candidate, dict)
        graph_before = context.service.get_graph(WORKFLOW_UUID)
        authoring_before = context.service.get_authoring(WORKFLOW_UUID)
        source_before = source_path.read_bytes()
        events_before = context.service.list_events(after_id=0)
        context.authority.reset_calls()

        with pytest.raises(WorkflowError) as caught:
            context.service.apply_authoring(
                WORKFLOW_UUID,
                candidate_hash=candidate["candidate_hash"],
            )

        assert caught.value.code == "not_found"
        assert any(uow is None for _, uow, _ in context.authority.calls)
        commit_calls = [call for call in context.authority.calls if call[1] is not None]
        assert commit_calls
        assert all(call[2] for call in commit_calls)
        assert all(call[1] is commit_calls[0][1] for call in commit_calls)
        assert context.service.get_graph(WORKFLOW_UUID) == graph_before
        assert context.service.get_workflow(WORKFLOW_UUID)["revision"] == 1
        assert source_path.read_bytes() == source_before
        assert context.service.get_authoring(WORKFLOW_UUID) == authoring_before
        assert context.service.list_events(after_id=0) == events_before
