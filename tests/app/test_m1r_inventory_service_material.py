"""M1R InventoryService Material 最小纵向合同。

测试只通过 public ``InventoryService`` 创建、读取和重开 Material；
不访问 InventoryStore、SQLite 表或旧 material_instance identity。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from unilabos.app.scheduler.inventory import InventoryService

MATERIAL_UUID = "50000000-0000-4000-8000-000000000017"
RESOURCE_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000017"
MATERIAL_CLASS = "SampleTube"

MATERIAL_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "deleted_at",
    "description",
    "meta_data",
    "resource_template_uuid",
    "parent_uuid",
    "class",
    "barcode",
    "name",
    "config",
    "data",
    "disposition",
    "material_kind",
    "version",
}
SECOND_IDENTITY_FIELDS = {
    "material_instance",
    "instance_uuid",
    "edge_uuid",
    "legacy_cloud_id",
}


def _resource_templates() -> dict[str, Any]:
    from unilabos.app.scheduler.inventory import ResourceTemplateIdentity

    identity = ResourceTemplateIdentity(
        uuid=RESOURCE_TEMPLATE_UUID,
        material_class=MATERIAL_CLASS,
    )
    return {identity.uuid: identity}


def _expected_projection(created: Any) -> dict[str, Any]:
    return {
        "uuid": MATERIAL_UUID,
        "create_time": created.create_time,
        "update_time": created.update_time,
        "deleted_at": None,
        "description": "M1R public InventoryService tracer",
        "meta_data": {"source": "m1r-red", "labels": ["fragile"]},
        "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
        "parent_uuid": None,
        "class": MATERIAL_CLASS,
        "barcode": "SAMPLE-017",
        "name": "Sample 17",
        "config": {"volume_ul": 125.5, "sterile": True},
        "data": {"measurements": [1, 2.5], "note": None},
        "disposition": "active",
        "material_kind": "business",
        "version": 1,
    }


def test_empty_inventory_service_creates_reads_and_reopens_one_material(
    tmp_path: Path,
) -> None:
    inventory_database = tmp_path / "inventory.db"
    assert not inventory_database.exists()

    inventory = InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    try:
        created = inventory.create_material(
            material_uuid=MATERIAL_UUID,
            resource_template_uuid=RESOURCE_TEMPLATE_UUID,
            barcode="SAMPLE-017",
            name="Sample 17",
            description="M1R public InventoryService tracer",
            meta_data={"source": "m1r-red", "labels": ["fragile"]},
            config={"volume_ul": 125.5, "sterile": True},
            data={"measurements": [1, 2.5], "note": None},
        )
        expected = _expected_projection(created)

        assert created.create_time
        assert created.create_time == created.update_time
        assert set(created.to_dict()) == MATERIAL_FIELDS
        assert SECOND_IDENTITY_FIELDS.isdisjoint(created.to_dict())
        assert created.to_dict() == expected
        assert inventory.get_material(MATERIAL_UUID).to_dict() == expected
    finally:
        inventory.close()

    assert inventory_database.is_file()
    assert not (tmp_path / "material.db").exists()

    reopened_inventory = InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    try:
        reopened = reopened_inventory.get_material(MATERIAL_UUID)

        assert set(reopened.to_dict()) == MATERIAL_FIELDS
        assert SECOND_IDENTITY_FIELDS.isdisjoint(reopened.to_dict())
        assert reopened.to_dict() == expected
    finally:
        reopened_inventory.close()
