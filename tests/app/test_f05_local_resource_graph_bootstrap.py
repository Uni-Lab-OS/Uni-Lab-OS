"""本地资源树进入公共物料图（MaterialGraph）的启动合同测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unilabos.app.main import should_bootstrap_local_resource_graph
from unilabos.app.scheduler import integration
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.resource_graph_bootstrap import (
    ResourceGraphBootstrapError,
    bootstrap_local_resource_graph,
)
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.config.config import HTTPConfig
from unilabos.registry.template_snapshot import RegistryTemplateSnapshot

MOUNT_MATERIAL_UUID = "97539b08-24de-5003-8b2e-9eb6e983c68a"
FIRST_SITE_UUID = "1962ab7c-b006-5e44-a1bd-9b1fde81d529"


class _Registry:
    """提供启动投影所需的最小设备注册表（Registry）快照。

    参数：构造时无参数。返回：两个读取方法分别返回设备与器材模板定义。
    异常：无；测试只提供一个身份唯一的设备资源模板（ResourceTemplate）。
    """

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """返回固定设备资源模板定义。

        参数：无。返回：包含稳定业务 ID 与 Python 源码身份的单元素列表。
        异常：无；``m2b_mount`` 是资源图设备类和资源模板的共同业务身份。
        """

        return [
            {
                "id": "m2b_mount",
                "display_name": "M2B Mount",
                "type": "device",
                "class": {
                    "module": "m2b_native_e2e.mount:M2BMount",
                    "type": "M2BMount",
                    "action_value_mappings": {},
                },
                "handles": [],
                "category": ["stacker"],
                "config_info": [],
                "scene": [],
                "device_params": {},
            }
        ]

    def obtain_registry_resource_info(self) -> list[dict[str, Any]]:
        """返回空器材资源模板集合。

        参数：无。返回：空列表。异常：无；本夹具只验证设备物料与库位（Site）。
        """

        return []


class _SharedImplementationRegistry(_Registry):
    """提供两个合法复用同一 Python 实现类的设备资源模板。"""

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """返回业务 ID 不同但实现类相同的两个设备模板。

        参数：无。返回：主设备和次设备定义；二者共享实现类身份，但资源图只按
        唯一业务 ID 引用主设备。异常：无；每次调用都返回无共享引用的新字典。
        """

        primary = super().obtain_registry_device_info()[0]
        secondary = {
            **primary,
            "id": "m2b_mount_secondary",
            "display_name": "M2B Mount Secondary",
            "class": dict(primary["class"]),
        }
        return [primary, secondary]


class _ResourceTree:
    """以产品 ``ResourceTreeSet.dump`` 形状暴露固定资源树。

    参数：``site_parent_uuid`` 可覆盖库位（Site）的父运行时 UUID；
    ``mount_name`` 可改变资源图内容以制造指纹冲突；``mount_scale`` 模拟旧资源
    跟踪器（ResourceTracker）的零值缩放。返回：``dump`` 提供一棵树。异常：无；
    非法父引用与缩放由被测深模块关闭式处理。
    """

    def __init__(
        self,
        *,
        site_parent_uuid: str = "64000000-0000-4000-8000-0000000002b0",
        mount_name: str = "Stacker A",
        mount_scale: float = 1,
    ) -> None:
        """保存测试资源树的可变输入。

        参数：三个参数分别表示库位父身份、设备展示名与设备三轴缩放值。返回：
        无。异常：无；``site_parent_uuid`` 是运行时父引用，不是正式物料 UUID。
        """

        self._site_parent_uuid = site_parent_uuid
        self._mount_name = mount_name
        self._mount_scale = mount_scale

    def dump(self) -> list[list[dict[str, Any]]]:
        """返回设备物料和两个有序库位（Site）的序列化树。

        参数：无。返回：与 ``ResourceTreeSet.dump`` 相同的嵌套列表。
        异常：无；运行时 UUID 只用于关系解析，正式身份由资源图来源生成。
        """

        # ``runtime_mount_uuid`` 是资源树内部关系身份，不得成为库存权威物料身份。
        runtime_mount_uuid = "64000000-0000-4000-8000-0000000002b0"
        # ``mount_pose`` 允许精确复现 ResourceTracker 未显式配置时的三轴零缩放。
        mount_pose = _pose(0, 0, 0, 360, 300, 720)
        mount_pose["scale"] = {
            "x": self._mount_scale,
            "y": self._mount_scale,
            "z": self._mount_scale,
        }
        return [
            [
                {
                    "id": "m2b_mount",
                    "uuid": runtime_mount_uuid,
                    "name": self._mount_name,
                    "description": "",
                    "parent_uuid": None,
                    "type": "device",
                    "class": "m2b_mount",
                    "pose": mount_pose,
                    "config": {"category": "stacker"},
                    "data": {},
                    "barcode": "",
                },
                {
                    "id": "slot_a",
                    "uuid": "64000000-0000-4000-8000-0000000002b1",
                    "name": "Slot 1",
                    "description": "",
                    "parent_uuid": self._site_parent_uuid,
                    "type": "well",
                    "class": "",
                    "pose": _pose(0, 0, 40, 100, 100, 24),
                    "config": {"category": "well"},
                    "data": {},
                    "barcode": "",
                },
                {
                    "id": "slot_b",
                    "uuid": "64000000-0000-4000-8000-0000000002b2",
                    "name": "Slot 2",
                    "description": "",
                    "parent_uuid": runtime_mount_uuid,
                    "type": "well",
                    "class": "",
                    "pose": _pose(120, 0, 40, 100, 100, 24),
                    "config": {"category": "well"},
                    "data": {},
                    "barcode": "",
                },
            ]
        ]


def _pose(
    x: float,
    y: float,
    z: float,
    width: float,
    height: float,
    depth: float,
) -> dict[str, Any]:
    """构造资源树位置与尺寸。

    参数：前三项是位置，后三项是宽、高、深。返回：产品 ``pose`` 字典。
    异常：无；数值均是已知有限测试向量。
    """

    return {
        "position": {"x": x, "y": y, "z": z},
        "size": {"width": width, "height": height, "depth": depth},
        "scale": {"x": 1, "y": 1, "z": 1},
        "rotation": {"x": 0, "y": 0, "z": 0},
    }


def _bootstrap(
    store: InventoryStore,
    tree: _ResourceTree,
    *,
    registry: _Registry | None = None,
) -> dict[str, Any]:
    """通过正式深模块接口执行一次启动投影。

    参数：``store`` 是本地库存权威，``tree`` 是资源树集合替身；``registry``
    可注入含共享实现类的注册表（Registry），缺省使用单模板注册表。返回：导入
    回执。异常：资源图非法或与既有权威冲突时传播
    ``ResourceGraphBootstrapError``。
    """

    # ``registry_snapshot`` 是模板同步与资源投影共同消费的单代注册表事实。
    registry_snapshot = RegistryTemplateSnapshot.from_registry(registry or _Registry())
    return bootstrap_local_resource_graph(
        store=store,
        resource_tree_set=tree,
        registry_snapshot=registry_snapshot,
        source_id="/workspace/m2b-native-workspace/graph.json",
    )


def test_first_bootstrap_exposes_stable_device_material_and_ordered_sites() -> None:
    """首次启动必须通过公共接口发布稳定设备物料和业务顺序库位。

    参数：无。返回：无。断言：UUID5 固定向量、父物料与 ``sort_order`` 不漂移。
    """

    store = InventoryStore(":memory:")
    try:
        receipt = _bootstrap(store, _ResourceTree())
        graph = BackendResourceService(store).material_graph()
    finally:
        store.close()

    assert receipt["status"] == "imported"
    assert [node["material"]["uuid"] for node in graph["nodes"]] == [
        MOUNT_MATERIAL_UUID
    ]
    sites = graph["nodes"][0]["sites"]
    assert [site["uuid"] for site in sites] == [
        FIRST_SITE_UUID,
        "56dfa4a8-06b8-5750-bff9-b2290766a57d",
    ]
    assert [site["sort_order"] for site in sites] == [0, 1]
    assert all(site["material_uuid"] == MOUNT_MATERIAL_UUID for site in sites)


def test_shared_implementation_class_keeps_unique_business_aliases() -> None:
    """共享 Python 实现类不得阻止按唯一业务 ID 投影本地资源图。

    参数：无。返回：无。断言：两个资源模板（ResourceTemplate）合法复用同一
    实现类时，模糊类别名不进入解析表，但资源图中的 ``m2b_mount`` 业务 ID 仍
    唯一解析并提交；这复现 SZLab 多设备复用 ``MoveitInterface`` 的启动形状。
    """

    store = InventoryStore(":memory:")
    try:
        receipt = _bootstrap(
            store,
            _ResourceTree(),
            registry=_SharedImplementationRegistry(),
        )
        graph = BackendResourceService(store).material_graph()
    finally:
        store.close()

    assert receipt["status"] == "imported"
    assert [node["material"]["uuid"] for node in graph["nodes"]] == [
        MOUNT_MATERIAL_UUID
    ]


def test_legacy_zero_scale_is_normalized_to_identity_scale() -> None:
    """旧资源跟踪器的零缩放应按“未指定”规范化为单位缩放。

    参数：无。返回：无。断言：SZLab 形状的 ``scale=(0,0,0)`` 不阻止首次投影，
    且 SQLite 中仍满足 Backend 形状 ``scale_* > 0`` 约束并保存三轴单位缩放；
    负缩放与非有限值不在本兼容规则内。
    """

    store = InventoryStore(":memory:")
    try:
        receipt = _bootstrap(store, _ResourceTree(mount_scale=0))
        position = store.query_one(
            "SELECT scale_x,scale_y,scale_z FROM relative_position "
            "WHERE material_uuid=?",
            (MOUNT_MATERIAL_UUID,),
        )
    finally:
        store.close()

    assert receipt["status"] == "imported"
    assert position == {"scale_x": 1.0, "scale_y": 1.0, "scale_z": 1.0}


def test_restart_with_same_source_and_fingerprint_is_idempotent(tmp_path: Path) -> None:
    """同一资源图跨进程重启只返回幂等回执。

    参数：``tmp_path`` 隔离 SQLite 文件。返回：无。断言：正式身份和行数不重复。
    """

    database_path = tmp_path / "inventory.db"
    first = InventoryStore(str(database_path))
    try:
        assert _bootstrap(first, _ResourceTree())["status"] == "imported"
    finally:
        first.close()
    reopened = InventoryStore(str(database_path))
    try:
        receipt = _bootstrap(reopened, _ResourceTree())
        material_count = reopened.query_one("SELECT COUNT(*) AS count FROM material")
        site_count = reopened.query_one("SELECT COUNT(*) AS count FROM site")
    finally:
        reopened.close()

    assert receipt["status"] == "unchanged"
    assert material_count == {"count": 1}
    assert site_count == {"count": 2}


def test_changed_fingerprint_fails_closed_without_overwriting_public_graph() -> None:
    """既有权威遇到同来源不同指纹时必须关闭式失败且保持原图。

    参数：无。返回：无。断言：冲突不会改名、追加物料或改变库位（Site）。
    """

    store = InventoryStore(":memory:")
    try:
        _bootstrap(store, _ResourceTree())
        before = BackendResourceService(store).material_graph()
        with pytest.raises(ResourceGraphBootstrapError, match="指纹|fingerprint"):
            _bootstrap(store, _ResourceTree(mount_name="Changed"))
        after = BackendResourceService(store).material_graph()
    finally:
        store.close()

    assert after == before


def test_dangling_site_parent_rolls_back_all_projection_rows() -> None:
    """悬空库位父引用必须在任何投影行提交前失败。

    参数：无。返回：无。断言：物料、库位和启动指纹全部保持空集合。
    """

    store = InventoryStore(":memory:")
    try:
        with pytest.raises(ResourceGraphBootstrapError, match="父|owner|parent"):
            _bootstrap(store, _ResourceTree(site_parent_uuid="missing-runtime-parent"))
        material_count = store.query_one("SELECT COUNT(*) AS count FROM material")
        site_count = store.query_one("SELECT COUNT(*) AS count FROM site")
        bootstrap_meta = store.query_all(
            "SELECT * FROM lab_meta WHERE meta_key LIKE 'resource_graph_bootstrap_%'"
        )
    finally:
        store.close()

    assert material_count == {"count": 0}
    assert site_count == {"count": 0}
    assert bootstrap_meta == []


def test_bootstrap_gate_requires_local_scheduler_and_embedded_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启动投影只允许本地调度与嵌入式库存共同启用。

    参数：``monkeypatch`` 隔离全局物料来源配置。返回：无。断言：后端控制、
    外部库存和显式关闭调度器均不得打开启动投影路径。
    """

    monkeypatch.setattr(HTTPConfig, "material_source", "microbackend")
    local_args = {
        "app_bridges": ["fastapi"],
        "edge_scheduler": True,
        "_material_service_mode": "embedded",
    }
    assert should_bootstrap_local_resource_graph(local_args, is_host_mode=True)

    backend_args = {**local_args, "app_bridges": ["edge_control", "fastapi"]}
    external_args = {**local_args, "_material_service_mode": "external"}
    scheduler_off_args = {**local_args, "edge_scheduler": False}
    assert not should_bootstrap_local_resource_graph(backend_args, is_host_mode=True)
    assert not should_bootstrap_local_resource_graph(external_args, is_host_mode=True)
    assert not should_bootstrap_local_resource_graph(
        scheduler_off_args, is_host_mode=True
    )
    assert not should_bootstrap_local_resource_graph(local_args, is_host_mode=False)


