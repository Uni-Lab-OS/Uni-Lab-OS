"""Startup projection for factory-owned PyLabRobot default children."""

from __future__ import annotations

from typing import Any

import pytest

from pylabrobot.resources import Coordinate, PlateCarrier, PlateHolder, Resource

from unilabos.package_manager.driver_runtime.factory_resource_projection import (
    FactoryResourceProjectionError,
    project_factory_resource_trees,
    take_prepared_factory_instance,
)
from unilabos.resources.resource_tracker import ResourceTreeSet


class _Registry:
    def __init__(self, entry: dict[str, Any]) -> None:
        self.entry = entry

    def resolve_definition(self, kind: str, identity: str) -> dict[str, Any]:
        assert kind == "device"
        if identity == "container_device":
            return {"id": identity, "metadata": {}}
        assert identity == "factory_device"
        return self.entry


class _FactoryDevice(Resource):
    def __init__(self, name: str) -> None:
        super().__init__(name=name, size_x=100, size_y=100, size_z=100)
        rack = PlateCarrier(
            name=f"{name}-rack",
            size_x=50,
            size_y=60,
            size_z=70,
            sites={
                0: PlateHolder(
                    name=f"{name}-rack-1",
                    size_x=10,
                    size_y=20,
                    size_z=30,
                    pedestal_size_z=0,
                ).at(Coordinate(x=1, y=2, z=3))
            },
            model="rack-model",
        )
        rack.unilabos_extra = {"unilabos_resource_class": "factory_rack"}
        self.assign_child_resource(rack, location=Coordinate.zero())


def _make_device(name: str) -> _FactoryDevice:
    return _FactoryDevice(name)


def _root_tree(*, with_child: bool = False) -> ResourceTreeSet:
    nodes = [
        {
            "id": "device-a",
            "uuid": "71000000-0000-4000-8000-000000000001",
            "name": "device-a",
            "class": "factory_device",
            "type": "device",
            "config": {"name": "device-a"},
            "data": {},
        }
    ]
    if with_child:
        nodes.append(
            {
                "id": "authored-rack",
                "uuid": "72000000-0000-4000-8000-000000000001",
                "parent_uuid": nodes[0]["uuid"],
                "name": "authored-rack",
                "class": "",
                "type": "plate_carrier",
                "config": {},
                "data": {},
            }
        )
    return ResourceTreeSet.from_raw_dict_list(nodes)


def _nested_tree() -> ResourceTreeSet:
    root_uuid = "70000000-0000-4000-8000-000000000001"
    return ResourceTreeSet.from_raw_dict_list(
        [
            {
                "id": "container",
                "uuid": root_uuid,
                "name": "container",
                "class": "container_device",
                "type": "device",
                "config": {},
                "data": {},
            },
            {
                "id": "device-a",
                "uuid": "71000000-0000-4000-8000-000000000001",
                "parent_uuid": root_uuid,
                "name": "device-a",
                "class": "factory_device",
                "type": "device",
                "config": {"name": "device-a"},
                "data": {},
            },
        ]
    )


def _fixture() -> tuple[_Registry, dict[str, Any], type[Any]]:
    calls: list[str] = []

    def make_device(name: str) -> _FactoryDevice:
        calls.append(name)
        return _FactoryDevice(name)

    # Runtime activation resolves postponed annotations in module globals.
    make_device.__annotations__["return"] = _FactoryDevice

    entry = {
        "id": "factory_device",
        "metadata": {"infer_resource_tree": True},
        "class": {
            "module": "fixture:_FactoryDevice",
            "type": "pylabrobot",
            "action_value_mappings": {},
            "status_types": {},
        },
        "factory": {
            "module": "fixture:make_device",
            "return_class": "fixture:_FactoryDevice",
        },
    }
    registry = _Registry(entry)
    symbols = {
        "fixture:_FactoryDevice": _FactoryDevice,
        "fixture:make_device": make_device,
        "calls": calls,
    }
    return registry, symbols, _FactoryDevice


def test_factory_tree_is_projected_once_and_prepared_instance_is_reused() -> None:
    registry, symbols, driver_class = _fixture()
    tree = _root_tree()

    assert (
        project_factory_resource_trees(
            registry,
            tree,
            loader=symbols.__getitem__,
        )
        == 1
    )

    root = tree.root_nodes[0]
    assert symbols["calls"] == ["device-a"]
    assert [child.res_content.type for child in root.children] == ["plate_carrier"]
    assert root.children[0].res_content.klass == "factory_rack"
    assert [child.res_content.type for child in root.children[0].children] == [
        "plate_holder"
    ]
    first_uuid = root.children[0].res_content.uuid

    prepared = take_prepared_factory_instance(
        root.res_content.uuid,
        "factory_device",
        driver_class,
    )
    assert isinstance(prepared, driver_class)
    assert (
        take_prepared_factory_instance(
            root.res_content.uuid,
            "factory_device",
            driver_class,
        )
        is None
    )
    assert first_uuid.startswith("b") or len(first_uuid) == 36


def test_factory_tree_rejects_a_second_authored_topology() -> None:
    registry, symbols, _driver_class = _fixture()

    with pytest.raises(
        FactoryResourceProjectionError,
        match="factory_resource_tree_explicit_children",
    ):
        project_factory_resource_trees(
            registry,
            _root_tree(with_child=True),
            loader=symbols.__getitem__,
        )


def test_factory_tree_child_identities_are_stable_across_restarts() -> None:
    first_registry, first_symbols, driver_class = _fixture()
    first_tree = _root_tree()
    project_factory_resource_trees(
        first_registry,
        first_tree,
        loader=first_symbols.__getitem__,
    )
    first_uuids = [node["uuid"] for node in first_tree.dump()[0]]
    take_prepared_factory_instance(
        first_tree.root_nodes[0].res_content.uuid,
        "factory_device",
        driver_class,
    )

    second_registry, second_symbols, _driver_class = _fixture()
    second_tree = _root_tree()
    project_factory_resource_trees(
        second_registry,
        second_tree,
        loader=second_symbols.__getitem__,
    )
    second_uuids = [node["uuid"] for node in second_tree.dump()[0]]
    take_prepared_factory_instance(
        second_tree.root_nodes[0].res_content.uuid,
        "factory_device",
        driver_class,
    )

    assert second_uuids == first_uuids


def test_nested_factory_device_projects_its_default_children() -> None:
    registry, symbols, driver_class = _fixture()
    tree = _nested_tree()

    assert (
        project_factory_resource_trees(
            registry,
            tree,
            loader=symbols.__getitem__,
        )
        == 1
    )

    factory_node = tree.find_by_uuid("71000000-0000-4000-8000-000000000001")
    assert factory_node is not None
    assert [child.res_content.type for child in factory_node.children] == [
        "plate_carrier"
    ]
    assert (
        take_prepared_factory_instance(
            factory_node.res_content.uuid,
            "factory_device",
            driver_class,
        )
        is not None
    )
