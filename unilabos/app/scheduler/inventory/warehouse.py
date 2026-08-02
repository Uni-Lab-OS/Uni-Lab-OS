"""Canonical stock/material read models built from InventoryService snapshots."""

from __future__ import annotations

from typing import Any

from unilabos.app.scheduler.inventory.service import InventoryService

STORAGE_CLASSES = [
    {"id": "ambient", "name": "常温", "color": "#3b82f6"},
    {"id": "cold", "name": "冷藏 2-8°C", "color": "#0ea5e9"},
    {"id": "frozen", "name": "冷冻 -20°C", "color": "#6366f1"},
    {"id": "flammable_cabinet", "name": "防爆柜", "color": "#ef4444"},
    {"id": "desiccator", "name": "干燥器", "color": "#f59e0b"},
]


def build_warehouse_view(inventory: InventoryService) -> dict[str, Any]:
    """Aggregate stock by Registry-owned ResourceTemplate UUID."""

    snapshot = inventory.inventory_snapshot()
    categories: dict[str, dict[str, Any]] = {}

    def category(resource_template_uuid: str) -> dict[str, Any]:
        if resource_template_uuid not in categories:
            categories[resource_template_uuid] = {
                "resource_template_uuid": resource_template_uuid,
                "quantity_total": 0.0,
                "quantity_available": 0.0,
                "quantity_reserved": 0.0,
                "batch_count": 0,
                "quarantined_batches": 0,
                "material_counts": {},
                "material_classes": [],
                "zones": {},
                "lots": [],
                "unit": "",
            }
        return categories[resource_template_uuid]

    for lot in snapshot["inventory_lots"]:
        projection = category(lot["resource_template_uuid"])
        projection["quantity_total"] += float(lot["quantity_total"])
        projection["quantity_available"] += float(lot["quantity_available"])
        projection["quantity_reserved"] += float(lot["quantity_reserved"])
        projection["batch_count"] += 1
        projection["quarantined_batches"] += int(bool(lot["quarantined"]))
        projection["unit"] = projection["unit"] or str(lot["unit"] or "")
        zone_id = str(lot["warehouse_zone_id"] or "")
        zone = projection["zones"].setdefault(
            zone_id,
            {"zone_id": zone_id, "quantity_available": 0.0, "batch_count": 0},
        )
        zone["quantity_available"] += float(lot["quantity_available"])
        zone["batch_count"] += 1
        projection["lots"].append(dict(lot))

    for material in snapshot["materials"]:
        projection = category(material["resource_template_uuid"])
        disposition = material.get("disposition") or "device"
        counts = projection["material_counts"]
        counts[disposition] = counts.get(disposition, 0) + 1
        if material["class"] not in projection["material_classes"]:
            projection["material_classes"].append(material["class"])

    result = []
    for projection in categories.values():
        projection["zones"] = sorted(
            projection["zones"].values(),
            key=lambda zone: zone["zone_id"],
        )
        projection["material_classes"].sort()
        result.append(projection)
    result.sort(key=lambda item: item["resource_template_uuid"])
    return {"categories": result, "storage_classes": STORAGE_CLASSES}


def build_zone_storage_summary(
    inventory: InventoryService,
) -> dict[str, list[dict[str, Any]]]:
    """Aggregate available lots by visual warehouse zone and template UUID."""

    snapshot = inventory.inventory_snapshot()
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for lot in snapshot["inventory_lots"]:
        if float(lot["quantity_available"]) <= 0:
            continue
        zone_id = str(lot["warehouse_zone_id"] or "")
        template_uuid = str(lot["resource_template_uuid"])
        item = buckets.setdefault(
            (zone_id, template_uuid),
            {
                "resource_template_uuid": template_uuid,
                "quantity_available": 0.0,
                "unit": str(lot["unit"] or ""),
                "batch_count": 0,
            },
        )
        item["quantity_available"] += float(lot["quantity_available"])
        item["batch_count"] += 1

    summary: dict[str, list[dict[str, Any]]] = {}
    for (zone_id, _), item in buckets.items():
        summary.setdefault(zone_id, []).append(item)
    for items in summary.values():
        items.sort(key=lambda item: item["resource_template_uuid"])
    return summary
