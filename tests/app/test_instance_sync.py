"""设备图实例通过正式后端 API 初始化的契约测试。"""

from __future__ import annotations

import json

import pytest

from unilabos.app.instance_sync import (
    INSTANCE_TOKEN_ENV,
    InstanceSyncError,
    InstanceSynchronizer,
    run_instance_sync_command,
)
from unilabos.app.main import parse_args
from unilabos.resources.instance_identity import normalize_resource_instance_barcode


def test_resource_instance_barcode_normalizes_plr_and_fallback_shapes() -> None:
    assert normalize_resource_instance_barcode({"data": " PLR-01 "}, "sample") == "PLR-01"
    assert normalize_resource_instance_barcode(" EXPLICIT-01 ", "sample") == "EXPLICIT-01"
    assert normalize_resource_instance_barcode(None, "sample") == "UNILAB-GRAPH-sample"


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.calls = []
        self.created = 0

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url.endswith("/resource-templates"):
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "uuid": "pump-template-uuid",
                                "name": "virtual_transfer_pump",
                                "resource_type": "device",
                            },
                            {
                                "uuid": "tube-template-uuid",
                                "name": "tube_15ml",
                                "resource_type": "resource",
                            },
                            {
                                "uuid": "warehouse-template-uuid",
                                "name": "sample_warehouse",
                                "resource_type": "resource",
                            },
                            {
                                "uuid": "host-template-uuid",
                                "name": "host_node",
                                "resource_type": "device",
                            },
                        ],
                        "has_more": False,
                        "page": 1,
                        "page_size": 100,
                    },
                }
            )
        if url.endswith("/materials"):
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "items": [],
                        "total": 0,
                        "page": 1,
                        "page_size": 100,
                    },
                }
            )
        raise AssertionError(url)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        self.created += 1
        return FakeResponse(
            {
                "code": 0,
                "data": {
                    "uuid": f"material-uuid-{self.created}",
                    **kwargs["json"],
                },
            },
            status_code=201,
        )


class ExistingSession(FakeSession):
    def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        return FakeResponse({"code": 0, "data": {"sites": []}})

    def get(self, url, **kwargs):
        if url.endswith("/materials"):
            self.calls.append(("GET", url, kwargs))
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "uuid": "existing-pump-uuid",
                                "resource_template_uuid": "pump-template-uuid",
                                "barcode": "DEV-PUMP-01",
                            }
                        ],
                        "total": 1,
                        "page": 1,
                        "page_size": 100,
                    },
                }
            )
        return super().get(url, **kwargs)


def test_instance_sync_creates_device_and_instrument_through_material_api() -> None:
    """验证实例同步创建物料、补齐父级，并兼容没有几何字段的旧设备图。

    测试不接收参数且无返回值；请求顺序、父级、身份、Host Node 默认位置或旧图省略规则
    任一变化时断言失败。
    """
    graph = {
        "nodes": [
            {
                "id": "pump_01",
                "name": "模拟注射泵",
                "type": "device",
                "class": "community.example.virtual_transfer_pump",
                "barcode": "DEV-PUMP-01",
                "config": {"port": "MOCK"},
                "data": {"status": "Idle"},
            },
            {
                "id": "tube_01",
                "name": "15 mL 离心管",
                "type": "resource",
                "class": "tube_15ml",
                "barcode": "INS-TUBE-01",
                "config": {},
                "data": {},
            },
        ],
        "links": [],
    }
    session = FakeSession()
    synchronizer = InstanceSynchronizer(
        "http://backend:8080/api/v1",
        "operator-secret",
        session=session,
    )

    report = synchronizer.sync_graph(graph)

    assert report.created_count == 3
    assert report.existing_count == 0
    assert report.material_uuids == {
        "host_node": "material-uuid-1",
        "pump_01": "material-uuid-2",
        "tube_01": "material-uuid-3",
    }
    get_calls = [call for call in session.calls if call[0] == "GET"]
    assert [call[1] for call in get_calls] == [
        "http://backend:8080/api/v1/resource-templates",
        "http://backend:8080/api/v1/materials",
    ]
    assert get_calls[1][2]["params"]["with_children"] == "true"
    post_calls = [call for call in session.calls if call[0] == "POST"]
    assert [call[2]["json"]["barcode"] for call in post_calls] == [
        "UNILAB-GRAPH-host_node",
        "DEV-PUMP-01",
        "INS-TUBE-01",
    ]
    assert post_calls[0][2]["json"]["relative_position"] == {
        "position_x": 0.0,
        "position_y": 0.0,
        "position_z": 0.0,
        "width": 1.0,
        "length": 1.0,
        "depth": 1.0,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "scale_z": 1.0,
        "rotation_x": 0.0,
        "rotation_y": 0.0,
        "rotation_z": 0.0,
    }
    assert post_calls[1][2]["json"] == {
        "resource_template_uuid": "pump-template-uuid",
        "barcode": "DEV-PUMP-01",
        "name": "模拟注射泵",
        "config": {"port": "MOCK"},
        "meta_data": {
            "edge_local_id": "pump_01",
            "edge_resource_type": "device",
            "initial_state": {"status": "Idle"},
        },
    }
    assert post_calls[2][2]["json"]["parent_uuid"] == "material-uuid-1"
    assert all(
        call[2]["headers"]["Authorization"] == "Bearer operator-secret"
        for call in session.calls
    )


