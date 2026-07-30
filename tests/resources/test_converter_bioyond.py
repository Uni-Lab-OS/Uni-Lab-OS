import json
from pathlib import Path

import pytest
from pylabrobot.resources import Resource as ResourcePLR

from unilabos.registry.registry import lab_registry
from unilabos.resources.bioyond.decks import (
    BIOYOND_PolymerPreparationStation_Deck,
    BIOYOND_PolymerReactionStation_Deck,
)
from unilabos.resources.graphio import resource_bioyond_to_plr
from unilabos.resources.itemized_carrier import BottleCarrier

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
    (
        "materials_fixture",
        "deck_type",
        "expected_names",
        "expected_models",
        "expected_parents",
        "expected_sites",
    ),
    [
        (
            "bioyond_materials_reaction",
            BIOYOND_PolymerReactionStation_Deck,
            ["ODA", "MPDA", "NMP", "PGME", "0917"],
            [
                "BIOYOND_PolymerStation_1FlaskCarrier",
                "BIOYOND_PolymerStation_1FlaskCarrier",
                "BIOYOND_PolymerStation_1BottleCarrier",
                "BIOYOND_PolymerStation_1BottleCarrier",
                "BIOYOND_PolymerStation_6StockCarrier",
            ],
            [
                "堆栈1左",
                "堆栈1左",
                "站内试剂存放堆栈",
                "站内试剂存放堆栈",
                "堆栈1左",
            ],
            [0, 1, 0, 1, 4],
        ),
        (
            "bioyond_materials_liquidhandling_1",
            BIOYOND_PolymerPreparationStation_Deck,
            ["NMP", "NMP_2", "NMP_3", "1010"],
            [
                "BIOYOND_PolymerStation_1BottleCarrier",
                "BIOYOND_PolymerStation_1BottleCarrier",
                "BIOYOND_PolymerStation_1BottleCarrier",
                "BIOYOND_PolymerStation_6StockCarrier",
            ],
            ["试剂堆栈", "试剂堆栈", "试剂堆栈", "粉末堆栈"],
            [7, 4, 5, 8],
        ),
    ],
)
def test_bioyond_to_plr(
    materials_fixture,
    deck_type,
    expected_names,
    expected_models,
    expected_parents,
    expected_sites,
    request,
    tmp_path,
) -> None:
    materials = request.getfixturevalue(materials_fixture)
    deck = deck_type("test_deck", setup=True)
    output = resource_bioyond_to_plr(materials, type_mapping=type_mapping, deck=deck)

    assert len(output) == len(materials)
    assert all(isinstance(resource, ResourcePLR) for resource in output)
    assert all(isinstance(resource, BottleCarrier) for resource in output)
    assert [resource.name for resource in output] == expected_names
    assert [resource.model for resource in output] == expected_models

    for resource, material, parent_name, site in zip(
        output,
        materials,
        expected_parents,
        expected_sites,
        strict=True,
    ):
        assert resource.unilabos_extra["material_bioyond_id"] == material["id"]
        assert resource.unilabos_extra["material_bioyond_name"] == material["name"]
        assert resource.parent.name == parent_name
        assert resource.parent.sites[site] is resource

    with (tmp_path / "test.json").open("w", encoding="utf-8") as f:
        json.dump(deck.serialize(), f, indent=4)
