"""M1C ResourceSlot resolver 的独立公共合同 RED。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, fields, is_dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.resources.authority import (
    MaterialConflict,
    MaterialInvalidInput,
    MaterialModule,
    MaterialNotFound,
    MaterialRecord,
    ResourceTemplateIdentity,
)
from unilabos.resources.authority.sqlite import SQLiteMaterialAdapter
from unilabos.workflow.models import WorkflowNodeWrite
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

MATERIAL_UUID = "5abcdef0-1234-4abc-8def-000000000017"
MISSING_MATERIAL_UUID = "5abcdef0-1234-4abc-8def-000000000099"
RESOURCE_TEMPLATE_UUID = "2abcdef0-1234-4abc-8def-000000000017"
SECOND_RESOURCE_TEMPLATE_UUID = "2abcdef0-1234-4abc-8def-000000000018"
WORKFLOW_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NODE_UUID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _resource_templates() -> Mapping[str, object]:
    identities = (
        ResourceTemplateIdentity(
            uuid=RESOURCE_TEMPLATE_UUID,
            material_class="SampleTube",
        ),
        ResourceTemplateIdentity(
            uuid=SECOND_RESOURCE_TEMPLATE_UUID,
            material_class="Microplate",
        ),
    )
    return MappingProxyType({identity.uuid: identity for identity in identities})


def _material_module(adapter: Any) -> MaterialModule:
    return MaterialModule(adapter, resource_templates=_resource_templates())


@contextmanager
def _open_authority(
    database_path: Path,
) -> Iterator[tuple[WorkflowStore, MaterialModule]]:
    coordinator = WorkflowStore(database_path)
    try:
        adapter = SQLiteMaterialAdapter.from_runtime_authority(coordinator)
        yield coordinator, _material_module(adapter)
    finally:
        coordinator.close()


def _create_active_material(materials: MaterialModule) -> MaterialRecord:
    return materials.create_business_material(
        material_uuid=MATERIAL_UUID,
        resource_template_uuid=RESOURCE_TEMPLATE_UUID,
        barcode="M1C-ACTIVE-017",
        name="M1C active material",
    )


def _material_record(
    *,
    deleted_at: str | None = None,
    disposition: str | None = "active",
    material_kind: str = "business",
) -> MaterialRecord:
    return MaterialRecord(
        uuid=MATERIAL_UUID,
        create_time="2026-08-02T00:00:00Z",
        update_time="2026-08-02T00:00:00Z",
        deleted_at=deleted_at,
        description=None,
        meta_data={},
        resource_template_uuid=RESOURCE_TEMPLATE_UUID,
        parent_uuid=None,
        klass="SampleTube",
        barcode="M1C-FIXTURE-017",
        name="M1C read-only fixture",
        config={},
        data={},
        disposition=disposition,
        material_kind=material_kind,
        version=1,
    )


class _ReadOnlyMaterialAdapter:
    """只提供 Material lookup，不提供资源树或任何写能力。"""

    def __init__(self, record: MaterialRecord | None) -> None:
        self._record = record
        self.requested_uuids: list[str] = []

    def get_material(
        self,
        material_uuid: str,
        *,
        uow: Any | None = None,
    ) -> MaterialRecord | None:
        del uow
        self.requested_uuids.append(material_uuid)
        if self._record is None or self._record.uuid != material_uuid:
            return None
        return self._record


class _AuthorityResourceSlotResolver:
    """把 Material authority facade 接到 02H 已冻结的 ``resolve`` port。"""

    def __init__(self, materials: MaterialModule) -> None:
        self._materials = materials
        self.calls: list[tuple[str, tuple[str, ...] | None]] = []

    def resolve(
        self,
        *,
        material_uuid: str,
        allowed_resource_template_uuids: tuple[str, ...] | None,
    ) -> Any:
        self.calls.append((material_uuid, allowed_resource_template_uuids))
        return self._materials.resolve_resource_slot(
            material_uuid=material_uuid,
            allowed_resource_template_uuids=allowed_resource_template_uuids,
        )


def _resolve(
    materials: MaterialModule,
    *,
    material_uuid: str = MATERIAL_UUID,
    allowed_resource_template_uuids: Any = None,
) -> Any:
    return materials.resolve_resource_slot(
        material_uuid=material_uuid,
        allowed_resource_template_uuids=allowed_resource_template_uuids,
    )


def _seed_workflow(
    store: WorkflowStore,
    *,
    allowed_resource_template_uuids: tuple[str, ...],
) -> None:
    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="M1C ResourceSlot task",
        tags=[],
        description=None,
        meta_data={
            "unilab": {
                "input_contract": {
                    "version": 1,
                    "parameters": [
                        {
                            "name": "sample",
                            "schema": {
                                "$slot": "ResourceSlot",
                                "allowed_resource_template_uuids": list(
                                    allowed_resource_template_uuids
                                ),
                            },
                            "required": True,
                        }
                    ],
                }
            }
        },
    )
    store.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[
            WorkflowNodeWrite(
                uuid=NODE_UUID,
                workflow_node_template_uuid=None,
                name="M1C active node",
                status="idle",
                type="compute",
                pose={},
                param={},
                execution_policy={},
                disabled=False,
                minimized=False,
                meta_data={},
            )
        ],
        edges=[],
    )


def _create_task(
    service: WorkflowService,
    external_slot: Any,
) -> dict[str, Any]:
    return service.create_workflow_task(
        workflow_uuid=WORKFLOW_UUID,
        run_mode="normal",
        target_node_uuid=None,
        input_value={"sample": external_slot},
        description=None,
        meta_data={},
    )


def _assert_identity(identity: Any) -> None:
    assert is_dataclass(identity)
    assert tuple(field.name for field in fields(identity)) == (
        "uuid",
        "resource_template_uuid",
    )
    assert identity.uuid == MATERIAL_UUID
    assert identity.resource_template_uuid == RESOURCE_TEMPLATE_UUID
    with pytest.raises(FrozenInstanceError):
        identity.uuid = MISSING_MATERIAL_UUID


def test_active_business_slot_resolves_canonical_frozen_identity_and_reopens(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    with _open_authority(database_path) as (store, materials):
        material_projection = _create_active_material(materials).to_dict()

        identity = _resolve(
            materials,
            material_uuid=MATERIAL_UUID.upper(),
            allowed_resource_template_uuids=(RESOURCE_TEMPLATE_UUID,),
        )

        _assert_identity(identity)
        assert materials.get_material(MATERIAL_UUID).to_dict() == material_projection
        assert store.list_tasks(page=1, page_size=20)["total"] == 0

    with _open_authority(database_path) as (_, reopened_materials):
        _assert_identity(
            _resolve(
                reopened_materials,
                material_uuid=MATERIAL_UUID.upper(),
                allowed_resource_template_uuids=(RESOURCE_TEMPLATE_UUID,),
            )
        )


def test_missing_material_is_not_found_without_side_effects(tmp_path: Path) -> None:
    with _open_authority(tmp_path / "workflow.db") as (store, materials):
        with pytest.raises(MaterialNotFound) as failure:
            _resolve(materials, material_uuid=MISSING_MATERIAL_UUID)

        assert failure.value.code == "not_found"
        assert store.list_tasks(page=1, page_size=20)["total"] == 0


def test_soft_deleted_material_is_not_found_at_the_public_facade() -> None:
    adapter = _ReadOnlyMaterialAdapter(
        _material_record(deleted_at="2026-08-02T01:00:00Z")
    )
    materials = _material_module(adapter)

    with pytest.raises(MaterialNotFound) as failure:
        _resolve(materials)

    assert failure.value.code == "not_found"
    assert adapter.requested_uuids == [MATERIAL_UUID]


def test_device_material_is_invalid_input_at_the_public_facade() -> None:
    adapter = _ReadOnlyMaterialAdapter(
        _material_record(disposition=None, material_kind="device")
    )
    materials = _material_module(adapter)

    with pytest.raises(MaterialInvalidInput) as failure:
        _resolve(materials)

    assert failure.value.code == "invalid_input"
    assert adapter.requested_uuids == [MATERIAL_UUID]


@pytest.mark.parametrize(
    "allowed_resource_template_uuids",
    [
        pytest.param((SECOND_RESOURCE_TEMPLATE_UUID,), id="template-mismatch"),
        pytest.param(("not-a-uuid",), id="invalid-allowlist-uuid"),
    ],
)
def test_template_mismatch_or_invalid_allowlist_is_invalid_input(
    tmp_path: Path,
    allowed_resource_template_uuids: Any,
) -> None:
    with _open_authority(tmp_path / "workflow.db") as (_, materials):
        material_projection = _create_active_material(materials).to_dict()

        with pytest.raises(MaterialInvalidInput) as failure:
            _resolve(
                materials,
                allowed_resource_template_uuids=allowed_resource_template_uuids,
            )

        assert failure.value.code == "invalid_input"
        assert materials.get_material(MATERIAL_UUID).to_dict() == material_projection


@pytest.mark.parametrize(
    "disposition",
    ["consumed", "discarded", "quarantined", "reconciling"],
)
def test_non_runnable_business_material_is_conflict(disposition: str) -> None:
    adapter = _ReadOnlyMaterialAdapter(_material_record(disposition=disposition))
    materials = _material_module(adapter)

    with pytest.raises(MaterialConflict) as failure:
        _resolve(materials)

    assert failure.value.code == "conflict"
    assert adapter.requested_uuids == [MATERIAL_UUID]


def test_lookup_only_adapter_is_sufficient_without_resource_tree_or_writes() -> None:
    adapter = _ReadOnlyMaterialAdapter(_material_record())
    materials = _material_module(adapter)

    identity = _resolve(materials)

    _assert_identity(identity)
    assert adapter.requested_uuids == [MATERIAL_UUID]


def test_workflow_task_freezes_authority_owned_slot_and_survives_reopen(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    with _open_authority(database_path) as (store, materials):
        material_projection = _create_active_material(materials).to_dict()
        _seed_workflow(
            store,
            allowed_resource_template_uuids=(
                RESOURCE_TEMPLATE_UUID,
                SECOND_RESOURCE_TEMPLATE_UUID,
            ),
        )
        resolver = _AuthorityResourceSlotResolver(materials)
        service = WorkflowService(store, resource_resolver=resolver)
        external = {"uuid": MATERIAL_UUID.upper()}

        task = _create_task(service, external)
        external["uuid"] = MISSING_MATERIAL_UUID

        assert resolver.calls == [
            (
                MATERIAL_UUID,
                (RESOURCE_TEMPLATE_UUID, SECOND_RESOURCE_TEMPLATE_UUID),
            )
        ]
        assert task["input"] == {
            "sample": {
                "uuid": MATERIAL_UUID,
                "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
            }
        }
        assert service.get_workflow_task(task["uuid"])["input"] == task["input"]
        assert len(service.list_workflow_node_jobs(task["uuid"])) == 1
        assert materials.get_material(MATERIAL_UUID).to_dict() == material_projection
        task_uuid = task["uuid"]

    reopened_store = WorkflowStore(database_path)
    try:
        reopened_service = WorkflowService(reopened_store)
        assert reopened_service.get_workflow_task(task_uuid)["input"] == {
            "sample": {
                "uuid": MATERIAL_UUID,
                "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
            }
        }
        assert len(reopened_service.list_workflow_node_jobs(task_uuid)) == 1
    finally:
        reopened_store.close()


@pytest.mark.parametrize(
    ("case", "expected_status", "expected_code"),
    [
        pytest.param("caller-template", 400, "invalid_input", id="caller-template"),
        pytest.param("template-mismatch", 400, "invalid_input", id="mismatch"),
        pytest.param("missing", 404, "not_found", id="missing"),
        pytest.param("non-runnable", 409, "conflict", id="non-runnable"),
    ],
)
def test_workflow_http_errors_are_stable_and_zero_task_job_write(
    tmp_path: Path,
    case: str,
    expected_status: int,
    expected_code: str,
) -> None:
    with _open_authority(tmp_path / "workflow.db") as (store, durable_materials):
        if case in {"caller-template", "template-mismatch"}:
            _create_active_material(durable_materials)
        materials = (
            _material_module(
                _ReadOnlyMaterialAdapter(_material_record(disposition="consumed"))
            )
            if case == "non-runnable"
            else durable_materials
        )
        allowed = (
            (SECOND_RESOURCE_TEMPLATE_UUID,)
            if case == "template-mismatch"
            else (RESOURCE_TEMPLATE_UUID,)
        )
        _seed_workflow(store, allowed_resource_template_uuids=allowed)
        resolver = _AuthorityResourceSlotResolver(materials)
        service = WorkflowService(store, resource_resolver=resolver)
        external: dict[str, str] = {
            "uuid": (MISSING_MATERIAL_UUID if case == "missing" else MATERIAL_UUID)
        }
        if case == "caller-template":
            external["resource_template_uuid"] = SECOND_RESOURCE_TEMPLATE_UUID

        with TestClient(
            create_workflow_app(service),
            raise_server_exceptions=False,
        ) as client:
            response = client.post(
                "/api/v1/workflow-tasks",
                json={
                    "workflow_uuid": WORKFLOW_UUID,
                    "input": {"sample": external},
                },
            )

        assert response.status_code == expected_status
        assert response.json()["error"]["code"] == expected_code
        assert service.list_workflow_tasks(workflow_uuid=WORKFLOW_UUID) == {
            "items": [],
            "total": 0,
            "page": 1,
            "page_size": 20,
        }
        assert resolver.calls == (
            []
            if case == "caller-template"
            else [
                (
                    external["uuid"],
                    allowed,
                )
            ]
        )
