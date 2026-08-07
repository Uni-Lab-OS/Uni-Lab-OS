"""验证 OS 本地库存（Inventory）的旧 HostNode 物料查询兼容投影。"""

from __future__ import annotations

import json
from copy import deepcopy

from fastapi.testclient import TestClient

from unilabos.app.scheduler.inventory.api import create_app
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.resources.plr_contract import SITE_NAME_BY_UUID_EXTRA_KEY
from unilabos.resources.resource_tracker import (
    DeviceNodeResourceTracker,
    ResourceDict,
    ResourceTreeSet,
)
from unilabos.ros.nodes.resource_slot_hydration import resolve_resource_slot_target

# ``ACTIVE_SITE_UUID`` 是父物料当前有效库位（Site）的稳定身份。
ACTIVE_SITE_UUID = "61000000-0000-4000-8000-000000000001"
# ``DELETED_SITE_UUID`` 是已软删除、不得进入驱动映射的库位身份。
DELETED_SITE_UUID = "61000000-0000-4000-8000-000000000002"
# ``FOREIGN_SITE_UUID`` 属于目标子物料，用来证明映射不能跨父物料泄漏。
FOREIGN_SITE_UUID = "61000000-0000-4000-8000-000000000003"


def _service(
    *,
    template_site_mapping: dict[str, str] | None = None,
) -> InventoryService:
    """建立含父子物料和旧 relation 库位关系的内存库存服务。

    参数说明：``template_site_mapping`` 可向子物料模板注入不可信同名映射，供
    测试只读库存投影是否覆盖它。返回：可通过公共 HTTP 兼容接口查询的
    ``InventoryService``；初始化或数据库异常原样传播。
    """

    # ``tube_extra`` 是模板层不可信扩展，不能成为父物料库位（Site）事实。
    tube_extra = (
        {SITE_NAME_BY_UUID_EXTRA_KEY: template_site_mapping}
        if template_site_mapping is not None
        else {}
    )
    service = InventoryService(InventoryStore(":memory:"))
    service.upsert_template(
        "tpl-rack",
        name="Rack template",
        category="container",
        spec={
            "storage_class": "ambient",
            "resource": {
                "id": "rack-logical-id",
                "name": "rack-a",
                "type": "container",
                "class": "",
                "config": {"size_x": 120, "size_y": 80, "size_z": 20},
                "data": {"template_state": True},
                "extra": {"fixture": "rack"},
            },
        },
    )
    service.upsert_template(
        "tpl-tube",
        name="Tube template",
        category="container",
        spec={
            "resource_dict": {
                "id": "tube-logical-id",
                "name": "tube-a1",
                "type": "container",
                "class": "",
                "config": {},
                "data": {"max_volume": 2.0},
                "extra": tube_extra,
            }
        },
    )
    service.register_instance(
        template_id="tpl-rack",
        edge_uuid="edge-rack",
        legacy_cloud_id="cloud-rack",
        barcode="RACK-001",
    )
    service.register_instance(
        template_id="tpl-tube",
        edge_uuid="edge-tube",
        barcode="TUBE-001",
        parent_uuid="edge-rack",
        slot_id="A1",
    )
    service.update_content(
        "edge-tube",
        {
            "data": {"temperature_c": 4},
            "liquids": [["water", 1.5]],
            "liquid_history": [["water", 1.5]],
            "unknown_counter": 2,
            "substance": "water",
        },
    )
    return service


