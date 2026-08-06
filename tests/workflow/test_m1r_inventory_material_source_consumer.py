"""M1R InventoryService 作为 M2A static consumer authority 的最小合同。

测试只通过 public InventoryService 构造 Material/Site 事实，再通过
public MaterialSource validator 消费；不访问旧 authority、Store 或 SQLite。
"""

from __future__ import annotations

from pathlib import Path

from unilabos.app.scheduler.inventory import (
    InventoryService,
    ResourceTemplateIdentity,
)
from unilabos.workflow.material_source import validate_material_source_authority

MOUNT_MATERIAL_UUID = "5aa00000-0000-4000-8000-000000000108"
OCCUPANT_MATERIAL_UUID = "5bb00000-0000-4000-8000-000000000108"
MOUNT_TEMPLATE_UUID = "2aa00000-0000-4000-8000-000000000108"
OCCUPANT_TEMPLATE_UUID = "2bb00000-0000-4000-8000-000000000108"
SITE_UUID = "6aa00000-0000-4000-8000-000000000108"
MATERIAL_SOURCE_NODE_UUID = "a0000000-0000-4000-8000-000000000108"


def _resource_templates() -> dict[str, ResourceTemplateIdentity]:
    identities = (
        ResourceTemplateIdentity(
            uuid=MOUNT_TEMPLATE_UUID,
            material_class="Deck",
        ),
        ResourceTemplateIdentity(
            uuid=OCCUPANT_TEMPLATE_UUID,
            material_class="Microplate",
        ),
    )
    return {identity.uuid: identity for identity in identities}


def _material_source_graph() -> dict[str, object]:
    return {
        "nodes": [
            {
                "uuid": MATERIAL_SOURCE_NODE_UUID,
                "type": "material_source",
                "param": {
                    "mode": "existing",
                    "resource_template_uuid": OCCUPANT_TEMPLATE_UUID,
                    "mount": {"uuid": MOUNT_MATERIAL_UUID},
                    "material_uuid": OCCUPANT_MATERIAL_UUID,
                    "site": None,
                    "slot_range": None,
                    "flow_role": "reagent",
                },
            }
        ]
    }


def test_m2a_static_validator_accepts_inventory_service_material_and_site_records(
    tmp_path: Path,
) -> None:
    inventory = InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    try:
        inventory.create_material(
            material_uuid=MOUNT_MATERIAL_UUID,
            resource_template_uuid=MOUNT_TEMPLATE_UUID,
            barcode="DECK-108",
            name="Static validation deck",
        )
        inventory.create_material(
            material_uuid=OCCUPANT_MATERIAL_UUID,
            resource_template_uuid=OCCUPANT_TEMPLATE_UUID,
            barcode="PLATE-108",
            name="Static validation plate",
        )
        inventory.create_site(
            site_uuid=SITE_UUID,
            description="M2A compatible position",
            meta_data={"zone": "static-consumer"},
            material_uuid=MOUNT_MATERIAL_UUID,
            name="A1",
            sort_order=0,
            allowed_resource_template_uuids=[OCCUPANT_TEMPLATE_UUID],
            occupied_material_uuid=OCCUPANT_MATERIAL_UUID,
            position_x=0.0,
            position_y=0.0,
            position_z=0.0,
            depth=1.0,
            length=1.0,
            width=1.0,
        )

        validate_material_source_authority(_material_source_graph(), inventory)
    finally:
        inventory.close()
