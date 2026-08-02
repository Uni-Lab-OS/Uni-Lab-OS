"""M1EF declared Material/Site effect and no-op invariants."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import unilabos.app.scheduler.inventory as inventory_api
from tests.app.test_m1ef_inventory_claim_lifecycle import (
    FIRST_JOB_UUID,
    SAMPLE_MATERIAL_UUID,
    SAMPLE_TEMPLATE_UUID,
    SITE_UUID,
    WORKFLOW_TASK_UUID,
    _admit_task,
    _claim_command,
    _open_inventory,
    _seed_business_material_and_site,
    _seed_device_material,
)

CREATED_MATERIAL_UUID = "51000000-0000-4000-8000-000000000421"


def _running_claim(
    tmp_path: Path,
) -> tuple[inventory_api.InventoryService, inventory_api.JobClaimRecord]:
    inventory = _open_inventory(tmp_path)
    _seed_device_material(inventory)
    _seed_business_material_and_site(inventory)
    _admit_task(inventory)
    acquired = inventory.acquire_job_claim(
        _claim_command(
            command_uuid="82000000-0000-4000-8000-000000000421",
            job_uuid=FIRST_JOB_UUID,
        )
    )
    assert acquired.claim is not None
    running = inventory.mark_job_claim_running(
        inventory_api.JobClaimStateCommand(
            schema_version=1,
            command_uuid="83000000-0000-4000-8000-000000000421",
            idempotency_key="m1ef-effect-running-421",
            workflow_node_job_uuid=FIRST_JOB_UUID,
            attempt=1,
            claim_uuid=acquired.claim.uuid,
            fencing_token=acquired.claim.fencing_token,
            evidence_kind="driver_accepted",
            evidence_fingerprint="c" * 64,
        )
    )
    assert running.claim is not None
    return inventory, running.claim


def _command(
    claim: inventory_api.JobClaimRecord,
    *,
    command_uuid: str,
    effects: tuple[inventory_api.MaterialChangeSetEffect, ...],
) -> inventory_api.MaterialChangeSetCommand:
    return inventory_api.MaterialChangeSetCommand(
        schema_version=1,
        command_uuid=command_uuid,
        idempotency_key=f"m1ef-effect-{command_uuid}",
        workflow_task_uuid=WORKFLOW_TASK_UUID,
        workflow_node_job_uuid=FIRST_JOB_UUID,
        attempt=1,
        claim_uuid=claim.uuid,
        fencing_token=claim.fencing_token,
        effect_identity="terminal",
        outcome="succeeded",
        result={},
        effects=effects,
    )


def test_declared_material_and_site_effects_apply_once(
    tmp_path: Path,
) -> None:
    inventory, claim = _running_claim(tmp_path)
    command = _command(
        claim,
        command_uuid="84000000-0000-4000-8000-000000000421",
        effects=(
            inventory_api.MaterialChangeSetEffect(
                effect_key="01-heat-sample",
                resource_kind="business_material",
                resource_uuid=SAMPLE_MATERIAL_UUID,
                operation="update",
                expected_version=1,
                before={"data": {"temperature_c": 20.0}},
                after={"data": {"temperature_c": 80.0}},
            ),
            inventory_api.MaterialChangeSetEffect(
                effect_key="02-clear-site",
                resource_kind="site",
                resource_uuid=SITE_UUID,
                operation="set_occupancy",
                expected_version=1,
                before={"occupied_material_uuid": SAMPLE_MATERIAL_UUID},
                after={"occupied_material_uuid": None},
            ),
        ),
    )
    try:
        receipt = inventory.commit_material_changeset(command)
        assert inventory.get_material(SAMPLE_MATERIAL_UUID).data == {
            "temperature_c": 80.0
        }
        assert inventory.get_material(SAMPLE_MATERIAL_UUID).version == 2
        assert inventory.get_site(SITE_UUID).occupied_material_uuid is None
        assert inventory.get_site(SITE_UUID).version == 2

        assert inventory.commit_material_changeset(command) == receipt
        assert inventory.get_material(SAMPLE_MATERIAL_UUID).version == 2
        assert inventory.get_site(SITE_UUID).version == 2
    finally:
        inventory.close()


def test_create_effect_uses_claimed_parent_and_registered_template(
    tmp_path: Path,
) -> None:
    inventory, claim = _running_claim(tmp_path)
    command = _command(
        claim,
        command_uuid="84000000-0000-4000-8000-000000000422",
        effects=(
            inventory_api.MaterialChangeSetEffect(
                effect_key="01-create-product",
                resource_kind="business_material",
                resource_uuid=CREATED_MATERIAL_UUID,
                operation="create",
                expected_version=None,
                before={},
                after={
                    "resource_template_uuid": SAMPLE_TEMPLATE_UUID,
                    "parent_uuid": SAMPLE_MATERIAL_UUID,
                    "class": "SampleTube",
                    "barcode": "M1EF-PRODUCT-421",
                    "name": "M1EF product",
                    "meta_data": {},
                    "config": {},
                    "data": {"temperature_c": 80.0},
                    "disposition": "active",
                },
            ),
        ),
    )
    try:
        inventory.commit_material_changeset(command)
        created = inventory.get_material(CREATED_MATERIAL_UUID)
        assert created.parent_uuid == SAMPLE_MATERIAL_UUID
        assert created.resource_template_uuid == SAMPLE_TEMPLATE_UUID
        assert created.version == 1
    finally:
        inventory.close()


def test_declared_noop_effect_writes_receipt_without_version_or_fake_ledger(
    tmp_path: Path,
) -> None:
    inventory, claim = _running_claim(tmp_path)
    database = tmp_path / "inventory.db"

    def ledger_count() -> int:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            return int(
                connection.execute("SELECT COUNT(*) FROM inventory_ledger").fetchone()[
                    0
                ]
            )
        finally:
            connection.close()

    command = _command(
        claim,
        command_uuid="84000000-0000-4000-8000-000000000423",
        effects=(
            inventory_api.MaterialChangeSetEffect(
                effect_key="01-observed-unchanged",
                resource_kind="business_material",
                resource_uuid=SAMPLE_MATERIAL_UUID,
                operation="update",
                expected_version=1,
                before={"data": {"temperature_c": 20.0}},
                after={"data": {"temperature_c": 20.0}},
            ),
        ),
    )
    try:
        before = ledger_count()
        receipt = inventory.commit_material_changeset(command)
        assert receipt.effects == command.effects
        assert inventory.get_material(SAMPLE_MATERIAL_UUID).version == 1
        assert ledger_count() == before
    finally:
        inventory.close()
