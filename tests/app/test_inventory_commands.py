"""Closed Task Material command and private HTTP/WS adapter tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from queue import Queue
from typing import Any

from fastapi.testclient import TestClient

from unilabos.app.scheduler.inventory import (
    InventoryService,
    ResourceTemplateIdentity,
    execute_command,
)
from unilabos.app.scheduler.inventory.api import create_app
from unilabos.app.ws_client import DeviceActionManager, MessageProcessor

SAMPLE_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000801"
MOUNT_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000802"
SAMPLE_UUID = "50000000-0000-4000-8000-000000000801"
MOUNT_UUID = "50000000-0000-4000-8000-000000000802"
TASK_UUID = "70000000-0000-4000-8000-000000000801"
NODE_UUID = "40000000-0000-4000-8000-000000000801"
ADMIT_UUID = "80000000-0000-4000-8000-000000000801"
RELEASE_UUID = "80000000-0000-4000-8000-000000000802"


def _open(path: Path) -> InventoryService:
    templates = {
        SAMPLE_TEMPLATE_UUID: ResourceTemplateIdentity(
            SAMPLE_TEMPLATE_UUID,
            "SampleTube",
        ),
        MOUNT_TEMPLATE_UUID: ResourceTemplateIdentity(
            MOUNT_TEMPLATE_UUID,
            "SampleRack",
        ),
    }
    inventory = InventoryService.open(
        working_dir=path,
        resource_templates=templates,
    )
    inventory.create_material(
        material_uuid=MOUNT_UUID,
        resource_template_uuid=MOUNT_TEMPLATE_UUID,
        barcode="COMMAND-MOUNT",
        name="Command mount",
    )
    inventory.create_material(
        material_uuid=SAMPLE_UUID,
        resource_template_uuid=SAMPLE_TEMPLATE_UUID,
        barcode="COMMAND-SAMPLE",
        name="Command sample",
    )
    return inventory


def _admission_envelope() -> dict[str, Any]:
    return {
        "command_id": ADMIT_UUID,
        "type": "material.admit",
        "payload": {
            "schema_version": 1,
            "command_uuid": ADMIT_UUID,
            "idempotency_key": f"task:{TASK_UUID}:admit",
            "workflow_task_uuid": TASK_UUID,
            "workflow_snapshot_fingerprint": "sha256:" + "a" * 64,
            "sources": [
                {
                    "material_source_node_uuid": NODE_UUID,
                    "mode": "existing",
                    "resource_template_uuid": SAMPLE_TEMPLATE_UUID,
                    "mount": {"uuid": MOUNT_UUID},
                    "material_uuid": SAMPLE_UUID,
                    "site_uuid": None,
                    "candidate_site_uuids": [],
                    "flow_role": "sample",
                }
            ],
        },
    }


def _release_envelope() -> dict[str, Any]:
    return {
        "command_id": RELEASE_UUID,
        "type": "material.release",
        "payload": {
            "schema_version": 1,
            "command_uuid": RELEASE_UUID,
            "idempotency_key": f"task:{TASK_UUID}:release",
            "workflow_task_uuid": TASK_UUID,
            "reason": "task_canceled",
        },
    }


def test_closed_commands_admit_release_and_replay(tmp_path: Path) -> None:
    inventory = _open(tmp_path)
    try:
        admitted = execute_command(inventory, _admission_envelope())
        replayed = execute_command(inventory, _admission_envelope())
        assert admitted == replayed
        assert admitted["status"] == "completed"
        assert admitted["result"]["status"] == "admitted"

        released = execute_command(inventory, _release_envelope())
        release_replay = execute_command(inventory, _release_envelope())
        assert released == release_replay
        assert released["result"]["status"] == "released"
    finally:
        inventory.close()


def test_command_adapter_rejects_unknown_or_mismatched_envelopes(
    tmp_path: Path,
) -> None:
    inventory = _open(tmp_path)
    try:
        mismatch = _admission_envelope()
        mismatch["command_id"] = RELEASE_UUID
        assert execute_command(inventory, mismatch)["status"] == "rejected"
        unknown = {
            "command_id": ADMIT_UUID,
            "type": "inventory.template.upsert",
            "payload": {"command_uuid": ADMIT_UUID},
        }
        result = execute_command(inventory, unknown)
        assert result["status"] == "rejected"
        assert "unknown command type" in result["error"]
    finally:
        inventory.close()


def test_private_http_adapter_projects_only_canonical_resources(tmp_path: Path) -> None:
    inventory = _open(tmp_path)
    try:
        client = TestClient(create_app(inventory))
        materials = client.get("/api/v1/inventory/materials")
        assert materials.status_code == 200
        assert {row["uuid"] for row in materials.json()["materials"]} == {
            SAMPLE_UUID,
            MOUNT_UUID,
        }
        detail = client.get(f"/api/v1/inventory/materials/{SAMPLE_UUID}")
        assert detail.status_code == 200
        assert detail.json()["resource_template_uuid"] == SAMPLE_TEMPLATE_UUID
        assert client.get("/api/v1/inventory/snapshot").status_code == 200
    finally:
        inventory.close()


def test_ws_channel_uses_the_same_closed_command_path(tmp_path: Path) -> None:
    inventory = _open(tmp_path)
    processor = MessageProcessor(
        "ws://test",
        Queue(maxsize=100),
        DeviceActionManager(),
    )
    processor.inventory_service = inventory
    try:
        asyncio.run(processor._handle_inventory_command(_admission_envelope()))
        messages = []
        while not processor.send_queue.empty():
            messages.append(processor.send_queue.get_nowait())
        results = [
            message
            for message in messages
            if message.get("action") == "inventory_command_result"
        ]
        assert len(results) == 1
        assert results[0]["data"]["result"]["status"] == "admitted"
    finally:
        inventory.close()
