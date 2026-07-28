from __future__ import annotations

from copy import deepcopy

import pytest

from unilabos.registry.template_catalog import (
    ResourceTemplateCatalog,
    TemplateAssetError,
    stable_template_uuid,
)


class FakeRegistry:
    _setup_called = True

    def __init__(self) -> None:
        self.device_type_registry = {
            "generic-device": {
                "class": {"module": "example:Generic"},
            },
            "liquid_handler.prcxi": {
                "catalog": {
                    "visibility": "public",
                    "display_name": "PRCXI 液体工作站",
                    "tags": ["移液"],
                },
                "category": ["liquid_handler"],
                "description": "Concrete liquid handler",
                "class": {
                    "module": "example:PRCXI",
                    "action_value_mappings": {
                        "run": {"schema": {"secret": "must-not-leak"}}
                    },
                },
                "init_param_schema": {
                    "type": "object",
                    "properties": {"address": {"type": "string"}},
                },
                "file_path": "/private/registry/liquid_handler.yaml",
            },
        }
        self.resource_type_registry = {
            "plate-4": {
                "displayname": "四孔板",
                "category": ["plates"],
                "description": "2 x 2 plate",
                "class": {
                    "module": "example:Plate",
                    "type": "pylabrobot",
                },
                "config_info": _plate_config(),
                "file_path": "/private/registry/plates.yaml",
            },
            "private-resource": {
                "catalog": {"visibility": "internal"},
                "class": {"module": "example:Private"},
            },
        }

    @staticmethod
    def _module_source_hash(module: str) -> str:
        return f"source:{module}"


def test_catalog_applies_visibility_and_stable_identity() -> None:
    catalog = ResourceTemplateCatalog(FakeRegistry())

    result = catalog.list_templates()

    assert result["stale"] is False
    assert [item["key"] for item in result["items"]] == [
        "liquid_handler.prcxi",
        "plate-4",
    ]
    device = result["items"][0]
    assert device["uuid"] == stable_template_uuid(
        "unilabos",
        "device",
        "liquid_handler.prcxi",
    )
    assert device["creation"] == {
        "mode": "dynamic-device",
        "available": False,
        "reason": "当前 Edge 尚未开放动态设备创建",
    }
    serialized = repr(result)
    assert "/private/" not in serialized
    assert "must-not-leak" not in serialized


def test_resource_detail_normalizes_geometry_and_grid_without_contents() -> None:
    catalog = ResourceTemplateCatalog(FakeRegistry())
    summary = next(
        item
        for item in catalog.list_templates()["items"]
        if item["key"] == "plate-4"
    )

    detail = catalog.get_template(summary["uuid"])

    assert detail["geometry"]["dimensions_mm"] == {
        "x": 127.0,
        "y": 85.0,
        "z": 15.0,
    }
    assert detail["container_layout"] == {
        "type": "grid",
        "container_kind": "well",
        "rows": ["A", "B"],
        "columns": 2,
        "column_labels": [1, 2],
        "naming": "row-column",
        "geometry": {
            "dimensions_mm": {"x": 8.0, "y": 8.0, "z": 10.0},
            "depth_mm": 10.0,
            "shape": "circle",
            "max_volume_ul": 200.0,
            "pitch_mm": {"x": 9.0, "y": -9.0},
            "offset_mm": {"x": 10.0, "y": 20.0, "z": 2.0},
            "first_key": "A1",
        },
    }
    assert "data" not in repr(detail)
    assert "liquids" not in repr(detail)


def test_template_identity_does_not_depend_on_mutable_metadata() -> None:
    first = FakeRegistry()
    second = FakeRegistry()
    second.device_type_registry["liquid_handler.prcxi"]["description"] = "updated"
    second.device_type_registry["liquid_handler.prcxi"]["file_path"] = (
        "/another/machine/device.yaml"
    )

    first_device = ResourceTemplateCatalog(first).list_templates()["items"][0]
    second_device = ResourceTemplateCatalog(second).list_templates()["items"][0]

    assert first_device["uuid"] == second_device["uuid"]
    assert first_device["content_hash"] != second_device["content_hash"]


def test_asset_resolution_rejects_undeclared_assets() -> None:
    catalog = ResourceTemplateCatalog(FakeRegistry())
    template_uuid = catalog.list_templates()["items"][0]["uuid"]

    with pytest.raises(TemplateAssetError):
        catalog.resolve_asset(template_uuid, "model")


def _plate_config() -> list[dict]:
    root_uuid = "root"
    root = {
        "id": "plate",
        "uuid": root_uuid,
        "parent_uuid": None,
        "type": "plate",
        "pose": {
            "size": {"width": 127, "height": 85, "depth": 15},
            "position": {"x": 0, "y": 0, "z": 0},
        },
        "config": {
            "size_x": 127,
            "size_y": 85,
            "size_z": 15,
            "ordering": {
                "A1": "well-a1",
                "B1": "well-b1",
                "A2": "well-a2",
                "B2": "well-b2",
            },
        },
    }
    nodes = [root]
    for key, x, y in (
        ("A1", 10, 20),
        ("B1", 10, 11),
        ("A2", 19, 20),
        ("B2", 19, 11),
    ):
        nodes.append(
            {
                "id": f"well-{key.casefold()}",
                "uuid": f"uuid-{key.casefold()}",
                "parent_uuid": root_uuid,
                "type": "well",
                "pose": {
                    "size": {"width": 8, "height": 8, "depth": 10},
                    "position": {"x": x, "y": y, "z": 2},
                    "cross_section_type": "circle",
                },
                "config": {
                    "size_x": 8,
                    "size_y": 8,
                    "size_z": 10,
                    "max_volume": 200,
                },
                "data": {"liquids": [["water", 10]]},
            }
        )
    return deepcopy(nodes)
