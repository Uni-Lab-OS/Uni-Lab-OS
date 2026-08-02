"""M1R InventoryService Site 最小纵向合同。

测试只通过 public ``InventoryService`` 创建和读取 Material/Site，
并通过关闭后重开证明 Site 及 allowlist 投影可持久化。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from unilabos.app.scheduler.inventory import (
    InventoryService,
    ResourceTemplateIdentity,
)

OWNER_MATERIAL_UUID = "50000000-0000-4000-8000-000000000101"
OCCUPANT_MATERIAL_UUID = "50000000-0000-4000-8000-000000000102"
OWNER_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000101"
OCCUPANT_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000102"
LOW_SITE_UUID = "60000000-0000-4000-8000-000000000101"
HIGH_SITE_UUID = "60000000-0000-4000-8000-000000000102"

SITE_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "description",
    "meta_data",
    "material_uuid",
    "name",
    "sort_order",
    "allowed_resource_template_uuids",
    "occupied_material_uuid",
    "position_x",
    "position_y",
    "position_z",
    "depth",
    "length",
    "width",
    "version",
}


def _resource_templates() -> dict[str, ResourceTemplateIdentity]:
    identities = (
        ResourceTemplateIdentity(
            uuid=OWNER_TEMPLATE_UUID,
            material_class="Deck",
        ),
        ResourceTemplateIdentity(
            uuid=OCCUPANT_TEMPLATE_UUID,
            material_class="Microplate",
        ),
    )
    return {identity.uuid: identity for identity in identities}


def _create_materials(inventory: InventoryService) -> None:
    inventory.create_material(
        material_uuid=OWNER_MATERIAL_UUID,
        resource_template_uuid=OWNER_TEMPLATE_UUID,
        barcode="DECK-101",
        name="Deck owner",
    )
    inventory.create_material(
        material_uuid=OCCUPANT_MATERIAL_UUID,
        resource_template_uuid=OCCUPANT_TEMPLATE_UUID,
        barcode="PLATE-102",
        name="Placed microplate",
    )


def _expected_site_projection(created: Any) -> dict[str, Any]:
    return {
        "uuid": HIGH_SITE_UUID,
        "create_time": created.create_time,
        "update_time": created.update_time,
        "description": "Cold deck position A1",
        "meta_data": {"zone": "cold", "labels": ["robot", "primary"]},
        "material_uuid": OWNER_MATERIAL_UUID,
        "name": "A1",
        "sort_order": 7,
        "allowed_resource_template_uuids": [
            OWNER_TEMPLATE_UUID,
            OCCUPANT_TEMPLATE_UUID,
        ],
        "occupied_material_uuid": OCCUPANT_MATERIAL_UUID,
        "position_x": 1.25,
        "position_y": -2.5,
        "position_z": 3.75,
        "depth": 10.0,
        "length": 20.5,
        "width": 30.25,
        "version": 1,
    }


def test_inventory_service_site_projection_order_and_reopen_are_stable(
    tmp_path: Path,
) -> None:
    inventory = InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    try:
        _create_materials(inventory)
        created = inventory.create_site(
            site_uuid=HIGH_SITE_UUID,
            description="Cold deck position A1",
            meta_data={"zone": "cold", "labels": ["robot", "primary"]},
            material_uuid=OWNER_MATERIAL_UUID,
            name="A1",
            sort_order=7,
            allowed_resource_template_uuids=[
                OCCUPANT_TEMPLATE_UUID,
                OWNER_TEMPLATE_UUID,
                OCCUPANT_TEMPLATE_UUID,
            ],
            occupied_material_uuid=OCCUPANT_MATERIAL_UUID,
            position_x=1.25,
            position_y=-2.5,
            position_z=3.75,
            depth=10.0,
            length=20.5,
            width=30.25,
        )
        inventory.create_site(
            site_uuid=LOW_SITE_UUID,
            description=None,
            meta_data={},
            material_uuid=OWNER_MATERIAL_UUID,
            name="A0",
            sort_order=7,
            allowed_resource_template_uuids=[OCCUPANT_TEMPLATE_UUID],
            occupied_material_uuid=None,
            position_x=0.0,
            position_y=0.0,
            position_z=0.0,
            depth=1.0,
            length=1.0,
            width=1.0,
        )
        expected = _expected_site_projection(created)

        assert created.create_time
        assert created.create_time == created.update_time
        assert set(created.to_dict()) == SITE_FIELDS
        assert created.to_dict() == expected
        assert inventory.get_site(HIGH_SITE_UUID).to_dict() == expected

        listed = inventory.list_sites(OWNER_MATERIAL_UUID)
        assert [site.uuid for site in listed] == [LOW_SITE_UUID, HIGH_SITE_UUID]
        projections = tuple(site.to_dict() for site in listed)
    finally:
        inventory.close()

    reopened_inventory = InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    try:
        assert reopened_inventory.get_site(HIGH_SITE_UUID).to_dict() == expected
        reopened = reopened_inventory.list_sites(OWNER_MATERIAL_UUID)

        assert [site.uuid for site in reopened] == [LOW_SITE_UUID, HIGH_SITE_UUID]
        assert tuple(site.to_dict() for site in reopened) == projections
    finally:
        reopened_inventory.close()
