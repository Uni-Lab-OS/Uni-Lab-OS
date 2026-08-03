"""ResourceTreeSet carrier Sites become durable public Inventory Sites."""

from __future__ import annotations

from pathlib import Path

from unilabos.app.scheduler.inventory import (
    InventoryService,
    ResourceTemplateIdentity,
)
from unilabos.app.scheduler.inventory.material_projection import (
    MaterialDefinitionProjection,
    PackageMaterialProjection,
    build_resource_graph_import,
)

WAREHOUSE_TEMPLATE_UUID = "82000000-0000-4000-8000-000000000001"
TIP_BOX_TEMPLATE_UUID = "82000000-0000-4000-8000-000000000002"
TIP_TEMPLATE_UUID = "82000000-0000-4000-8000-000000000003"


def test_carrier_config_sites_are_persisted_and_exposed_in_business_order(
    tmp_path: Path,
) -> None:
    projection = PackageMaterialProjection(
        definitions={
            "warehouse": _definition("warehouse", "warehouse_factory"),
            "tip_box": _definition("tip-box", "tip_box_factory"),
            "pipette_tip": _definition("tip", "pipette_tip_factory"),
        },
        shapes=(),
        model_assets=(),
        fingerprint="sha256:test-sites",
    )
    resolved = {
        "warehouse_factory": WAREHOUSE_TEMPLATE_UUID,
        "tip_box_factory": TIP_BOX_TEMPLATE_UUID,
        "pipette_tip_factory": TIP_TEMPLATE_UUID,
    }
    imported = build_resource_graph_import(
        {
            "source_id": "site-projection.json",
            "nodes": [
                {
                    "id": "warehouse-a",
                    "uuid": "runtime-warehouse-a",
                    "name": "Warehouse A",
                    "class": "warehouse",
                    "type": "warehouse",
                    "config": {
                        "sites": [
                            _site("S02", x=20, occupied_by="tip-box-a"),
                            _site("S01", x=10, occupied_by=None),
                        ]
                    },
                    "data": {},
                },
                {
                    "id": "tip-box-a",
                    "uuid": "runtime-tip-box-a",
                    "parent_uuid": "runtime-warehouse-a",
                    "name": "Tip box A",
                    "class": "tip_box",
                    "type": "container",
                    "config": {
                        "category": "tip_box",
                        "sites": [
                            {
                                **_site("tip-01", x=1, occupied_by=None),
                                "content_type": ["pipette_tip"],
                            }
                        ],
                    },
                    "data": {},
                },
            ],
        },
        projection,
        resolved,
    )

    assert len(imported["materials"]) == 2
    assert len(imported["sites"]) == 3
    warehouse_material = next(
        item
        for item in imported["materials"]
        if item["meta_data"]["source_node_id"] == "warehouse-a"
    )
    tip_box_material = next(
        item
        for item in imported["materials"]
        if item["meta_data"]["source_node_id"] == "tip-box-a"
    )
    warehouse_sites = [
        site
        for site in imported["sites"]
        if site["material_uuid"] == warehouse_material["uuid"]
    ]
    assert [site["name"] for site in warehouse_sites] == ["S02", "S01"]
    assert [site["sort_order"] for site in warehouse_sites] == [0, 1]
    assert warehouse_sites[0]["occupied_material_uuid"] == tip_box_material["uuid"]
    assert warehouse_sites[1]["occupied_material_uuid"] is None
    assert warehouse_sites[0]["allowed_resource_template_uuids"] == [
        TIP_BOX_TEMPLATE_UUID
    ]
    nested_site = next(
        site
        for site in imported["sites"]
        if site["material_uuid"] == tip_box_material["uuid"]
    )
    assert nested_site["meta_data"] == {
        "source": "resource-tree-set-config",
        "source_owner_node_id": "tip-box-a",
        "key": "tip-01",
        "kind": "tip-spot",
        "shape": "circle",
        "visible": True,
    }
    assert nested_site["allowed_resource_template_uuids"] == [TIP_TEMPLATE_UUID]

    inventory = InventoryService.open(
        working_dir=tmp_path,
        resource_templates={
            template_uuid: ResourceTemplateIdentity(
                uuid=template_uuid,
                material_class=source_identity,
            )
            for source_identity, template_uuid in resolved.items()
        },
    )
    try:
        inventory.bootstrap_resource_graph(imported)
        graph = inventory.backend_material_graph()["nodes"]
        warehouse = next(
            node
            for node in graph
            if node["material"]["uuid"] == warehouse_material["uuid"]
        )
        tip_box = next(
            node
            for node in graph
            if node["material"]["uuid"] == tip_box_material["uuid"]
        )
        assert [site["name"] for site in warehouse["sites"]] == ["S02", "S01"]
        assert warehouse["sites"][1]["occupied_material_uuid"] is None
        assert tip_box["current_site_uuid"] == warehouse["sites"][0]["uuid"]
        assert tip_box["sites"][0]["meta_data"]["kind"] == "tip-spot"
    finally:
        inventory.close()


def _definition(kind: str, source_identity: str) -> MaterialDefinitionProjection:
    return MaterialDefinitionProjection(
        graph_class=kind,
        source_identity=source_identity,
        kind=kind,
        categories=(kind,),
        envelope_mm=(100.0, 100.0, 100.0),
        model=None,
    )


def _site(label: str, *, x: float, occupied_by: str | None) -> dict[str, object]:
    return {
        "label": label,
        "name": label,
        "position": {"x": x, "y": 2, "z": 3},
        "size": {"width": 10, "height": 11, "depth": 12},
        "content_type": ["tip_box"],
        "visible": True,
        "occupied_by": occupied_by,
    }
