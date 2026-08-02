"""Inventory 到冻结 Backend Material read Interface 的合同测试。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from unilabos.app.scheduler.inventory import (
    InventoryService,
    ResourceTemplateIdentity,
)
from unilabos.app.scheduler.inventory.api import create_app

DECK_TEMPLATE_UUID = "81000000-0000-4000-8000-000000000501"
VESSEL_TEMPLATE_UUID = "82000000-0000-4000-8000-000000000501"
DECK_UUID = "91000000-0000-4000-8000-000000000501"
VESSEL_UUID = "92000000-0000-4000-8000-000000000501"
DECK_POSITION_UUID = "a1000000-0000-4000-8000-000000000501"
VESSEL_POSITION_UUID = "a2000000-0000-4000-8000-000000000501"
SITE_UUID = "b1000000-0000-4000-8000-000000000501"


def _service(tmp_path: Path) -> InventoryService:
    return InventoryService.open(
        working_dir=tmp_path,
        resource_templates={
            DECK_TEMPLATE_UUID: ResourceTemplateIdentity(
                uuid=DECK_TEMPLATE_UUID,
                material_class="lab.resources:deck",
            ),
            VESSEL_TEMPLATE_UUID: ResourceTemplateIdentity(
                uuid=VESSEL_TEMPLATE_UUID,
                material_class="lab.resources:vessel",
            ),
        },
        material_shapes=(
            {
                "id": "vessel",
                "bundle": "community.lab",
                "categories": ["vessel"],
                "categoryTokens": [],
                "priority": 0,
                "units": "mm",
                "shadow": "round",
                "sort": "center",
                "parts": [
                    {
                        "type": "cylinder",
                        "style": "glass",
                        "center": [50, 50],
                        "d": 80,
                        "z": [0, 150],
                    }
                ],
            },
        ),
    )


def _position(
    *,
    position_uuid: str,
    material_uuid: str,
    x: float,
    width: float,
    length: float,
    depth: float,
) -> dict[str, object]:
    return {
        "uuid": position_uuid,
        "material_uuid": material_uuid,
        "description": None,
        "meta_data": {"source": "test"},
        "position_x": x,
        "position_y": 20.0,
        "position_z": 0.0,
        "depth": depth,
        "length": length,
        "width": width,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "scale_z": 1.0,
        "rotation_x": 0.0,
        "rotation_y": 0.0,
        "rotation_z": 0.0,
    }


def _bootstrap(inventory: InventoryService) -> dict[str, object]:
    return inventory.bootstrap_resource_graph(
        {
            "source_id": "backend-material-graph.json",
            "fingerprint": "sha256:" + "1" * 64,
            "materials": [
                {
                    "uuid": DECK_UUID,
                    "resource_template_uuid": DECK_TEMPLATE_UUID,
                    "parent_uuid": None,
                    "class": "lab_deck",
                    "barcode": "DECK-001",
                    "name": "Deck",
                    "description": "MaterialGraph root",
                    "meta_data": {"source_node_id": "deck"},
                    "config": {
                        "rendering": {
                            "kind": "deck",
                            "dimensions_mm": [1000, 700, 50],
                        }
                    },
                    "data": {},
                    "material_kind": "business",
                },
                {
                    "uuid": VESSEL_UUID,
                    "resource_template_uuid": VESSEL_TEMPLATE_UUID,
                    "parent_uuid": DECK_UUID,
                    "class": "lab_vessel",
                    "barcode": "VESSEL-001",
                    "name": "Vessel",
                    "description": None,
                    "meta_data": {"source_node_id": "vessel"},
                    "config": {
                        "rendering": {
                            "kind": "vessel",
                            "dimensions_mm": [100, 100, 150],
                        }
                    },
                    "data": {"temperature": 20},
                    "material_kind": "business",
                },
            ],
            "relative_positions": [
                _position(
                    position_uuid=DECK_POSITION_UUID,
                    material_uuid=DECK_UUID,
                    x=0,
                    width=1000,
                    length=700,
                    depth=50,
                ),
                _position(
                    position_uuid=VESSEL_POSITION_UUID,
                    material_uuid=VESSEL_UUID,
                    x=120,
                    width=100,
                    length=100,
                    depth=150,
                ),
            ],
            "sites": [],
        }
    )


def test_bootstrap_is_atomic_idempotent_and_never_overwrites_existing_truth(
    tmp_path: Path,
) -> None:
    inventory = _service(tmp_path)
    try:
        assert _bootstrap(inventory) == {
            "status": "imported",
            "source_id": "backend-material-graph.json",
            "fingerprint": "sha256:" + "1" * 64,
            "material_count": 2,
            "site_count": 0,
        }
        assert _bootstrap(inventory)["status"] == "unchanged"
        preserved = inventory.bootstrap_resource_graph(
            {
                "source_id": "different.json",
                "fingerprint": "sha256:" + "2" * 64,
                "materials": [{}],
                "relative_positions": [],
                "sites": [],
            }
        )
        assert preserved["status"] == "preserved"
        assert inventory.inventory_snapshot()["materials"][0]["uuid"] == DECK_UUID
    finally:
        inventory.close()


def test_backend_material_graph_route_matches_frozen_wire_shape(tmp_path: Path) -> None:
    inventory = _service(tmp_path)
    try:
        _bootstrap(inventory)
        inventory.create_site(
            site_uuid=SITE_UUID,
            description="Deck slot",
            meta_data={"key": "A1"},
            material_uuid=DECK_UUID,
            name="A1",
            sort_order=0,
            allowed_resource_template_uuids=[VESSEL_TEMPLATE_UUID],
            occupied_material_uuid=VESSEL_UUID,
            position_x=100,
            position_y=100,
            position_z=50,
            depth=150,
            length=100,
            width=100,
        )
        with TestClient(create_app(inventory)) as client:
            response = client.get("/api/v1/materials/graph")
            assert response.status_code == 200
            body = response.json()
            assert body["code"] == 0
            nodes = body["data"]["nodes"]
            assert len(nodes) == 2
            deck = next(node for node in nodes if node["material"]["uuid"] == DECK_UUID)
            vessel = next(
                node for node in nodes if node["material"]["uuid"] == VESSEL_UUID
            )
            assert set(deck) == {
                "material",
                "relative_position",
                "sites",
                "current_site_uuid",
                "handles",
            }
            assert set(deck["material"]) == {
                "uuid",
                "create_time",
                "update_time",
                "description",
                "meta_data",
                "resource_template_uuid",
                "class",
                "barcode",
                "name",
                "config",
                "data",
            }
            assert set(vessel["material"]) == {
                "uuid",
                "create_time",
                "update_time",
                "meta_data",
                "resource_template_uuid",
                "parent_uuid",
                "class",
                "barcode",
                "name",
                "config",
                "data",
            }
            assert set(deck["relative_position"]) == {
                "uuid",
                "create_time",
                "update_time",
                "meta_data",
                "material_uuid",
                "position_x",
                "position_y",
                "position_z",
                "depth",
                "length",
                "width",
                "scale_x",
                "scale_y",
                "scale_z",
                "rotation_x",
                "rotation_y",
                "rotation_z",
            }
            assert set(deck["sites"][0]) == {
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
            }
            assert deck["relative_position"]["width"] == 1000
            assert deck["sites"][0]["allowed_resource_template_uuids"] == [
                VESSEL_TEMPLATE_UUID
            ]
            assert deck["sites"][0]["occupied_material_uuid"] == VESSEL_UUID
            assert vessel["current_site_uuid"] == SITE_UUID
            assert vessel["handles"] == []
            assert vessel["material"]["barcode"] == "VESSEL-001"
            assert "disposition" not in vessel["material"]
            assert "material_kind" not in vessel["material"]
            assert "version" not in vessel["material"]
            assert "version" not in deck["sites"][0]

            listing = client.get("/api/v1/materials?page=1&page_size=1").json()
            assert listing["code"] == 0
            assert listing["data"]["total"] == 2
            assert listing["data"]["page_size"] == 1

            detail = client.get(f"/api/v1/materials/{VESSEL_UUID}").json()["data"]
            assert detail["current_site"]["uuid"] == SITE_UUID
            assert detail["relative_position"]["material_uuid"] == VESSEL_UUID

            shapes = client.get("/api/v1/material-shapes").json()
            assert shapes["data"]["items"][0]["id"] == "vessel"
    finally:
        inventory.close()