def test_legacy_query_returns_flat_resource_dict_tree() -> None:
    client = TestClient(create_app(_service()))

    response = client.post(
        "/api/v1/edge/material/query",
        json={"uuids": ["edge-rack"], "with_children": True},
    )

    assert response.status_code == 200
    assert response.json()["code"] == 0
    nodes = response.json()["data"]["nodes"]
    assert [node["uuid"] for node in nodes] == ["edge-rack", "edge-tube"]
    assert nodes[1]["parent_uuid"] == "edge-rack"
    assert nodes[1]["extra"]["update_resource_site"] == "A1"
    assert nodes[1]["extra"]["edge_inventory"]["template_id"] == "tpl-tube"
    assert nodes[1]["data"] == {
        "max_volume": 2.0,
        "temperature_c": 4,
        "substance": "water",
    }
    assert nodes[1]["liquids"] == [["water", 1.5]]
    assert nodes[1]["liquid_history"] == [["water", 1.5]]
    assert nodes[1]["unknown_counter"] == 2

    for node in nodes:
        ResourceDict.model_validate(node)
    tree_set = ResourceTreeSet.from_raw_dict_list(deepcopy(nodes))
    assert len(tree_set.trees) == 1
    assert tree_set.trees[0].root_node.res_content.uuid == "edge-rack"
    assert tree_set.trees[0].root_node.children[0].res_content.uuid == "edge-tube"


def test_canonical_generic_resource_instance_remains_plr_convertible() -> None:
    """规范物料字段不得在旧 HostNode 查询边界降级或丢失。"""

    service = InventoryService(InventoryStore(":memory:"))
    service.upsert_template(
        "tpl-beaker",
        name="SZLab 500 mL 烧杯",
        category="resource",
        spec={"format": "xacro", "entry": "beaker/resource.xacro"},
    )
    service.register_instance(
        template_id="tpl-beaker",
        edge_uuid="edge-beaker",
    )
    with service.store.transaction() as connection:
        connection.execute(
            "UPDATE material SET description=?,class=?,name=?,config=?,data=? "
            "WHERE uuid=?",
            (
                "S03/S11 使用的 500 mL 烧杯",
                "community.szlab.beaker_500ml",
                "烧杯堆栈 L1B1 烧杯",
                json.dumps(
                    {
                        "size_x": 86,
                        "size_y": 86,
                        "size_z": 120,
                        "category": "beaker",
                        "num_items_x": 6,
                    }
                ),
                json.dumps({"sample_id": "sample-001"}),
                "edge-beaker",
            ),
        )

    nodes = TestClient(create_app(service)).post(
        "/api/v1/edge/material/query",
        json={"uuids": ["edge-beaker"], "with_children": True},
    ).json()["data"]["nodes"]
    resource = ResourceTreeSet.from_raw_dict_list(deepcopy(nodes)).to_plr_resources()[0]

    assert nodes[0]["name"] == "烧杯堆栈 L1B1 烧杯"
    assert nodes[0]["description"] == "S03/S11 使用的 500 mL 烧杯"
    assert nodes[0]["class"] == "community.szlab.beaker_500ml"
    assert nodes[0]["config"]["category"] == "beaker"
    assert nodes[0]["config"]["num_items_x"] == 6
    assert nodes[0]["data"]["sample_id"] == "sample-001"
    assert resource.name == "烧杯堆栈 L1B1 烧杯"
    assert resource.category == "beaker"


def test_query_supports_legacy_cloud_uuid_id_and_without_children() -> None:
    client = TestClient(create_app(_service()))

    by_cloud_uuid = client.post(
        "/api/v1/edge/material/query",
        json={"uuids": ["cloud-rack"], "with_children": False},
    ).json()["data"]["nodes"]
    by_logical_id = client.post(
        "/api/v1/edge/material/query",
        json={"id": "tube-logical-id", "with_children": True},
    ).json()["data"]["nodes"]

    assert [node["uuid"] for node in by_cloud_uuid] == ["edge-rack"]
    assert [node["uuid"] for node in by_logical_id] == ["edge-tube"]


def test_query_deduplicates_overlapping_roots_and_validates_selector() -> None:
    client = TestClient(create_app(_service()))

    response = client.post(
        "/api/v1/edge/material/query",
        json={"uuids": ["edge-rack", "edge-tube"], "with_children": True},
    )

    assert [node["uuid"] for node in response.json()["data"]["nodes"]] == [
        "edge-rack",
        "edge-tube",
    ]
    assert (
        client.post(
            "/api/v1/edge/material/query",
            json={"uuids": [], "with_children": True},
        ).status_code
        == 422
    )


