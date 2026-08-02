"""M1EF D1A dispatch/result two-database saga integration evidence."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import tests.app.test_d1a_device_action_task_contract as contract
from tests.workflow.test_d1a_device_action_runtime_bridge import FeedbackHost
from unilabos.app.scheduler.backend import create_edge_stack
from unilabos.app.scheduler.inventory import (
    InventoryService,
    JobClaimAcquireCommand,
    MaterialClaimCorrupt,
    ResourceTemplateIdentity,
)
from unilabos.app.workflow_api import create_workflow_app
from unilabos.utils.type_check import serialize_result_info
from unilabos.workflow.catalog import TemplateCatalog
from unilabos.workflow.device_action_task import (
    DeviceActionTaskRuntimeBridge,
    DeviceActionTaskService,
)
from unilabos.workflow.runtime import WorkflowRuntimeCoordinator
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

DEVICE_MATERIAL_UUID = "51000000-0000-4000-8000-000000000401"


def _wait(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _job_status(store: WorkflowStore, job_uuid: str) -> str:
    with store.transaction() as connection:
        row = connection.execute(
            "SELECT status FROM workflow_node_job WHERE uuid = ?",
            (job_uuid,),
        ).fetchone()
    return str(row["status"])


def _inventory(tmp_path: Path) -> InventoryService:
    inventory = InventoryService.open(
        working_dir=tmp_path,
        resource_templates={
            contract.RESOURCE_TEMPLATE_UUID: ResourceTemplateIdentity(
                uuid=contract.RESOURCE_TEMPLATE_UUID,
                material_class="Robot",
            )
        },
    )
    inventory.bootstrap_resource_graph(
        {
            "source_id": "m1ef-d1a-device.json",
            "fingerprint": "sha256:" + "8" * 64,
            "materials": [
                {
                    "uuid": DEVICE_MATERIAL_UUID,
                    "resource_template_uuid": contract.RESOURCE_TEMPLATE_UUID,
                    "parent_uuid": None,
                    "class": "Robot",
                    "barcode": "",
                    "name": "robot",
                    "description": "D1A selected executor",
                    "meta_data": {
                        "source": "resource-tree-set",
                        "source_node_id": "robot",
                    },
                    "config": {},
                    "data": {},
                    "material_kind": "device",
                }
            ],
            "relative_positions": [],
            "sites": [],
        }
    )
    return inventory


def _database_rows(database: Path) -> dict[str, tuple[tuple[Any, ...], ...]]:
    connection = sqlite3.connect(database)
    try:
        tables = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        ]
        return {
            table: tuple(connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid'))
            for table in tables
        }
    finally:
        connection.close()


def _create_faulted_d1a_task(tmp_path: Path, fault_stage: str) -> dict[str, Any]:
    store = WorkflowStore(tmp_path / "workflow.db")
    catalog = TemplateCatalog(store)
    snapshot = catalog.replace(
        contract.AUTHORITY,
        [
            contract._template_import(
                name="move",
                display_name="移动",
                resource_template_uuid=contract.RESOURCE_TEMPLATE_UUID,
                schema=contract.SIMPLE_SCHEMA,
            )
        ],
    )
    inventory = _inventory(tmp_path)
    host = FeedbackHost()
    host.auto_complete = False
    scheduler, backend = create_edge_stack(
        host_node_getter=lambda: host,
        inventory=inventory,
    )
    host.backend = backend

    def fault(stage: str) -> None:
        if stage == fault_stage:
            raise RuntimeError(f"simulated crash at {stage}")

    bridge = DeviceActionTaskRuntimeBridge(
        store=store,
        coordinator=WorkflowRuntimeCoordinator(store),
        scheduler=scheduler,
        backend=backend,
        fault_hook=fault,
    )
    bridge.start()
    live = contract.MutableLiveCatalog()
    service = DeviceActionTaskService(
        store=store,
        template_catalog=catalog,
        authority=contract.AUTHORITY,
        live_catalog=live,
        admission=bridge,
    )
    client = TestClient(
        create_workflow_app(WorkflowService(store), device_action_tasks=service)
    )
    harness = contract.Harness(
        tmp_path / "workflow.db",
        store,
        client,
        catalog,
        snapshot.fingerprint,
        str(snapshot.node_templates[0]["uuid"]),
        "",
        live,
        bridge,
    )
    try:
        response = client.post(
            "/api/v1/device-action-tasks",
            json=contract._request(harness),
        )
        assert response.status_code == 201
        created = response.json()["data"]
        if fault_stage == "after_workflow_terminal_commit":
            assert _wait(lambda: len(host.sent) == 1)
            backend.publish_job_status(
                {"completed": True},
                host.sent[0],
                "success",
                serialize_result_info("", True, {"completed": True}),
            )
            assert _wait(lambda: _job_status(store, created["job_uuid"]) == "succeeded")
        return created
    finally:
        client.close()
        bridge.stop()
        backend.stop()
        inventory.close()
        store.close()


def test_d1a_terminal_saga_commits_receipt_before_releasing_claim_and_reopens(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    catalog = TemplateCatalog(store)
    snapshot = catalog.replace(
        contract.AUTHORITY,
        [
            contract._template_import(
                name="move",
                display_name="移动",
                resource_template_uuid=contract.RESOURCE_TEMPLATE_UUID,
                schema=contract.SIMPLE_SCHEMA,
            )
        ],
    )
    template = snapshot.node_templates[0]
    inventory = _inventory(tmp_path)
    workflow = WorkflowService(store)
    host = FeedbackHost()
    scheduler, backend = create_edge_stack(
        host_node_getter=lambda: host,
        inventory=inventory,
        workflow_tasks=workflow,
    )
    host.backend = backend
    bridge = DeviceActionTaskRuntimeBridge(
        store=store,
        coordinator=WorkflowRuntimeCoordinator(store),
        scheduler=scheduler,
        backend=backend,
    )
    bridge.start()
    live = contract.MutableLiveCatalog()
    service = DeviceActionTaskService(
        store=store,
        template_catalog=catalog,
        authority=contract.AUTHORITY,
        live_catalog=live,
        admission=bridge,
    )
    client = TestClient(create_workflow_app(workflow, device_action_tasks=service))
    harness = contract.Harness(
        tmp_path / "workflow.db",
        store,
        client,
        catalog,
        snapshot.fingerprint,
        str(template["uuid"]),
        "",
        live,
        bridge,
    )
    try:
        response = client.post(
            "/api/v1/device-action-tasks",
            json=contract._request(harness),
        )
        assert response.status_code == 201
        created = response.json()["data"]
        assert _wait(lambda: service.get(created["task_uuid"])["status"] == "succeeded")

        claim = inventory.get_job_claim(created["job_uuid"], 1)
        assert claim.state == "released"
        assert claim.terminal_changeset_uuid is not None
        assert claim.workflow_terminal_fingerprint is not None
        assert inventory.list_unsettled_claims() == ()

        connection = sqlite3.connect(tmp_path / "workflow.db")
        connection.row_factory = sqlite3.Row
        try:
            projection = connection.execute(
                """
                SELECT claim_status, inventory_claim_uuid,
                       inventory_fencing_token,
                       material_changeset_uuid,
                       material_changeset_fingerprint,
                       material_changeset_outbox_sequence,
                       workflow_terminal_fingerprint
                FROM device_action_task WHERE workflow_node_job_uuid = ?
                """,
                (created["job_uuid"],),
            ).fetchone()
        finally:
            connection.close()
        assert projection["claim_status"] == "released"
        assert projection["inventory_claim_uuid"] == claim.uuid
        assert projection["inventory_fencing_token"] == claim.fencing_token
        assert projection["material_changeset_uuid"] == claim.terminal_changeset_uuid
        assert projection["material_changeset_fingerprint"].startswith("sha256:")
        assert projection["material_changeset_outbox_sequence"] > 0
        assert (
            projection["workflow_terminal_fingerprint"]
            == claim.workflow_terminal_fingerprint
        )
        guarded = sqlite3.connect(tmp_path / "workflow.db")
        try:
            with pytest.raises(sqlite3.IntegrityError, match="projection"):
                guarded.execute(
                    """
                    UPDATE device_action_task
                    SET material_changeset_fingerprint = NULL
                    WHERE workflow_node_job_uuid = ?
                    """,
                    (created["job_uuid"],),
                )
            guarded.rollback()
        finally:
            guarded.close()
    finally:
        client.close()
        bridge.stop()
        backend.stop()
        inventory.close()
        store.close()

    reopened = _inventory(tmp_path)
    try:
        durable = reopened.get_job_claim(created["job_uuid"], 1)
        assert durable.state == "released"
        assert durable.fencing_token == claim.fencing_token
        assert reopened.list_unsettled_claims() == ()
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ("fault_stage", "expected_state", "expected_job_status"),
    [
        ("after_inventory_claim_commit", "released", "succeeded"),
        ("after_workflow_dispatch_commit", "uncertain", "execution_unknown"),
        ("after_inventory_claim_running", "uncertain", "execution_unknown"),
        ("after_material_changeset_commit", "released", "succeeded"),
        ("after_workflow_terminal_commit", "released", "succeeded"),
        ("after_inventory_claim_release", "released", "succeeded"),
        ("before_material_changeset_outbox_ack", "released", "succeeded"),
        ("before_material_claim_release_outbox_ack", "released", "succeeded"),
    ],
    ids=["c1", "c2", "c3", "c4", "c5", "c6", "c7-receipt", "c7-release"],
)
def test_d1a_crash_windows_replay_from_two_durable_databases(
    tmp_path: Path,
    fault_stage: str,
    expected_state: str,
    expected_job_status: str,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    catalog = TemplateCatalog(store)
    snapshot = catalog.replace(
        contract.AUTHORITY,
        [
            contract._template_import(
                name="move",
                display_name="移动",
                resource_template_uuid=contract.RESOURCE_TEMPLATE_UUID,
                schema=contract.SIMPLE_SCHEMA,
            )
        ],
    )
    template = snapshot.node_templates[0]
    inventory = _inventory(tmp_path)
    workflow = WorkflowService(store)
    first_host = FeedbackHost()
    first_host.auto_complete = False
    scheduler, backend = create_edge_stack(
        host_node_getter=lambda: first_host,
        inventory=inventory,
    )
    first_host.backend = backend

    def fault(stage: str) -> None:
        if stage == fault_stage:
            raise RuntimeError(f"simulated crash at {stage}")

    bridge = DeviceActionTaskRuntimeBridge(
        store=store,
        coordinator=WorkflowRuntimeCoordinator(store),
        scheduler=scheduler,
        backend=backend,
        fault_hook=fault,
    )
    bridge.start()
    live = contract.MutableLiveCatalog()
    service = DeviceActionTaskService(
        store=store,
        template_catalog=catalog,
        authority=contract.AUTHORITY,
        live_catalog=live,
        admission=bridge,
    )
    client = TestClient(create_workflow_app(workflow, device_action_tasks=service))
    harness = contract.Harness(
        tmp_path / "workflow.db",
        store,
        client,
        catalog,
        snapshot.fingerprint,
        str(template["uuid"]),
        "",
        live,
        bridge,
    )
    response = client.post(
        "/api/v1/device-action-tasks",
        json=contract._request(harness),
    )
    assert response.status_code == 201
    created = response.json()["data"]
    if fault_stage == "after_inventory_claim_running":
        assert _wait(
            lambda: inventory.get_job_claim(created["job_uuid"], 1).state == "running"
        )
    if fault_stage not in {
        "after_inventory_claim_commit",
        "after_workflow_dispatch_commit",
        "after_inventory_claim_running",
    }:
        assert _wait(lambda: len(first_host.sent) == 1)
        backend.publish_job_status(
            {"completed": True},
            first_host.sent[0],
            "success",
            serialize_result_info("", True, {"completed": True}),
        )
        assert _wait(
            lambda: (
                inventory.get_terminal_material_changeset(
                    created["job_uuid"],
                    1,
                )
                is not None
            )
        )

    client.close()
    bridge.stop()
    backend.stop()
    inventory.close()
    store.close()

    reopened_store = WorkflowStore(tmp_path / "workflow.db")
    coordinator = WorkflowRuntimeCoordinator(reopened_store)
    coordinator.recover_startup()
    reopened_inventory = _inventory(tmp_path)
    second_host = FeedbackHost()
    second_host.auto_complete = fault_stage == "after_inventory_claim_commit"
    second_scheduler, second_backend = create_edge_stack(
        host_node_getter=lambda: second_host,
        inventory=reopened_inventory,
    )
    second_host.backend = second_backend
    recovered_bridge = DeviceActionTaskRuntimeBridge(
        store=reopened_store,
        coordinator=coordinator,
        scheduler=second_scheduler,
        backend=second_backend,
    )
    try:
        recovered_bridge.start()
        assert _wait(
            lambda: (
                reopened_inventory.get_job_claim(created["job_uuid"], 1).state
                == expected_state
            )
        )
        assert _wait(
            lambda: (
                _job_status(reopened_store, created["job_uuid"]) == expected_job_status
            )
        )
        claim = reopened_inventory.get_job_claim(created["job_uuid"], 1)
        assert claim.state == expected_state
        if expected_state == "released":
            assert reopened_inventory.list_unsettled_claims() == ()
            with reopened_store.transaction() as connection:
                projection = connection.execute(
                    """
                    SELECT claim_status FROM device_action_task
                    WHERE workflow_node_job_uuid = ?
                    """,
                    (created["job_uuid"],),
                ).fetchone()[0]
            assert projection == "released"
        else:
            assert second_host.sent == []
            assert recovered_bridge.busy_device_action_keys() == {"/devices/robot"}
    finally:
        recovered_bridge.stop()
        second_backend.stop()
        reopened_inventory.close()
        reopened_store.close()

    before_second_restart = {
        "workflow": _database_rows(tmp_path / "workflow.db"),
        "inventory": _database_rows(tmp_path / "inventory.db"),
    }
    second_reopened_store = WorkflowStore(tmp_path / "workflow.db")
    second_coordinator = WorkflowRuntimeCoordinator(second_reopened_store)
    second_coordinator.recover_startup()
    second_reopened_inventory = _inventory(tmp_path)
    third_host = FeedbackHost()
    third_host.auto_complete = False
    third_scheduler, third_backend = create_edge_stack(
        host_node_getter=lambda: third_host,
        inventory=second_reopened_inventory,
    )
    third_bridge = DeviceActionTaskRuntimeBridge(
        store=second_reopened_store,
        coordinator=second_coordinator,
        scheduler=third_scheduler,
        backend=third_backend,
    )
    try:
        third_bridge.start()
        assert third_host.sent == []
    finally:
        third_bridge.stop()
        third_backend.stop()
        second_reopened_inventory.close()
        second_reopened_store.close()
    assert {
        "workflow": _database_rows(tmp_path / "workflow.db"),
        "inventory": _database_rows(tmp_path / "inventory.db"),
    } == before_second_restart


def test_d1a_dispatch_startup_fails_closed_for_corrupt_inventory_fence(
    tmp_path: Path,
) -> None:
    inventory = _inventory(tmp_path)
    task_uuid = "91000000-0000-4000-8000-000000000901"
    job_uuid = "b1000000-0000-4000-8000-000000000901"
    acquired = inventory.acquire_job_claim(
        JobClaimAcquireCommand(
            schema_version=1,
            command_uuid="82000000-0000-4000-8000-000000000901",
            idempotency_key="m1ef-audit-corrupt-fence",
            workflow_task_uuid=task_uuid,
            workflow_node_job_uuid=job_uuid,
            attempt=1,
            device_material_uuid=DEVICE_MATERIAL_UUID,
            mutable_material_root_uuids=(),
            occupancy_changing_site_uuids=(),
        )
    )
    assert acquired.claim is not None
    connection = sqlite3.connect(tmp_path / "inventory.db")
    try:
        connection.execute(
            "DELETE FROM material_resource_fence WHERE claim_uuid = ?",
            (acquired.claim.uuid,),
        )
        connection.commit()
    finally:
        connection.close()

    store = WorkflowStore(tmp_path / "workflow.db")
    scheduler, backend = create_edge_stack(
        host_node_getter=lambda: None,
        inventory=inventory,
    )
    bridge = DeviceActionTaskRuntimeBridge(
        store=store,
        coordinator=WorkflowRuntimeCoordinator(store),
        scheduler=scheduler,
        backend=backend,
    )
    try:
        with pytest.raises(MaterialClaimCorrupt, match="fence"):
            bridge.start()
        assert not bridge.is_available()
    finally:
        bridge.stop()
        backend.stop()
        store.close()
        inventory.close()


def test_d1a_startup_fails_closed_for_inventory_claim_without_workflow_facts(
    tmp_path: Path,
) -> None:
    inventory = _inventory(tmp_path)
    acquired = inventory.acquire_job_claim(
        JobClaimAcquireCommand(
            schema_version=1,
            command_uuid="82000000-0000-4000-8000-000000000902",
            idempotency_key="m1ef-orphan-claim",
            workflow_task_uuid="91000000-0000-4000-8000-000000000902",
            workflow_node_job_uuid="b1000000-0000-4000-8000-000000000902",
            attempt=1,
            device_material_uuid=DEVICE_MATERIAL_UUID,
            mutable_material_root_uuids=(),
            occupancy_changing_site_uuids=(),
        )
    )
    assert acquired.claim is not None
    store = WorkflowStore(tmp_path / "workflow.db")
    scheduler, backend = create_edge_stack(
        host_node_getter=lambda: None,
        inventory=inventory,
    )
    bridge = DeviceActionTaskRuntimeBridge(
        store=store,
        coordinator=WorkflowRuntimeCoordinator(store),
        scheduler=scheduler,
        backend=backend,
    )
    try:
        with pytest.raises(WorkflowError) as raised:
            bridge.start()
        assert raised.value.code == "reconciliation_required"
        assert not bridge.is_available()
        assert inventory.list_unsettled_claims() == (acquired.claim,)
    finally:
        bridge.stop()
        backend.stop()
        store.close()
        inventory.close()


def test_c1_claim_followed_by_durable_cancel_releases_only_with_no_send_proof(
    tmp_path: Path,
) -> None:
    created = _create_faulted_d1a_task(tmp_path, "after_inventory_claim_commit")
    connection = sqlite3.connect(tmp_path / "workflow.db")
    try:
        connection.execute(
            "UPDATE workflow_node_job SET status = 'canceled' WHERE uuid = ?",
            (created["job_uuid"],),
        )
        connection.execute(
            """
            UPDATE workflow_task
            SET status = 'canceled', cleanup_status = 'pending' WHERE uuid = ?
            """,
            (created["task_uuid"],),
        )
        connection.commit()
    finally:
        connection.close()

    store = WorkflowStore(tmp_path / "workflow.db")
    inventory = _inventory(tmp_path)
    host = FeedbackHost()
    scheduler, backend = create_edge_stack(
        host_node_getter=lambda: host,
        inventory=inventory,
    )
    bridge = DeviceActionTaskRuntimeBridge(
        store=store,
        coordinator=WorkflowRuntimeCoordinator(store),
        scheduler=scheduler,
        backend=backend,
    )
    try:
        bridge.start()
        claim = inventory.get_job_claim(created["job_uuid"], 1)
        assert claim.state == "released"
        assert claim.release_proof_kind == "not_submitted"
        assert claim.terminal_changeset_uuid is None
        assert claim.workflow_terminal_fingerprint is None
        assert host.sent == []
        with store.transaction() as workflow_connection:
            projection = workflow_connection.execute(
                """
                SELECT claim_status FROM device_action_task
                WHERE workflow_node_job_uuid = ?
                """,
                (created["job_uuid"],),
            ).fetchone()
        assert projection["claim_status"] == "released"
    finally:
        bridge.stop()
        backend.stop()
        inventory.close()
        store.close()

    before_restart = {
        "workflow": _database_rows(tmp_path / "workflow.db"),
        "inventory": _database_rows(tmp_path / "inventory.db"),
    }
    reopened_store = WorkflowStore(tmp_path / "workflow.db")
    reopened_inventory = _inventory(tmp_path)
    reopened_scheduler, reopened_backend = create_edge_stack(
        host_node_getter=lambda: None,
        inventory=reopened_inventory,
    )
    reopened_bridge = DeviceActionTaskRuntimeBridge(
        store=reopened_store,
        coordinator=WorkflowRuntimeCoordinator(reopened_store),
        scheduler=reopened_scheduler,
        backend=reopened_backend,
    )
    try:
        reopened_bridge.start()
    finally:
        reopened_bridge.stop()
        reopened_backend.stop()
        reopened_inventory.close()
        reopened_store.close()
    assert {
        "workflow": _database_rows(tmp_path / "workflow.db"),
        "inventory": _database_rows(tmp_path / "inventory.db"),
    } == before_restart


def test_terminal_receipt_recovery_rejects_mismatched_workflow_payload(
    tmp_path: Path,
) -> None:
    created = _create_faulted_d1a_task(tmp_path, "after_workflow_terminal_commit")
    connection = sqlite3.connect(tmp_path / "workflow.db")
    try:
        connection.execute(
            "UPDATE workflow_node_job SET return_info = ? WHERE uuid = ?",
            ('{"tampered":true}', created["job_uuid"]),
        )
        connection.commit()
    finally:
        connection.close()

    store = WorkflowStore(tmp_path / "workflow.db")
    inventory = _inventory(tmp_path)
    scheduler, backend = create_edge_stack(
        host_node_getter=lambda: None,
        inventory=inventory,
    )
    bridge = DeviceActionTaskRuntimeBridge(
        store=store,
        coordinator=WorkflowRuntimeCoordinator(store),
        scheduler=scheduler,
        backend=backend,
    )
    try:
        with pytest.raises(WorkflowError) as raised:
            bridge.start()
        assert raised.value.code == "reconciliation_required"
        assert not bridge.is_available()
        assert inventory.get_job_claim(created["job_uuid"], 1).state != "released"
    finally:
        bridge.stop()
        backend.stop()
        inventory.close()
        store.close()
