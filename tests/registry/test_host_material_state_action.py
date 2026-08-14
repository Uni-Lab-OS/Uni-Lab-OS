"""HostNode 通用物料状态记录动作的运行时与 Catalog 合同回归。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.registry.registry import Registry
from unilabos.registry.template_projection import RegistryTemplateProjection
from unilabos.ros.nodes.presets.host_node import HostNode
from unilabos.workflow.store import WorkflowStore

HOST_RESOURCE_TEMPLATE_UUID = "90000000-0000-4000-8000-000000000011"


def _host_resource_template_identity(_registry_identity: str) -> str:
    """返回测试宿主节点的稳定资源模板 UUID。"""

    return HOST_RESOURCE_TEMPLATE_UUID


@pytest.fixture
def material_inventory(
    tmp_path: Path,
) -> Iterator[tuple[InventoryStore, InventoryService, BackendResourceService, dict[str, Any]]]:
    """构造共享同一 SQLite 权威的库存服务、Backend 合同与一个 Material。"""

    store = InventoryStore(str(tmp_path / "material-state.db"))
    inventory = InventoryService(store)
    backend = BackendResourceService(store)
    template_uuid = backend.sync_resource_templates(
        [
            {
                "id": "test.material_state_sample",
                "display_name": "Material state sample",
                "registry_type": "resource",
            }
        ]
    )["templates"][0]["uuid"]
    material = backend.create_material(
        {
            "resource_template_uuid": template_uuid,
            "name": "sample-1",
            "barcode": "STATE-001",
        }
    )
    try:
        yield store, inventory, backend, material
    finally:
        store.close()


def test_record_material_state_persists_history_and_preserves_uuid(
    material_inventory: tuple[
        InventoryStore,
        InventoryService,
        BackendResourceService,
        dict[str, Any],
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功动作追加时序、刷新 latest projection，并透传同一 Material UUID。"""

    store, inventory, backend, material = material_inventory
    monkeypatch.setattr(
        "unilabos.app.scheduler.integration.get_inventory_service",
        lambda: inventory,
    )
    resource = {
        "uuid": material["uuid"],
        "name": "sample-1",
        "type": "resource",
    }
    state_data = {"sealed": True, "seal_temperature_c": 172.5}

    result = asyncio.run(
        HostNode.record_material_state(
            object(),
            resource,
            state_data,
            status="completed",
            source="workflow.process",
            description="工艺完成后的低频状态",
        )
    )

    assert result["resource"] == [[resource]]
    assert result["resource"][0][0]["uuid"] == material["uuid"]
    assert result == {
        "resource": [[resource]],
        "state_uuid": result["state_uuid"],
        "material_uuid": material["uuid"],
        "status": "completed",
        "state_data": state_data,
        "source": "workflow.process",
        "observed_at": result["observed_at"],
        "description": "工艺完成后的低频状态",
    }
    # 运行时结果必须可直接进入 JSON action transport。
    assert json.loads(json.dumps(result, ensure_ascii=False)) == result

    history = backend.list_material_states(
        material["uuid"],
        before_time=None,
        before_uuid=None,
        limit=20,
    )
    assert [item["uuid"] for item in history["items"]] == [result["state_uuid"]]
    assert backend.latest_material_state(material["uuid"]) == history["items"][0]
    assert backend.get_material(material["uuid"])["data"] == state_data
    assert store.query_one(
        "SELECT COUNT(*) AS count FROM material_state_history WHERE material_uuid=?",
        (material["uuid"],),
    )["count"] == 1


