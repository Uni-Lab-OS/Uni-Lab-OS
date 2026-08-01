"""M1 Material Module 最小纵向 RED 合同。

行为只通过 public ``MaterialModule`` create/read port 观察。测试使用真实
SQLite durable adapter，并仅通过关闭后重开 adapter 证明持久性；不查询私有表，
也不把旧 Inventory service 当作新 Material Authority。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from unilabos.resources.authority import (
    MaterialAuthorityUnavailable,
    MaterialConflict,
    MaterialInvalidInput,
    MaterialModule,
    MaterialNotFound,
    MaterialRecord,
)
from unilabos.resources.authority.sqlite import SQLiteMaterialAdapter
from unilabos.workflow.store import WorkflowStore

MATERIAL_UUID = "50000000-0000-4000-8000-000000000017"
SECOND_MATERIAL_UUID = "50000000-0000-4000-8000-000000000018"
RESOURCE_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000017"
SECOND_RESOURCE_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000018"
UNKNOWN_RESOURCE_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000099"
MATERIAL_CLASS = "SampleTube"
SECOND_MATERIAL_CLASS = "Microplate"
MATERIAL_NAME = "Sample 17"
SECOND_MATERIAL_NAME = "Sample 18"
SITE_UUID = "60000000-0000-4000-8000-000000000017"

EXPECTED_INITIAL_MATERIAL = {
    "uuid": MATERIAL_UUID,
    "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
    "parent_uuid": None,
    "barcode": "SAMPLE-017",
    "disposition": "active",
    "version": 1,
    "deleted_at": None,
}


class _RollbackSentinel(RuntimeError):
    pass


def _resource_template_snapshot() -> Mapping[str, object]:
    from unilabos.resources.authority import ResourceTemplateIdentity

    identities = (
        ResourceTemplateIdentity(
            uuid=RESOURCE_TEMPLATE_UUID,
            material_class=MATERIAL_CLASS,
        ),
        ResourceTemplateIdentity(
            uuid=SECOND_RESOURCE_TEMPLATE_UUID,
            material_class=SECOND_MATERIAL_CLASS,
        ),
    )
    return MappingProxyType({identity.uuid: identity for identity in identities})


def _material_module(adapter: SQLiteMaterialAdapter) -> MaterialModule:
    return MaterialModule(
        adapter,
        resource_templates=_resource_template_snapshot(),
    )


@contextmanager
def _open_material_module(database_path: Path) -> Iterator[MaterialModule]:
    adapter = SQLiteMaterialAdapter(database_path)
    try:
        yield _material_module(adapter)
    finally:
        adapter.close()


@contextmanager
def _open_runtime_authority(database_path: Path) -> Iterator[WorkflowStore]:
    coordinator = WorkflowStore(database_path)
    try:
        yield coordinator
    finally:
        coordinator.close()


def _observable_material(record: MaterialRecord) -> dict[str, Any]:
    return {
        "uuid": record.uuid,
        "resource_template_uuid": record.resource_template_uuid,
        "parent_uuid": record.parent_uuid,
        "barcode": record.barcode,
        "disposition": record.disposition,
        "version": record.version,
        "deleted_at": record.deleted_at,
    }


def test_business_material_create_read_survives_sqlite_reopen(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"

    with _open_material_module(database_path) as materials:
        created = materials.create_business_material(
            material_uuid=MATERIAL_UUID,
            resource_template_uuid=RESOURCE_TEMPLATE_UUID,
            barcode="SAMPLE-017",
            name=MATERIAL_NAME,
        )

        assert _observable_material(created) == EXPECTED_INITIAL_MATERIAL
        assert (
            _observable_material(materials.get_material(MATERIAL_UUID))
            == EXPECTED_INITIAL_MATERIAL
        )

    with _open_material_module(database_path) as reopened_materials:
        assert (
            _observable_material(reopened_materials.get_material(MATERIAL_UUID))
            == EXPECTED_INITIAL_MATERIAL
        )


def test_business_material_barcode_is_unique_case_insensitively(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"

    with _open_material_module(database_path) as materials:
        materials.create_business_material(
            material_uuid=MATERIAL_UUID,
            resource_template_uuid=RESOURCE_TEMPLATE_UUID,
            barcode="SAMPLE-017",
            name=MATERIAL_NAME,
        )

        with pytest.raises(MaterialConflict):
            materials.create_business_material(
                material_uuid=SECOND_MATERIAL_UUID,
                resource_template_uuid=RESOURCE_TEMPLATE_UUID,
                barcode="sample-017",
                name=SECOND_MATERIAL_NAME,
            )

        assert (
            _observable_material(materials.get_material(MATERIAL_UUID))
            == EXPECTED_INITIAL_MATERIAL
        )


def test_business_material_barcode_uniqueness_uses_unicode_casefold(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"

    with _open_material_module(database_path) as materials:
        materials.create_business_material(
            material_uuid=MATERIAL_UUID,
            resource_template_uuid=RESOURCE_TEMPLATE_UUID,
            barcode="ÄBC",
            name=MATERIAL_NAME,
        )

        with pytest.raises(MaterialConflict):
            materials.create_business_material(
                material_uuid=SECOND_MATERIAL_UUID,
                resource_template_uuid=RESOURCE_TEMPLATE_UUID,
                barcode="äbc",
                name=SECOND_MATERIAL_NAME,
            )

        original = materials.get_material(MATERIAL_UUID)
        assert (original.uuid, original.barcode) == (MATERIAL_UUID, "ÄBC")


def test_distinct_business_materials_may_share_empty_barcode(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"

    with _open_material_module(database_path) as materials:
        first = materials.create_business_material(
            material_uuid=MATERIAL_UUID,
            resource_template_uuid=RESOURCE_TEMPLATE_UUID,
            barcode="",
            name=MATERIAL_NAME,
        )
        second = materials.create_business_material(
            material_uuid=SECOND_MATERIAL_UUID,
            resource_template_uuid=RESOURCE_TEMPLATE_UUID,
            barcode="",
            name=SECOND_MATERIAL_NAME,
        )

        assert _observable_material(first) == {
            "uuid": MATERIAL_UUID,
            "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
            "parent_uuid": None,
            "barcode": "",
            "disposition": "active",
            "version": 1,
            "deleted_at": None,
        }
        assert _observable_material(second) == {
            "uuid": SECOND_MATERIAL_UUID,
            "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
            "parent_uuid": None,
            "barcode": "",
            "disposition": "active",
            "version": 1,
            "deleted_at": None,
        }


def test_runtime_authority_uow_rolls_back_material_with_outer_transaction(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"

    with _open_runtime_authority(database_path) as coordinator:
        adapter = SQLiteMaterialAdapter.from_runtime_authority(coordinator)
        materials = _material_module(adapter)

        with pytest.raises(_RollbackSentinel):
            with coordinator.transaction() as uow:
                materials.create_business_material(
                    material_uuid=MATERIAL_UUID,
                    resource_template_uuid=RESOURCE_TEMPLATE_UUID,
                    barcode="SAMPLE-017",
                    name=MATERIAL_NAME,
                    uow=uow,
                )
                raise _RollbackSentinel

        with pytest.raises(MaterialNotFound):
            materials.get_material(MATERIAL_UUID)


def test_runtime_authority_uow_commits_material_with_outer_transaction(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"

    with _open_runtime_authority(database_path) as coordinator:
        adapter = SQLiteMaterialAdapter.from_runtime_authority(coordinator)
        materials = _material_module(adapter)

        with coordinator.transaction() as uow:
            created = materials.create_business_material(
                material_uuid=MATERIAL_UUID,
                resource_template_uuid=RESOURCE_TEMPLATE_UUID,
                barcode="SAMPLE-017",
                name=MATERIAL_NAME,
                uow=uow,
            )

            assert _observable_material(created) == EXPECTED_INITIAL_MATERIAL
            assert (
                _observable_material(materials.get_material(MATERIAL_UUID, uow=uow))
                == EXPECTED_INITIAL_MATERIAL
            )

        assert (
            _observable_material(materials.get_material(MATERIAL_UUID))
            == EXPECTED_INITIAL_MATERIAL
        )


def test_material_module_rejects_uow_from_another_runtime_authority(
    tmp_path: Path,
) -> None:
    with (
        _open_runtime_authority(tmp_path / "authority-a.db") as authority_a,
        _open_runtime_authority(tmp_path / "authority-b.db") as authority_b,
    ):
        materials_a = _material_module(
            SQLiteMaterialAdapter.from_runtime_authority(authority_a)
        )
        materials_b = _material_module(
            SQLiteMaterialAdapter.from_runtime_authority(authority_b)
        )

        with authority_b.transaction() as foreign_uow:
            with pytest.raises(MaterialAuthorityUnavailable) as error:
                materials_a.create_business_material(
                    material_uuid=MATERIAL_UUID,
                    resource_template_uuid=RESOURCE_TEMPLATE_UUID,
                    barcode="SAMPLE-017",
                    name=MATERIAL_NAME,
                    uow=foreign_uow,
                )

        assert error.type is MaterialAuthorityUnavailable
        with pytest.raises(MaterialNotFound):
            materials_a.get_material(MATERIAL_UUID)
        with pytest.raises(MaterialNotFound):
            materials_b.get_material(MATERIAL_UUID)

        with authority_a.transaction() as uow_a:
            assert uow_a is not None
        with authority_b.transaction() as uow_b:
            assert uow_b is not None


def test_coordinator_backed_adapter_close_keeps_workflow_store_open(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"

    with _open_runtime_authority(database_path) as coordinator:
        adapter = SQLiteMaterialAdapter.from_runtime_authority(coordinator)

        adapter.close()

        with coordinator.transaction() as uow:
            assert uow is not None


def test_resource_template_identity_is_public_and_backend_aligned() -> None:
    from unilabos.resources.authority import ResourceTemplateIdentity

    identity = ResourceTemplateIdentity(
        uuid=RESOURCE_TEMPLATE_UUID,
        material_class=MATERIAL_CLASS,
    )

    assert identity.uuid == RESOURCE_TEMPLATE_UUID
    assert identity.material_class == MATERIAL_CLASS


def test_complete_backend_material_fields_survive_create_read_and_reopen(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"

    with _open_material_module(database_path) as materials:
        created = materials.create_business_material(
            material_uuid=MATERIAL_UUID,
            resource_template_uuid=RESOURCE_TEMPLATE_UUID,
            barcode="SAMPLE-017",
            name=MATERIAL_NAME,
            description="Primary sample for reviewer B2",
            meta_data={"source": "reviewer-b2", "labels": ["fragile"]},
            config={"volume_ul": 125.5, "sterile": True},
            data={"measurements": [1, 2.5], "note": None},
        )
        expected_projection = {
            "uuid": MATERIAL_UUID,
            "create_time": created.create_time,
            "update_time": created.update_time,
            "deleted_at": None,
            "description": "Primary sample for reviewer B2",
            "meta_data": {"source": "reviewer-b2", "labels": ["fragile"]},
            "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
            "parent_uuid": None,
            "class": MATERIAL_CLASS,
            "barcode": "SAMPLE-017",
            "name": MATERIAL_NAME,
            "config": {"volume_ul": 125.5, "sterile": True},
            "data": {"measurements": [1, 2.5], "note": None},
            "disposition": "active",
            "material_kind": "business",
            "version": 1,
        }

        assert created.to_dict() == expected_projection
        assert "resource_class" not in created.to_dict()
        assert materials.get_material(MATERIAL_UUID).to_dict() == expected_projection

    with _open_material_module(database_path) as reopened_materials:
        assert (
            reopened_materials.get_material(MATERIAL_UUID).to_dict()
            == expected_projection
        )


def test_unknown_resource_template_is_rejected_without_material_write(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"

    with _open_material_module(database_path) as materials:
        with pytest.raises(MaterialInvalidInput):
            materials.create_business_material(
                material_uuid=MATERIAL_UUID,
                resource_template_uuid=UNKNOWN_RESOURCE_TEMPLATE_UUID,
                barcode="SAMPLE-017",
                name=MATERIAL_NAME,
            )

        with pytest.raises(MaterialNotFound):
            materials.get_material(MATERIAL_UUID)


@pytest.mark.parametrize("blank_name", ["", "   "])
def test_blank_material_name_is_rejected_without_material_write(
    tmp_path: Path,
    blank_name: str,
) -> None:
    database_path = tmp_path / "workflow.db"

    with _open_material_module(database_path) as materials:
        with pytest.raises(MaterialInvalidInput):
            materials.create_business_material(
                material_uuid=MATERIAL_UUID,
                resource_template_uuid=RESOURCE_TEMPLATE_UUID,
                barcode="SAMPLE-017",
                name=blank_name,
            )

        with pytest.raises(MaterialNotFound):
            materials.get_material(MATERIAL_UUID)


def test_material_class_cannot_be_supplied_by_create_caller(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"

    with _open_material_module(database_path) as materials:
        with pytest.raises(TypeError):
            materials.create_business_material(
                material_uuid=MATERIAL_UUID,
                resource_template_uuid=RESOURCE_TEMPLATE_UUID,
                barcode="SAMPLE-017",
                name=MATERIAL_NAME,
                **{"class": "CallerForgedClass"},
            )

        with pytest.raises(MaterialNotFound):
            materials.get_material(MATERIAL_UUID)


def test_adapter_init_wraps_filesystem_failure_without_path_disclosure(
    tmp_path: Path,
) -> None:
    non_directory_parent = tmp_path / "ordinary-file"
    non_directory_parent.write_text("not a directory", encoding="utf-8")
    database_path = non_directory_parent / "workflow.db"

    with pytest.raises(MaterialAuthorityUnavailable) as error:
        SQLiteMaterialAdapter(database_path)

    assert error.type is MaterialAuthorityUnavailable
    assert str(error.value) == "failed to initialize Material Authority"
    assert str(database_path) not in str(error.value)


def test_adapter_init_wraps_nul_path_without_raw_error_disclosure() -> None:
    database_path = "review\x00.db"

    with pytest.raises(MaterialAuthorityUnavailable) as error:
        SQLiteMaterialAdapter(database_path)

    assert error.type is MaterialAuthorityUnavailable
    assert str(error.value) == "failed to initialize Material Authority"
    assert database_path not in str(error.value)


def test_generic_runtime_authority_uow_excludes_sqlite_collation_capability() -> None:
    from unilabos.resources.authority.models import RuntimeAuthorityUnitOfWork

    assert hasattr(RuntimeAuthorityUnitOfWork, "execute")
    assert not hasattr(RuntimeAuthorityUnitOfWork, "create_collation")


def test_site_create_read_reopen_preserves_backend_projection_and_composition(
    tmp_path: Path,
) -> None:
    from unilabos.resources.authority import SiteRecord

    database_path = tmp_path / "workflow.db"
    with _open_material_module(database_path) as materials:
        materials.create_business_material(
            material_uuid=MATERIAL_UUID,
            resource_template_uuid=RESOURCE_TEMPLATE_UUID,
            barcode="OWNER-017",
            name="Deck owner",
        )
        materials.create_business_material(
            material_uuid=SECOND_MATERIAL_UUID,
            resource_template_uuid=SECOND_RESOURCE_TEMPLATE_UUID,
            barcode="OCCUPANT-018",
            name="Placed microplate",
        )

        created = materials.create_site(
            site_uuid=SITE_UUID,
            description="Cold deck position A1",
            meta_data={"zone": "cold", "labels": ["robot", "primary"]},
            material_uuid=MATERIAL_UUID,
            name="A1",
            sort_order=7,
            allowed_resource_template_uuids=[
                SECOND_RESOURCE_TEMPLATE_UUID,
                RESOURCE_TEMPLATE_UUID,
            ],
            occupied_material_uuid=SECOND_MATERIAL_UUID,
            position_x=1.25,
            position_y=-2.5,
            position_z=3.75,
            depth=10.0,
            length=20.5,
            width=30.25,
        )
        expected_projection = {
            "uuid": SITE_UUID,
            "create_time": created.create_time,
            "update_time": created.update_time,
            "deleted_at": None,
            "description": "Cold deck position A1",
            "meta_data": {"zone": "cold", "labels": ["robot", "primary"]},
            "material_uuid": MATERIAL_UUID,
            "name": "A1",
            "sort_order": 7,
            "allowed_resource_template_uuids": [
                RESOURCE_TEMPLATE_UUID,
                SECOND_RESOURCE_TEMPLATE_UUID,
            ],
            "occupied_material_uuid": SECOND_MATERIAL_UUID,
            "position_x": 1.25,
            "position_y": -2.5,
            "position_z": 3.75,
            "depth": 10.0,
            "length": 20.5,
            "width": 30.25,
            "version": 1,
        }

        assert isinstance(created, SiteRecord)
        assert created.to_dict() == expected_projection
        assert materials.get_site(SITE_UUID).to_dict() == expected_projection
        assert (
            materials.get_material(MATERIAL_UUID).parent_uuid,
            materials.get_material(SECOND_MATERIAL_UUID).parent_uuid,
        ) == (None, None)

    with _open_material_module(database_path) as reopened_materials:
        reopened = reopened_materials.get_site(SITE_UUID)
        assert isinstance(reopened, SiteRecord)
        assert reopened.to_dict() == expected_projection
        assert (
            reopened_materials.get_material(MATERIAL_UUID).parent_uuid,
            reopened_materials.get_material(SECOND_MATERIAL_UUID).parent_uuid,
        ) == (None, None)
