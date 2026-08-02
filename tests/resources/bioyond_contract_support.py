"""BioYond 转换合同的隔离测试支持。

本模块由测试通过独立 Python 子进程执行，避免 Registry singleton 和它持有的
ThreadPoolExecutor 泄漏到 pytest 主进程或其他测试。
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

FIXTURE_DIR = Path(__file__).parent
REPOSITORY_ROOT = FIXTURE_DIR.parents[1]


@dataclass(frozen=True)
class BioyondCase:
    fixture_name: str
    deck_kind: Literal["reaction", "preparation"]
    type_mapping: dict[str, tuple[str, str]]
    expected_names: tuple[str, ...]
    expected_models: tuple[str, ...]
    expected_parents: tuple[str, ...]
    expected_sites: tuple[int, ...]
    expected_detail_names: dict[str, tuple[str, ...]]


COMMON_TYPE_MAPPING = {
    "BIOYOND_PolymerStation_1FlaskCarrier": (
        "烧杯",
        "3a14196b-24f2-ca49-9081-0cab8021bf1a",
    ),
    "BIOYOND_PolymerStation_1BottleCarrier": ("试剂瓶", ""),
    "BIOYOND_PolymerStation_Solid_Stock": (
        "样品瓶",
        "3a14196a-cf7d-8aea-48d8-b9662c7dba94",
    ),
    "BIOYOND_PolymerStation_Solid_Vial": (
        "90%分装小瓶",
        "3a14196c-cdcf-088d-dc7d-5cf38f0ad9ea",
    ),
    "BIOYOND_PolymerStation_Liquid_Vial": (
        "10%分装小瓶",
        "3a14196c-76be-2279-4e22-7310d69aed68",
    ),
}

CASES = {
    "reaction": BioyondCase(
        fixture_name="bioyond_materials_reaction.json",
        deck_kind="reaction",
        type_mapping={
            **COMMON_TYPE_MAPPING,
            "BIOYOND_PolymerStation_6StockCarrier": (
                "样品板",
                "3a14196e-b7a0-a5da-1931-35f3000281e9",
            ),
        },
        expected_names=("ODA", "MPDA", "NMP", "PGME", "0917"),
        expected_models=(
            "BIOYOND_PolymerStation_1FlaskCarrier",
            "BIOYOND_PolymerStation_1FlaskCarrier",
            "BIOYOND_PolymerStation_1BottleCarrier",
            "BIOYOND_PolymerStation_1BottleCarrier",
            "BIOYOND_PolymerStation_6StockCarrier",
        ),
        expected_parents=(
            "堆栈1左",
            "堆栈1左",
            "站内试剂存放堆栈",
            "站内试剂存放堆栈",
            "堆栈1左",
        ),
        expected_sites=(0, 1, 0, 1, 4),
        expected_detail_names={
            "0917": (
                "SIDA_2",
                "BTDA-2_3",
                "BTDA-DD_0",
                "BTDA-3_5",
                "BTDA-1_1",
            ),
        },
    ),
    "preparation": BioyondCase(
        fixture_name="bioyond_materials_liquidhandling_1.json",
        deck_kind="preparation",
        type_mapping={
            **COMMON_TYPE_MAPPING,
            "BIOYOND_PolymerStation_6StockCarrier": (
                "分装板",
                "3a14196e-5dfe-6e21-0c79-fe2036d052c4",
            ),
        },
        expected_names=("NMP", "NMP_2", "NMP_3", "1010"),
        expected_models=(
            "BIOYOND_PolymerStation_1BottleCarrier",
            "BIOYOND_PolymerStation_1BottleCarrier",
            "BIOYOND_PolymerStation_1BottleCarrier",
            "BIOYOND_PolymerStation_6StockCarrier",
        ),
        expected_parents=("试剂堆栈", "试剂堆栈", "试剂堆栈", "粉末堆栈"),
        expected_sites=(7, 4, 5, 8),
        expected_detail_names={
            "1010": (
                "90%分装小瓶_5",
                "10%分装小瓶_4",
                "90%分装小瓶_1",
                "10%分装小瓶_0",
                "10%分装小瓶_2",
                "90%分装小瓶_3",
            ),
        },
    ),
}


def run_isolated_contract(
    check: Literal["conversion", "tree"],
    case_name: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            check,
            case_name,
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _convert(case: BioyondCase):
    from unilabos.registry.registry import lab_registry
    from unilabos.resources.bioyond.decks import (
        BIOYOND_PolymerPreparationStation_Deck,
        BIOYOND_PolymerReactionStation_Deck,
    )
    from unilabos.resources.graphio import resource_bioyond_to_plr

    lab_registry.setup()
    deck_types = {
        "reaction": BIOYOND_PolymerReactionStation_Deck,
        "preparation": BIOYOND_PolymerPreparationStation_Deck,
    }
    materials = json.loads((FIXTURE_DIR / case.fixture_name).read_text())
    deck = deck_types[case.deck_kind]("test_deck", setup=True)
    converted = resource_bioyond_to_plr(
        materials,
        type_mapping=case.type_mapping,
        deck=deck,
    )
    return materials, deck, converted


def _assert_conversion(case: BioyondCase):
    from pylabrobot.resources import Resource as ResourcePLR

    from unilabos.resources.itemized_carrier import BottleCarrier

    materials, deck, converted = _convert(case)

    assert len(converted) == len(materials)
    assert all(isinstance(resource, ResourcePLR) for resource in converted)
    assert all(isinstance(resource, BottleCarrier) for resource in converted)
    assert tuple(resource.name for resource in converted) == case.expected_names
    assert tuple(resource.model for resource in converted) == case.expected_models

    for resource, material, parent_name, site in zip(
        converted,
        materials,
        case.expected_parents,
        case.expected_sites,
        strict=True,
    ):
        assert resource.unilabos_extra["material_bioyond_id"] == material["id"]
        assert resource.unilabos_extra["material_bioyond_name"] == material["name"]
        assert resource.parent.name == parent_name
        assert resource.parent.sites[site] is resource
        assert all(child.parent is resource for child in resource.children)

    resources_by_name = {resource.name: resource for resource in converted}
    for resource_name, detail_names in case.expected_detail_names.items():
        assert tuple(
            child.name for child in resources_by_name[resource_name].children
        ) == detail_names

    return deck, converted


def _assert_tree(case: BioyondCase) -> None:
    from unilabos.resources.resource_tracker import ResourceTreeSet

    deck, converted = _assert_conversion(case)
    dumped_trees = ResourceTreeSet.from_plr_resources([deck]).dump()

    assert len(dumped_trees) == 1
    assert dumped_trees[0][0]["name"] == deck.name
    nodes_by_name = {node["name"]: node for node in dumped_trees[0]}
    nodes_by_uuid = {node["uuid"]: node for node in dumped_trees[0]}

    for resource, parent_name in zip(
        converted,
        case.expected_parents,
        strict=True,
    ):
        material_node = nodes_by_name[resource.name]
        assert material_node["type"] == "bottle_carrier"
        assert nodes_by_uuid[material_node["parent_uuid"]]["name"] == parent_name

    for resource_name, detail_names in case.expected_detail_names.items():
        material_node = nodes_by_name[resource_name]
        for detail_name in detail_names:
            detail_node = nodes_by_name[detail_name]
            assert nodes_by_uuid[detail_node["parent_uuid"]] is material_node


def main() -> None:
    check, case_name = sys.argv[1:]
    case = CASES[case_name]
    if check == "conversion":
        _assert_conversion(case)
    elif check == "tree":
        _assert_tree(case)
    else:
        raise ValueError(f"unknown check: {check}")


if __name__ == "__main__":
    main()
