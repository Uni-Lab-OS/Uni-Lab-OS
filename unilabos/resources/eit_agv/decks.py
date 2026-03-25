"""
EIT AGV 资源甲板定义

设计目标：
1) 为 AGV 提供一个独立的前端可视化区域（独立设备，不并入任一工站 deck）。
2) 结构：Deck → WareHouse("AGV") → ResourceHolder(AGV-1 ~ AGV-4)，共 1x4 = 4 个车载槽位。
3) 物料状态在运行时由 pick/put 操作实时追踪更新，不依赖定时轮询 shelf。
4) 上报前端时使用与 Synthesis Station 相同的资源树扁平化逻辑，空 ResourceHolder 自然不显示。
"""

import uuid
from typing import Any
from typing import Self

from pylabrobot.resources import Coordinate, Deck

from unilabos.utils.log import logger


class EIT_AGV_Deck(Deck):
    """
    AGV 车载 Deck。

    层级：Deck → WareHouse("AGV") → ResourceHolder(AGV-1 ~ AGV-4)
    - WareHouse 由 eit_warehouse_AGV 工厂创建（1x4 布局）
    - 物料由 pick_tray_with_material / put_tray_with_material 实时追踪更新
    - 上报时扁平化，空 ResourceHolder 不显示（与手套箱 TB 区一致）
    """

    def __init__(
        self,
        name: str = "EIT_AGV_Deck",
        size_x: float = 800.0,
        size_y: float = 200.0,
        size_z: float = 200.0,
        setup: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(name=name, size_x=size_x, size_y=size_y, size_z=size_z)
        if not getattr(self, "unilabos_uuid", None):
            self.unilabos_uuid = str(uuid.uuid4())
        self.warehouses = {}
        self.warehouse_locations = {}
        if setup:
            self.setup()

    @classmethod
    def deserialize(cls, data, allow_marshal=False) -> Self:
        """
        反序列化：跳过 setup() 以避免重复生成 children。
        """
        data_modified = dict(data)
        requested_setup = bool(data_modified.get("setup", False))
        data_modified["setup"] = False
        resource = super().deserialize(data_modified, allow_marshal=allow_marshal)
        if not resource.children and requested_setup:
            logger.info("EIT AGV Deck 反序列化后无子资源，补执行 setup()")
            resource.setup()
        else:
            # 从已有 children 中恢复 warehouses 字典
            resource.warehouses = {}
            resource.warehouse_locations = {}
            for child in resource.children:
                if hasattr(child, "num_items_x"):  # WareHouse 类型
                    resource.warehouses[child.name] = child
        return resource

    def _recursive_assign_uuid(self, resource: Any) -> None:
        """递归补齐资源 UUID。"""
        if not hasattr(resource, "unilabos_uuid") or not resource.unilabos_uuid:
            resource.unilabos_uuid = str(uuid.uuid4())
        if hasattr(resource, "children"):
            for child in resource.children:
                self._recursive_assign_uuid(child)

    def setup(self) -> None:
        """
        构建 AGV 车载资源结构：Deck → WareHouse("AGV") → ResourceHolder × 4
        使用 eit_warehouse_AGV 工厂函数创建 WareHouse（1x4 布局）。
        """
        from unilabos.resources.eit_synthesis_station.warehouses import eit_warehouse_AGV

        self.warehouses = {}
        self.warehouse_locations = {}

        agv_wh = eit_warehouse_AGV("AGV")
        self._recursive_assign_uuid(agv_wh)
        loc = Coordinate(10.0, 10.0, 0.0)
        self.assign_child_resource(agv_wh, location=loc)
        self.warehouses["AGV"] = agv_wh
        self.warehouse_locations["AGV"] = loc

        logger.info(f"已将仓库 AGV 挂载到 Deck")

        self._recursive_assign_uuid(self)
        logger.info("EIT AGV Deck: 已构建 1x4 = 4 个车载槽位 (Deck → WareHouse → ResourceHolder)")
