import pytest
import json
from pathlib import Path

from unilabos.resources.graphio import resource_bioyond_to_plr
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.registry.registry import lab_registry

from unilabos.resources.bioyond.decks import BIOYOND_PolymerReactionStation_Deck

lab_registry.setup()

FIXTURE_DIR = Path(__file__).parent


type_mapping = {
    "烧杯": ("BIOYOND_PolymerStation_1FlaskCarrier", "3a14196b-24f2-ca49-9081-0cab8021bf1a"),
    "试剂瓶": ("BIOYOND_PolymerStation_1BottleCarrier", ""),
    "样品板": ("BIOYOND_PolymerStation_6StockCarrier", "3a14196e-b7a0-a5da-1931-35f3000281e9"),
    "分装板": ("YB_6VialCarrier", "3a14196e-5dfe-6e21-0c79-fe2036d052c4"),
    "样品瓶": ("BIOYOND_PolymerStation_Solid_Stock", "3a14196a-cf7d-8aea-48d8-b9662c7dba94"),
    "90%分装小瓶": ("BIOYOND_PolymerStation_Solid_Vial", "3a14196c-cdcf-088d-dc7d-5cf38f0ad9ea"),
    "10%分装小瓶": ("BIOYOND_PolymerStation_Liquid_Vial", "3a14196c-76be-2279-4e22-7310d69aed68"),
}


@pytest.fixture
def bioyond_materials_reaction() -> list[dict]:
    with (FIXTURE_DIR / "bioyond_materials_reaction.json").open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data


@pytest.fixture
def bioyond_materials_liquidhandling_1() -> list[dict]:
    with (FIXTURE_DIR / "bioyond_materials_liquidhandling_1.json").open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data


@pytest.fixture
def bioyond_materials_liquidhandling_2() -> list[dict]:
    with (FIXTURE_DIR / "bioyond_materials_liquidhandling_2.json").open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data


@pytest.mark.parametrize("materials_fixture", [
    "bioyond_materials_reaction",
    "bioyond_materials_liquidhandling_1",
])
def test_resourcetreeset_from_plr(materials_fixture, request) -> list[dict]:
    materials = request.getfixturevalue(materials_fixture)
    deck = BIOYOND_PolymerReactionStation_Deck("test_deck")
    output = resource_bioyond_to_plr(materials, type_mapping=type_mapping, deck=deck)
    tree_set = ResourceTreeSet.from_plr_resources(output)
    dumped = tree_set.dump()
    assert len(dumped) == len(output)
    assert {tree[0]["name"] for tree in dumped} == {resource.name for resource in output}


def test_merge_disjoint_trees_by_id_keeps_authoritative_runtime_identity() -> None:
    """Edge 重报相同物理图时不复制节点，只接纳真正的新树。"""

    authoritative = ResourceTreeSet.from_raw_dict_list(
        [
            {
                "id": "device-a",
                "uuid": "10000000-0000-4000-8000-000000000001",
                "name": "Authoring Device A",
                "type": "device",
                "class": "community.device_a",
            }
        ]
    )
    reported = ResourceTreeSet.from_raw_dict_list(
        [
            {
                "id": "device-a",
                "uuid": "20000000-0000-4000-8000-000000000001",
                "name": "Edge Device A",
                "type": "device",
                "class": "community.device_a",
            },
            {
                "id": "device-b",
                "uuid": "20000000-0000-4000-8000-000000000002",
                "name": "Edge Device B",
                "type": "device",
                "class": "community.device_b",
            },
        ]
    )

    added, conflicts = authoritative.merge_disjoint_trees_by_id(reported)

    assert added == 1
    assert conflicts == ("device-a",)
    assert [node.res_content.id for node in authoritative.all_nodes] == [
        "device-a",
        "device-b",
    ]
    assert authoritative.all_nodes[0].res_content.name == "Authoring Device A"


def test_device_nodes_include_nested_devices_and_exclude_materials() -> None:
    """设备节点视图必须包含挂载在地轨下的机械臂。"""

    tree_set = ResourceTreeSet.from_raw_dict_list(
        [
            {
                "id": "rail",
                "uuid": "30000000-0000-4000-8000-000000000001",
                "name": "Rail",
                "type": "device",
                "class": "community.rail",
            },
            {
                "id": "robot",
                "uuid": "30000000-0000-4000-8000-000000000002",
                "parent_uuid": "30000000-0000-4000-8000-000000000001",
                "name": "Robot",
                "type": "device",
                "class": "community.robot",
            },
            {
                "id": "tool",
                "uuid": "30000000-0000-4000-8000-000000000003",
                "parent_uuid": "30000000-0000-4000-8000-000000000002",
                "name": "Tool",
                "type": "tool",
                "class": "community.tool",
            },
        ]
    )

    assert [node.res_content.id for node in tree_set.device_nodes] == [
        "rail",
        "robot",
    ]
