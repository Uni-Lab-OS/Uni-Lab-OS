from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import networkx as nx
import pytest

from unilabos.app.web import client as web_client
from unilabos.resources import graphio
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.ros.nodes.presets.host_node import HostNode


def _local_material_tree() -> ResourceTreeSet:
    return ResourceTreeSet.from_raw_dict_list(
        [
            {
                "id": "local-material",
                "uuid": "local-material-uuid",
                "parent_uuid": None,
                "name": "Local material",
                "type": "resource",
                "class": "Resource",
                "position": {"x": 0, "y": 0, "z": 0},
                "config": {"type": "Resource"},
                "data": {},
                "extra": {},
            }
        ]
    )


def _local_host_without_material_bridge() -> SimpleNamespace:
    host = SimpleNamespace(
        bridges=[object(), object()],
        lab_logger=Mock(return_value=Mock()),
    )
    host._material_resource_sync_client = MethodType(HostNode._material_resource_sync_client, host)
    return host


def _reject_implicit_cloud_sync(*_args, **_kwargs):
    raise AssertionError("local resource bookkeeping must not use the global cloud HTTP client")


@pytest.mark.asyncio
async def test_resource_tree_add_stays_local_without_explicit_material_bridge(monkeypatch) -> None:
    local_graph = nx.DiGraph()
    monkeypatch.setattr(graphio, "physical_setup_graph", local_graph)
    monkeypatch.setattr(web_client.http_client, "resource_tree_add", _reject_implicit_cloud_sync)
    response = SimpleNamespace(response="")

    await HostNode._resource_tree_action_add_callback(
        _local_host_without_material_bridge(),
        {"data": _local_material_tree().dump(), "mount_uuid": "", "first_add": False},
        response,
    )

    assert response.response == "{}"
    assert local_graph.nodes["local-material"]["uuid"] == "local-material-uuid"


@pytest.mark.asyncio
async def test_resource_tree_update_stays_local_without_explicit_material_bridge(monkeypatch) -> None:
    monkeypatch.setattr(web_client.http_client, "resource_tree_add", _reject_implicit_cloud_sync)
    response = SimpleNamespace(response="")

    await HostNode._resource_tree_action_update_callback(
        _local_host_without_material_bridge(),
        {"data": _local_material_tree().dump()},
        response,
    )

    assert response.response == "{}"


@pytest.mark.asyncio
async def test_resource_tree_add_uses_an_explicit_material_bridge(monkeypatch) -> None:
    material_bridge = Mock()
    material_bridge.resource_tree_add.return_value = {"local-material-uuid": "cloud-material-uuid"}
    host = SimpleNamespace(
        bridges=[object(), material_bridge],
        lab_logger=Mock(return_value=Mock()),
    )
    host._material_resource_sync_client = MethodType(HostNode._material_resource_sync_client, host)
    local_graph = nx.DiGraph()
    monkeypatch.setattr(graphio, "physical_setup_graph", local_graph)
    tree = _local_material_tree()
    response = SimpleNamespace(response="")

    await HostNode._resource_tree_action_add_callback(
        host,
        {"data": tree.dump(), "mount_uuid": "mount-uuid", "first_add": True},
        response,
    )

    material_bridge.resource_tree_add.assert_called_once()
    assert response.response == '{"local-material-uuid":"cloud-material-uuid"}'
    assert local_graph.nodes["local-material"]["uuid"] == "local-material-uuid"
