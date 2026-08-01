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
UNKNOWN_MATERIAL_UUID = "50000000-0000-4000-8000-000000000099"
RESOURCE_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000017"
SECOND_RESOURCE_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000018"
UNKNOWN_RESOURCE_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000099"
MATERIAL_CLASS = "SampleTube"
SECOND_MATERIAL_CLASS = "Microplate"
MATERIAL_NAME = "Sample 17"
SECOND_MATERIAL_NAME = "Sample 18"
SITE_UUID = "60000000-0000-4000-8000-000000000017"
SECOND_SITE_UUID = "60000000-0000-4000-8000-000000000018"

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


def _create_site_with_references(
    materials: MaterialModule,
    *,
    material_uuid: str,
    occupied_material_uuid: str | None,
    allowed_resource_template_uuids: list[str],
    site_uuid: str = SITE_UUID,
    name: str = "A1",
) -> Any:
    return materials.create_site(
        site_uuid=site_uuid,
        description="Reference validation site",
        meta_data={"slice": "m1b-references"},
        material_uuid=material_uuid,
        name=name,
        sort_order=0,
        allowed_resource_template_uuids=allowed_resource_template_uuids,
        occupied_material_uuid=occupied_material_uuid,
        position_x=0.0,
        position_y=0.0,
        position_z=0.0,
        depth=1.0,
        length=1.0,
        width=1.0,
    )


def _create_placement_materials(materials: MaterialModule) -> None:
    materials.create_business_material(
        material_uuid=MATERIAL_UUID,
        resource_template_uuid=RESOURCE_TEMPLATE_UUID,
        barcode="OWNER-PLACEMENT-017",
        name="Placement owner",
    )
    materials.create_business_material(
        material_uuid=SECOND_MATERIAL_UUID,
        resource_template_uuid=SECOND_RESOURCE_TEMPLATE_UUID,
        barcode="OCCUPANT-PLACEMENT-018",
        name="Placement occupant",
    )


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


@pytest.mark.parametrize(
    (
        "owner_uuid",
        "occupant_uuid",
        "allowed_template_uuids",
        "expected_error",
    ),
    [
        pytest.param(
            UNKNOWN_MATERIAL_UUID,
            None,
            [RESOURCE_TEMPLATE_UUID],
            MaterialNotFound,
            id="unknown-owner",
        ),
        pytest.param(
            MATERIAL_UUID,
            UNKNOWN_MATERIAL_UUID,
            [RESOURCE_TEMPLATE_UUID, SECOND_RESOURCE_TEMPLATE_UUID],
            MaterialNotFound,
            id="unknown-occupant",
        ),
        pytest.param(
            MATERIAL_UUID,
            None,
            [UNKNOWN_RESOURCE_TEMPLATE_UUID],
            MaterialInvalidInput,
            id="unregistered-allowlist-template",
        ),
        pytest.param(
            MATERIAL_UUID,
            SECOND_MATERIAL_UUID,
            [RESOURCE_TEMPLATE_UUID],
            MaterialInvalidInput,
            id="occupant-template-not-allowed",
        ),
    ],
)
def test_site_create_rejects_invalid_authority_references_without_side_effects(
    tmp_path: Path,
    owner_uuid: str,
    occupant_uuid: str | None,
    allowed_template_uuids: list[str],
    expected_error: type[Exception],
) -> None:
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
        material_snapshot = {
            MATERIAL_UUID: materials.get_material(MATERIAL_UUID).to_dict(),
            SECOND_MATERIAL_UUID: materials.get_material(
                SECOND_MATERIAL_UUID
            ).to_dict(),
        }

        with pytest.raises(expected_error) as error:
            _create_site_with_references(
                materials,
                material_uuid=owner_uuid,
                occupied_material_uuid=occupant_uuid,
                allowed_resource_template_uuids=allowed_template_uuids,
            )

        assert error.type is expected_error
        with pytest.raises(MaterialNotFound):
            materials.get_site(SITE_UUID)
        assert {
            material_uuid: materials.get_material(material_uuid).to_dict()
            for material_uuid in material_snapshot
        } == material_snapshot

    with _open_material_module(database_path) as reopened_materials:
        with pytest.raises(MaterialNotFound):
            reopened_materials.get_site(SITE_UUID)
        assert {
            material_uuid: reopened_materials.get_material(material_uuid).to_dict()
            for material_uuid in material_snapshot
        } == material_snapshot