def test_parent_site_name_mapping_survives_real_plr_target_resolution() -> None:
    """父物料库位名称映射必须穿过真实物理位置资源（PLR）转换并保留目标物料语义。

    参数：无。返回：无；断言只读映射只含父物料的有效库位（Site），排除已
    删除和其他物料的库位，并确认物料占位符（ResourceSlot）解析仍返回目标
    子物料而不是父物料。
    """

    service = _service()
    # ``site_rows`` 同时提供有效、已删除和其他父物料三种库位事实。
    site_rows = (
        (ACTIVE_SITE_UUID, None, "L1B1", "edge-rack"),
        (
            DELETED_SITE_UUID,
            "2026-08-07T00:00:00Z",
            "L1B2",
            "edge-rack",
        ),
        (FOREIGN_SITE_UUID, None, "Tube Local", "edge-tube"),
    )
    with service.store.transaction() as connection:
        # 关闭旧 relation 兼容触发器自动创建的随机 UUID 库位，避免预期依赖随机身份。
        connection.execute(
            "UPDATE site SET deleted_at=? WHERE material_uuid=?",
            ("2026-08-07T00:00:00Z", "edge-rack"),
        )
        # 循环变量依次表示稳定库位 UUID、软删除时间、设备局部名称和所属父物料 UUID。
        for site_uuid, deleted_at, local_name, owner_material_uuid in site_rows:
            connection.execute(
                """
                INSERT INTO site(
                    uuid,create_time,update_time,deleted_at,meta_data,
                    material_uuid,name,sort_order,allowed_resource_template_uuids,
                    occupied_material_uuid,position_x,position_y,position_z,
                    depth,length,width
                ) VALUES (?,?,?,?,'{}',?,?,0,'[]',NULL,0,0,0,0,0,0)
                """,
                (
                    site_uuid,
                    "2026-08-07T00:00:00Z",
                    "2026-08-07T00:00:00Z",
                    deleted_at,
                    owner_material_uuid,
                    local_name,
                ),
            )

    nodes = TestClient(create_app(service)).post(
        "/api/v1/edge/material/query",
        json={"uuids": ["edge-rack"], "with_children": True},
    ).json()["data"]["nodes"]
    # ``parent_material`` 是资源树集合（ResourceTreeSet）转为物理位置资源（PLR）的父物料实例。
    parent_material = ResourceTreeSet.from_raw_dict_list(
        deepcopy(nodes)
    ).to_plr_resources()[0]
    # ``target_material`` 是普通动作最终应收到的原目标子物料。
    target_material = resolve_resource_slot_target(
        "edge-tube",
        source_root=parent_material,
        resolved_root=parent_material,
        resource_tracker=DeviceNodeResourceTracker(),
    )

    assert parent_material.unilabos_extra[SITE_NAME_BY_UUID_EXTRA_KEY] == {
        ACTIVE_SITE_UUID: "L1B1"
    }
    assert target_material.unilabos_uuid == "edge-tube"
    assert target_material.parent is parent_material


def test_empty_site_mapping_overrides_untrusted_template_projection() -> None:
    """无有效库位时必须发布空映射并覆盖模板伪造值。

    参数：无。返回：无；断言物料兼容投影和真实物理位置资源（PLR）对象都
    显式携带空映射，模板中的同名扩展不能伪造库存权威（Inventory Authority）
    的库位（Site）身份或设备局部名称。
    """

    # ``forged_mapping`` 模拟包模板试图注入不属于库存事实的库位映射。
    forged_mapping = {
        "62000000-0000-4000-8000-000000000001": "Forged Local Name"
    }
    service = _service(template_site_mapping=forged_mapping)
    nodes = TestClient(create_app(service)).post(
        "/api/v1/edge/material/query",
        json={"uuids": ["edge-tube"], "with_children": True},
    ).json()["data"]["nodes"]
    # ``target_material`` 是没有直接拥有有效库位的目标子物料。
    target_material = ResourceTreeSet.from_raw_dict_list(
        deepcopy(nodes)
    ).to_plr_resources()[0]

    assert nodes[0]["extra"][SITE_NAME_BY_UUID_EXTRA_KEY] == {}
    assert target_material.unilabos_extra[SITE_NAME_BY_UUID_EXTRA_KEY] == {}
