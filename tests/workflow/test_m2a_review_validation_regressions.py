"""M2A review：纯校验、异常归一化与 authoring marker 回归。"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from unilabos.app.scheduler.inventory import MaterialRecord, SiteRecord
from unilabos.workflow.authoring import resource_ref
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.catalog import TemplateCatalog
from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

from .m2a_material_source_authority_fixture import (
    SITE_A_UUID,
    default_material_source_authority,
)
from .test_m2a_material_source_site_authority import _source
from .test_m2a_material_source_vertical_slice import (
    AUTHORITY,
    MATERIAL_SOURCE_NODE_UUID,
    WORKFLOW_UUID,
    _catalog_imports,
    _StaticResourceTemplateIdentityIndex,
)

MISSING_MOUNT_UUID = "50000000-0000-4000-8000-000000000099"
SECRET = "secret-material-adapter-detail"


class _BoundaryMaterialSourceAuthority:
    """可观测、可注入失败的 Material/Site 系统边界 fake。"""

    def __init__(
        self,
        *,
        failing_method: str | None = None,
        error_type: type[BaseException] = RuntimeError,
    ) -> None:
        self._delegate = default_material_source_authority()
        self._failing_method = failing_method
        self._error_type = error_type
        self.calls: list[tuple[str, object | None]] = []

    def reset_calls(self) -> None:
        self.calls.clear()

    def _record_or_fail(self, method: str, uow: object | None) -> None:
        self.calls.append((method, uow))
        if method == self._failing_method:
            raise self._error_type(SECRET)

    def get_material(
        self,
        material_uuid: str,
        *,
        uow: object | None = None,
    ) -> MaterialRecord:
        self._record_or_fail("get_material", uow)
        return self._delegate.get_material(material_uuid)

    def get_site(
        self,
        site_uuid: str,
        *,
        uow: object | None = None,
    ) -> SiteRecord:
        self._record_or_fail("get_site", uow)
        return self._delegate.get_site(site_uuid)

    def list_sites(
        self,
        material_uuid: str,
        *,
        uow: object | None = None,
    ) -> Sequence[SiteRecord]:
        self._record_or_fail("list_sites", uow)
        return self._delegate.list_sites(material_uuid)


@dataclass
class _Context:
    store: WorkflowStore
    engine: WorkflowAuthoringEngine
    service: WorkflowService
    applied_graph: dict[str, Any]


@contextmanager
def _opened_context(
    database_path: Path,
    *,
    engine_authority: object,
    service_authority: object | None = None,
) -> Iterator[_Context]:
    store = WorkflowStore(database_path)
    try:
        catalog = TemplateCatalog(store)
        catalog.replace(AUTHORITY, _catalog_imports())
        engine = WorkflowAuthoringEngine(
            catalog=catalog,
            authority=AUTHORITY,
            resource_template_identity_index=_StaticResourceTemplateIdentityIndex(),
            material_source_authority=engine_authority,
        )
        service = WorkflowService(
            store,
            compiler=engine,
            material_source_authority=(
                engine_authority if service_authority is None else service_authority
            ),
        )
        service.create_workflow(
            workflow_uuid=WORKFLOW_UUID,
            name="M2A review",
            tags=[],
            description=None,
            meta_data={},
        )
        yield _Context(store, engine, service, service.get_graph(WORKFLOW_UUID))
    finally:
        store.close()


def _compile(
    context: _Context,
    source: str,
) -> CandidateCompilation:
    return context.engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=1,
        python_source=source,
        source_uri="package://lab/workflows/m2a_review.py",
        applied_graph=context.applied_graph,
    )


def _candidate_graph(context: _Context, source: str) -> dict[str, Any]:
    compiled = _compile(context, source)
    assert compiled.valid, compiled.diagnostics
    assert compiled.graph is not None
    return compiled.graph


def _source_for_authority_method(method: str) -> str:
    return _source(site=None if method == "list_sites" else SITE_A_UUID)


def test_direct_save_validates_selector_before_calling_material_authority(
    tmp_path: Path,
) -> None:
    authority = _BoundaryMaterialSourceAuthority()
    with _opened_context(
        tmp_path / "workflow.db",
        engine_authority=authority,
    ) as context:
        graph = deepcopy(_candidate_graph(context, _source(site=SITE_A_UUID)))
        authority.reset_calls()
        node = next(
            item for item in graph["nodes"] if item["uuid"] == MATERIAL_SOURCE_NODE_UUID
        )
        node["param"]["mode"] = "bogus"
        node["param"]["mount"] = {"uuid": MISSING_MOUNT_UUID}
        graph_before = context.service.get_graph(WORKFLOW_UUID)

        with pytest.raises(WorkflowError) as caught:
            context.service.save_graph(
                WORKFLOW_UUID,
                revision=1,
                nodes=graph["nodes"],
                edges=graph["edges"],
            )

        assert caught.value.code == "invalid_material_source"
        assert authority.calls == []
        assert context.service.get_graph(WORKFLOW_UUID) == graph_before
        assert context.service.get_workflow(WORKFLOW_UUID)["revision"] == 1


@pytest.mark.parametrize("method", ["get_material", "get_site", "list_sites"])
@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
def test_engine_redacts_unexpected_material_authority_failures(
    tmp_path: Path,
    method: str,
    error_type: type[BaseException],
) -> None:
    authority = _BoundaryMaterialSourceAuthority(
        failing_method=method,
        error_type=error_type,
    )
    with _opened_context(
        tmp_path / "workflow.db",
        engine_authority=authority,
    ) as context:
        result = _compile(context, _source_for_authority_method(method))

    assert not result.valid
    assert result.graph is None
    assert [item["code"] for item in result.diagnostics] == [
        "material_authority_unavailable"
    ]
    assert SECRET not in str(result.diagnostics)


@pytest.mark.parametrize("method", ["get_material", "get_site", "list_sites"])
@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
def test_direct_save_redacts_unexpected_material_authority_failures(
    tmp_path: Path,
    method: str,
    error_type: type[BaseException],
) -> None:
    authority = _BoundaryMaterialSourceAuthority(
        failing_method=method,
        error_type=error_type,
    )
    with _opened_context(
        tmp_path / "workflow.db",
        engine_authority=default_material_source_authority(),
        service_authority=authority,
    ) as context:
        graph = _candidate_graph(context, _source_for_authority_method(method))
        graph_before = context.service.get_graph(WORKFLOW_UUID)

        with pytest.raises(WorkflowError) as caught:
            context.service.save_graph(
                WORKFLOW_UUID,
                revision=1,
                nodes=graph["nodes"],
                edges=graph["edges"],
            )

        assert caught.value.code == "material_authority_unavailable"
        assert SECRET not in caught.value.message
        assert SECRET not in str(caught.value)
        assert context.service.get_graph(WORKFLOW_UUID) == graph_before
        assert context.service.get_workflow(WORKFLOW_UUID)["revision"] == 1


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_engine_does_not_swallow_process_control_exceptions(
    tmp_path: Path,
    error_type: type[BaseException],
) -> None:
    authority = _BoundaryMaterialSourceAuthority(
        failing_method="get_material",
        error_type=error_type,
    )
    with (
        _opened_context(
            tmp_path / "workflow.db",
            engine_authority=authority,
        ) as context,
        pytest.raises(error_type, match=SECRET),
    ):
        _compile(context, _source(site=SITE_A_UUID))


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_direct_save_does_not_swallow_process_control_exceptions(
    tmp_path: Path,
    error_type: type[BaseException],
) -> None:
    authority = _BoundaryMaterialSourceAuthority(
        failing_method="get_material",
        error_type=error_type,
    )
    with _opened_context(
        tmp_path / "workflow.db",
        engine_authority=default_material_source_authority(),
        service_authority=authority,
    ) as context:
        graph = _candidate_graph(context, _source(site=SITE_A_UUID))

        with pytest.raises(error_type, match=SECRET):
            context.service.save_graph(
                WORKFLOW_UUID,
                revision=1,
                nodes=graph["nodes"],
                edges=graph["edges"],
            )


def test_resource_ref_direct_misuse_does_not_disclose_material_uuid() -> None:
    secret_material_uuid = "private-material-uuid"

    with pytest.raises(RuntimeError) as caught:
        resource_ref(secret_material_uuid)

    assert str(caught.value) == (
        "Workflow authoring resource_ref() 只能由静态编译器解析"
    )
    assert secret_material_uuid not in str(caught.value)


def test_material_source_result_attribute_is_not_a_second_material_handle(
    tmp_path: Path,
) -> None:
    source = _source(site=SITE_A_UUID).replace(
        "sample=assay_plate",
        "sample=assay_plate.material",
    )
    with _opened_context(
        tmp_path / "workflow.db",
        engine_authority=default_material_source_authority(),
    ) as context:
        result = _compile(context, source)

    assert not result.valid
    assert result.graph is None
    assert [item["code"] for item in result.diagnostics] == ["invalid_material_source"]
