"""InventoryService 的封闭 Task Material 命令适配器。"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from unilabos.app.scheduler.inventory.domain import (
    InventoryError,
    TaskMaterialAdmissionCommand,
    TaskMaterialAdmissionSource,
    TaskMaterialReleaseCommand,
)
from unilabos.app.scheduler.inventory.service import InventoryService


def _admission_command(payload: dict[str, Any]) -> TaskMaterialAdmissionCommand:
    raw_sources = payload["sources"]
    if not isinstance(raw_sources, list):
        raise TypeError("sources must be an array")
    sources = tuple(
        TaskMaterialAdmissionSource(
            material_source_node_uuid=item["material_source_node_uuid"],
            mode=item["mode"],
            resource_template_uuid=item["resource_template_uuid"],
            mount=dict(item.get("mount") or {}),
            material_uuid=item.get("material_uuid"),
            site_uuid=item.get("site_uuid"),
            candidate_site_uuids=tuple(item.get("candidate_site_uuids") or ()),
            flow_role=item["flow_role"],
        )
        for item in raw_sources
    )
    return TaskMaterialAdmissionCommand(
        schema_version=payload["schema_version"],
        command_uuid=payload["command_uuid"],
        idempotency_key=payload["idempotency_key"],
        workflow_task_uuid=payload["workflow_task_uuid"],
        workflow_snapshot_fingerprint=payload["workflow_snapshot_fingerprint"],
        sources=sources,
    )


def _release_command(payload: dict[str, Any]) -> TaskMaterialReleaseCommand:
    return TaskMaterialReleaseCommand(
        schema_version=payload["schema_version"],
        command_uuid=payload["command_uuid"],
        idempotency_key=payload["idempotency_key"],
        workflow_task_uuid=payload["workflow_task_uuid"],
        reason=payload["reason"],
    )


def execute_command(
    service: InventoryService,
    command: dict[str, Any],
) -> dict[str, Any]:
    """只执行允许从远端进入的、封闭且带版本的 Material 命令。"""

    command_type = str(command.get("type") or "")
    payload = command.get("payload")
    command_id = str(command.get("command_id") or "")
    if not isinstance(payload, dict):
        return {
            "command_id": command_id,
            "status": "rejected",
            "error": "payload must be an object",
        }
    if command_id and payload.get("command_uuid") != command_id:
        return {
            "command_id": command_id,
            "status": "rejected",
            "error": "command_id must match payload.command_uuid",
        }
    try:
        if command_type == "material.admit":
            result = service.admit_task(_admission_command(payload))
        elif command_type == "material.release":
            result = service.release_task(_release_command(payload))
        else:
            return {
                "command_id": command_id,
                "status": "rejected",
                "error": f"unknown command type: {command_type}",
            }
    except InventoryError as exc:
        return {
            "command_id": command_id,
            "status": "rejected",
            "error": str(exc),
            "error_code": exc.code,
        }
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "command_id": command_id,
            "status": "rejected",
            "error": f"bad payload: {exc}",
        }
    return {
        "command_id": result.command_uuid,
        "status": "completed",
        "result": asdict(result),
    }
