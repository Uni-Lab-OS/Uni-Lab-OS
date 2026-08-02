"""M1R InventoryService concurrent admission 最小纵向合同。

测试只通过两个 public ``InventoryService`` 实例同时提交同一
Material，不访问 Store、SQLite、私有锁或 Scheduler。
"""

from __future__ import annotations

from pathlib import Path
from threading import Barrier, Lock, Thread
from typing import Any

import unilabos.app.scheduler.inventory as inventory_api

MATERIAL_UUID = "5aa00000-0000-4000-8000-000000000106"
MOUNT_UUID = "5aa00000-0000-4000-8000-000000000108"
SITE_UUID = "6aa00000-0000-4000-8000-000000000106"
RESOURCE_TEMPLATE_UUID = "2bb00000-0000-4000-8000-000000000106"
COMMAND_A_UUID = "80000000-0000-4000-8000-000000000106"
COMMAND_B_UUID = "80000000-0000-4000-8000-000000000107"
WORKFLOW_TASK_A_UUID = "90000000-0000-4000-8000-000000000106"
WORKFLOW_TASK_B_UUID = "90000000-0000-4000-8000-000000000107"
MATERIAL_SOURCE_NODE_A_UUID = "a0000000-0000-4000-8000-000000000106"
MATERIAL_SOURCE_NODE_B_UUID = "a0000000-0000-4000-8000-000000000107"


def _resource_templates() -> dict[str, inventory_api.ResourceTemplateIdentity]:
    identity = inventory_api.ResourceTemplateIdentity(
        uuid=RESOURCE_TEMPLATE_UUID,
        material_class="SampleTube",
    )
    return {identity.uuid: identity}


def _admission_command(
    *,
    command_uuid: str,
    workflow_task_uuid: str,
    material_source_node_uuid: str,
    fingerprint: str,
) -> inventory_api.TaskMaterialAdmissionCommand:
    source = inventory_api.TaskMaterialAdmissionSource(
        material_source_node_uuid=material_source_node_uuid,
        mode="existing",
        resource_template_uuid=RESOURCE_TEMPLATE_UUID,
        mount={"uuid": MOUNT_UUID},
        material_uuid=MATERIAL_UUID,
        site_uuid=SITE_UUID,
        candidate_site_uuids=(),
        flow_role="sample",
    )
    return inventory_api.TaskMaterialAdmissionCommand(
        schema_version=1,
        command_uuid=command_uuid,
        idempotency_key=f"m1r-contention-{workflow_task_uuid}",
        workflow_task_uuid=workflow_task_uuid,
        workflow_snapshot_fingerprint=fingerprint,
        sources=(source,),
    )


def _admit_at_barrier(
    inventory: inventory_api.InventoryService,
    command: inventory_api.TaskMaterialAdmissionCommand,
    barrier: Barrier,
    outcomes: dict[str, Any],
    failures: list[tuple[str, Exception]],
    result_lock: Lock,
) -> None:
    try:
        barrier.wait()
        result = inventory.admit_task(command)
    except Exception as exc:  # noqa: BLE001 - 保留 public 并发结果供断言
        with result_lock:
            failures.append((command.command_uuid, exc))
    else:
        with result_lock:
            outcomes[command.command_uuid] = result


def test_concurrent_tasks_get_admitted_and_blocked_durable_results(
    tmp_path: Path,
) -> None:
    seeder = inventory_api.InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    try:
        seeder.create_material(
            material_uuid=MOUNT_UUID,
            resource_template_uuid=RESOURCE_TEMPLATE_UUID,
            barcode="MOUNT-106",
            name="Contended mount 106",
        )
        seeder.create_material(
            material_uuid=MATERIAL_UUID,
            resource_template_uuid=RESOURCE_TEMPLATE_UUID,
            barcode="SAMPLE-106",
            name="Contended sample 106",
        )
        seeder.create_site(
            site_uuid=SITE_UUID,
            description=None,
            meta_data={},
            material_uuid=MOUNT_UUID,
            name="A1",
            sort_order=0,
            allowed_resource_template_uuids=[RESOURCE_TEMPLATE_UUID],
            occupied_material_uuid=MATERIAL_UUID,
            position_x=0.0,
            position_y=0.0,
            position_z=0.0,
            depth=1.0,
            length=1.0,
            width=1.0,
        )
    finally:
        seeder.close()

    command_a = _admission_command(
        command_uuid=COMMAND_A_UUID,
        workflow_task_uuid=WORKFLOW_TASK_A_UUID,
        material_source_node_uuid=MATERIAL_SOURCE_NODE_A_UUID,
        fingerprint="a" * 64,
    )
    command_b = _admission_command(
        command_uuid=COMMAND_B_UUID,
        workflow_task_uuid=WORKFLOW_TASK_B_UUID,
        material_source_node_uuid=MATERIAL_SOURCE_NODE_B_UUID,
        fingerprint="b" * 64,
    )
    inventory_a = inventory_api.InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    inventory_b = inventory_api.InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    barrier = Barrier(3, timeout=10.0)
    result_lock = Lock()
    outcomes: dict[str, Any] = {}
    failures: list[tuple[str, Exception]] = []
    threads = (
        Thread(
            target=_admit_at_barrier,
            args=(inventory_a, command_a, barrier, outcomes, failures, result_lock),
        ),
        Thread(
            target=_admit_at_barrier,
            args=(inventory_b, command_b, barrier, outcomes, failures, result_lock),
        ),
    )
    try:
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=10.0)

        assert all(not thread.is_alive() for thread in threads)
        assert failures == []
        assert set(outcomes) == {COMMAND_A_UUID, COMMAND_B_UUID}

        results = tuple(outcomes.values())
        assert [result.status for result in results].count("admitted") == 1
        assert [result.status for result in results].count("blocked") == 1
        admitted = next(result for result in results if result.status == "admitted")
        blocked = next(result for result in results if result.status == "blocked")

        assert admitted.reservation_uuid
        assert len(admitted.bindings) == 1
        assert blocked.reservation_uuid is None
        assert blocked.bindings == ()
        assert tuple(item.get("code") for item in blocked.diagnostics) == (
            "material_reserved",
        )
    finally:
        inventory_a.close()
        inventory_b.close()

    reopened = inventory_api.InventoryService.open(
        working_dir=tmp_path,
        resource_templates=_resource_templates(),
    )
    try:
        assert reopened.get_command_result(COMMAND_A_UUID) == outcomes[COMMAND_A_UUID]
        assert reopened.get_command_result(COMMAND_B_UUID) == outcomes[COMMAND_B_UUID]
    finally:
        reopened.close()