def test_site_rejects_owner_as_its_own_occupant_without_write(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    with _open_material_module(database_path) as materials:
        _create_placement_materials(materials)
        owner_projection = materials.get_material(MATERIAL_UUID).to_dict()

        with pytest.raises(MaterialInvalidInput) as error:
            _create_site_with_references(
                materials,
                material_uuid=MATERIAL_UUID,
                occupied_material_uuid=MATERIAL_UUID,
                allowed_resource_template_uuids=[RESOURCE_TEMPLATE_UUID],
            )

        assert error.type is MaterialInvalidInput
        with pytest.raises(MaterialNotFound):
            materials.get_site(SITE_UUID)
        assert materials.get_material(MATERIAL_UUID).to_dict() == owner_projection

    with _open_material_module(database_path) as reopened_materials:
        with pytest.raises(MaterialNotFound):
            reopened_materials.get_site(SITE_UUID)
        assert (
            reopened_materials.get_material(MATERIAL_UUID).to_dict() == owner_projection
        )


@pytest.mark.parametrize(
    ("first_placement", "rejected_placement"),
    [
        pytest.param(
            {
                "material_uuid": MATERIAL_UUID,
                "occupied_material_uuid": SECOND_MATERIAL_UUID,
                "allowed_resource_template_uuids": [SECOND_RESOURCE_TEMPLATE_UUID],
                "name": "A1",
            },
            {
                "material_uuid": MATERIAL_UUID,
                "occupied_material_uuid": SECOND_MATERIAL_UUID,
                "allowed_resource_template_uuids": [SECOND_RESOURCE_TEMPLATE_UUID],
                "name": "B1",
            },
            id="occupant-already-placed",
        ),
        pytest.param(
            {
                "material_uuid": MATERIAL_UUID,
                "occupied_material_uuid": SECOND_MATERIAL_UUID,
                "allowed_resource_template_uuids": [SECOND_RESOURCE_TEMPLATE_UUID],
                "name": "A1",
            },
            {
                "material_uuid": SECOND_MATERIAL_UUID,
                "occupied_material_uuid": MATERIAL_UUID,
                "allowed_resource_template_uuids": [RESOURCE_TEMPLATE_UUID],
                "name": "B1",
            },
            id="placement-cycle",
        ),
        pytest.param(
            {
                "material_uuid": MATERIAL_UUID,
                "occupied_material_uuid": None,
                "allowed_resource_template_uuids": [],
                "name": "Ä1",
            },
            {
                "material_uuid": MATERIAL_UUID,
                "occupied_material_uuid": None,
                "allowed_resource_template_uuids": [],
                "name": "ä1",
            },
            id="unicode-casefold-owner-name",
        ),
    ],
)
def test_site_rejects_conflicting_second_placement_and_preserves_first(
    tmp_path: Path,
    first_placement: dict[str, Any],
    rejected_placement: dict[str, Any],
) -> None:
    database_path = tmp_path / "workflow.db"
    with _open_material_module(database_path) as materials:
        _create_placement_materials(materials)
        first = _create_site_with_references(
            materials,
            site_uuid=SITE_UUID,
            **first_placement,
        )
        first_projection = first.to_dict()

        with pytest.raises(MaterialConflict) as error:
            _create_site_with_references(
                materials,
                site_uuid=SECOND_SITE_UUID,
                **rejected_placement,
            )

        assert error.type is MaterialConflict
        assert materials.get_site(SITE_UUID).to_dict() == first_projection
        with pytest.raises(MaterialNotFound):
            materials.get_site(SECOND_SITE_UUID)

    with _open_material_module(database_path) as reopened_materials:
        assert reopened_materials.get_site(SITE_UUID).to_dict() == first_projection
        with pytest.raises(MaterialNotFound):
            reopened_materials.get_site(SECOND_SITE_UUID)


def test_empty_site_template_allowlist_accepts_any_registered_occupant(
    tmp_path: Path,
) -> None:
    from unilabos.resources.authority import SiteRecord

    database_path = tmp_path / "workflow.db"
    with _open_material_module(database_path) as materials:
        _create_placement_materials(materials)

        created = _create_site_with_references(
            materials,
            material_uuid=MATERIAL_UUID,
            occupied_material_uuid=SECOND_MATERIAL_UUID,
            allowed_resource_template_uuids=[],
        )
        projection = created.to_dict()

        assert isinstance(created, SiteRecord)
        assert projection["material_uuid"] == MATERIAL_UUID
        assert projection["occupied_material_uuid"] == SECOND_MATERIAL_UUID
        assert projection["allowed_resource_template_uuids"] == []
        assert materials.get_site(SITE_UUID).to_dict() == projection

    with _open_material_module(database_path) as reopened_materials:
        assert reopened_materials.get_site(SITE_UUID).to_dict() == projection
