"""M2A MaterialSource Draft→Candidate→Apply 的 stale-authority 合同。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from unilabos.resources.authority import MaterialNotFound, MaterialRecord, SiteRecord
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
from .test_m2a_material_source_vertical_slice import (
    AUTHORITY,
    WORKFLOW_UUID,
    _catalog_imports,
    _StaticResourceTemplateIdentityIndex,
)


class _MutableMaterialSourceAuthority:
    """只为跨 Candidate/Apply 边界模拟 durable authority 变化。"""

    def __init__(self) -> None:
        self._delegate = default_material_source_authority()
        self._mount_state = "available"

    def make_mount_unavailable(self, state: str) -> None:
        assert state in {"missing", "deleted"}
        self._mount_state = state

    def get_material(
        self,
        material_uuid: str,
        *,
        uow: object | None = None,
    ) -> MaterialRecord:
        material = self._delegate.get_material(material_uuid, uow=uow)
        if material_uuid != MOUNT_MATERIAL_UUID:
            return material
        if self._mount_state == "missing":
            raise MaterialNotFound(f"material {material_uuid} not found")
        if self._mount_state == "deleted":
            return replace(material, deleted_at="2026-08-02T01:00:00Z")
        return material

    def get_site(
        self,
        site_uuid: str,
        *,
        uow: object | None = None,
    ) -> SiteRecord:
        return self._delegate.get_site(site_uuid, uow=uow)

    def list_sites(
        self,
        material_uuid: str,
        *,
        uow: object | None = None,
    ) -> Sequence[SiteRecord]:
        return self._delegate.list_sites(material_uuid, uow=uow)


def _save_materialized_candidate(
    service: WorkflowService,
) -> dict[str, object]:
    saved = service.save_draft(
        WORKFLOW_UUID,
        python_source=_source(site=SITE_A_UUID),
        expected_draft_hash=None,
        expected_workflow_revision=1,
    )
    candidate = saved["candidate"]
    assert isinstance(candidate, dict)
    if saved["draft"]["python_source"] != candidate["normalized_python_source"]:
        saved = service.save_draft(
            WORKFLOW_UUID,
            python_source=candidate["normalized_python_source"],
            expected_draft_hash=saved["draft"]["draft_hash"],
            expected_workflow_revision=1,
        )
        candidate = saved["candidate"]
        assert isinstance(candidate, dict)
    assert saved["draft"]["python_source"] == candidate["normalized_python_source"]
    return saved


@pytest.mark.parametrize("stale_state", ["missing", "deleted"])
def test_apply_rejects_candidate_when_material_source_mount_became_stale(
    tmp_path: Path,
    stale_state: str,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        catalog = TemplateCatalog(store)
        catalog.replace(AUTHORITY, _catalog_imports())
        material_source_authority = _MutableMaterialSourceAuthority()
        engine = WorkflowAuthoringEngine(
            catalog=catalog,
            authority=AUTHORITY,
            resource_template_identity_index=_StaticResourceTemplateIdentityIndex(),
            material_source_authority=material_source_authority,
        )
        service = WorkflowService(
            store,
            compiler=engine,
            material_source_authority=material_source_authority,
        )
        service.create_workflow(
            workflow_uuid=WORKFLOW_UUID,
            name="Assay",
            tags=[],
            description=None,
            meta_data={},
        )
        package_root = tmp_path / "package"
        package_root.mkdir()
        service.register_editable_source(
            workflow_uuid=WORKFLOW_UUID,
            package_id="m2a_stale_apply",
            package_root=package_root,
            relative_path="workflows/assay.py",
        )
        source_path = package_root / "workflows" / "assay.py"
        saved = _save_materialized_candidate(service)
        candidate = saved["candidate"]
        assert isinstance(candidate, dict)

        graph_before = service.get_graph(WORKFLOW_UUID)
        authoring_before = service.get_authoring(WORKFLOW_UUID)
        source_before = source_path.read_bytes()
        events_before = service.list_events(after_id=0)

        material_source_authority.make_mount_unavailable(stale_state)

        with pytest.raises(WorkflowError) as error:
            service.apply_authoring(
                WORKFLOW_UUID,
                candidate_hash=candidate["candidate_hash"],
            )

        assert error.value.code == "draft_invalid"
        assert service.get_graph(WORKFLOW_UUID) == graph_before
        assert service.get_workflow(WORKFLOW_UUID)["revision"] == 1
        assert source_path.read_bytes() == source_before
        assert service.get_authoring(WORKFLOW_UUID) == authoring_before
        assert service.list_events(after_id=0) == events_before
    finally:
        store.close()
