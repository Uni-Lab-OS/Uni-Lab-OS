"""M1EF declared Material/Site effect and no-op invariants."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

import unilabos.app.scheduler.inventory as inventory_api
from tests.app.test_m1ef_inventory_claim_lifecycle import (
    ADMISSION_COMMAND_UUID,
    DEVICE_MATERIAL_UUID,
    FIRST_JOB_UUID,
    MATERIAL_SOURCE_NODE_UUID,
    MOUNT_MATERIAL_UUID,
    MOUNT_TEMPLATE_UUID,
    SAMPLE_MATERIAL_UUID,
    SAMPLE_TEMPLATE_UUID,
    SITE_UUID,
    WORKFLOW_SNAPSHOT_FINGERPRINT,
    WORKFLOW_TASK_UUID,
    _admit_task,
    _claim_command,
    _open_inventory,
    _seed_business_material_and_site,
    _seed_device_material,
)

CREATED_MATERIAL_UUID = "51000000-0000-4000-8000-000000000421"
UNCLAIMED_PARENT_UUID = "51000000-0000-4000-8000-000000000422"
CREATED_SITE_UUID = "61000000-0000-4000-8000-000000000421"
OUTER_MOUNT_UUID = "51000000-0000-4000-8000-000000000423"
OUTER_SITE_UUID = "61000000-0000-4000-8000-000000000423"


def _running_claim(
    tmp_path: Path,
    *,
    mutable_roots: tuple[str, ...] | None = None,
) -> tuple[inventory_api.InventoryService, inventory_api.JobClaimRecord]:
    inventory = _open_inventory(tmp_path)
    _seed_device_material(inventory)
    _seed_business_material_and_site(inventory)
    if mutable_roots is None:
        _admit_task(inventory)
    else:
        inventory.create_material(
            material_uuid=OUTER_MOUNT_UUID,
            resource_template_uuid=MOUNT_TEMPLATE_UUID,
            barcode="M1EF-OUTER-MOUNT-421",
            name="M1EF outer mount",
        )
        inventory.create_site(
            site_uuid=OUTER_SITE_UUID,
            description="M1EF outer mount Site",
            meta_data={"slot": "outer"},
            material_uuid=OUTER_MOUNT_UUID,
            name="outer",
            sort_order=0,
            allowed_resource_template_uuids=[MOUNT_TEMPLATE_UUID],
            occupied_material_uuid=MOUNT_MATERIAL_UUID,
            position_x=0.0,
            position_y=0.0,
            position_z=0.0,
            depth=1.0,
            length=1.0,
            width=1.0,
        )
        admitted = inventory.admit_task(
            inventory_api.TaskMaterialAdmissionCommand(
                schema_version=1,
                command_uuid=ADMISSION_COMMAND_UUID,
                idempotency_key="m1ef-admit-mount-and-sample-421",
                workflow_task_uuid=WORKFLOW_TASK_UUID,
                workflow_snapshot_fingerprint=WORKFLOW_SNAPSHOT_FINGERPRINT,
                sources=(
                    inventory_api.TaskMaterialAdmissionSource(
                        material_source_node_uuid=MATERIAL_SOURCE_NODE_UUID,
                        mode="existing",
                        resource_template_uuid=SAMPLE_TEMPLATE_UUID,
                        mount={"uuid": MOUNT_MATERIAL_UUID},
                        material_uuid=SAMPLE_MATERIAL_UUID,
                        site_uuid=SITE_UUID,
                        candidate_site_uuids=(),
                        flow_role="primary_sample",
                    ),
                    inventory_api.TaskMaterialAdmissionSource(
                        material_source_node_uuid=(
                            "a1000000-0000-4000-8000-000000000421"
                        ),
                        mode="existing",
                        resource_template_uuid=MOUNT_TEMPLATE_UUID,
                            mount={"uuid": OUTER_MOUNT_UUID},
                            material_uuid=MOUNT_MATERIAL_UUID,
                            site_uuid=OUTER_SITE_UUID,
                        candidate_site_uuids=(),
                        flow_role="consumable",
                    ),
                ),
            )
        )
        assert admitted.status == "admitted"
    acquire_command = _claim_command(
        command_uuid="82000000-0000-4000-8000-000000000421",
        job_uuid=FIRST_JOB_UUID,
    )
    if mutable_roots is not None:
        acquire_command = replace(
            acquire_command,
            mutable_material_root_uuids=mutable_roots,
        )
    acquired = inventory.acquire_job_claim(acquire_command)
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


def test_changeset_cannot_replace_claim_member_version_baseline(
    tmp_path: Path,
) -> None:
    inventory, claim = _running_claim(tmp_path)
    connection = sqlite3.connect(tmp_path / "inventory.db")
    try:
        connection.execute(
            "UPDATE material SET version = 2 WHERE uuid = ?",
            (SAMPLE_MATERIAL_UUID,),
        )
        connection.commit()
    finally:
        connection.close()
    command = _command(
        claim,
        command_uuid="84000000-0000-4000-8000-000000000424",
        effects=(
            inventory_api.MaterialChangeSetEffect(
                effect_key="01-stale-sample",
                resource_kind="business_material",
                resource_uuid=SAMPLE_MATERIAL_UUID,
                operation="update",
                expected_version=2,
                before={},
                after={"data": {"temperature_c": 90.0}},
            ),
        ),
    )
    try:
        with pytest.raises(
            inventory_api.MaterialClaimCorrupt,
            match="durable reality",
        ):
            inventory.commit_material_changeset(command)
        assert inventory.get_terminal_material_changeset(FIRST_JOB_UUID, 1) is None
        assert inventory.get_material(SAMPLE_MATERIAL_UUID).version == 2
    finally:
        inventory.close()


def test_reparent_requires_the_target_parent_in_the_live_claim(
    tmp_path: Path,
) -> None:
    inventory, claim = _running_claim(tmp_path)
    inventory.create_material(
        material_uuid=UNCLAIMED_PARENT_UUID,
        resource_template_uuid=MOUNT_TEMPLATE_UUID,
        barcode="M1EF-UNCLAIMED-PARENT",
        name="Unclaimed parent",
    )
    command = _command(
        claim,
        command_uuid="84000000-0000-4000-8000-000000000425",
        effects=(
            inventory_api.MaterialChangeSetEffect(
                effect_key="01-reparent-outside-claim",
                resource_kind="business_material",
                resource_uuid=SAMPLE_MATERIAL_UUID,
                operation="reparent",
                expected_version=1,
                before={"parent_uuid": None},
                after={"parent_uuid": UNCLAIMED_PARENT_UUID},
            ),
        ),
    )
    try:
        with pytest.raises(inventory_api.MaterialConflict, match="Claim member"):
            inventory.commit_material_changeset(command)
        assert inventory.get_material(SAMPLE_MATERIAL_UUID).parent_uuid is None
    finally:
        inventory.close()


def test_reparent_rejects_cycle_through_site_occupancy(
    tmp_path: Path,
) -> None:
    inventory, claim = _running_claim(
        tmp_path,
        mutable_roots=(MOUNT_MATERIAL_UUID, SAMPLE_MATERIAL_UUID),
    )
    command = _command(
        claim,
        command_uuid="84000000-0000-4000-8000-000000000429",
        effects=(
            inventory_api.MaterialChangeSetEffect(
                effect_key="01-reparent-site-owner-under-occupant",
                resource_kind="business_material",
                resource_uuid=MOUNT_MATERIAL_UUID,
                operation="reparent",
                expected_version=1,
                before={"parent_uuid": None},
                after={"parent_uuid": SAMPLE_MATERIAL_UUID},
            ),
        ),
    )
    try:
        with pytest.raises(inventory_api.MaterialConflict, match="cycle"):
            inventory.commit_material_changeset(command)
        assert inventory.get_material(MOUNT_MATERIAL_UUID).parent_uuid is None
        assert inventory.get_terminal_material_changeset(FIRST_JOB_UUID, 1) is None
    finally:
        inventory.close()


@pytest.mark.parametrize(
    ("owner_uuid", "allowlist", "message"),
    [
        (DEVICE_MATERIAL_UUID, [MOUNT_TEMPLATE_UUID], "not allowed"),
        (SAMPLE_MATERIAL_UUID, [SAMPLE_TEMPLATE_UUID], "cycle"),
        (DEVICE_MATERIAL_UUID, [SAMPLE_TEMPLATE_UUID], "another Site"),
    ],
    ids=["allowlist", "cycle", "duplicate-occupant"],
)
def test_create_site_enforces_complete_placement_invariants(
    tmp_path: Path,
    owner_uuid: str,
    allowlist: list[str],
    message: str,
) -> None:
    inventory, claim = _running_claim(tmp_path)
    command = _command(
        claim,
        command_uuid={
            "not allowed": "84000000-0000-4000-8000-000000000426",
            "cycle": "84000000-0000-4000-8000-000000000427",
            "another Site": "84000000-0000-4000-8000-000000000428",
        }[message],
        effects=(
            inventory_api.MaterialChangeSetEffect(
                effect_key="01-create-site",
                resource_kind="site",
                resource_uuid=CREATED_SITE_UUID,
                operation="create",
                expected_version=None,
                before={},
                after={
                    "material_uuid": owner_uuid,
                    "name": "created-site",
                    "allowed_resource_template_uuids": allowlist,
                    "occupied_material_uuid": SAMPLE_MATERIAL_UUID,
                },
            ),
        ),
    )
    try:
        with pytest.raises(inventory_api.MaterialConflict, match=message):
            inventory.commit_material_changeset(command)
        with pytest.raises(inventory_api.MaterialNotFound):
            inventory.get_site(CREATED_SITE_UUID)
    finally:
        inventory.close()
