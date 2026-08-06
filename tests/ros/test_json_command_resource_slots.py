from __future__ import annotations

import asyncio
import json
from types import MethodType, SimpleNamespace

import pytest

from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode


class _SyncDriver:
    def pick(self, resource: ResourceSlot, warehouse: ResourceSlot):
        return resource, warehouse


class _AsyncDriver:
    async def transfer(self, resource):
        return resource


class _SingleResourceSyncDriver:
    def consume(self, resource: ResourceSlot):
        return resource


_AsyncDriver.transfer.__annotations__["resource"] = (
    "unilabos.registry.placeholder_type:ResourceSlot"
)


def _node(driver) -> BaseROS2DeviceNode:
    node = object.__new__(BaseROS2DeviceNode)
    node.driver_instance = driver
    node._resolve_driver_method_name = lambda action_name: action_name
    node.resource_tracker = None
    return node


def test_sync_json_command_resolves_short_resource_slot_refs_by_uuid() -> None:
    node = _node(_SyncDriver())
    resolved = {
        "material-uuid": object(),
        "warehouse-uuid": object(),
    }
    node._convert_resources_sync = MethodType(
        lambda _self, *uuids: [resolved[uuid] for uuid in uuids],
        node,
    )

    result = BaseROS2DeviceNode._execute_driver_command(
        node,
        json.dumps(
            {
                "function_name": "pick",
                "function_args": {
                    "resource": {"uuid": "material-uuid"},
                    "warehouse": {"uuid": "warehouse-uuid"},
                },
                "unilabos_param": {},
            }
        ),
    )

    assert result == (resolved["material-uuid"], resolved["warehouse-uuid"])


def test_sync_json_command_resolves_upstream_resource_slot_payload_by_unilabos_uuid() -> None:
    node = _node(_SyncDriver())
    resolved = object()
    node._convert_resources_sync = MethodType(
        lambda _self, *uuids: [resolved for uuid in uuids if uuid == "material-uuid"],
        node,
    )

    result = BaseROS2DeviceNode._execute_driver_command(
        node,
        json.dumps(
            {
                "function_name": "pick",
                "function_args": {
                    "resource": {
                        "unilabos_uuid": "material-uuid",
                        "_name": "upstream material",
                    },
                    "warehouse": {"uuid": "material-uuid"},
                },
                "unilabos_param": {},
            }
        ),
    )

    assert result == (resolved, resolved)


def test_sync_json_command_assembles_single_tree_resource_set_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ResourceSlot action output uses ResourceTreeSet.dump()'s [[node]] wire shape."""

    node = _node(_SingleResourceSyncDriver())
    resolved = object()
    raw_node = {"uuid": "material-uuid", "name": "material"}
    parsed_inputs = []

    def parse_flat_tree(raw_nodes):
        parsed_inputs.append(raw_nodes)
        assert raw_nodes == [raw_node]
        tree = SimpleNamespace(
            root_node=SimpleNamespace(
                res_content=SimpleNamespace(name="material"),
            )
        )
        return SimpleNamespace(
            trees=[tree],
            to_plr_resources=lambda: [resolved],
        )

    monkeypatch.setattr(
        ResourceTreeSet,
        "from_raw_dict_list",
        staticmethod(parse_flat_tree),
    )
    node.resource_tracker = SimpleNamespace(
        figure_resource=lambda _resource, try_mode: [],
    )
    logger = SimpleNamespace(
        error=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
    )
    node.lab_logger = lambda: logger

    result = BaseROS2DeviceNode._execute_driver_command(
        node,
        json.dumps(
            {
                "function_name": "consume",
                "function_args": {"resource": [[raw_node]]},
                "unilabos_param": {},
            }
        ),
    )

    assert result is resolved
    assert parsed_inputs == [[raw_node]]


def test_single_resource_slot_rejects_multi_tree_resource_set_dump() -> None:
    node = _node(_SingleResourceSyncDriver())

    with pytest.raises(
        ValueError,
        match="单物料输入要求恰好一棵资源树，实际得到 2 棵",
    ):
        BaseROS2DeviceNode._assemble_single_resource(
            node,
            [
                [{"uuid": "material-a", "name": "material-a"}],
                [{"uuid": "material-b", "name": "material-b"}],
            ],
        )


def test_async_json_command_resolves_canonical_resource_slot_ref_by_uuid() -> None:
    node = _node(_AsyncDriver())
    resolved = object()

    async def convert(_self, resource_data):
        assert resource_data == {"uuid": "material-uuid"}
        return resolved

    node._convert_resource_async = MethodType(convert, node)

    result = asyncio.run(
        BaseROS2DeviceNode._execute_driver_command_async(
            node,
            json.dumps(
                {
                    "function_name": "transfer",
                    "function_args": {"resource": {"uuid": "material-uuid"}},
                    "unilabos_param": {},
                }
            ),
        )
    )

    assert result is resolved


def test_async_json_command_resolves_upstream_resource_slot_payload_by_unilabos_uuid() -> None:
    node = _node(_AsyncDriver())
    resolved = object()

    async def convert(_self, resource_data):
        assert resource_data == {
            "unilabos_uuid": "material-uuid",
            "_name": "upstream material",
        }
        return resolved

    node._convert_resource_async = MethodType(convert, node)

    result = asyncio.run(
        BaseROS2DeviceNode._execute_driver_command_async(
            node,
            json.dumps(
                {
                    "function_name": "transfer",
                    "function_args": {
                        "resource": {
                            "unilabos_uuid": "material-uuid",
                            "_name": "upstream material",
                        }
                    },
                    "unilabos_param": {},
                }
            ),
        )
    )

    assert result is resolved
