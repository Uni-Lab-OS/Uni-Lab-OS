"""边缘调度器（EdgeScheduler）按仓库与库位范围自动分配物料合同。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.domain import (
    CommandRejected,
    InsufficientStock,
    MaterialRequirement,
    MaterialSourceAdmissionRequest,
)
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore

SITE_A = "10000000-0000-4000-8000-000000000001"
SITE_B = "10000000-0000-4000-8000-000000000002"
SITE_EMPTY = "10000000-0000-4000-8000-000000000003"
TARGET_SITE = "10000000-0000-4000-8000-000000000004"


@pytest.fixture()
def inventory(tmp_path: Path) -> tuple[InventoryStore, InventoryService, dict[str, str]]:
    """建立同一仓库下有序库位与两件兼容物料（Material）的库存事实。"""

    store = InventoryStore(str(tmp_path / "inventory.db"))
    backend = BackendResourceService(store)
    templates = backend.sync_resource_templates(
        [
            {
                "id": "test.warehouse",
                "display_name": "测试仓库",
                "registry_type": "resource",
                "class": {},
            },
            {
                "id": "test.plate",
                "display_name": "测试孔板",
                "registry_type": "material",
                "class": {},
            },
        ]
    )["templates"]
    template_by_name = {item["name"]: item["uuid"] for item in templates}
    mount = backend.create_material(
        {
            "resource_template_uuid": template_by_name["test.warehouse"],
            "barcode": "WAREHOUSE-1",
            "name": "一号仓库",
        }
    )
    first = backend.create_material(
        {
            "resource_template_uuid": template_by_name["test.plate"],
            "parent_uuid": mount["uuid"],
            "barcode": "PLATE-A",
            "name": "孔板 A",
        }
    )
    second = backend.create_material(
        {
            "resource_template_uuid": template_by_name["test.plate"],
            "parent_uuid": mount["uuid"],
            "barcode": "PLATE-B",
            "name": "孔板 B",
        }
    )
    with store.transaction() as connection:
        for site_uuid, name, order, occupant in (
            (SITE_A, "A1", 10, first["uuid"]),
            (SITE_B, "B1", 5, second["uuid"]),
            (SITE_EMPTY, "C1", 0, None),
        ):
            connection.execute(
                """
                INSERT INTO site(
                    uuid,create_time,update_time,meta_data,material_uuid,name,
                    sort_order,allowed_resource_template_uuids,
                    occupied_material_uuid,position_x,position_y,position_z,
                    depth,length,width
                ) VALUES (?,?,?,'{}',?,?,?,?,?,0,0,0,0,0,0)
                """,
                (
                    site_uuid,
                    "2026-08-06T00:00:00Z",
                    "2026-08-06T00:00:00Z",
                    mount["uuid"],
                    name,
                    order,
                    json.dumps([template_by_name["test.plate"]]),
                    occupant,
                ),
            )
    try:
        yield store, InventoryService(store), {
            "mount": mount["uuid"],
            "template": template_by_name["test.plate"],
            "first": first["uuid"],
            "second": second["uuid"],
        }
    finally:
        store.close()


def test_exact_site_selects_and_reserves_its_occupant_atomically(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """精确库位（Site）应返回并占用该位置的兼容物料（Material）。"""

    store, service, identities = inventory
    result = service.reserve_workflow(
        "workflow-exact",
        {
            "source": [
                MaterialRequirement(
                    template_id=identities["template"],
                    mount_uuid=identities["mount"],
                    site_uuid=SITE_A,
                )
            ]
        },
    )

    assert result["allocations"] == {"source": [identities["first"]]}
    assert store.get_instance(identities["first"])["status"] == "reserved"
    assert store.get_instance(identities["second"])["status"] == "warehouse"


def test_slot_range_uses_site_order_and_whole_set_failure_rolls_back(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """库位范围按位置顺序选取；同批另一来源不足时整组零占用。"""

    store, service, identities = inventory
    with pytest.raises(InsufficientStock):
        service.reserve_workflow(
            "workflow-range",
            {
                "available": [
                    MaterialRequirement(
                        template_id=identities["template"],
                        mount_uuid=identities["mount"],
                        slot_uuids=[SITE_A, SITE_B],
                    )
                ],
                "empty": [
                    MaterialRequirement(
                        template_id=identities["template"],
                        mount_uuid=identities["mount"],
                        site_uuid=SITE_EMPTY,
                    )
                ],
            },
        )

    assert store.get_instance(identities["first"])["status"] == "warehouse"
    assert store.get_instance(identities["second"])["status"] == "warehouse"
    assert store.reservations_for_workflow("workflow-range") == []


def test_slot_range_selects_lowest_site_order_deterministically(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """库位范围不依赖调用方数组顺序，始终选择 ``sort_order`` 最小的位置。"""

    store, service, identities = inventory
    result = service.reserve_workflow(
        "workflow-range-success",
        {
            "source": [
                MaterialRequirement(
                    template_id=identities["template"],
                    mount_uuid=identities["mount"],
                    slot_uuids=[SITE_A, SITE_B],
                )
            ]
        },
    )

    assert result["allocations"] == {"source": [identities["second"]]}
    replay = service.reserve_workflow(
        "workflow-range-success",
        {
            "source": [
                MaterialRequirement(
                    template_id=identities["template"],
                    mount_uuid=identities["mount"],
                    slot_uuids=[SITE_A, SITE_B],
                )
            ]
        },
    )
    assert replay["allocations"] == result["allocations"]
    assert store.get_instance(identities["second"])["status"] == "reserved"


def test_shared_source_admits_two_tasks_without_task_reservation(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """两个任务应能冻结绑定同一共享试剂而不互相阻塞。

    参数：``inventory`` 提供同一库位（Site）中的候选物料与
    库存权威（Inventory Authority）。返回：无；通过
    ``admit_material_sources`` 公共接缝断言两个工作流任务
    （WorkflowTask）取得同一稳定绑定，且都不创建任务物料预留
    （TaskMaterialReservation）。
    """

    store, service, identities = inventory
    # ``source_request`` 是两个任务共用的固定位置试剂准入意图。
    source_request = MaterialSourceAdmissionRequest(
        node_id="reagent-source",
        resource_template_uuid=identities["template"],
        custody_policy="shared_source",
        requirement=MaterialRequirement(
            template_id=identities["template"],
            mount_uuid=identities["mount"],
            site_uuid=SITE_A,
        ),
    )

    first = service.admit_material_sources("workflow-shared-a", [source_request])
    second = service.admit_material_sources("workflow-shared-b", [source_request])

    expected_allocations = {"reagent-source": [identities["first"]]}
    assert first["allocations"] == expected_allocations
    assert second["allocations"] == expected_allocations
    assert first["reserved_nodes"] == []
    assert second["reserved_nodes"] == []
    assert store.get_instance(identities["first"])["status"] == "warehouse"
    bindings = store.query_all(
        "SELECT workflow_id,material_uuid,custody_policy,status "
        "FROM inventory_material_source_binding ORDER BY workflow_id"
    )
    assert bindings == [
        {
            "workflow_id": "workflow-shared-a",
            "material_uuid": identities["first"],
            "custody_policy": "shared_source",
            "status": "active",
        },
        {
            "workflow_id": "workflow-shared-b",
            "material_uuid": identities["first"],
            "custody_policy": "shared_source",
            "status": "active",
        },
    ]


def test_task_exclusive_source_blocks_second_task_until_release(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """任务全程独占来源必须继续阻止第二个任务占用同一试剂。

    参数：``inventory`` 提供固定库位中的单件试剂。返回：无；断言第一个任务
    创建库存预留并把实例置为 ``reserved``，第二个任务的同一来源准入失败。
    异常：库存不足必须以 ``InsufficientStock`` 失败关闭。
    """

    store, service, identities = inventory
    # ``exclusive_request`` 明确选择任务全程持有，而不是共享来源动作锁。
    exclusive_request = MaterialSourceAdmissionRequest(
        node_id="reagent-source",
        resource_template_uuid=identities["template"],
        custody_policy="task_exclusive",
        requirement=MaterialRequirement(
            template_id=identities["template"],
            mount_uuid=identities["mount"],
            site_uuid=SITE_A,
        ),
    )

    first = service.admit_material_sources("workflow-exclusive-a", [exclusive_request])
    replay = service.admit_material_sources("workflow-exclusive-a", [exclusive_request])

    assert first["reserved_nodes"] == ["reagent-source"]
    assert replay["allocations"] == first["allocations"]
    assert replay["reserved_nodes"] == []
    assert store.get_instance(identities["first"])["status"] == "reserved"
    with pytest.raises(InsufficientStock):
        service.admit_material_sources("workflow-exclusive-b", [exclusive_request])


def test_material_source_admission_rolls_back_whole_request_set(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """任一物料来源不可用时必须回滚同任务的全部新绑定。

    参数：``inventory`` 提供一个有物料库位和一个空库位。返回：无；断言先选中
    的共享来源不会在后一来源失败后残留持久绑定。异常：空库位以
    ``InsufficientStock`` 终止整组准入。
    """

    store, service, identities = inventory
    requests = [
        MaterialSourceAdmissionRequest(
            node_id="a-shared-source",
            resource_template_uuid=identities["template"],
            custody_policy="shared_source",
            requirement=MaterialRequirement(
                template_id=identities["template"],
                mount_uuid=identities["mount"],
                site_uuid=SITE_A,
            ),
        ),
        MaterialSourceAdmissionRequest(
            node_id="z-missing-source",
            resource_template_uuid=identities["template"],
            custody_policy="task_exclusive",
            requirement=MaterialRequirement(
                template_id=identities["template"],
                mount_uuid=identities["mount"],
                site_uuid=SITE_EMPTY,
            ),
        ),
    ]

    with pytest.raises(InsufficientStock):
        service.admit_material_sources("workflow-atomic", requests)

    assert store.query_all(
        "SELECT * FROM inventory_material_source_binding WHERE workflow_id=?",
        ("workflow-atomic",),
    ) == []
    assert store.reservations_for_workflow("workflow-atomic") == []


def test_material_source_binding_replays_after_service_restart_and_rejects_change(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """持久绑定必须跨服务重建重放，并拒绝同尝试偷换选择器。

    参数：``inventory`` 提供同一个 SQLite 库及库存身份。返回：无；断言新建
    ``InventoryService`` 后仍返回首次选择的物料。异常：同一任务尝试修改保管
    策略时抛 ``CommandRejected``，避免重放漂移。
    """

    store, service, identities = inventory
    shared_request = MaterialSourceAdmissionRequest(
        node_id="reagent-source",
        resource_template_uuid=identities["template"],
        custody_policy="shared_source",
        requirement=MaterialRequirement(
            template_id=identities["template"],
            mount_uuid=identities["mount"],
            site_uuid=SITE_A,
        ),
    )
    first = service.admit_material_sources("workflow-replay", [shared_request])

    restarted_service = InventoryService(store)
    replay = restarted_service.admit_material_sources(
        "workflow-replay", [shared_request]
    )

    assert replay["allocations"] == first["allocations"]
    changed_request = MaterialSourceAdmissionRequest(
        node_id="reagent-source",
        resource_template_uuid=identities["template"],
        custody_policy="task_exclusive",
        requirement=shared_request.requirement,
    )
    with pytest.raises(CommandRejected):
        restarted_service.admit_material_sources(
            "workflow-replay", [changed_request]
        )
    released = restarted_service.release_workflow(
        "workflow-replay",
        reason="workflow_succeeded",
    )
    second_attempt = restarted_service.admit_material_sources(
        "workflow-replay",
        [shared_request],
        attempt=2,
    )

    assert released["released_bindings"] == ["reagent-source"]
    assert second_attempt["allocations"] == first["allocations"]


def test_fixed_material_source_rejects_instance_from_another_template(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """固定物料身份必须仍满足来源节点声明的资源模板。

    参数：``inventory`` 提供一个真实孔板实例。返回：无；断言伪造的另一模板
    选择器不能把该实例冻结为共享绑定。异常：模板与实例事实不一致时库存权威
    抛 ``CommandRejected``，且不留下绑定。
    """

    store, service, identities = inventory
    wrong_template_uuid = "90000000-0000-4000-8000-000000000099"
    request = MaterialSourceAdmissionRequest(
        node_id="reagent-source",
        resource_template_uuid=wrong_template_uuid,
        custody_policy="shared_source",
        requirement=MaterialRequirement(
            template_id=wrong_template_uuid,
            instance_uuid=identities["first"],
        ),
    )

    with pytest.raises(CommandRejected):
        service.admit_material_sources("workflow-wrong-template", [request])

    assert store.query_all(
        "SELECT * FROM inventory_material_source_binding WHERE workflow_id=?",
        ("workflow-wrong-template",),
    ) == []


def test_move_instance_commits_parent_and_site_occupancy_atomically(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """系统转运必须同时清空来源库位并占用目标库位（Site）。

    参数：``inventory`` 提供共享资源、库存实例及来源库位事实。返回：无；断言
    ``move_instance`` 在一个事务中更新物料父级、来源/目标库位占用及可同步账本，
    从而为主机转运动作提供正式库存提交入口。异常：目标身份或库存结构非法时由
    库存服务失败关闭。
    """

    store, service, identities = inventory
    backend = BackendResourceService(store)
    target = backend.create_material(
        {
            "resource_template_uuid": backend.get_material(identities["mount"])[
                "resource_template_uuid"
            ],
            "barcode": "WAREHOUSE-2",
            "name": "二号仓库",
        }
    )
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO site(
                uuid,create_time,update_time,meta_data,material_uuid,name,
                sort_order,allowed_resource_template_uuids,
                occupied_material_uuid,position_x,position_y,position_z,
                depth,length,width
            ) VALUES (?,?,?,'{}',?,?,?,?,NULL,0,0,0,0,0,0)
            """,
            (
                TARGET_SITE,
                "2026-08-06T00:00:00Z",
                "2026-08-06T00:00:00Z",
                target["uuid"],
                "A1",
                0,
                json.dumps([identities["template"]]),
            ),
        )

    moved = service.move_instance(
        identities["first"],
        parent_uuid=target["uuid"],
        slot_id="A1",
        actor="host_node.transfer_resource",
    )

    assert moved["parent_uuid"] == target["uuid"]
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?", (SITE_A,)
    )["occupied_material_uuid"] is None
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?", (TARGET_SITE,)
    )["occupied_material_uuid"] == identities["first"]
    ledger = store.query_one(
        "SELECT op_type,delta_json,actor FROM inventory_ledger "
        "WHERE aggregate_id=? ORDER BY ledger_id DESC LIMIT 1",
        (identities["first"],),
    )
    assert ledger["op_type"] == "instance.moved"
    assert ledger["actor"] == "host_node.transfer_resource"
    assert json.loads(ledger["delta_json"])["to_slot"] == "A1"
