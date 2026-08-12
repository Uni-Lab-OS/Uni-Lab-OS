"""Edge 设备目录（Device Catalog）公共投影测试。"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

import pytest

from unilabos.app.scheduler import integration
from unilabos.app.scheduler.inventory import resource_reference
from unilabos.app.web import api as web_api
from unilabos.app.web.device_catalog import project_device_catalog


class _Content:
    """测试用资源节点内容。"""

    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def model_dump(self, *, by_alias: bool) -> dict[str, object]:
        assert by_alias is True
        return dict(self._values)


def test_project_device_catalog_joins_resource_online_and_registry_facts() -> None:
    """设备实例、库存物料身份、ROS 在线事实和动作合同汇合为唯一目录。

    参数：无。返回：无。断言：目录只输出设备节点、库存权威物料 UUID、在线
    状态和公开动作合同，并过滤内部动作；不把资源树运行时 UUID 当成执行身份。
    """

    # ``device_material_uuid`` 是库存权威分配的实际设备物料（Material）身份；
    # 它故意不同于资源树运行时 UUID，防止目录把临时身份误作执行身份。
    device_material_uuid = "20000000-0000-4000-8000-000000000001"

    def resolve_material(device_id: str) -> dict[str, str] | None:
        """按设备部署 ID 返回测试库存中的唯一物料身份。

        参数：``device_id`` 是设备资源图部署 ID。返回：一号泵对应的物料 UUID
        与资源模板 UUID；其他设备返回 ``None``。异常：无。
        """

        if device_id != "pump-1":
            return None
        return {
            "uuid": device_material_uuid,
            "resource_template_uuid": "30000000-0000-4000-8000-000000000001",
        }

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
        material_resolver=resolve_material,
        generated_at=123.0,
    )

    assert result == {
        "schemaVersion": "device-catalog/v1",
        "source": "edge",
        "generatedAt": 123.0,
        "items": [
            {
                "id": "pump-1",
                "materialUuid": device_material_uuid,
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


def test_get_devices_connects_inventory_material_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产设备端点必须把库存解析器接入设备目录投影。

    参数：``monkeypatch`` 隔离 Host、注册表（Registry）和库存组合根。返回：无。
    断言：``GET /devices`` 的处理函数输出库存权威提供的实际设备物料
    （Material）UUID，而不是资源树运行时 UUID。异常：无。
    """

    # ``device_material_uuid`` 是库存权威身份；``runtime_uuid`` 只属于当前资源树
    # 快照，两者故意不同以守住设备动作（Action）的执行身份边界。
    device_material_uuid = "20000000-0000-4000-8000-000000000001"
    runtime_uuid = "10000000-0000-4000-8000-000000000001"
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
    # ``inventory_store`` 代表已装配的本地库存权威存储端口，仅用于验证接线。
    inventory_store = object()

    def read_host_devices() -> tuple[bool, object]:
        """返回测试 Host 持有的资源树设备事实。"""

        return True, resources

    def read_online_devices() -> tuple[bool, dict[str, object]]:
        """返回测试 ROS 图中的在线设备事实。"""

        return True, {"online_devices": {"pump-1": {"namespace": "/devices"}}}

    def read_registry_devices() -> list[dict[str, object]]:
        """返回空动作合同，保持本测试只关注物料身份接线。"""

        return []

    def get_inventory_service() -> SimpleNamespace:
        """返回携带测试存储端口的库存服务组合根。"""

        return SimpleNamespace(store=inventory_store)

    def build_resolver(
        store: object,
    ) -> Callable[[str], dict[str, str] | None]:
        """验证端点使用当前库存存储并返回唯一设备身份解析器。

        参数：``store`` 是端点传入的库存存储。返回：按设备部署 ID 解析物料身份
        的只读函数。异常：传入其他存储时断言失败。
        """

        assert store is inventory_store

        def resolve(device_id: str) -> dict[str, str] | None:
            """按设备部署 ID 返回实际设备物料身份。

            参数：``device_id`` 是资源图设备部署 ID。返回：一号泵的库存物料
            UUID，其他设备为 ``None``。异常：无。
            """

            if device_id != "pump-1":
                return None
            return {"uuid": device_material_uuid}

        return resolve

    monkeypatch.setattr(web_api, "devices", read_host_devices)
    monkeypatch.setattr(web_api, "get_online_devices", read_online_devices)
    monkeypatch.setattr(
        web_api.lab_registry,
        "obtain_registry_device_info",
        read_registry_devices,
    )
    monkeypatch.setattr(integration, "get_inventory_service", get_inventory_service)
    monkeypatch.setattr(
        resource_reference,
        "build_inventory_resource_reference_resolver",
        build_resolver,
    )

    response = web_api.get_devices()

    assert response.data["items"][0]["materialUuid"] == device_material_uuid
    assert response.data["items"][0]["materialUuid"] != runtime_uuid
