"""Independent 2D lab layout projected through InventoryService."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from unilabos.app.scheduler.inventory.domains import (
    ZONE_KINDS,
    get_domain_pack,
    list_domain_packs,
)
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.warehouse import (
    build_warehouse_view,
    build_zone_storage_summary,
)


def get_profile(inventory: InventoryService) -> dict[str, Any]:
    profile = inventory.get_lab_profile()
    domain = profile["domain"]
    return {
        **profile,
        "pack": get_domain_pack(domain),
        "domains": list_domain_packs(),
        "zone_kinds": ZONE_KINDS,
    }


def update_profile(
    inventory: InventoryService,
    name: str | None = None,
    domain: str | None = None,
) -> dict[str, Any]:
    inventory.update_lab_profile(name=name, domain=domain)
    return get_profile(inventory)


def upsert_zone(
    inventory: InventoryService,
    zone: dict[str, Any],
) -> dict[str, Any]:
    return inventory.upsert_lab_zone(zone)


def delete_zone(inventory: InventoryService, zone_id: str) -> dict[str, Any]:
    return inventory.delete_lab_zone(zone_id)


def upsert_placement(
    inventory: InventoryService,
    placement: dict[str, Any],
) -> dict[str, Any]:
    return inventory.upsert_lab_placement(placement)


def delete_placement(
    inventory: InventoryService,
    subject_id: str,
) -> dict[str, Any]:
    return inventory.delete_lab_placement(subject_id)


def get_layout(inventory: InventoryService) -> dict[str, Any]:
    projection = inventory.get_lab_layout()
    projection["storage_summary"] = build_zone_storage_summary(inventory)
    return projection


def get_assembly(
    inventory: InventoryService,
    material_uuid: str,
) -> dict[str, Any]:
    return inventory.get_material_assembly(material_uuid)


def seed_demo(inventory: InventoryService) -> dict[str, int]:
    """Seed only visual zones; durable Material fixtures require Registry identities."""

    zones = (
        {
            "zone_id": "zone-bench-a",
            "name": "实验台 A",
            "kind": "bench",
            "x": 40,
            "y": 60,
            "w": 360,
            "h": 200,
        },
        {
            "zone_id": "zone-storage",
            "name": "常温试剂柜",
            "kind": "storage",
            "x": 40,
            "y": 300,
            "w": 200,
            "h": 160,
        },
    )
    for zone in zones:
        inventory.upsert_lab_zone(zone)
    return {
        "zones": len(zones),
        "materials": 0,
        "placements": 0,
        "lots": 0,
    }


def create_lab_router(inventory: InventoryService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/lab", tags=["lab"])

    @router.get("/profile")
    def profile() -> dict[str, Any]:
        return get_profile(inventory)

    @router.put("/profile")
    def put_profile(body: dict[str, Any]) -> dict[str, Any]:
        return update_profile(
            inventory,
            name=body.get("name"),
            domain=body.get("domain"),
        )

    @router.get("/layout")
    def layout() -> dict[str, Any]:
        return get_layout(inventory)

    @router.get("/warehouse")
    def warehouse() -> dict[str, Any]:
        return build_warehouse_view(inventory)

    @router.post("/zones")
    def post_zone(body: dict[str, Any]) -> dict[str, Any]:
        return upsert_zone(inventory, body)

    @router.delete("/zones/{zone_id}")
    def remove_zone(zone_id: str) -> dict[str, Any]:
        return delete_zone(inventory, zone_id)

    @router.post("/placements")
    def post_placement(body: dict[str, Any]) -> dict[str, Any]:
        return upsert_placement(inventory, body)

    @router.delete("/placements/{subject_id}")
    def remove_placement(subject_id: str) -> dict[str, Any]:
        return delete_placement(inventory, subject_id)

    @router.get("/assembly/{material_uuid}")
    def assembly(material_uuid: str) -> dict[str, Any]:
        return get_assembly(inventory, material_uuid)

    @router.post("/demo")
    def demo() -> dict[str, Any]:
        return {"seeded": seed_demo(inventory)}

    return router
