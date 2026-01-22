"""
EIT 合成工作站物料同步系统
实现 EIT 工站与 UniLab 前端的实时物料同步与控制钩子
"""

import time
import threading
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
from unilabos.devices.eit_synthesis_station.manager.station_manager import SynthesisStationManager
from unilabos.devices.eit_synthesis_station.config.setting import Settings
from unilabos.devices.eit_synthesis_station.config.constants import ResourceCode
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

class EITSynthesisResourceSynchronizer(ResourceSynchronizer):
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

    def __init__(self, workstation: 'EITSynthesisWorkstation'):
        super().__init__(workstation)
        self.manager: Optional[SynthesisStationManager] = None
        self.initialize()
    
    def initialize(self) -> bool:
        """初始化 EIT Manager 并登录"""
        try:
            settings = Settings.from_env()
            self.manager = SynthesisStationManager(settings=settings)
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

            # 保留原始 layout_code 作为唯一索引
            hardware_items = {item.get("layout_code"): item for item in raw_data} if raw_data else {}
            occupied_codes = set(hardware_items.keys())
            
            # 记录发生变化的仓库，用于增量上传
            changed_warehouses = [] 
            # 获取是否已完成首次全量同步的标识（在 EITSynthesisWorkstation 中定义）
            is_first_sync = not getattr(self.workstation, "_first_full_sync_done", False)

            # 2. 安全地获取所有仓库资源
            # 修复点：通过名称列表过滤 Deck 的子资源，避免直接访问不存在的 .warehouses 属性
            # EIT 仓库标准命名：W, N, TB, AS, FF, MS, MSB, SC, T, TS
            eit_zones = ["W", "N", "TB", "AS", "FF", "MS", "MSB", "SC", "T", "TS"]
            warehouses = [res for res in self.workstation.deck.children if res.name in eit_zones]
            
            if not warehouses and is_first_sync:
                logger.warning("Deck 上未发现 WareHouse 资源，请检查 Deck 是否正确执行了 setup() 初始化")

            # 3. 核心比对逻辑：以 EIT 原始 layout_code 为准定位槽位
            #    这样前端展示的 layout_code 与 EIT 保持一致（两段式/三段式混用）
            occupied_slots = {}
            for eit_code in occupied_codes:
                slot = self.workstation._resolve_slot_by_eit_code(eit_code)
                if slot:
                    occupied_slots[slot] = eit_code

            for wh in warehouses:
                wh_changed = False
                for slot in wh.children:
                    # 使用 EIT 原始 layout_code 匹配槽位
                    eit_code = occupied_slots.get(slot)
                    current_child = slot.children[0] if slot.children else None
                    
                    # --- 情况 A：硬件端该位点有物料 (增加或更新) ---
                    if eit_code:
                        item = hardware_items[eit_code]
                        res_type = int(item.get("resource_type"))
                        details = item.get("substance_details", [])
                        tray_display_name = item.get("resource_type_name") or "EIT Tray"
                        desired_tray_name = f"{tray_display_name}@{eit_code}"
                        
                        # 【增量优化】如果类型没变且非首次同步，跳过重建以减少前端卡顿
                        if not is_first_sync and current_child:
                            existing_type = getattr(current_child, "eit_resource_type", None)
                            if existing_type == res_type:
                                names_match = current_child.name == desired_tray_name
                                if names_match and details and hasattr(current_child, "sites"):
                                    for detail in details:
                                        slot_idx = detail.get("slot")
                                        if slot_idx is None or slot_idx >= len(current_child.sites):
                                            continue
                                        well_name = detail.get("well") or f"slot_{slot_idx + 1}"
                                        substance_name = detail.get("substance") or well_name
                                        desired_bottle_name = f"{substance_name}@{well_name}"
                                        site = current_child.get_item(slot_idx)
                                        child = site.children[0] if site and site.children else None
                                        if not child or child.name != desired_bottle_name:
                                            names_match = False
                                            break
                                if names_match:
                                    # 保留已有的 eit_layout_code，避免覆盖原始格式
                                    if not getattr(current_child, "unilabos_extra", None):
                                        current_child.unilabos_extra = {}
                                    current_child.unilabos_extra["eit_layout_code"] = eit_code
                                    current_child.unilabos_extra["eit_resource_type"] = res_type
                                    continue 

                        # 若类型不匹配或原槽位为空，清理旧物料并重建
                        if current_child:
                            slot.unassign_child_resource(current_child)
                        
                        # 根据资源类型调用对应的载架工厂函数
                        factory_func = self.CARRIER_FACTORY.get(res_type)
                        if factory_func:
                            new_carrier = factory_func(name=desired_tray_name)
                        else:
                            new_carrier = Container(name=desired_tray_name, size_x=127.8, size_y=85.5, size_z=20)
                            new_carrier.description = item.get("resource_type_name")
                        
                        # 注入 UUID 与同步必需的元数据
                        new_carrier.unilabos_uuid = str(uuid.uuid4())
                        new_carrier.eit_resource_type = res_type 
                        new_carrier.unilabos_extra = {
                            "eit_layout_code": eit_code,
                            "eit_resource_type": res_type
                        }
                        new_carrier.description = tray_display_name

                        # 填充载架内部细节（例如试剂瓶/吸头等子物料）
                        item_factory = self.TRAY_TO_ITEM_MAP.get(res_type)
                        if item_factory and hasattr(new_carrier, 'sites'):
                            for detail in details:
                                slot_idx = detail.get("slot")
                                if slot_idx < len(new_carrier.sites):
                                    well_name = detail.get("well") or f"slot_{slot_idx + 1}"
                                    substance_name = detail.get("substance") or well_name
                                    bottle = item_factory(name=f"{substance_name}@{well_name}")
                                    bottle.unilabos_uuid = str(uuid.uuid4())
                                    bottle.description = substance_name
                                    new_carrier.get_item(slot_idx).assign_child_resource(bottle)
                        
                        # 将新创建的物料挂载到虚拟槽位
                        slot.assign_child_resource(new_carrier)
                        wh_changed = True

                    # --- 情况 B：硬件端该位点为空，但本地有物料 (检测到物料被移除/减少) ---
                    elif current_child:
                        logger.info(f"[同步] 检测到硬件位点为空，同步移除本地物料")
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
        """[虚拟 -> 硬件] 将本地操作同步到物理硬件"""
        # 获取物理编码
        eit_code = getattr(resource, "unilabos_extra", {}).get("eit_layout_code")
        if not eit_code and resource.parent:
            eit_code = self.workstation._resolve_eit_code_by_slot(resource.parent)
        
        if eit_code:
            logger.info(f"同步至硬件: {resource.name} -> {eit_code}")
            tray_type = getattr(resource, "description", None) or "2 mL试剂瓶托盘"
            rows = [(eit_code, tray_type, "")] 
            payload = self.manager.build_batch_in_tray_payload(rows)
            # return self.manager.batch_in_tray(payload) is not None
        return False

    def handle_external_change(self, change_info: Dict[str, Any]) -> bool:
        """
        [Physical -> Virtual] 处理外部硬件触发的变更（如手动搬运托盘）。
        参考 Bioyond 模式：记录日志并触发强制同步。
        """
        logger.info(f"处理 EIT 外部变更通知: {change_info}")
        # 触发全量状态更新以确保前端一致性
        return self.sync_from_external()

