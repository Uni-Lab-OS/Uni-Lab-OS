"""M1 Material Module 最小纵向 RED 合同。

行为只通过 public ``MaterialModule`` create/read port 观察。测试使用真实
SQLite durable adapter，并仅通过关闭后重开 adapter 证明持久性；不查询私有表，
也不把旧 Inventory service 当作新 Material Authority。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from unilabos.resources.authority import (
    MaterialConflict,
    MaterialModule,
    MaterialNotFound,
)
from unilabos.resources.authority.sqlite import SQLiteMaterialAdapter
from unilabos.workflow.store import WorkflowStore

MATERIAL_UUID = "50000000-0000-4000-8000-000000000017"
SECOND_MATERIAL_UUID = "50000000-0000-4000-8000-000000000018"
RESOURCE_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000017"

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


@contextmanager
def _open_material_module(database_path: Path) -> Iterator[MaterialModule]:
    adapter = SQLiteMaterialAdapter(database_path)
    try:
        yield MaterialModule(adapter)
    finally:
        adapter.close()


@contextmanager
def _open_runtime_authority(database_path: Path) -> Iterator[WorkflowStore]:
    coordinator = WorkflowStore(database_path)
    try:
        yield coordinator
    finally:
        coordinator.close()


def _observable_material(record: Any) -> dict[str, Any]:
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
        )

        with pytest.raises(MaterialConflict):
            materials.create_business_material(
                material_uuid=SECOND_MATERIAL_UUID,
                resource_template_uuid=RESOURCE_TEMPLATE_UUID,
                barcode="sample-017",
            )

        assert (
            _observable_material(materials.get_material(MATERIAL_UUID))
            == EXPECTED_INITIAL_MATERIAL
        )


def test_distinct_business_materials_may_share_empty_barcode(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"

    with _open_material_module(database_path) as materials:
        first = materials.create_business_material(
            material_uuid=MATERIAL_UUID,
            resource_template_uuid=RESOURCE_TEMPLATE_UUID,
            barcode="",
        )
        second = materials.create_business_material(
            material_uuid=SECOND_MATERIAL_UUID,
            resource_template_uuid=RESOURCE_TEMPLATE_UUID,
            barcode="",
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
        materials = MaterialModule(adapter)

        with pytest.raises(_RollbackSentinel):
            with coordinator.transaction() as uow:
                materials.create_business_material(
                    material_uuid=MATERIAL_UUID,
                    resource_template_uuid=RESOURCE_TEMPLATE_UUID,
                    barcode="SAMPLE-017",
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
        materials = MaterialModule(adapter)

        with coordinator.transaction() as uow:
            created = materials.create_business_material(
                material_uuid=MATERIAL_UUID,
                resource_template_uuid=RESOURCE_TEMPLATE_UUID,
                barcode="SAMPLE-017",
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


def test_coordinator_backed_adapter_close_keeps_workflow_store_open(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"

    with _open_runtime_authority(database_path) as coordinator:
        adapter = SQLiteMaterialAdapter.from_runtime_authority(coordinator)

        adapter.close()

        with coordinator.transaction() as uow:
            assert uow is not None