def test_instance_sync_sends_graph_geometry_as_material_relative_position() -> None:
    """验证实例同步把设备图位置、尺寸和旋转映射到云端物料相对位置创建合同。

    测试不接收额外参数且无返回值；任一轴映射、尺寸语义或单位缩放缺失时断言失败。
    """
    # graph 只包含一个带完整几何定义的设备，不需要补充场景 Host Node。
    graph = {
        "nodes": [
            {
                "id": "pump_01",
                "name": "模拟注射泵",
                "type": "device",
                "class": "community.example.virtual_transfer_pump",
                "barcode": "DEV-PUMP-01",
                "position": {"x": 10, "y": 20.5, "z": 30},
                "config": {
                    "size_x": 100,
                    "size_y": 80,
                    "size_z": 60,
                    "rotation": {"x": 1, "y": 2, "z": 90},
                },
                "data": {},
            }
        ],
        "links": [],
    }
    # session 记录正式创建请求，使测试能直接核对跨仓库 HTTP 合同。
    session = FakeSession()
    synchronizer = InstanceSynchronizer(
        "http://backend:8080/api/v1",
        "operator-secret",
        session=session,
    )

    synchronizer.sync_graph(graph)

    # pump_request 是目标设备对应的 POST /materials 请求，不包含自动补齐的 HostNode。
    pump_request = next(
        call[2]["json"]
        for call in session.calls
        if call[0] == "POST" and call[2]["json"]["barcode"] == "DEV-PUMP-01"
    )
    assert pump_request["relative_position"] == {
        "position_x": 10.0,
        "position_y": 20.5,
        "position_z": 30.0,
        "width": 100.0,
        "length": 80.0,
        "depth": 60.0,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "scale_z": 1.0,
        "rotation_x": 1.0,
        "rotation_y": 2.0,
        "rotation_z": 90.0,
    }


def test_instance_sync_reads_position_and_rotation_from_pose() -> None:
    """验证旧图缺少顶层 position 时从 pose 读取坐标和旋转。
    测试不接收参数且无返回值；pose 兼容路径或尺寸映射改变时断言失败。
    """
    graph = {
        "nodes": [
            {
                "id": "pump_01",
                "name": "模拟注射泵",
                "type": "device",
                "class": "community.example.virtual_transfer_pump",
                "barcode": "DEV-PUMP-01",
                "pose": {
                    "position": {"x": -10, "y": 0, "z": 5},
                    "rotation": {"x": -1, "y": 0, "z": 45},
                },
                "config": {"size_x": 100, "size_y": 80, "size_z": 60},
            }
        ]
    }
    # session 保存 Backend 物料创建入参，供兼容路径断言使用。
    session = FakeSession()
    synchronizer = InstanceSynchronizer(
        "http://backend:8080/api/v1",
        "operator-secret",
        session=session,
    )

    synchronizer.sync_graph(graph)

    request = next(call[2]["json"] for call in session.calls if call[0] == "POST")
    assert request["relative_position"] == {
        "position_x": -10.0,
        "position_y": 0.0,
        "position_z": 5.0,
        "width": 100.0,
        "length": 80.0,
        "depth": 60.0,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "scale_z": 1.0,
        "rotation_x": -1.0,
        "rotation_y": 0.0,
        "rotation_z": 45.0,
    }


@pytest.mark.parametrize(
    ("geometry", "expected_error"),
    [
        ({"position": {"x": float("nan")}}, "position.x must be finite"),
        ({"position": {"x": True}}, "position.x must be a number"),
        ({"config": {"size_x": -1}}, "config.size_x must be at least 0"),
        ({"pose": []}, "pose must be an object"),
    ],
)
def test_instance_sync_rejects_invalid_graph_geometry(
    geometry: dict[str, object], expected_error: str
) -> None:
    """验证非法几何在任何模板查询或物料写入前失败关闭。

    Args:
        geometry: 覆盖基础设备节点的非法几何片段。
        expected_error: 预期稳定错误消息片段。

    Returns:
        无返回值；未抛出同步错误、错误消息漂移或发生 HTTP 调用时断言失败。
    """
    node = {
        "id": "pump_01",
        "name": "模拟注射泵",
        "type": "device",
        "class": "community.example.virtual_transfer_pump",
        "barcode": "DEV-PUMP-01",
        **geometry,
    }
    # session 用于证明失败发生在外部 HTTP 副作用之前。
    session = FakeSession()
    synchronizer = InstanceSynchronizer(
        "http://backend:8080/api/v1",
        "operator-secret",
        session=session,
    )

    with pytest.raises(InstanceSyncError, match=expected_error):
        synchronizer.sync_graph({"nodes": [node]})

    assert session.calls == []


