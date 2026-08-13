"""Edge 设备目录（Device Catalog）公共投影测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from unilabos.app.web import api as device_api
from unilabos.app.web.device_catalog import project_device_catalog


class _Content:
    """测试用资源节点内容。"""

    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def model_dump(self, *, by_alias: bool) -> dict[str, object]:
        assert by_alias is True
        return dict(self._values)


def test_project_device_catalog_joins_resource_online_and_registry_facts() -> None:
    """设备目录使用库存权威中的稳定设备物料身份。

    参数：无。返回：无；断言资源树运行时 UUID 只用于进程内关系，前端目录中的
    ``materialUuid`` 必须来自库存权威（Inventory Authority）对部署设备 ID 的
    唯一解析。异常：目录投影异常原样传播。
    """

    resources = SimpleNamespace(
        all_nodes=[
            SimpleNamespace(
                res_content=_Content(
                    {
                        "id": "pump-1",
                        "uuid": "10000000-0000-4000-8000-000000000001",
                        "name": "一号泵",
                        "type": "device",
                        "class": "community.lab.pump",
                    }
                )
            ),
            SimpleNamespace(
                res_content=_Content(
                    {
                        "id": "rack-1",
                        "uuid": "10000000-0000-4000-8000-000000000002",
                        "name": "物料架",
                        "type": "warehouse",
                        "class": "community.lab.rack",
                    }
                )
            ),
        ]
    )
    registry_devices = [
        {
            "id": "community.lab.pump",
            "displayname": "泵类型",
            "category": ["pump"],
            "manufacturer": "Uni-Lab",
            "package_definition": {
                "fqid": "community.lab.pump",
                "version": "1.2.0",
                "content_hash": f"sha256:{'1' * 64}",
                "source_identity": "lab.devices.pump:Pump",
                "title": "泵类型",
                "description": "可编程泵",
            },
            "package_catalog": {
                "schema_version": "1",
                "distribution": {
                    "name": "lab-devices",
                    "normalized_name": "lab",
                    "version": "0.4.0",
                },
                "import_package": "lab",
                "namespace": "community.lab",
                "content_digest": f"sha256:{'2' * 64}",
                "catalog_digest": f"sha256:{'3' * 64}",
            },
            "class": {
                "status_types": {"pressure": "float"},
                "action_value_mappings": {
                    "dose": {
                        "display_name": "加液",
                        "type": "Dose",
                        "schema": {
                            "properties": {
                                "goal": {
                                    "type": "object",
                                    "properties": {"volume": {"type": "number"}},
                                },
                                "result": {
                                    "type": "object",
                                    "properties": {"success": {"type": "boolean"}},
                                },
                            }
                        },
                    },
                    "_execute_driver_command": {"type": "Internal"},
                },
            },
        }
    ]
    online_devices = {
        "pump-1": {
            "device_key": "/devices/pump-1/pump-1",
            "namespace": "/devices/pump-1",
            "machine_name": "本地",
        }
    }

    result = project_device_catalog(
        resources=resources,
        registry_devices=registry_devices,
        online_devices=online_devices,
        material_identity_resolver=lambda resource_id: (
            {
                "uuid": "30000000-0000-4000-8000-000000000001",
                "resource_template_uuid": "20000000-0000-4000-8000-000000000001",
            }
            if resource_id == "pump-1"
            else None
        ),
        generated_at=123.0,
    )

    assert result == {
        "schemaVersion": "device-catalog/v2",
        "source": "edge",
        "generatedAt": 123.0,
        "items": [
            {
                "id": "pump-1",
                "materialUuid": "30000000-0000-4000-8000-000000000001",
                "definition": {
                    "fqid": "community.lab.pump",
                    "version": "1.2.0",
                    "contentHash": f"sha256:{'1' * 64}",
                    "sourceIdentity": "lab.devices.pump:Pump",
                    "title": "泵类型",
                    "description": "可编程泵",
                    "category": ["pump"],
                    "manufacturer": "Uni-Lab",
                    "packageCatalog": {
                        "schemaVersion": "1",
                        "distribution": {
                            "name": "lab-devices",
                            "normalizedName": "lab",
                            "version": "0.4.0",
                        },
                        "importPackage": "lab",
                        "namespace": "community.lab",
                        "contentDigest": f"sha256:{'2' * 64}",
                        "catalogDigest": f"sha256:{'3' * 64}",
                    },
                },
                "deviceTypeId": "community.lab.pump",
                "deviceKey": "/devices/pump-1/pump-1",
                "namespace": "/devices/pump-1",
                "name": "一号泵",
                "online": True,
                "stateSchema": {
                    "pressure": {
                        "type": "number",
                        "source": "driver",
                        "status": "resolved",
                    }
                },
                "actions": [
                    {
                        "id": "dose",
                        "actionRef": "pump-1.dose",
                        "name": "加液",
                        "typeName": "Dose",
                        "riskLevel": "normal",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"volume": {"type": "number"}},
                        },
                        "outputSchema": {
                            "type": "object",
                            "properties": {"success": {"type": "boolean"}},
                        },
                        "busy": False,
                        "currentJobId": None,
                    }
                ],
            }
        ],
    }


def test_project_device_catalog_fails_closed_without_package_evidence() -> None:
    """遗留或身份不一致的注册表条目不能伪装为领域设备包定义。

    参数：无。返回：无；断言设备管理信息仍可用，但 ``definition`` 明确为空。
    异常：目录投影异常原样传播。
    """

    resources = SimpleNamespace(
        all_nodes=[
            SimpleNamespace(
                res_content=_Content(
                    {
                        "id": "legacy-1",
                        "name": "遗留设备",
                        "type": "device",
                        "class": "legacy_driver",
                    }
                )
            )
        ]
    )

    result = project_device_catalog(
        resources=resources,
        registry_devices=[{"id": "legacy_driver", "class": {}}],
        online_devices={},
        material_identity_resolver=lambda _device_id: None,
        generated_at=1.0,
    )

    assert result["items"][0]["deviceTypeId"] == "legacy_driver"
    assert result["items"][0]["definition"] is None


def test_device_route_reads_stable_material_identity_from_inventory(
    monkeypatch: Any,
) -> None:
    """正式设备路由把库存权威解析器接入设备目录。

    参数：``monkeypatch`` 隔离当前进程的 Host、注册表与库存组合根。返回：无；
    断言 ``GET /devices`` 的处理函数按部署设备 ID 查询库存权威（Inventory
    Authority），不回退到资源树运行时 UUID。异常：组合根错误原样传播。
    """

    runtime_uuid = "10000000-0000-4000-8000-000000000001"
    stable_uuid = "30000000-0000-4000-8000-000000000001"
    resources = SimpleNamespace(
        all_nodes=[
            SimpleNamespace(
                res_content=_Content(
                    {
                        "id": "pump-1",
                        "uuid": runtime_uuid,
                        "name": "一号泵",
                        "type": "device",
                        "class": "community.lab.pump",
                    }
                )
            )
        ]
    )
    inventory_store = object()
    resolved_ids: list[str] = []

    def build_resolver(store: object) -> Any:
        assert store is inventory_store

        def resolve(device_id: str) -> dict[str, str] | None:
            resolved_ids.append(device_id)
            return {"uuid": stable_uuid, "resource_template_uuid": "template-1"}

        return resolve

    monkeypatch.setattr(device_api, "devices", lambda: (True, resources))
    monkeypatch.setattr(
        device_api,
        "get_online_devices",
        lambda: (True, {"online_devices": {"pump-1": {}}}),
    )
    monkeypatch.setattr(
        device_api,
        "lab_registry",
        SimpleNamespace(obtain_registry_device_info=list),
    )
    monkeypatch.setattr(
        device_api,
        "get_inventory_service",
        lambda: SimpleNamespace(store=inventory_store),
    )
    monkeypatch.setattr(
        device_api,
        "build_inventory_resource_reference_resolver",
        build_resolver,
    )

    response = device_api.get_devices()

    assert resolved_ids == ["pump-1"]
    assert response.data["items"][0]["materialUuid"] == stable_uuid
    assert response.data["items"][0]["materialUuid"] != runtime_uuid
