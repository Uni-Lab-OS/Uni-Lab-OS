import json
from pathlib import Path

import pytest

from unilabos.registry.registry import lab_registry
from unilabos.resources.bioyond.decks import (
    BIOYOND_PolymerPreparationStation_Deck,
    BIOYOND_PolymerReactionStation_Deck,
)
from unilabos.resources.graphio import resource_bioyond_to_plr
from unilabos.resources.resource_tracker import ResourceTreeSet

lab_registry.setup()

FIXTURE_DIR = Path(__file__).parent


type_mapping = {
    "烧杯": ("BIOYOND_PolymerStation_1FlaskCarrier", "3a14196b-24f2-ca49-9081-0cab8021bf1a"),
    "试剂瓶": ("BIOYOND_PolymerStation_1BottleCarrier", ""),
    "样品板": ("BIOYOND_PolymerStation_6StockCarrier", "3a14196e-b7a0-a5da-1931-35f3000281e9"),
    "分装板": ("BIOYOND_PolymerStation_6VialCarrier", "3a14196e-5dfe-6e21-0c79-fe2036d052c4"),
    "样品瓶": ("BIOYOND_PolymerStation_Solid_Stock", "3a14196a-cf7d-8aea-48d8-b9662c7dba94"),
    "90%分装小瓶": ("BIOYOND_PolymerStation_Solid_Vial", "3a14196c-cdcf-088d-dc7d-5cf38f0ad9ea"),
    "10%分装小瓶": ("BIOYOND_PolymerStation_Liquid_Vial", "3a14196c-76be-2279-4e22-7310d69aed68"),
}


@pytest.fixture
def bioyond_materials_reaction() -> list[dict]:
    with (FIXTURE_DIR / "bioyond_materials_reaction.json").open(encoding="utf-8") as f:
        data = json.load(f)
    return data


@pytest.fixture
def bioyond_materials_liquidhandling_1() -> list[dict]:
    with (FIXTURE_DIR / "bioyond_materials_liquidhandling_1.json").open(encoding="utf-8") as f:
        data = json.load(f)
    return data


@pytest.mark.parametrize(
    ("materials_fixture", "deck_type", "expected_parents"),
    [
        (
            "bioyond_materials_reaction",
            BIOYOND_PolymerReactionStation_Deck,
            {
                "ODA": "堆栈1左",
                "MPDA": "堆栈1左",
                "NMP": "站内试剂存放堆栈",
                "PGME": "站内试剂存放堆栈",
                "0917": "堆栈1左",
            },
        ),
        (
            "bioyond_materials_liquidhandling_1",
            BIOYOND_PolymerPreparationStation_Deck,
            {
                "NMP": "试剂堆栈",
                "NMP_2": "试剂堆栈",
                "NMP_3": "试剂堆栈",
                "1010": "粉末堆栈",
            },
        ),
    ],
)
def test_resourcetreeset_from_plr(
    materials_fixture,
    deck_type,
    expected_parents,
    request,
) -> None:
    materials = request.getfixturevalue(materials_fixture)
    deck = deck_type("test_deck", setup=True)
    converted = resource_bioyond_to_plr(
        materials,
        type_mapping=type_mapping,
        deck=deck,
    )

    resource_trees = ResourceTreeSet.from_plr_resources([deck])
    dumped_trees = resource_trees.dump()

    assert len(converted) == len(materials)
    assert len(dumped_trees) == 1
    assert dumped_trees[0][0]["name"] == deck.name

    nodes_by_name = {node["name"]: node for node in dumped_trees[0]}
    nodes_by_uuid = {node["uuid"]: node for node in dumped_trees[0]}
    for material_name, parent_name in expected_parents.items():
        material_node = nodes_by_name[material_name]
        assert material_node["type"] == "bottle_carrier"
        assert nodes_by_uuid[material_node["parent_uuid"]]["name"] == parent_name
