"""M1B 在 Material Authority 公开边界上的评审修复 RED 合同。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

import pytest

from unilabos.resources.authority import (
    MaterialConflict,
    MaterialError,
    MaterialInvalidInput,
    MaterialModule,
    MaterialNotFound,
    ResourceTemplateIdentity,
)
from unilabos.resources.authority.sqlite import SQLiteMaterialAdapter
from unilabos.workflow.store import WorkflowStore

MATERIAL_UUID = "50000000-0000-4000-8000-000000000017"
UNKNOWN_MATERIAL_UUID = "50000000-0000-4000-8000-000000000099"
RESOURCE_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000017"
SECOND_RESOURCE_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000018"
SITE_UUID = "60000000-0000-4000-8000-000000000017"


def _resource_templates() -> Mapping[str, object]:
    identities = (
        ResourceTemplateIdentity(
            uuid=RESOURCE_TEMPLATE_UUID,
            material_class="Deck",
        ),
        ResourceTemplateIdentity(
            uuid=SECOND_RESOURCE_TEMPLATE_UUID,
            material_class="Microplate",
        ),
    )
    return MappingProxyType({identity.uuid: identity for identity in identities})


def _material_module(coordinator: WorkflowStore) -> MaterialModule:
    return MaterialModule(
        SQLiteMaterialAdapter.from_runtime_authority(coordinator),
        resource_templates=_resource_templates(),
    )


def _create_owner(materials: MaterialModule) -> None:
    materials.create_business_material(
        material_uuid=MATERIAL_UUID,
        resource_template_uuid=RESOURCE_TEMPLATE_UUID,
        barcode="OWNER-REVIEW-FIX",
        name="Review-fix deck owner",
    )


def _site_arguments() -> dict[str, object]:
    return {
        "site_uuid": SITE_UUID,
        "description": "M1B review-fix site",
        "meta_data": {"slice": "m1b-review-fixes"},
        "material_uuid": MATERIAL_UUID,
        "name": "A1",
        "sort_order": 0,
        "allowed_resource_template_uuids": [
            RESOURCE_TEMPLATE_UUID,
            SECOND_RESOURCE_TEMPLATE_UUID,
        ],
        "occupied_material_uuid": None,
        "position_x": 0.0,
        "position_y": 0.0,
        "position_z": 0.0,
        "depth": 1.0,
        "length": 1.0,
        "width": 1.0,
    }


def test_borrowed_uow_site_create_is_atomic_when_allowlist_insert_fails(
    tmp_path: Path,
) -> None:
    coordinator = WorkflowStore(tmp_path / "workflow.db")
    try:
        materials = _material_module(coordinator)
        _create_owner(materials)

        # SQLite 是唯一的故障注入边界；写入 Site 和首个白名单行后，
        # 第二个白名单行会失败。
        with coordinator.transaction() as sqlite:
            sqlite.execute(
                f"""
                CREATE TRIGGER inject_second_site_allowlist_failure
                BEFORE INSERT ON site_allowed_resource_template
                WHEN NEW.resource_template_uuid = '{SECOND_RESOURCE_TEMPLATE_UUID}'
                BEGIN
                    SELECT RAISE(ABORT, 'injected allowlist write failure');
                END
                """
            )

        caught_error: MaterialError | None = None
        with coordinator.transaction() as uow:
            try:
                materials.create_site(**_site_arguments(), uow=uow)
            except MaterialError as error:
                caught_error = error

        assert caught_error is not None
        with pytest.raises(MaterialNotFound):
            materials.get_site(SITE_UUID)
    finally:
        coordinator.close()


@pytest.mark.parametrize("field", ["position_x", "sort_order"])
def test_site_create_rejects_unrepresentable_numbers_without_write(
    tmp_path: Path,
    field: str,
) -> None:
    coordinator = WorkflowStore(tmp_path / "workflow.db")
    try:
        materials = _material_module(coordinator)
        _create_owner(materials)
        arguments = _site_arguments()
        arguments[field] = 10**400

        caught_error: Exception | None = None
        try:
            materials.create_site(**arguments)
        except (MaterialError, OverflowError) as error:
            caught_error = error

        assert caught_error is not None
        with pytest.raises(MaterialNotFound):
            materials.get_site(SITE_UUID)
        assert type(caught_error) is MaterialInvalidInput
    finally:
        coordinator.close()


def test_site_create_reports_missing_shared_owner_and_occupant_before_self_occupancy(
    tmp_path: Path,
) -> None:
    coordinator = WorkflowStore(tmp_path / "workflow.db")
    try:
        materials = _material_module(coordinator)
        arguments = _site_arguments()
        arguments["material_uuid"] = UNKNOWN_MATERIAL_UUID
        arguments["occupied_material_uuid"] = UNKNOWN_MATERIAL_UUID

        caught_error: MaterialError | None = None
        try:
            materials.create_site(**arguments)
        except MaterialError as error:
            caught_error = error

        assert caught_error is not None
        with pytest.raises(MaterialNotFound):
            materials.get_site(SITE_UUID)
        assert type(caught_error) is MaterialNotFound
    finally:
        coordinator.close()


def test_site_create_reports_existing_self_occupancy_before_allowlist_mismatch(
    tmp_path: Path,
) -> None:
    coordinator = WorkflowStore(tmp_path / "workflow.db")
    try:
        materials = _material_module(coordinator)
        _create_owner(materials)
        arguments = _site_arguments()
        arguments["occupied_material_uuid"] = MATERIAL_UUID
        arguments["allowed_resource_template_uuids"] = [SECOND_RESOURCE_TEMPLATE_UUID]

        caught_error: MaterialError | None = None
        try:
            materials.create_site(**arguments)
        except MaterialError as error:
            caught_error = error

        assert caught_error is not None
        with pytest.raises(MaterialNotFound):
            materials.get_site(SITE_UUID)
        assert type(caught_error) is MaterialConflict
    finally:
        coordinator.close()
