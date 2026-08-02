"""Canonical Material/Site-aware lab layout and warehouse read-model tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.scheduler.inventory import (
    InventoryService,
    ResourceTemplateIdentity,
)
from unilabos.app.scheduler.inventory.domains import get_domain_pack, list_domain_packs
from unilabos.app.scheduler.inventory.layout import (
    create_lab_router,
    delete_placement,
    delete_zone,
    get_assembly,
    get_layout,
    get_profile,
    seed_demo,
    update_profile,
    upsert_placement,
    upsert_zone,
)
from unilabos.app.scheduler.inventory.warehouse import build_warehouse_view

TUBE_TEMPLATE_UUID = "20000000-0000-4000-8000-000000001001"
RACK_TEMPLATE_UUID = "20000000-0000-4000-8000-000000001002"
RACK_UUID = "50000000-0000-4000-8000-000000001001"
TUBE_UUID = "50000000-0000-4000-8000-000000001002"


@pytest.fixture()
def inventory(tmp_path: Path) -> InventoryService:
    service = InventoryService.open(
        working_dir=tmp_path,
        resource_templates={
            TUBE_TEMPLATE_UUID: ResourceTemplateIdentity(
                TUBE_TEMPLATE_UUID,
                "SampleTube",
            ),
            RACK_TEMPLATE_UUID: ResourceTemplateIdentity(
                RACK_TEMPLATE_UUID,
                "SampleRack",
            ),
        },
    )
    yield service
    service.close()


def test_domain_pack_and_profile_roundtrip(inventory: InventoryService) -> None:
    assert {pack["domain"] for pack in list_domain_packs()} >= {
        "general",
        "organic",
        "bio",
    }
    assert get_domain_pack("missing")["domain"] == "general"
    assert get_profile(inventory)["domain"] == "general"

    updated = update_profile(
        inventory,
        name="有机一号实验室",
        domain="organic",
    )
    assert updated["name"] == "有机一号实验室"
    assert updated["pack"]["name"] == "有机化学实验室"


def test_zone_and_visual_placement_do_not_change_material_truth(
    inventory: InventoryService,
) -> None:
    zone = upsert_zone(
        inventory,
        {
            "zone_id": "z1",
            "name": "台 A",
            "kind": "bench",
            "x": 10,
            "y": 20,
            "w": 300,
            "h": 150,
        },
    )
    placement = upsert_placement(
        inventory,
        {
            "subject_id": "device-a",
            "subject_kind": "device",
            "zone_id": "z1",
            "x": 1,
            "y": 2,
        },
    )
    assert zone["version"] == 1
    assert placement["zone_id"] == "z1"
    assert inventory.inventory_snapshot()["materials"] == []

    assert delete_placement(inventory, "device-a")["deleted"] is True
    assert delete_zone(inventory, "z1")["deleted"] is True


def test_material_parent_tree_drives_assembly_projection(
    inventory: InventoryService,
) -> None:
    inventory.create_material(
        material_uuid=RACK_UUID,
        resource_template_uuid=RACK_TEMPLATE_UUID,
        barcode="LAYOUT-RACK",
        name="Rack",
    )
    inventory.create_material(
        material_uuid=TUBE_UUID,
        resource_template_uuid=TUBE_TEMPLATE_UUID,
        parent_uuid=RACK_UUID,
        barcode="LAYOUT-TUBE",
        name="Tube",
    )
    upsert_placement(
        inventory,
        {
            "subject_id": RACK_UUID,
            "subject_kind": "container",
            "zone_id": "bench",
        },
    )

    assembly = get_assembly(inventory, RACK_UUID)
    assert assembly["root"]["uuid"] == RACK_UUID
    assert [child["uuid"] for child in assembly["root"]["children"]] == [TUBE_UUID]
    assert assembly["placement"]["subject_id"] == RACK_UUID


def test_seed_is_idempotent_and_stock_summary_is_derived_from_lots(
    inventory: InventoryService,
) -> None:
    assert seed_demo(inventory)["zones"] == 2
    assert seed_demo(inventory)["zones"] == 2
    inventory.inbound_lot(
        resource_template_uuid=TUBE_TEMPLATE_UUID,
        quantity=12.0,
        unit="mL",
        lot_id="layout-lot",
        warehouse_zone_id="zone-storage",
    )

    layout = get_layout(inventory)
    assert len(layout["zones"]) == 2
    summary = layout["storage_summary"]["zone-storage"]
    assert summary[0]["resource_template_uuid"] == TUBE_TEMPLATE_UUID
    assert summary[0]["quantity_available"] == 12.0


def test_warehouse_aggregates_lots_and_materials_by_registry_identity(
    inventory: InventoryService,
) -> None:
    inventory.create_material(
        material_uuid=TUBE_UUID,
        resource_template_uuid=TUBE_TEMPLATE_UUID,
        barcode="WAREHOUSE-TUBE",
        name="Tube",
    )
    inventory.inbound_lot(
        resource_template_uuid=TUBE_TEMPLATE_UUID,
        quantity=5.0,
        unit="mL",
        lot_id="warehouse-lot",
    )

    view = build_warehouse_view(inventory)
    category = view["categories"][0]
    assert category["resource_template_uuid"] == TUBE_TEMPLATE_UUID
    assert category["quantity_available"] == 5.0
    assert category["material_counts"] == {"active": 1}


def test_lab_router_uses_inventory_service_only(inventory: InventoryService) -> None:
    app = FastAPI()
    app.include_router(create_lab_router(inventory))
    client = TestClient(app)

    assert client.get("/api/v1/lab/profile").json()["domain"] == "general"
    assert (
        client.put("/api/v1/lab/profile", json={"domain": "bio"}).json()["pack"]["name"]
        == "生物实验室"
    )
    assert client.post("/api/v1/lab/demo").status_code == 200
    assert len(client.get("/api/v1/lab/layout").json()["zones"]) == 2
