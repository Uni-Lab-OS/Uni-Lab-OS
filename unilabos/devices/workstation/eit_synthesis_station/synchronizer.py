"""
EIT 合成工作站物料同步系统
实现 EIT 工站与 UniLab 前端的实时物料同步与控制钩子
"""

import time
from typing import Dict, Any, List, Optional, Tuple
from unilabos.devices.workstation.workstation_base import WorkstationBase, ResourceSynchronizer, WorkflowStatus
from unilabos.utils.log import logger
from unilabos.ros.nodes.presets.workstation import ROS2WorkstationNode
from pylabrobot.resources import Resource, Container, ResourceHolder, Well
from unilabos.resources import resource_tracker, graphio
from unilabos.utils import cls_creator
import asyncio
from unilabos.ros.nodes.base_device_node import ROS2DeviceNode
import uuid

# EIT 专用依赖
from unilabos.devices.workstation.eit_synthesis_station.station_manager import StationManager
from unilabos.devices.workstation.eit_synthesis_station.config.setting import Settings
from unilabos.devices.workstation.eit_synthesis_station.config.constants import ResourceCode
from unilabos.resources.eit_synthesis_station import bottle_carriers, items
from unilabos.resources.eit_synthesis_station.decks import EIT_Synthesis_Station_Deck
from unilabos.resources.warehouse import WareHouse
from unilabos.resources.itemized_carrier import BottleCarrier

def _force_inject_eit_types():
    # 注册到 ResourceTracker (解决反序列化为 dict 的问题)
    # 注意：这里需要注册类名字符串和对应的类
    mappings = {
        "EIT_Synthesis_Station_Deck": EIT_Synthesis_Station_Deck,
        "WareHouse": WareHouse,
        "BottleCarrier": BottleCarrier,
        "ResourceHolder": ResourceHolder,
        "Container": Container,
        "Well": Well,
        "deck": EIT_Synthesis_Station_Deck,
        "warehouse": WareHouse,
        "bottle_carrier": BottleCarrier,
        "resource_holder": ResourceHolder,
        "container": Container, 
        "well": Well,
    }
    
    # 1. 注册到 unilabos 全局类创建器 (cls_creator)
    # 确保传入的是 cls 类对象，而不是字符串
    for key, cls in mappings.items():
        if hasattr(cls_creator, 'register'):
            cls_creator.register(key, cls)
        elif hasattr(cls_creator, 'CLASS_MAP'):
            cls_creator.CLASS_MAP[key] = cls
            
    # 2. 特别注意：某些版本的 UniLab 需要将 PLR 原生资源注册到特定的 ResourceTracker
    # 如果报错依然存在，可以尝试直接注入到 ResourceTracker
    try:
        from unilabos.resources import resource_tracker
        if hasattr(resource_tracker, 'ResourceTracker'):
            for key, cls in mappings.items():
                # 某些版本直接在类或实例上维护 CLASS_MAP
                resource_tracker.ResourceTracker.CLASS_MAP[key] = cls
    except Exception as e:
        logger.debug(f"跳过 ResourceTracker 直接注入: {e}")
            

_force_inject_eit_types()

