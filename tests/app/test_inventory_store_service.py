"""Canonical regression coverage for the reviewed Inventory donor algorithms."""

from __future__ import annotations

from pathlib import Path

import pytest

from unilabos.app.scheduler.inventory import (
    InvariantViolation,
    InventoryService,
    MaterialConflict,
    MaterialInvalidInput,
    ResourceTemplateIdentity,
)

TEMPLATE_A = "20000000-0000-4000-8000-000000000601"
TEMPLATE_B = "20000000-0000-4000-8000-000000000602"
PARENT_UUID = "50000000-0000-4000-8000-000000000601"
CHILD_UUID = "50000000-0000-4000-8000-000000000602"


def _templates() -> dict[str, ResourceTemplateIdentity]:
    return {
        TEMPLATE_A: ResourceTemplateIdentity(TEMPLATE_A, "SampleTube"),
        TEMPLATE_B: ResourceTemplateIdentity(TEMPLATE_B, "SampleRack"),
    }


def _open(path: Path, *, time_fn=lambda: 1_700_000_000.0) -> InventoryService:
    return InventoryService.open(
        working_dir=path,
        resource_templates=_templates(),
        time_fn=time_fn,
    )


def test_inbound_preserves_quantity_invariants_and_fifo_order(tmp_path: Path) -> None:
    inventory = _open(tmp_path)
    try:
        first = inventory.inbound_lot(
            resource_template_uuid=TEMPLATE_A,
            quantity=10.0,
            unit="mL",
            lot_id="lot-first",
        )
        inventory.inbound_lot(
            resource_template_uuid=TEMPLATE_A,
            quantity=5.0,
            unit="mL",
            lot_id="lot-second",
        )
        replayed_lot = inventory.inbound_lot(
            resource_template_uuid=TEMPLATE_A,
            quantity=2.5,
            unit="mL",
            lot_id="lot-first",
        )

        assert first["quantity_total"] == 10.0
        assert replayed_lot["quantity_total"] == 12.5
        assert replayed_lot["quantity_available"] == 12.5
        assert replayed_lot["quantity_reserved"] == 0.0
        assert [
            lot["lot_id"] for lot in inventory.inventory_snapshot()["inventory_lots"]
        ] == ["lot-first", "lot-second"]

        before_failure = inventory.outbox_status()["max_sequence"]
        with pytest.raises(InvariantViolation):
            inventory.inbound_lot(
                resource_template_uuid=TEMPLATE_A,
                quantity=-1.0,
                lot_id="lot-invalid",
            )
        assert inventory.outbox_status()["max_sequence"] == before_failure
    finally:
        inventory.close()


def test_lot_identity_cannot_switch_template_or_unit(tmp_path: Path) -> None:
    inventory = _open(tmp_path)
    try:
        inventory.inbound_lot(
            resource_template_uuid=TEMPLATE_A,
            quantity=1.0,
            unit="mL",
            lot_id="lot-stable",
        )
        with pytest.raises(MaterialConflict):
            inventory.inbound_lot(
                resource_template_uuid=TEMPLATE_B,
                quantity=1.0,
                unit="mL",
                lot_id="lot-stable",
            )
        with pytest.raises(MaterialConflict):
            inventory.inbound_lot(
                resource_template_uuid=TEMPLATE_A,
                quantity=1.0,
                unit="g",
                lot_id="lot-stable",
            )
        with pytest.raises(MaterialInvalidInput):
            inventory.inbound_lot(
                resource_template_uuid="20000000-0000-4000-8000-000000000699",
                quantity=1.0,
            )
    finally:
        inventory.close()


def test_adjust_is_audited_versioned_and_cannot_break_invariants(
    tmp_path: Path,
) -> None:
    inventory = _open(tmp_path)
    try:
        inventory.inbound_lot(
            resource_template_uuid=TEMPLATE_A,
            quantity=10.0,
            lot_id="lot-adjust",
        )
        adjusted = inventory.adjust_lot(
            lot_id="lot-adjust",
            new_total=7.0,
            reason="physical count",
            actor="operator-1",
            expected_version=1,
        )
        assert adjusted["quantity_total"] == 7.0
        assert adjusted["quantity_available"] == 7.0
        assert adjusted["version"] == 2
        with pytest.raises(MaterialConflict):
            inventory.adjust_lot(
                lot_id="lot-adjust",
                new_total=6.0,
                reason="stale count",
                actor="operator-2",
                expected_version=1,
            )
        with pytest.raises(InvariantViolation):
            inventory.adjust_lot(
                lot_id="lot-adjust",
                new_total=-1.0,
                reason="invalid count",
                actor="operator-2",
            )
        assert inventory.read_ledger()[-1]["op_type"] == "lot.adjusted"
    finally:
        inventory.close()


def test_material_identity_parent_and_barcode_survive_reopen(tmp_path: Path) -> None:
    inventory = _open(tmp_path)
    try:
        inventory.create_material(
            material_uuid=PARENT_UUID,
            resource_template_uuid=TEMPLATE_B,
            barcode="RACK-601",
            name="Rack",
        )
        inventory.create_material(
            material_uuid=CHILD_UUID,
            resource_template_uuid=TEMPLATE_A,
            parent_uuid=PARENT_UUID,
            barcode="Tube-601",
            name="Tube",
        )
        with pytest.raises(MaterialConflict):
            inventory.create_material(
                material_uuid="50000000-0000-4000-8000-000000000603",
                resource_template_uuid=TEMPLATE_A,
                barcode="tube-601",
                name="Duplicate barcode",
            )
    finally:
        inventory.close()

    reopened = _open(tmp_path)
    try:
        child = reopened.get_material(CHILD_UUID)
        assert child.parent_uuid == PARENT_UUID
        assembly = reopened.get_material_assembly(PARENT_UUID)
        assert assembly["root"]["uuid"] == PARENT_UUID
        assert [item["uuid"] for item in assembly["root"]["children"]] == [CHILD_UUID]
    finally:
        reopened.close()
