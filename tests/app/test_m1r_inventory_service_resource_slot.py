"""M1R InventoryService concrete ResourceSlot 最小纵向合同。

测试只通过 public ``InventoryService`` 创建 Material 并解析
ResourceSlot identity，不访问 Store、SQLite 或预留逻辑。
"""

from __future__ import annotations

from pathlib import Path

from unilabos.app.scheduler.inventory import (
    InventoryService,
    ResourceTemplateIdentity,
)

MATERIAL_UUID = "5aa00000-0000-4000-8000-000000000103"
RESOURCE_TEMPLATE_UUID = "2bb00000-0000-4000-8000-000000000103"


def _resource_templates() -> dict[str, ResourceTemplateIdentity]:
    identity = ResourceTemplateIdentity(
        uuid=RESOURCE_TEMPLATE_UUID,
        material_class="SampleTube",
    )
    return {identity.uuid: identity}


def test_concrete_resource_slot_resolution_is_canonical_after_reopen(
    tmp_path: Path,
) -> None:
    inventory = InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    try:
        inventory.create_material(
            material_uuid=MATERIAL_UUID.upper(),
            resource_template_uuid=RESOURCE_TEMPLATE_UUID.upper(),
            barcode="SAMPLE-103",
            name="Concrete sample 103",
        )
        resolved = inventory.resolve_resource_slot(
            material_uuid=MATERIAL_UUID.upper(),
            allowed_resource_template_uuids=(RESOURCE_TEMPLATE_UUID.upper(),),
        )

        from unilabos.app.scheduler.inventory import ResourceSlotResolution

        expected = ResourceSlotResolution(
            uuid=MATERIAL_UUID,
            resource_template_uuid=RESOURCE_TEMPLATE_UUID,
        )
        assert resolved == expected
        assert (resolved.uuid, resolved.resource_template_uuid) == (
            MATERIAL_UUID,
            RESOURCE_TEMPLATE_UUID,
        )
    finally:
        inventory.close()

    reopened_inventory = InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    try:
        assert (
            reopened_inventory.resolve_resource_slot(
                material_uuid=MATERIAL_UUID.upper(),
                allowed_resource_template_uuids=(RESOURCE_TEMPLATE_UUID.upper(),),
            )
            == expected
        )
    finally:
        reopened_inventory.close()
