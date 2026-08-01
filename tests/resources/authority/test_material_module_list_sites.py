"""MaterialModule 按 owner 读取 active Site 的最小公开 RED。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType

import pytest

from unilabos.resources.authority import (
    MaterialModule,
    MaterialNotFound,
    ResourceTemplateIdentity,
)
from unilabos.resources.authority.sqlite import SQLiteMaterialAdapter
from unilabos.workflow.store import WorkflowStore

OWNER_UUID = "70000000-0000-4000-8000-000000000001"
OTHER_OWNER_UUID = "70000000-0000-4000-8000-000000000002"
OWNER_TEMPLATE_UUID = "71000000-0000-4000-8000-000000000001"
TARGET_TEMPLATE_UUID = "71000000-0000-4000-8000-000000000002"
LOW_SITE_UUID = "72000000-0000-4000-8000-000000000001"
HIGH_SITE_UUID = "72000000-0000-4000-8000-000000000002"
LATE_SITE_UUID = "72000000-0000-4000-8000-000000000003"
DELETED_SITE_UUID = "72000000-0000-4000-8000-000000000004"
FOREIGN_SITE_UUID = "72000000-0000-4000-8000-000000000005"


def _resource_templates() -> Mapping[str, object]:
    identities = (
        ResourceTemplateIdentity(
            uuid=OWNER_TEMPLATE_UUID,
            material_class="Warehouse",
        ),
        ResourceTemplateIdentity(
            uuid=TARGET_TEMPLATE_UUID,
            material_class="Microplate",
        ),
    )
    return MappingProxyType({item.uuid: item for item in identities})


def _material_module(store: WorkflowStore) -> MaterialModule:
    return MaterialModule(
        SQLiteMaterialAdapter.from_runtime_authority(store),
        resource_templates=_resource_templates(),
    )


@contextmanager
def _opened_material_module(
    database_path: Path,
) -> Iterator[tuple[WorkflowStore, MaterialModule]]:
    store = WorkflowStore(database_path)
    try:
        yield store, _material_module(store)
    finally:
        store.close()


def _create_site(
    materials: MaterialModule,
    *,
    site_uuid: str,
    material_uuid: str,
    sort_order: int,
) -> None:
    materials.create_site(
        site_uuid=site_uuid,
        description=None,
        meta_data={},
        material_uuid=material_uuid,
        name=f"Site {site_uuid}",
        sort_order=sort_order,
        allowed_resource_template_uuids=[TARGET_TEMPLATE_UUID],
        occupied_material_uuid=None,
        position_x=0.0,
        position_y=0.0,
        position_z=0.0,
        depth=1.0,
        length=1.0,
        width=1.0,
    )


def _seed_sites(store: WorkflowStore, materials: MaterialModule) -> None:
    for material_uuid, barcode in (
        (OWNER_UUID, "OWNER-1"),
        (OTHER_OWNER_UUID, "OWNER-2"),
    ):
        materials.create_business_material(
            material_uuid=material_uuid,
            resource_template_uuid=OWNER_TEMPLATE_UUID,
            barcode=barcode,
            name=barcode,
        )
    # 创建顺序故意与合同顺序不同。
    _create_site(
        materials,
        site_uuid=HIGH_SITE_UUID,
        material_uuid=OWNER_UUID,
        sort_order=1,
    )
    _create_site(
        materials,
        site_uuid=LATE_SITE_UUID,
        material_uuid=OWNER_UUID,
        sort_order=2,
    )
    _create_site(
        materials,
        site_uuid=LOW_SITE_UUID,
        material_uuid=OWNER_UUID,
        sort_order=1,
    )
    _create_site(
        materials,
        site_uuid=DELETED_SITE_UUID,
        material_uuid=OWNER_UUID,
        sort_order=0,
    )
    _create_site(
        materials,
        site_uuid=FOREIGN_SITE_UUID,
        material_uuid=OTHER_OWNER_UUID,
        sort_order=0,
    )
    with store.transaction() as uow:
        uow.execute(
            "UPDATE site SET deleted_at = ?, update_time = ? WHERE uuid = ?",
            (
                "2026-08-02T01:00:00Z",
                "2026-08-02T01:00:00Z",
                DELETED_SITE_UUID,
            ),
        )


def _assert_owner_site_projection(materials: MaterialModule) -> None:
    sites = materials.list_sites(OWNER_UUID)

    assert [item.uuid for item in sites] == [
        LOW_SITE_UUID,
        HIGH_SITE_UUID,
        LATE_SITE_UUID,
    ]
    assert [(item.sort_order, item.uuid) for item in sites] == sorted(
        (item.sort_order, item.uuid) for item in sites
    )
    assert all(item.material_uuid == OWNER_UUID for item in sites)
    assert all(item.deleted_at is None for item in sites)
    with pytest.raises(MaterialNotFound):
        materials.get_site(DELETED_SITE_UUID)


def test_list_sites_filters_owner_and_soft_delete_with_stable_order(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    with _opened_material_module(database_path) as (store, materials):
        _seed_sites(store, materials)
        _assert_owner_site_projection(materials)

    with _opened_material_module(database_path) as (_, reopened):
        _assert_owner_site_projection(reopened)