class EITResourceSynchronizer(ResourceSynchronizer):
    """EIT 资源同步器：负责具体的 API 调用与数据转换"""

    # 1. 建立 ResourceCode 与载架工厂函数的映射
    CARRIER_FACTORY = {
        int(ResourceCode.REAGENT_BOTTLE_TRAY_2ML): bottle_carriers.EIT_REAGENT_BOTTLE_TRAY_2ML,
        int(ResourceCode.REAGENT_BOTTLE_TRAY_8ML): bottle_carriers.EIT_REAGENT_BOTTLE_TRAY_8ML,
        int(ResourceCode.REAGENT_BOTTLE_TRAY_40ML): bottle_carriers.EIT_REAGENT_BOTTLE_TRAY_40ML,
        int(ResourceCode.REAGENT_BOTTLE_TRAY_125ML): bottle_carriers.EIT_REAGENT_BOTTLE_TRAY_125ML,
        int(ResourceCode.POWDER_BUCKET_TRAY_30ML): bottle_carriers.EIT_POWDER_BUCKET_TRAY_30ML,
        int(ResourceCode.TIP_TRAY_1ML): bottle_carriers.EIT_TIP_TRAY_1ML,
        int(ResourceCode.TIP_TRAY_5ML): bottle_carriers.EIT_TIP_TRAY_5ML,
        int(ResourceCode.TIP_TRAY_50UL): bottle_carriers.EIT_TIP_TRAY_50UL,
        int(ResourceCode.REACTION_TUBE_TRAY_2ML): bottle_carriers.EIT_REACTION_TUBE_TRAY_2ML,
        int(ResourceCode.TEST_TUBE_MAGNET_TRAY_2ML): bottle_carriers.EIT_TEST_TUBE_MAGNET_TRAY_2ML,
        int(ResourceCode.REACTION_SEAL_CAP_TRAY): bottle_carriers.EIT_REACTION_SEAL_CAP_TRAY,
        int(ResourceCode.FLASH_FILTER_INNER_BOTTLE_TRAY): bottle_carriers.EIT_FLASH_FILTER_INNER_BOTTLE_TRAY,
        int(ResourceCode.FLASH_FILTER_OUTER_BOTTLE_TRAY): bottle_carriers.EIT_FLASH_FILTER_OUTER_BOTTLE_TRAY,
    }

    # 2. 建立托盘类型与容器物品的对应关系
    TRAY_TO_ITEM_MAP = {
        int(ResourceCode.REAGENT_BOTTLE_TRAY_2ML): items.EIT_REAGENT_BOTTLE_2ML,
        int(ResourceCode.REAGENT_BOTTLE_TRAY_8ML): items.EIT_REAGENT_BOTTLE_8ML,
        int(ResourceCode.REAGENT_BOTTLE_TRAY_40ML): items.EIT_REAGENT_BOTTLE_40ML,
        int(ResourceCode.REAGENT_BOTTLE_TRAY_125ML): items.EIT_REAGENT_BOTTLE_125ML,
        int(ResourceCode.POWDER_BUCKET_TRAY_30ML): items.EIT_POWDER_BUCKET_30ML,
        int(ResourceCode.FLASH_FILTER_INNER_BOTTLE_TRAY): items.EIT_FLASH_FILTER_INNER_BOTTLE,
        int(ResourceCode.FLASH_FILTER_OUTER_BOTTLE_TRAY): items.EIT_FLASH_FILTER_OUTER_BOTTLE,
        int(ResourceCode.REACTION_SEAL_CAP_TRAY): items.EIT_REACTION_SEAL_CAP,
        int(ResourceCode.REACTION_TUBE_TRAY_2ML): items.EIT_REACTION_TUBE_2ML,
        int(ResourceCode.TEST_TUBE_MAGNET_TRAY_2ML): items.EIT_TEST_TUBE_MAGNET_2ML,
    }

    TRAY_SPEC_STRINGS = {
        "EIT_REAGENT_BOTTLE_TRAY_2ML": "2 mL试剂瓶托盘(201000730) [A1-F8]",
        "EIT_REAGENT_BOTTLE_TRAY_8ML": "8 mL试剂瓶托盘(201000502) [A1-C4]",
        "EIT_REAGENT_BOTTLE_TRAY_40ML": "40 mL试剂瓶托盘(201000503) [A1-B3]",
        "EIT_REAGENT_BOTTLE_TRAY_125ML": "125 mL试剂瓶托盘(220000023) [A1-A2]",
        "EIT_POWDER_BUCKET_TRAY_30ML": "30 mL粉桶托盘(201000600) [A1-B1]",
        "EIT_TIP_TRAY_1ML": "1 mL Tip 头托盘(201000731) [1-96]",
        "EIT_TIP_TRAY_5ML": "5 mL Tip 头托盘(201000512) [1-24]",
        "EIT_TIP_TRAY_50UL": "50 μL Tip 头托盘(201000815) [1-96]",
        "EIT_FLASH_FILTER_OUTER_BOTTLE_TRAY": "闪滤瓶外瓶托盘(201000728) [1-48]",
        "EIT_FLASH_FILTER_INNER_BOTTLE_TRAY": "闪滤瓶内瓶托盘(201000727) [1-48]",
        "EIT_REACTION_SEAL_CAP_TRAY": "反应密封盖托盘(201000712) [1-1]",
        "EIT_TEST_TUBE_MAGNET_TRAY_2ML": "2 mL试管磁子托盘(201000711) [1-24]",
        "EIT_REACTION_TUBE_TRAY_2ML": "2 mL反应试管托盘(201000726) [1-24]",
    }

    SUBSTANCE_TRAYS = {
        "EIT_REAGENT_BOTTLE_TRAY_2ML", 
        "EIT_REAGENT_BOTTLE_TRAY_8ML", 
        "EIT_REAGENT_BOTTLE_TRAY_40ML", 
        "EIT_REAGENT_BOTTLE_TRAY_125ML", 
        "EIT_POWDER_BUCKET_TRAY_30ML"
    }

    def __init__(self, workstation: 'EITWorkstation'):
        super().__init__(workstation)
        self.manager: Optional[StationManager] = None
        self.initialize()
    
    def initialize(self) -> bool:
        """初始化 EIT Manager 并登录"""
        try:
            settings = Settings.from_env()
            self.manager = StationManager(settings=settings)
            self.manager.login()
            return True
        except Exception as e:
            logger.error(f"EIT Synchronizer 初始化失败: {e}")
            return False

    def _push_deck_to_cloud(self):
        """安全地将当前 Deck 状态推送到云端，处理 Loop 未启动的情况"""
        if self.workstation and hasattr(self.workstation, "_ros_node"):
            deck = self.workstation.deck
            # if not getattr(deck, "parent", None):
            #     deck.parent = self.workstation
            
            if hasattr(deck, "_recursive_assign_uuid"):
                deck._recursive_assign_uuid(deck)

            logger.info("正在上传 EIT Deck 到云端...")
            ROS2DeviceNode.run_async_func(
                self.workstation._ros_node.update_resource, 
                True, 
                **{"resources": [deck]}
            )

    def sync_from_external(self) -> bool:
        """[工站 -> 前端] 从 EIT 获取资源信息并更新 UniLab Deck"""
        try:
            raw_data = self.manager.get_resource_info()
            if not raw_data: return True

            hardware_items = {item.get("layout_code"): item for item in raw_data} if raw_data else {}
            occupied_codes = set(hardware_items.keys())
            
            # 记录发生变化的仓库，用于增量上传
            changed_warehouses = [] 
            # 获取是否已完成首次全量同步的标识（在 EITWorkstation 中定义）
            is_first_sync = not getattr(self.workstation, "_first_full_sync_done", False)

            # 2. 安全地获取所有仓库资源
            # 修复点：通过名称列表过滤 Deck 的子资源，避免直接访问不存在的 .warehouses 属性
            # EIT 仓库标准命名：W, N, TB, AS, FF, MS, MSB, SC, T, TS
            eit_zones = ["W", "N", "TB", "AS", "FF", "MS", "MSB", "SC", "T", "TS"]
            warehouses = [res for res in self.workstation.deck.children if res.name in eit_zones]
            
            if not warehouses and is_first_sync:
                logger.warning("Deck 上未发现 WareHouse 资源，请检查 Deck 是否正确执行了 setup() 初始化")

            # 3. 核心比对逻辑：遍历本地所有已知槽位，与硬件状态对齐
            for wh in warehouses:
                wh_changed = False
                for slot in wh.children:
                    # 将虚拟 Slot 解析为 EIT 物理编码 (如 W-1 或 W-1-1)
                    eit_code = self.workstation._resolve_eit_code_by_slot(slot)
                    if not eit_code: continue

                    # 获取本地槽位当前挂载的物料（通常是 BottleCarrier）
                    current_child = slot.children[0] if slot.children else None
                    
                    # --- 情况 A：硬件端该位点有物料 (增加或更新) ---
                    if eit_code in occupied_codes:
                        item = hardware_items[eit_code]
                        res_type = int(item.get("resource_type"))
                        
                        # 【增量优化】如果类型没变且非首次同步，跳过重建以减少前端卡顿
                        if not is_first_sync and current_child:
                            existing_type = getattr(current_child, "eit_resource_type", None)
                            if existing_type == res_type:
                                continue 

                        # 若类型不匹配或原槽位为空，清理旧物料并重建
                        if current_child:
                            slot.unassign_child_resource(current_child)
                        
                        # 根据资源类型调用对应的载架工厂函数
                        factory_func = self.CARRIER_FACTORY.get(res_type)
                        if factory_func:
                            new_carrier = factory_func(name=f"eit_tray_{eit_code}")
                        else:
                            new_carrier = Container(name=f"eit_tray_{eit_code}", size_x=127.8, size_y=85.5, size_z=20)
                            new_carrier.description = item.get("resource_type_name")
                        
                        # 注入 UUID 与同步必需的元数据
                        new_carrier.unilabos_uuid = str(uuid.uuid4())
                        new_carrier.eit_resource_type = res_type 
                        new_carrier.unilabos_extra = {
                            "eit_layout_code": eit_code,
                            "eit_resource_type": res_type
                        }

                        # 填充载架内部细节（例如试剂瓶/吸头等子物料）
                        details = item.get("substance_details", [])
                        item_factory = self.TRAY_TO_ITEM_MAP.get(res_type)
                        if item_factory and hasattr(new_carrier, 'sites'):
                            for detail in details:
                                slot_idx = detail.get("slot")
                                if slot_idx < len(new_carrier.sites):
                                    bottle = item_factory(name=f"{new_carrier.name}_well_{detail.get('well')}")
                                    bottle.unilabos_uuid = str(uuid.uuid4())
                                    new_carrier.get_item(slot_idx).assign_child_resource(bottle)
                        
                        # 将新创建的物料挂载到虚拟槽位
                        slot.assign_child_resource(new_carrier)
                        wh_changed = True

                    # --- 情况 B：硬件端该位点为空，但本地有物料 (检测到物料被移除/减少) ---
                    elif current_child:
                        logger.info(f"[同步] 检测到硬件位点 {eit_code} 为空，同步移除本地物料")
                        # 执行逻辑移除，清空本地虚拟槽位
                        slot.unassign_child_resource(current_child)
                        wh_changed = True

                # 如果该分区（Warehouse）内有任何变动，将其加入更新队列
                if wh_changed:
                    changed_warehouses.append(wh)

            # 4. 执行云端推送策略
            if is_first_sync:
                # 首次全量同步：必须发送整个 Deck 以初始化完整的仓库和槽位结构
                self._push_deck_to_cloud()
                self.workstation._first_full_sync_done = True
                logger.info("EIT 首次全量同步上报完成")
            elif changed_warehouses:
                # 增量推送：仅发送发生变动的仓库对象，有效解决减少物料同步并减轻前端性能压力
                logger.info(f"检测到 {len(changed_warehouses)} 个分区变动，执行增量更新上报")
                ROS2DeviceNode.run_async_func(
                    self.workstation._ros_node.update_resource, 
                    True, 
                    **{"resources": changed_warehouses}
                )
            
            return True
        except Exception as e:
            logger.error(f"同步 EIT 硬件状态异常: {e}")
            return False
        
    def batch_in_tray(self, resource: Resource, target_eit_code: str) -> bool:
        """[UniLab -> 工站] 物理上料动作"""    
        try:
            tray_type = resource.description or "2 mL试剂瓶托盘"
            rows = [(target_eit_code, tray_type, "")] 
            payload = self.manager.build_batch_in_tray_payload(rows)
            resp = self.manager.batch_in_tray(payload)
            return resp is not None
        except Exception as e:
            logger.error(f"执行物理上料失败: {e}")
            return False

    def sync_to_external(self, resource: Resource) -> bool:
        """
        [虚拟 -> 硬件] 核心上料同步逻辑
        将 UniLab 中的拖拽动作转换为 EIT 硬件识别的 Payload，支持物质托盘详情填充。
        """
        # --- 0. 防抖处理：防止 Transfer 和 Add 钩子同时触发导致的 1134 冲突 ---
        now = time.time()
        if hasattr(resource, "_last_sync_time") and (now - resource._last_sync_time < 2.0):
            return True # 2秒内不重复同步同一资源
        resource._last_sync_time = now

        # --- 1. 坐标转换：将虚拟 Slot 解析为物理坐标 (例如 W-30 -> W-4-7) ---
        eit_code = getattr(resource, "unilabos_extra", {}).get("eit_layout_code")
        if not eit_code and resource.parent:
            eit_code = self.workstation._resolve_eit_code_by_slot(resource.parent)
        
        if not eit_code:
            logger.error(f"无法解析资源 {resource.name} 的物理坐标")
            return False

        # --- 2. 获取硬件规格字符串 (处理 BottleCarrier 类名不匹配问题) ---
        # 优先通过资源名称匹配，因为拖拽物料的 .name 通常是具体的类型
        tray_type_text = self.TRAY_SPEC_STRINGS.get(resource.name) or \
                         self.TRAY_SPEC_STRINGS.get(resource.__class__.__name__)
        
        # 兜底：如果映射表未命中，尝试从 description 属性安全获取
        if not tray_type_text:
            tray_type_text = getattr(resource, "description", None)
            if not tray_type_text or "(" not in tray_type_text:
                logger.error(f"不支持的托盘类型: {resource.name}，请在 TRAY_SPEC_STRINGS 中定义")
                return False

        # --- 3. 构造内容字符串 (Content) ---
        content_parts = []
        # 判断是否为物质类托盘 (试剂瓶/粉桶)
        is_substance = any(kw in resource.name or kw in resource.__class__.__name__ 
                          for kw in ["REAGENT", "POWDER", "BOTTLE"])

        if is_substance and hasattr(resource, "children"):
            # 遍历托盘下的每一个槽位 (ResourceHolder)
            for site in resource.children:
                if site.children: # 只有槽位里有瓶子时处理
                    bottle = site.children[0]
                    # 解析槽位名称中的坐标 (假设格式为 resourceholder_行_列)
                    try:
                        # 动态根据托盘类型确定列数，用于计算 A1/B2 坐标
                        cols = 8 # 默认 8 列
                        if "8 mL" in tray_type_text: cols = 4
                        elif "40 mL" in tray_type_text: cols = 3
                        elif "30 mL" in tray_type_text: cols = 2
                        
                        idx = resource.children.index(site)
                        well_coord = f"{chr(65 + (idx // cols))}{ (idx % cols) + 1 }"
                    except Exception:
                        well_coord = site.name 

                    sub_name = bottle.name
                    # 安全获取数值：优先取元数据 value，若无则取 description，最后默认为 2mL
                    val = getattr(bottle, "unilabos_extra", {}).get("value") or \
                          getattr(bottle, "description", "2mL")
                    
                    content_parts.append(f"{well_coord}|{sub_name}|{val}")
            
            content_text = "; ".join(content_parts)
        else:
            # 非物质耗材类托盘 (吸头/反应管)：从规格中提取最大容量，例如 [1-96] 提取 96
            import re
            match = re.search(r"-(\d+)\]", tray_type_text)
            content_text = match.group(1) if match else "96"

        # --- 4. 调用控制器执行同步 ---
        logger.info(f"同步至硬件: {resource.name} -> {eit_code} | 类型: {tray_type_text} | 内容: {content_text}")
        rows = [(eit_code, tray_type_text, content_text)] 
        payload = self.manager.build_batch_in_tray_payload(rows)
        
        if not payload:
            logger.error(f"Payload 构建失败，硬件无法识别类型: {tray_type_text}")
            return False
            
        return self.manager.batch_in_tray(payload) is not None
       
    def handle_external_change(self, change_info: Dict[str, Any]) -> bool:
        """
        [Physical -> Virtual] 处理外部硬件触发的变更（如手动搬运托盘）。
        参考 Bioyond 模式：记录日志并触发强制同步。
        """
        logger.info(f"处理 EIT 外部变更通知: {change_info}")
        # 触发全量状态更新以确保前端一致性
        return self.sync_from_external()