@pytest.mark.parametrize(
    ("state_data", "kwargs", "error_type", "message"),
    [
        pytest.param({}, {}, ValueError, "state_data 必须是非空对象", id="empty-state"),
        pytest.param([], {}, ValueError, "state_data 必须是非空对象", id="non-object-state"),
        pytest.param(
            {"sealed": True},
            {"status": 1},
            TypeError,
            "status",
            id="invalid-optional-text",
        ),
    ],
)
def test_record_material_state_validates_public_fields_before_inventory_write(
    state_data: Any,
    kwargs: dict[str, Any],
    error_type: type[Exception],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无效状态字段关闭式失败，且不会调用库存写入口。"""

    inventory = SimpleNamespace(
        append_material_state=lambda *_args, **_kwargs: pytest.fail(
            "invalid fields must not reach inventory"
        )
    )
    monkeypatch.setattr(
        "unilabos.app.scheduler.integration.get_inventory_service",
        lambda: inventory,
    )

    with pytest.raises(error_type, match=message):
        asyncio.run(
            HostNode.record_material_state(
                object(),
                {"uuid": "10000000-0000-4000-8000-000000000001"},
                state_data,
                **kwargs,
            )
        )


def test_record_material_state_requires_stable_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺少 Material UUID 时在库存调用之前明确失败。"""

    inventory = SimpleNamespace(
        append_material_state=lambda *_args, **_kwargs: pytest.fail(
            "missing UUID must not reach inventory"
        )
    )
    monkeypatch.setattr(
        "unilabos.app.scheduler.integration.get_inventory_service",
        lambda: inventory,
    )

    with pytest.raises(ValueError, match="物料缺少稳定 UUID"):
        asyncio.run(
            HostNode.record_material_state(
                object(),
                {
                    "id": "10000000-0000-4000-8000-000000000099",
                    "name": "anonymous-sample",
                },
                {"sealed": True},
            )
        )


def test_record_material_state_requires_initialized_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """库存权威未初始化时动作明确失败而不伪造状态成功。"""

    monkeypatch.setattr(
        "unilabos.app.scheduler.integration.get_inventory_service",
        lambda: None,
    )

    with pytest.raises(RuntimeError, match="本地库存权威尚未初始化"):
        asyncio.run(
            HostNode.record_material_state(
                object(),
                {"uuid": "10000000-0000-4000-8000-000000000001"},
                {"sealed": True},
            )
        )


def test_record_material_state_catalog_exposes_resource_slot_passthrough(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 AST/Catalog 把 resource 同时发布为默认锁定输入与类型化输出。"""

    registry = Registry()
    monkeypatch.setattr(registry, "_setup_called", False)
    monkeypatch.setattr(registry, "_startup_executor", None)
    monkeypatch.setattr(registry, "device_type_registry", {})
    monkeypatch.setattr(registry, "resource_type_registry", {})
    registry.setup(external_only=True)
    mapping = registry.device_type_registry["host_node"]["class"][
        "action_value_mappings"
    ]["record_material_state"]
    assert mapping["goal"] == {
        "resource": "resource",
        "state_data": "state_data",
        "status": "status",
        "source": "source",
        "description": "description",
    }
    assert mapping["placeholder_keys"] == {"resource": "unilabos_resources"}
    assert mapping["always_free"] is True

    projection = RegistryTemplateProjection(
        WorkflowStore(tmp_path / "workflow-history.db"),
        authority_id="host-material-state-test",
        resource_template_identity_resolver=_host_resource_template_identity,
    )
    try:
        catalog = projection.refresh(registry)
        action = catalog.require_action(
            "unilabos.ros.nodes.presets.host_node:HostNode",
            "record_material_state",
        )
        template = action.detached_template()
    finally:
        projection.close()

    contract = template["meta_data"]["unilab"]["action_contract_schema"]
    goal = contract["properties"]["goal"]["properties"]
    result = contract["properties"]["result"]["properties"]
    assert goal["resource"]["x-unilabos-material-lock"] is True
    assert goal["resource"]["properties"]["uuid"] == {
        "format": "uuid",
        "type": "string",
    }
    assert goal["state_data"]["type"] == "object"
    assert result["resource"]["properties"]["uuid"] == {
        "format": "uuid",
        "type": "string",
    }
    assert result["state_uuid"]["type"] == "string"
    handles = {
        (str(handle["handle_key"]), str(handle["io_type"]))
        for handle in action.handles
    }
    assert {
        ("resource", "target"),
        ("resource", "source"),
        ("state_uuid", "source"),
        ("state_data", "target"),
        ("state_data", "source"),
    } <= handles
    resource_handles = [
        handle
        for handle in action.handles
        if str(handle["handle_key"]) == "resource"
    ]
    assert {str(handle["io_type"]) for handle in resource_handles} == {
        "source",
        "target",
    }
    assert {str(handle["type"]) for handle in resource_handles} == {
        "ResourceSlot"
    }
