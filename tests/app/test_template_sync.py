"""Edge Registry 到正式后端模板协议的契约测试。"""

from __future__ import annotations

import gzip
import json

import pytest

from unilabos.app.main import parse_args
from unilabos.app.register import register_devices_and_resources
from unilabos.app.template_sync import (
    DEVELOPER_TOKEN_ENV,
    TemplateSyncError,
    TemplateSynchronizer,
    run_template_sync_command,
)
from unilabos.registry.template_projection import RegistryTemplateProjection
from unilabos.registry.template_snapshot import RegistryTemplateSnapshot
from unilabos.workflow.store import WorkflowStore


RESOURCE_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000001"


class FakeRegistry:
    def obtain_registry_device_info(self):
        return [
            {
                "id": "pump",
                "displayname": "注射泵",
                "registry_type": "device",
                "file_path": "/private/pump.py",
                "class": {
                    "module": "drivers.pump:Pump",
                    "type": "python",
                    "status_types": {"status": "String"},
                    "action_value_mappings": {
                        "transfer": {
                            "contract_kind": "typed",
                            "displayname": "输送",
                            "description": "把物料输送到目标库位",
                            "type": "UniLabJsonCommand",
                            "goal": {
                                "unilabos_device_id": "unilabos_device_id",
                                "volume": "volume",
                            },
                            "goal_default": {
                                "unilabos_device_id": "",
                                "volume": 1.0,
                            },
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "goal": {
                                        "type": "object",
                                        "properties": {
                                            "unilabos_device_id": {
                                                "type": "string",
                                                "default": "",
                                            },
                                            "volume": {"type": "number"},
                                        },
                                        "required": ["unilabos_device_id", "volume"],
                                    }
                                },
                                "x-unilabos-action-contract": {
                                    "version": 2,
                                    "input_order": ["volume"],
                                    "output_order": [],
                                    "resource_template_symbols": {
                                        "goal": {},
                                        "result": {},
                                    },
                                },
                            },
                            "handles": {
                                "input": [
                                    {
                                        "handler_key": "volume",
                                        "label": "体积",
                                        "data_type": "number",
                                        "data_source": "param",
                                        "data_key": "volume",
                                        "io_type": "target",
                                    }
                                ],
                                "output": [],
                            },
                        }
                    },
                },
                "handles": [],
                "category": ["pump"],
                "init_param_schema": {
                    "config": {
                        "type": "object",
                        "properties": {"port": {"type": "string"}},
                    }
                },
            }
        ]

    def obtain_registry_resource_info(self):
        return [
            {
                "id": "tube_15ml",
                "displayname": "15 mL 离心管",
                "registry_type": "resource",
                "class": {
                    "module": "resources.tube:Tube15mL",
                    "type": "pylabrobot",
                },
                "handles": [],
                "category": ["container"],
            }
        ]


class DuplicateDeviceRegistry(FakeRegistry):
    """返回两个相同设备业务名，用于验证完整快照唯一性错误。"""

    def obtain_registry_device_info(self):
        """复制同一设备定义，制造重复活动业务名。"""

        devices = super().obtain_registry_device_info()
        return [devices[0], dict(devices[0])]


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {
            "code": 0,
            "data": {
                "templates": [
                    {"uuid": "device-template-uuid", "name": "pump"},
                    {"uuid": "resource-template-uuid", "name": "tube_15ml"},
                ]
            },
        }
        self.text = json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response=None):
        self.response = response or FakeResponse()
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_sync_merges_device_and_resource_templates_into_one_transaction():
    session = FakeSession()
    synchronizer = TemplateSynchronizer(
        "http://backend:8080",
        "developer-secret",
        session=session,
    )

    report = synchronizer.sync(FakeRegistry())

    assert report.device_count == 1
    assert report.resource_count == 1
    assert report.template_uuids == {
        "pump": "device-template-uuid",
        "tube_15ml": "resource-template-uuid",
    }
    assert len(session.calls) == 1
    url, request = session.calls[0]
    assert url == "http://backend:8080/api/v1/resource-templates"
    assert request["headers"]["Authorization"] == "Bearer developer-secret"
    assert request["headers"]["Content-Encoding"] == "gzip"
    payload = json.loads(gzip.decompress(request["data"]))
    assert [resource["id"] for resource in payload["resources"]] == [
        "pump",
        "tube_15ml",
    ]
    device, resource = payload["resources"]
    assert device["display_name"] == "注射泵"
    assert device["class"]["action_value_mappings"]["transfer"]["display_name"] == "输送"
    action = device["class"]["action_value_mappings"]["transfer"]
    assert "unilabos_device_id" not in action["goal"]
    assert "unilabos_device_id" not in action["goal_default"]
    assert "unilabos_device_id" not in action["schema"]["properties"]["goal"]["properties"]
    assert action["schema"]["properties"]["goal"]["required"] == ["volume"]
    assert device["init_param_schema"] == {
        "config": {"properties": {"port": {"type": "string"}}}
    }
    assert "file_path" not in device
    assert "status_types" not in device["class"]
    assert resource["display_name"] == "15 mL 离心管"
    assert resource["registry_type"] == "resource"