def test_inventory_composition_bootstraps_before_legacy_cloud_upload(
    tmp_path: Path,
) -> None:
    """库存组合根必须先建立公共物料图且不依赖旧云端上传成功。

    参数：``tmp_path`` 提供隔离的库存 SQLite。返回：无。断言：正式
    ``setup_edge_inventory`` 接线公开稳定设备物料；随后模拟旧云端资源树上传失败，
    本地公共物料图（MaterialGraph）仍保持可查询。异常：上传失败只属于旧桥接路径，
    不得回滚已经提交的本地库存权威（Inventory Authority）事实。
    """

    class _FailingLegacyCloudBridge:
        """模拟拒绝旧资源树上传的云端桥接器。

        参数：无。返回：无。异常：``resource_tree_add`` 固定抛出网络错误。
        """

        def resource_tree_add(self, _resource_tree: object) -> None:
            """拒绝一次旧资源树上传。

            参数：``_resource_tree`` 是不参与断言的旧上传负载。返回：无。
            异常：固定抛出 ``RuntimeError``，模拟云端不可达。
            """

            raise RuntimeError("legacy cloud unavailable")

    integration.reset_for_test()
    try:
        # ``inventory_service`` 是主机组合根创建的唯一嵌入式库存服务。
        inventory_service = integration.setup_edge_inventory(
            str(tmp_path / "inventory.db"),
            resource_tree_set=_ResourceTree(),
            registry_snapshot=RegistryTemplateSnapshot.from_registry(_Registry()),
            resource_graph_source_id="/workspace/m2b-native-workspace/graph.json",
        )
        before_upload = BackendResourceService(inventory_service.store).material_graph()
        with pytest.raises(RuntimeError, match="legacy cloud unavailable"):
            _FailingLegacyCloudBridge().resource_tree_add(_ResourceTree())
        after_upload = BackendResourceService(inventory_service.store).material_graph()
    finally:
        integration.reset_for_test()

    assert [node["material"]["uuid"] for node in before_upload["nodes"]] == [
        MOUNT_MATERIAL_UUID
    ]
    assert after_upload == before_upload