class EITSynthesisWorkstation(WorkstationBase):
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
        self.resource_synchronizer = EITSynthesisResourceSynchronizer(self)
        self.manager = self.resource_synchronizer.manager

 
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

    def _get_eit_layout_code(self, res: Resource) -> str:
        """优先使用物料自带的 layout_code，缺失时再回退到槽位反解"""
        eit_code = getattr(res, "unilabos_extra", {}).get("eit_layout_code")
        if eit_code:
            return eit_code
        if res.parent:
            return self._resolve_eit_code_by_slot(res.parent)
        return ""
    
    def resource_tree_add(self, resources: List[Resource]):
        """处理前端物料添加请求（上料）"""
        for res in resources:
            if res.parent:
                eit_code = self._get_eit_layout_code(res)
                if eit_code:
                    if not hasattr(res, "unilabos_extra"): res.unilabos_extra = {}
                    res.unilabos_extra["eit_layout_code"] = eit_code
                    self.resource_synchronizer.sync_to_external(res)

    def resource_tree_remove(self, resources: List[Resource]):
        """用户在前端删除物料时触发：执行物理下料"""
        top_level_names = {res.name for res in resources}
        processed_codes = set()
        layout_list = []

        for res in resources:
            eit_code = self._get_eit_layout_code(res)
            if eit_code and eit_code not in processed_codes:
                # 只有当该资源是顶层或其父节点不在删除列表中时才处理
                parent_resource = res.parent
                if parent_resource and parent_resource.name not in top_level_names:
                    logger.info(f"[EIT] 真正触发硬件下料动作: {eit_code}")
                    layout_list.append({"layout_code": eit_code})
                    processed_codes.add(eit_code)

        if layout_list:
            # 下料接口会阻塞等待设备空闲，需异步执行避免阻塞资源树回调导致超时
            def _run_batch_out():
                try:
                    self.manager.batch_out_tray(layout_list)
                except Exception as e:
                    logger.error(f"[EIT] batch_out_tray 异步执行失败: {e}")

            threading.Thread(target=_run_batch_out, daemon=True).start()

    def resource_tree_transfer(self, old_parent: Optional[Resource], resource: Resource, new_parent: Resource):
        """处理跨设备拖入：例如从物料库拖到工站槽位"""
        logger.info(f"[Transfer] 资源 {resource.name} 移入工站")
        # 可以在此处执行预登记逻辑，或者直接调用 add 逻辑
        self.resource_tree_add([resource])
        
    def resource_tree_update(self, resources: List[Resource]):
        """处理前端拖拽或修改属性后的同步"""
        for res in resources:
            if res.parent:
                new_eit_code = self._get_eit_layout_code(res)
                old_eit_code = getattr(res, "unilabos_extra", {}).get("eit_layout_code")
                
                if new_eit_code != old_eit_code:
                    logger.info(f"物料位置变更: {old_eit_code} -> {new_eit_code}")
                    # 1. 更新内部物理编码
                    if not hasattr(res, "unilabos_extra"): res.unilabos_extra = {}
                    res.unilabos_extra["eit_layout_code"] = new_eit_code
                    # 2. 同步硬件动作
                    self.resource_synchronizer.sync_to_external(res)


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
