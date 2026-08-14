"""Edge 设备目录（Device Catalog）公共投影测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from unilabos.app.web import api as device_api
from unilabos.app.web.device_catalog import project_device_catalog
from unilabos.package_manager.package_catalog import (
    PackageCatalog,
    PackageDefinition,
    PackageDefinitionCatalog,
    PackageDistributionIdentity,
    compile_registry_snapshot,
)


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
        "schemaVersion": "device-catalog/v1",
        "source": "edge",
        "generatedAt": 123.0,
        "items": [
            {
                "id": "pump-1",
                "materialUuid": "30000000-0000-4000-8000-000000000001",
                "deviceTypeId": "community.lab.pump",
                "deviceKey": "/devices/pump-1/pump-1",
                "namespace": "/devices/pump-1",
                "name": "一号泵",
                "online": True,
                "stateSchema": {"pressure": {"type": "number"}},
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
    assert "definition" not in response.data["items"][0]


def test_project_device_catalog_attaches_package_definition_reference() -> None:
    """包托管设备目录必须携带完整 PackageCatalog definition 来源证据。

    参数：无。返回：无；断言前端 Device Catalog 的 ``definition`` 来自当前
    注册表快照（Registry Snapshot）中的包定义与所属包目录，FQID 与
    ``deviceTypeId`` 一致，且不得从实例 ID 反推软件包身份。异常：目录投影
    异常原样传播。
    """

    catalog, snapshot = _package_hosted_pump_snapshot()
    resources = SimpleNamespace(
        all_nodes=[
            SimpleNamespace(
                res_content=_Content(
                    {
                        "id": "pump-1",
                        "uuid": "10000000-0000-4000-8000-000000000001",
                        "name": "一号泵",
                        "type": "device",
                        "class": "community.review_lab.pump",
                    }
                )
            ),
            SimpleNamespace(
                res_content=_Content(
                    {
                        "id": "legacy-1",
                        "uuid": "10000000-0000-4000-8000-000000000003",
                        "name": "遗留加热器",
                        "type": "device",
                        "class": "heater",
                    }
                )
            ),
        ]
    )

    result = project_device_catalog(
        resources=resources,
        registry_devices=[
            {
                "id": "community.review_lab.pump",
                "displayname": "蠕动泵",
                "class": {"status_types": {}, "action_value_mappings": {}},
            },
            {
                "id": "heater",
                "displayname": "加热器",
                "class": {"status_types": {}, "action_value_mappings": {}},
            },
        ],
        online_devices={},
        material_identity_resolver=lambda _device_id: None,
        registry_snapshot=snapshot,
        generated_at=123.0,
    )

    hosted = next(item for item in result["items"] if item["id"] == "pump-1")
    legacy = next(item for item in result["items"] if item["id"] == "legacy-1")
    assert hosted["deviceTypeId"] == "community.review_lab.pump"
    assert hosted["definition"] == {
        "fqid": "community.review_lab.pump",
        "version": "1.0.0",
        "contentHash": _DIGEST_A,
        "sourceIdentity": "review_lab.devices.pump:Pump",
        "title": "蠕动泵",
        "description": "测试设备定义",
        "category": ["liquid_handling"],
        "manufacturer": "",
        "packageCatalog": {
            "schemaVersion": "1",
            "distribution": {
                "name": "review-lab",
                "normalizedName": "review_lab",
                "version": "0.1.0",
            },
            "importPackage": "review_lab",
            "namespace": "community.review_lab",
            "contentDigest": _DIGEST_B,
            "catalogDigest": catalog.catalog_digest,
        },
    }
    assert "definition" not in legacy


def test_project_device_catalog_omits_incomplete_package_definition() -> None:
    """摘要不完整的包定义不得写入设备目录，以免前端整表拒绝。

    参数：无。返回：无；断言 content_hash 不是 sha256 摘要时省略 ``definition``，
    目录其余字段仍可读取。异常：目录投影异常原样传播。
    """

    _catalog, snapshot = _package_hosted_pump_snapshot(content_hash="not-a-digest")
    resources = SimpleNamespace(
        all_nodes=[
            SimpleNamespace(
                res_content=_Content(
                    {
                        "id": "pump-1",
                        "uuid": "10000000-0000-4000-8000-000000000001",
                        "name": "一号泵",
                        "type": "device",
                        "class": "community.review_lab.pump",
                    }
                )
            )
        ]
    )

    result = project_device_catalog(
        resources=resources,
        registry_devices=[
            {
                "id": "community.review_lab.pump",
                "displayname": "蠕动泵",
                "class": {"status_types": {}, "action_value_mappings": {}},
            }
        ],
        online_devices={},
        material_identity_resolver=lambda _device_id: None,
        registry_snapshot=snapshot,
        generated_at=123.0,
    )

    assert result["items"][0]["id"] == "pump-1"
    assert "definition" not in result["items"][0]


def test_device_route_projects_published_registry_snapshot_definition(
    monkeypatch: Any,
) -> None:
    """正式设备路由把已发布注册表快照接入设备目录 definition。

    参数：``monkeypatch`` 隔离 Host、注册表与库存组合根。返回：无；断言
    ``GET /devices`` 对包托管实例输出完整 ``definition``，遗留类型省略该字段。
    异常：组合根错误原样传播。
    """

    catalog, snapshot = _package_hosted_pump_snapshot()
    resources = SimpleNamespace(
        all_nodes=[
            SimpleNamespace(
                res_content=_Content(
                    {
                        "id": "pump-1",
                        "uuid": "10000000-0000-4000-8000-000000000001",
                        "name": "一号泵",
                        "type": "device",
                        "class": "community.review_lab.pump",
                    }
                )
            )
        ]
    )
    monkeypatch.setattr(device_api, "devices", lambda: (True, resources))
    monkeypatch.setattr(
        device_api,
        "get_online_devices",
        lambda: (True, {"online_devices": {"pump-1": {}}}),
    )
    monkeypatch.setattr(
        device_api,
        "lab_registry",
        SimpleNamespace(
            obtain_registry_device_info=lambda: [
                {
                    "id": "community.review_lab.pump",
                    "displayname": "蠕动泵",
                    "class": {"status_types": {}, "action_value_mappings": {}},
                }
            ],
            published_registry_snapshot=lambda: snapshot,
        ),
    )
    monkeypatch.setattr(
        device_api,
        "get_inventory_service",
        lambda: SimpleNamespace(store=None),
    )

    response = device_api.get_devices()
    item = response.data["items"][0]
    assert item["definition"]["fqid"] == "community.review_lab.pump"
    assert item["definition"]["packageCatalog"]["catalogDigest"] == catalog.catalog_digest
    assert item["deviceTypeId"] == item["definition"]["fqid"]


_DIGEST_A = "sha256:" + "1" * 64
_DIGEST_B = "sha256:" + "2" * 64


def _package_hosted_pump_snapshot(
    *,
    content_hash: str = _DIGEST_A,
) -> tuple[PackageCatalog, Any]:
    """构造与 Core #147 前端合同一致的包托管泵定义快照。

    参数：无。返回：主包目录及其注册表快照；摘要、命名空间和源码身份均为测试
    字面量，不由投影实现回填。异常：目录或快照不变量失败时传播构造异常。
    """

    definition = PackageDefinition(
        kind="device",
        id="pump",
        fqid="community.review_lab.pump",
        module="review_lab.devices.pump",
        symbol="Pump",
        declaring_file="review_lab/devices/pump.py",
        content_hash=content_hash,
        version="1.0.0",
        title="蠕动泵",
        description="测试设备定义",
        details={
            "registry_entry": {
                "category": ["liquid_handling"],
                "class": {
                    "module": "review_lab.devices.pump:Pump",
                    "type": "python",
                    "action_value_mappings": {},
                    "status_types": {},
                },
                "description": "测试设备定义",
                "displayname": "蠕动泵",
                "registry_type": "device",
                "version": "1.0.0",
            }
        },
    )
    catalog = PackageCatalog.create(
        distribution=PackageDistributionIdentity(
            name="review-lab",
            normalized_name="review_lab",
            version="0.1.0",
        ),
        import_package="review_lab",
        namespace="community.review_lab",
        definitions=PackageDefinitionCatalog(devices=(definition,)),
        assets=(),
        content_digest=_DIGEST_B,
    )
    return catalog, compile_registry_snapshot((catalog,))