def test_instance_sync_does_not_overwrite_existing_material_geometry() -> None:
    """验证已存在物料复用身份且不覆盖平台相对位置。

    测试不接收参数且无返回值；允许最新 OS 补齐资源模板（ResourceTemplate）
    声明的库位（Site），但禁止创建重复物料或发送位置更新。
    """
    graph = {
        "nodes": [
            {
                "id": "pump_01",
                "name": "模拟注射泵",
                "type": "device",
                "class": "community.example.virtual_transfer_pump",
                "barcode": "DEV-PUMP-01",
                "position": {"x": 10, "y": 20, "z": 30},
                "config": {"size_x": 100, "size_y": 80, "size_z": 60},
            }
        ]
    }
    # session 提供一个已存在物料，用于验证幂等复用边界。
    session = ExistingSession()
    synchronizer = InstanceSynchronizer(
        "http://backend:8080/api/v1",
        "operator-secret",
        session=session,
    )

    report = synchronizer.sync_graph(graph)

    assert report.created_count == 0
    assert report.existing_count == 1
    assert report.material_uuids == {"pump_01": "existing-pump-uuid"}
    write_calls = [call for call in session.calls if call[0] != "GET"]
    assert [(call[0], call[1], call[2]["json"]) for call in write_calls] == [
        (
            "PUT",
            "http://backend:8080/api/v1/edge/materials/"
            "existing-pump-uuid/sites/from-template",
            {},
        )
    ]


def test_instance_sync_command_reads_graph_without_starting_edge(tmp_path):
    graph_path = tmp_path / "devices.json"
    graph_path.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "pump_01",
                        "name": "模拟注射泵",
                        "type": "device",
                        "class": "virtual_transfer_pump",
                        "barcode": "DEV-PUMP-01",
                    }
                ],
                "links": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    parsed = vars(
        parse_args().parse_args(
            [
                "--graph",
                str(graph_path),
                "--addr",
                "http://backend:8080/api/v1",
                "instance-sync",
            ]
        )
    )

    report = run_instance_sync_command(
        parsed,
        backend_address=parsed["addr"],
        environment={INSTANCE_TOKEN_ENV: "operator-secret"},
        session=FakeSession(),
    )

    assert report.created_count == 1
    assert report.material_uuids == {"pump_01": "material-uuid-1"}


def test_instance_sync_normalizes_barcode_less_graph_resources():
    graph = {
        "nodes": [
            {
                "id": "sample_warehouse_01",
                "name": "样品仓",
                "type": "warehouse",
                "class": "community.example.sample_warehouse",
                "parent": "external_deck",
            }
        ]
    }
    session = FakeSession()
    synchronizer = InstanceSynchronizer(
        "http://backend:8080/api/v1",
        "operator-secret",
        session=session,
    )

    report = synchronizer.sync_graph(graph)

    assert report.created_count == 2
    assert report.material_uuids == {
        "host_node": "material-uuid-1",
        "sample_warehouse_01": "material-uuid-2",
    }
    requests = [call[2]["json"] for call in session.calls if call[0] == "POST"]
    request = requests[1]
    assert request["barcode"] == "UNILAB-GRAPH-sample_warehouse_01"
    assert request["parent_uuid"] == "material-uuid-1"
    assert request["meta_data"]["edge_resource_type"] == "resource"


def test_read_only_check_blocks_edge_until_instances_exist():
    graph = {
        "nodes": [
            {
                "id": "pump_01",
                "name": "模拟注射泵",
                "type": "device",
                "class": "virtual_transfer_pump",
                "barcode": "DEV-PUMP-01",
            }
        ]
    }
    session = ExistingSession()
    synchronizer = InstanceSynchronizer(
        "http://backend:8080/api/v1",
        "",
        session=session,
    )

    report = synchronizer.check_graph(graph)

    assert report.created_count == 0
    assert report.existing_count == 1
    assert report.material_uuids == {"pump_01": "existing-pump-uuid"}
    assert not any(call[0] == "POST" for call in session.calls)
    assert all("Authorization" not in call[2]["headers"] for call in session.calls)


def test_instance_sync_reconciles_template_sites_for_existing_material():
    graph = {
        "nodes": [
            {
                "id": "pump_01",
                "name": "模拟注射泵",
                "type": "device",
                "class": "virtual_transfer_pump",
                "barcode": "DEV-PUMP-01",
            }
        ]
    }
    session = ExistingSession()
    synchronizer = InstanceSynchronizer(
        "http://backend:8080/api/v1",
        "operator-secret",
        session=session,
    )

    report = synchronizer.sync_graph(graph)

    assert report.existing_count == 1
    put_calls = [call for call in session.calls if call[0] == "PUT"]
    assert [call[1] for call in put_calls] == [
        "http://backend:8080/api/v1/edge/materials/existing-pump-uuid/sites/from-template"
    ]
    assert put_calls[0][2]["json"] == {}