def test_sync_rejects_backend_business_error():
    session = FakeSession(
        FakeResponse(
            payload={
                "code": 5003,
                "error": {"msg": "template definition invalid"},
            }
        )
    )
    synchronizer = TemplateSynchronizer(
        "http://backend:8080/api/v1",
        "developer-secret",
        session=session,
    )

    with pytest.raises(TemplateSyncError, match="5003"):
        synchronizer.sync(FakeRegistry())


def test_template_sync_command_builds_complete_registry_without_starting_edge():
    parsed = vars(
        parse_args().parse_args(
            [
                "--addr",
                "http://backend:8080/api/v1",
                "--registry_path",
                "/registry-a",
                "--devices",
                "/drivers-a",
                "--skip_env_check",
                "template-sync",
            ]
        )
    )
    builder_calls = []

    def registry_builder(**kwargs):
        builder_calls.append(kwargs)
        return FakeRegistry()

    session = FakeSession()
    report = run_template_sync_command(
        parsed,
        backend_address=parsed["addr"],
        environment={DEVELOPER_TOKEN_ENV: "developer-secret"},
        registry_builder=registry_builder,
        session=session,
    )

    assert report.device_count == 1
    assert builder_calls == [
        {
            "registry_paths": ["/registry-a"],
            "devices_dirs": ["/drivers-a"],
            "upload_registry": False,
            "complete_registry": False,
            "external_only": False,
        }
    ]


def test_legacy_startup_registration_is_read_only():
    with pytest.raises(RuntimeError, match="template-sync"):
        register_devices_and_resources(FakeRegistry())


def test_local_projection_and_template_sync_share_one_registry_snapshot(
    tmp_path,
) -> None:
    """本地模板投影和 Backend 同步必须消费同一不可变 Registry 定义快照。

    参数说明：``tmp_path`` 隔离本地工作流数据库；测试比较两条消费路径中的动作
    业务名和最终生产 JSON Schema，禁止二次 Registry 遍历产生漂移。
    """

    registry_snapshot = RegistryTemplateSnapshot.from_registry(FakeRegistry())
    projection = RegistryTemplateProjection(
        WorkflowStore(tmp_path / "workflow_history.db"),
        authority_id="local",
        resource_template_identity_resolver=lambda resource_name: (
            RESOURCE_TEMPLATE_UUID if resource_name == "pump" else ""
        ),
    )
    local_action = projection.refresh(registry_snapshot).require_action(
        "drivers.pump:Pump",
        "transfer",
    )

    session = FakeSession()
    synchronizer = TemplateSynchronizer(
        "http://backend:8080",
        "developer-secret",
        session=session,
    )
    synchronizer.sync(registry_snapshot)
    payload = json.loads(gzip.decompress(session.calls[0][1]["data"]))
    synchronized_action = payload["resources"][0]["class"][
        "action_value_mappings"
    ]["transfer"]

    assert synchronized_action["schema"] == local_action.detached_template()["schema"]
    assert synchronized_action["display_name"] == local_action.template["display_name"]
    projection.close()


def test_template_sync_maps_registry_snapshot_error_to_sync_domain_error() -> None:
    """Registry 快照唯一性失败不得泄露模板同步模块之外的异常类型。"""

    synchronizer = TemplateSynchronizer(
        "http://backend:8080",
        "developer-secret",
        session=FakeSession(),
    )

    with pytest.raises(TemplateSyncError, match="重复"):
        synchronizer.sync(DuplicateDeviceRegistry())