class EITWorkstation(WorkstationBase):
    """EIT 工作站核心类：集成资源树钩子与状态监控"""

    def __init__(
            self, 
            config: Optional[Dict] = None, 
            deck: Optional[Any] = None, 
            **kwargs):
        super().__init__(deck=deck, **kwargs)
        self.name = getattr(self, "device_id", "eit_station") 
        self.unilabos_uuid = getattr(self, "uuid", None)
        self.config = config or {}
        self.resource_synchronizer = EITResourceSynchronizer(self)
        self.manager = self.resource_synchronizer.manager

    def batch_in_from_file(self, file_path: str = "unilabos/devices/workstation/eit_synthesis_station/batch_in_tray.xlsx"):
        """批量进料接口，支持指定文件路径"""
        return self.manager.batch_in_tray_by_file(file_path)
   
    def post_init(self, ros_node: ROS2WorkstationNode):
        """初始化后上传 Deck 资源树"""
        self._ros_node = ros_node
        # 首次同步工站状态
        self.resource_synchronizer.sync_from_external()
        self._ros_node.create_timer(30.0, self.resource_synchronizer.sync_from_external)
        logger.info(f"EIT 工作站 {ros_node.device_id} 定时同步任务已通过 ROS Timer 启动")

    @property
    def station_status(self) -> Dict[str, Any]:
        """[状态上报] 对接底层控制器获取工站环境数据"""
        try:
            env = self.manager.get_glovebox_env() #
            state = self.manager.station_state() #
            return {
                "connected": True,
                "station_state": state,
                "o2_ppm": env.get("oxygen_content"),
                "h2o_ppm": env.get("water_content"),
                "pressure_pa": env.get("box_pressure")
            }
        except:
            return {"connected": False}

    # ================= 资源树操作钩子 =================
    
    def resource_tree_add(self, resources: List[Resource]):
        """处理前端物料添加请求（上料）"""
        for res in resources:
            # 同样增加判断，避免与 Transfer 重复触发
            if not getattr(res, "_last_sync_time", 0):
                self.resource_synchronizer.sync_to_external(res)

    def resource_tree_remove(self, resources: List[Resource]):
        """用户在前端删除物料时触发：执行物理下料"""
        top_level_names = {res.name for res in resources}
        processed_codes = set()

        for res in resources:
            eit_code = getattr(res, "unilabos_extra", {}).get("eit_layout_code")
            if not eit_code and res.parent:
                eit_code = self._resolve_eit_code_by_slot(res.parent)
            if eit_code and eit_code not in processed_codes:
                # 只有当该资源是顶层或其父节点不在删除列表中时才处理
                parent_resource = res.parent
                if parent_resource and parent_resource.name not in top_level_names:
                    logger.info(f"[EIT] 真正触发硬件下料动作: {eit_code}")
                    # 调用 API，move_type 默认为 0（普通移动/下料）
                    self.manager.batch_out_tray([eit_code])
                    processed_codes.add(eit_code)


    def resource_tree_transfer(self, old_parent: Optional[Resource], resource: Resource, new_parent: Resource):
        """处理跨设备拖入：例如从物料库拖到工站槽位"""
        logger.info(f"[Transfer] 资源 {resource.name} 移入新槽位: {new_parent.name}")
        if old_parent and not self.is_descendant_of_me(old_parent):
            self.resource_synchronizer.sync_to_external(resource)

    def resource_tree_update(self, resources: List[Resource]):
        """处理前端拖拽或修改属性后的同步"""
        for res in resources:
            if res.parent:
                new_eit_code = self._resolve_eit_code_by_slot(res.parent)
                old_eit_code = getattr(res, "unilabos_extra", {}).get("eit_layout_code")
                
                if new_eit_code != old_eit_code:
                    logger.info(f"物料位置变更: {old_eit_code} -> {new_eit_code}")
                    # 1. 更新内部物理编码
                    if not hasattr(res, "unilabos_extra"): res.unilabos_extra = {}
                    res.unilabos_extra["eit_layout_code"] = new_eit_code
                    # 2. 同步硬件动作
                    self.resource_synchronizer.sync_to_external(res)

    def batch_in_from_file(self, file_path: str) -> Dict[str, Any]:
        """
        处理来自前端的批量进料请求
        
        Args:
            file_path: 上传到服务器的进料表格路径 (.csv 或 .xlsx)
        """
        logger.info(f"收到前端批量进料请求，正在处理文件: {file_path}")
        
        try:
            # 1. 直接调用 StationManager 已有的文件处理逻辑
            # 该方法会自动解析 CSV/Excel 并构建 Payload 发送给 API
            result = self.manager.batch_in_tray_by_file(file_path)
            
            # 2. 操作完成后，触发一次全量同步，更新前端物料显示
            if result:
                logger.info("物理进料 API 调用成功，正在更新云端物料树...")
                self.resource_synchronizer.sync_from_external()
                # 重新上传 Deck 状态，确保前端 3D 视图刷新
                self.resource_synchronizer._push_deck_to_cloud()
                
            return {"success": True, "result": result}
        except Exception as e:
            logger.error(f"批量进料失败: {e}")
            return {"success": False, "error": str(e)}

    # ================= 动态坐标转换逻辑 =================

    def _resolve_eit_code_by_slot(self, slot: Resource) -> str:
        """[UniLab -> EIT] 将虚拟 Slot 反向解析为物理坐标字符串"""
        try:
            wh = slot.parent
            if not wh or wh.name == "EIT_Synthesis_Station_Deck": 
                # 如果解析到了 Deck 层级，说明槽位挂载层级有问题
                # 或者该 slot 直接挂在了 Deck 上
                return ""
            
            zone_name = wh.name
            idx = wh.children.index(slot)
            num_cols = getattr(wh, "num_items_x", 1) #
            num_rows = getattr(wh, "num_items_y", 1) #

            # 根据行数动态决定输出格式：多行则输出三段式，单行则输出两段式
            if num_rows > 1:
                row = (idx // num_cols) + 1
                col = (idx % num_cols) + 1
                return f"{zone_name}-{row}-{col}"
            else:
                return f"{zone_name}-{idx + 1}"
        except:
            return ""
        
    def _resolve_slot_by_eit_code(self, eit_code: str) -> Optional[Resource]:
        """使用 A01 命名规则精确定位 Slot"""
        try:
            
            if not eit_code or '-' not in eit_code: return None
            parts = eit_code.split('-')
            zone_key = parts[0]
            
            wh = self.deck.get_resource(zone_key)
            if not wh: return None

            num_cols = getattr(wh, "num_items_x", 1)
            LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

            # 计算行列索引
            if len(parts) == 2:
                idx = int(parts[1]) - 1
                row = idx // num_cols
                col = idx % num_cols
            elif len(parts) == 3:
                row = int(parts[1]) - 1
                col = int(parts[2]) - 1
            else:
                return None

            # 生成符合 warehouse_factory 规则的 A01 风格键值
            target_key = f"{LETTERS[row]}{col + 1:02d}"
            return wh.get_item(target_key)
            
        except Exception as e:
            logger.debug(f"坐标映射失败 {eit_code}: {e}")
            return None