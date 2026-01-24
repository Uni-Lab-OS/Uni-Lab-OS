"""
EIT 合成工作站物料同步系统
实现 EIT 工站与 UniLab 前端的实时物料同步与控制钩子
"""

import threading
import re
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from typing import Dict, Any, List, Optional, Tuple
from unilabos.devices.workstation.workstation_base import WorkstationBase, ResourceSynchronizer
from unilabos.utils.log import logger
from unilabos.ros.nodes.presets.workstation import ROS2WorkstationNode
from pylabrobot.resources import Resource, Container, ResourceHolder, Well
from unilabos.resources import resource_tracker
from unilabos.utils import cls_creator
from unilabos.ros.nodes.base_device_node import ROS2DeviceNode
import uuid
from unilabos.devices.eit_synthesis_station.manager.station_manager import SynthesisStationManager
from unilabos.devices.eit_synthesis_station.config.setting import Settings
from unilabos.devices.eit_synthesis_station.config.constants import ResourceCode, TRAY_CODE_DISPLAY_NAME, TraySpec
from unilabos.resources.eit_synthesis_station import bottle_carriers, items
from unilabos.resources.eit_synthesis_station.decks import EIT_Synthesis_Station_Deck
from unilabos.resources.warehouse import WareHouse
from unilabos.resources.itemized_carrier import BottleCarrier

def normalize_layout_code(eit_code: Optional[str]) -> Optional[str]:
    if not eit_code or "-" not in eit_code:
        return eit_code
    parts = eit_code.split("-")
    if len(parts) < 2:
        return eit_code
    normalized = [parts[0]]
    for part in parts[1:]:
        if part.isdigit():
            normalized.append(str(int(part)))
        else:
            normalized.append(part)
    return "-".join(normalized)

_EIT_TYPE_MAPPINGS = {
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

for key, cls in _EIT_TYPE_MAPPINGS.items():
    if hasattr(cls_creator, "register"):
        cls_creator.register(key, cls)
    elif hasattr(cls_creator, "CLASS_MAP"):
        cls_creator.CLASS_MAP[key] = cls

try:
    from unilabos.resources import resource_tracker as _resource_tracker
    if hasattr(_resource_tracker, "ResourceTracker"):
        for key, cls in _EIT_TYPE_MAPPINGS.items():
            _resource_tracker.ResourceTracker.CLASS_MAP[key] = cls
except Exception as e:
    logger.debug(f"跳过 ResourceTracker 直接注入: {e}")

class EITSynthesisResourceSynchronizer(ResourceSynchronizer):
    """EIT 资源同步器"""

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
        self._chemical_name_map: Optional[Dict[str, str]] = None
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

    def sync_from_external(self) -> bool:
        """[工站 -> 前端] 从 EIT 获取资源信息并更新 UniLab Deck"""
        try:
            raw_data = self.manager.get_resource_info()
            if not raw_data: return True

            hardware_items = {}
            if raw_data:
                for item in raw_data:
                    raw_code = item.get("layout_code")
                    norm_code = normalize_layout_code(raw_code)
                    if norm_code:
                        item["layout_code"] = norm_code
                        hardware_items[norm_code] = item
            occupied_codes = set(hardware_items.keys())
            
            # 记录发生变化的仓库，用于增量上传
            changed_warehouses = [] 
            # 获取是否已完成首次全量同步的标识（在 EITSynthesisWorkstation 中定义）
            is_first_sync = not getattr(self.workstation, "_first_full_sync_done", False)

            # 2. 安全地获取所有仓库资源
            eit_zones = ["W", "N", "TB", "AS", "FF", "MS", "MSB", "SC", "T", "TS"]
            warehouses = [res for res in self.workstation.deck.children if res.name in eit_zones]
            
            if not warehouses and is_first_sync:
                logger.warning("Deck 上未发现 WareHouse 资源，请检查 Deck 是否正确执行了 setup() 初始化")

            # 3. 核心比对逻辑：以 EIT 原始 layout_code 为准定位槽位
            slot_index: Dict[str, ResourceHolder] = {}
            for wh in warehouses:
                for slot in wh.children:
                    if not isinstance(slot, ResourceHolder):
                        continue
                    slot_name = normalize_layout_code(getattr(slot, "name", None))
                    if slot_name:
                        slot_index[slot_name] = slot
            missing_codes: List[str] = []
            for eit_code in occupied_codes:
                if eit_code not in slot_index:
                    missing_codes.append(eit_code)
            if missing_codes:
                sample = missing_codes[:10]
                logger.warning(
                    f"layout_code 未匹配到仓库槽位: {sample} (total={len(missing_codes)})"
                )
                missing_zones = sorted({code.split("-")[0] for code in missing_codes if "-" in code})
                for zone in missing_zones:
                    wh = self.workstation.deck.get_resource(zone)
                    if wh and hasattr(wh, "_ordering"):
                        keys = list(getattr(wh, "_ordering", {}).keys())
                        logger.warning(f"{zone} 仓库现有槽位示例: {keys[:10]}")

            for wh in warehouses:
                wh_changed = False
                for slot in wh.children:
                    if not isinstance(slot, ResourceHolder):
                        continue
                    slot_name = normalize_layout_code(getattr(slot, "name", None))
                    if not slot_name:
                        continue
                    eit_code = slot_name if slot_name in hardware_items else None
                    current_child = slot.children[0] if slot.children else None
                    
                    # --- 情况 A：硬件端该位点有物料 (增加或更新) ---
                    if eit_code:
                        item = hardware_items[eit_code]
                        res_type = int(item.get("resource_type"))
                        details = item.get("substance_details", [])
                        tray_display_name = item.get("resource_type_name") or "EIT Tray"
                        desired_tray_name = f"{tray_display_name}@{eit_code}"
                        
                        # 同类型托盘只做 rename/update，不因名称不同而重建
                        if current_child:
                            existing_type = getattr(current_child, "eit_resource_type", None)
                            if existing_type == res_type:
                                if not getattr(current_child, "unilabos_extra", None):
                                    current_child.unilabos_extra = {}
                                current_child.unilabos_extra["eit_layout_code"] = eit_code
                                current_child.unilabos_extra["eit_resource_type"] = res_type
                                if current_child.name != desired_tray_name:
                                    current_child.name = desired_tray_name
                                if details and hasattr(current_child, "sites"):
                                    item_factory = self.TRAY_TO_ITEM_MAP.get(res_type)
                                    for detail in details:
                                        slot_idx = detail.get("slot")
                                        if slot_idx is None or slot_idx >= len(current_child.sites):
                                            continue
                                        well_name = detail.get("well") or f"slot_{slot_idx + 1}"
                                        substance_name = detail.get("substance") or well_name
                                        desired_bottle_name = f"{substance_name}@{well_name}"
                                        site = current_child.get_item(slot_idx)
                                        if isinstance(site, ResourceHolder):
                                            child = site.children[0] if site.children else None
                                        else:
                                            child = site
                                        if child is None and item_factory:
                                            bottle = item_factory(name=desired_bottle_name)
                                            bottle.unilabos_uuid = str(uuid.uuid4())
                                            bottle.description = substance_name
                                            current_child[slot_idx] = bottle
                                        elif child and getattr(child, "name", None) != desired_bottle_name:
                                            child.name = desired_bottle_name
                                            child.description = substance_name
                                continue

                        # 若类型不匹配或原槽位为空，清理旧物料并重建
                        if current_child:
                            slot.unassign_child_resource(current_child)
                        
                        # 根据资源类型调用对应的载架工厂函数
                        factory_func = self.CARRIER_FACTORY.get(res_type)
                        if factory_func:
                            try:
                                new_carrier = factory_func(name=desired_tray_name, prefill_items=False)
                            except TypeError:
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
                                    new_carrier[slot_idx] = bottle
                        
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
                if self.workstation and hasattr(self.workstation, "_ros_node"):
                    deck = self.workstation.deck
                    if hasattr(deck, "_recursive_assign_uuid"):
                        deck._recursive_assign_uuid(deck)
                    logger.info("正在上传 EIT Deck 到云端...")
                    self._update_resource_flattened([deck])
                self.workstation._first_full_sync_done = True
                logger.info("EIT 首次全量同步上报完成")
            elif changed_warehouses:
                # 增量推送：仅发送发生变动的仓库对象，有效解决减少物料同步并减轻前端性能压力
                logger.info(f"检测到 {len(changed_warehouses)} 个分区变动，执行增量更新上报")
                self._update_resource_flattened(changed_warehouses)
            
            return True
        except Exception as e:
            logger.error(f"同步 EIT 硬件状态异常: {e}")
            return False
        
    def _update_resource_flattened(self, resources: List[Resource]) -> None:
        """上传前扁平化资源树，移除仓库/托盘内的站位节点。"""
        tree_set = resource_tracker.ResourceTreeSet.from_plr_resources(resources)
        tree_dump = tree_set.dump()
        flattened_trees: List[List[Dict[str, Any]]] = []
        for tree_nodes in tree_dump:
            nodes_by_uuid = {node.get("uuid"): node for node in tree_nodes if node.get("uuid")}
            children_map: Dict[Optional[str], List[str]] = {}
            for node in tree_nodes:
                node_uuid = node.get("uuid")
                if not node_uuid:
                    continue
                parent_uuid = node.get("parent_uuid")
                children_map.setdefault(parent_uuid, []).append(node_uuid)

            for node_uuid, node in list(nodes_by_uuid.items()):
                if node.get("type") != "resource_holder":
                    continue
                parent_uuid = node.get("parent_uuid")
                parent = nodes_by_uuid.get(parent_uuid)
                if not parent or parent.get("type") not in {"warehouse", "bottle_carrier"}:
                    continue
                parent_uuid = node.get("parent_uuid")
                slot_label = node.get("name") or node.get("id")
                reparented_children: List[Dict[str, Any]] = []
                for child_uuid in children_map.get(node_uuid, []):
                    child = nodes_by_uuid.get(child_uuid)
                    if not child:
                        continue
                    child["parent_uuid"] = parent_uuid
                    child_pose = child.get("pose", {})
                    offset_pose = node.get("pose", {})
                    for key in ("position", "position3d"):
                        child_pos = child_pose.get(key)
                        offset_pos = offset_pose.get(key)
                        if not isinstance(child_pos, dict) or not isinstance(offset_pos, dict):
                            continue
                        child_pos["x"] = float(child_pos.get("x", 0.0)) + float(offset_pos.get("x", 0.0))
                        child_pos["y"] = float(child_pos.get("y", 0.0)) + float(offset_pos.get("y", 0.0))
                        child_pos["z"] = float(child_pos.get("z", 0.0)) + float(offset_pos.get("z", 0.0))
                    children_map.setdefault(parent_uuid, []).append(child_uuid)
                    reparented_children.append(child)
                if parent_uuid in children_map:
                    children_map[parent_uuid] = [c for c in children_map[parent_uuid] if c != node_uuid]
                parent_node = nodes_by_uuid.get(parent_uuid) if parent_uuid else None
                if parent_node and slot_label:
                    child_name = reparented_children[0].get("name") if reparented_children else None
                    config = parent_node.get("config")
                    if isinstance(config, dict):
                        sites = config.get("sites")
                        if isinstance(sites, list):
                            label_candidates = [slot_label]
                            layout_label = None
                            if slot_label and "-" not in slot_label:
                                row_letter = slot_label[:1]
                                col_str = slot_label[1:]
                                if row_letter.isalpha() and col_str.isdigit():
                                    try:
                                        row = "ABCDEFGHIJKLMNOPQRSTUVWXYZ".index(row_letter.upper()) + 1
                                        col = int(col_str)
                                    except ValueError:
                                        row = None
                                        col = None
                                    if row is not None and col is not None:
                                        parent_name = parent_node.get("name") or parent_node.get("id")
                                        if parent_name:
                                            num_items_y = config.get("num_items_y")
                                            if num_items_y == 1:
                                                layout_label = f"{parent_name}-{col}"
                                            else:
                                                layout_label = f"{parent_name}-{row}-{col}"
                            if layout_label and layout_label not in label_candidates:
                                label_candidates.append(layout_label)
                            for site in sites:
                                if site.get("label") in label_candidates:
                                    site["occupied_by"] = child_name
                                    break
                nodes_by_uuid.pop(node_uuid, None)
            flattened_trees.append(list(nodes_by_uuid.values()))
        ROS2DeviceNode.run_async_func(
            self.workstation._ros_node.update_resource,
            True,
            **{"resources": resources, "resource_tree_dump": flattened_trees},
        )

    def sync_to_external(self, resource: Resource) -> bool:
        """[虚拟 -> 硬件] 组装 BatchInTray payload，补齐化学品与单位后同步。"""
        def _load_parameters(*sources: Any) -> Dict[str, Any]:
            for src in sources:
                if not isinstance(src, dict):
                    continue
                for key in ("Parameters", "parameters", "params"):
                    if key not in src:
                        continue
                    val = src.get(key)
                    if isinstance(val, dict):
                        return val
                    if isinstance(val, str):
                        text = val.strip()
                        if text in ("", "{}", "null", "None"):
                            continue
                        try:
                            parsed = json.loads(text)
                        except Exception:
                            continue
                        if isinstance(parsed, dict):
                            return parsed
            return {}

        def _get_liquids_from_source(source: Any) -> Optional[List[Any]]:
            if source is None:
                return None
            if isinstance(source, dict):
                liquids = source.get("liquids") or source.get("pending_liquids")
                if isinstance(liquids, list):
                    return liquids
                return None
            liquids = getattr(source, "liquids", None) or getattr(source, "pending_liquids", None)
            if isinstance(liquids, list):
                return liquids
            return None

        def _resolve_slot_label_from_name(name: Optional[str]) -> Optional[str]:
            if not name:
                return None
            name = str(name).strip()
            if "@" in name:
                left, right = [part.strip() for part in name.split("@", 1)]
                if re.match(r"^[A-Za-z]+\d+$", right):
                    return right
                if re.match(r"^[A-Za-z]+\d+$", left):
                    return left
            if re.match(r"^[A-Za-z]+\d+$", name):
                return name
            return None

        def _extract_substance(
            bottle: Resource,
            slot_label: Optional[str],
            extra_sources: Optional[List[Dict[str, Any]]] = None,
        ) -> Optional[str]:
            extra = getattr(bottle, "unilabos_extra", {}) or {}
            state = getattr(bottle, "_unilabos_state", {}) or {}
            data = getattr(bottle, "data", {}) or {}
            if not isinstance(extra, dict):
                extra = {}
            if not isinstance(state, dict):
                state = {}
            if not isinstance(data, dict):
                data = {}
            sources: List[Dict[str, Any]] = [extra, state, data]
            if extra_sources:
                sources.extend([src for src in extra_sources if isinstance(src, dict)])
            params = _load_parameters(*sources)
            sources = [extra, params, state, data] + (extra_sources or [])
            for source in sources:
                for key in ("substance", "material_name", "chemical_name", "material", "name"):
                    value = source.get(key)
                    if value:
                        return str(value).strip()
            liquid_sources: List[Any] = [data, state, params] + (extra_sources or [])
            tracker = getattr(bottle, "tracker", None)
            if tracker is not None:
                liquid_sources.append(tracker)
            for source in liquid_sources:
                liquids = _get_liquids_from_source(source)
                if not isinstance(liquids, list) or not liquids:
                    continue
                first = liquids[0]
                if isinstance(first, (list, tuple)) and first:
                    name = str(first[0]).strip()
                    if name:
                        return name
                if isinstance(first, dict):
                    for key in ("substance", "name", "material", "chemical_name"):
                        value = first.get(key)
                        if value:
                            return str(value).strip()
            for key in ("substance", "material_name", "chemical_name", "name"):
                value = extra.get(key)
                if value:
                    return str(value).strip()
            desc = str(getattr(bottle, "description", "") or "").strip()
            if desc:
                return desc
            name = str(getattr(bottle, "name", "") or "").strip()
            if "@" in name:
                name = name.split("@", 1)[0].strip()
            if slot_label and name == slot_label:
                return None
            if re.match(r"^[A-Z]+\d+$", name):
                return None
            return name or None

        def _extract_amount(
            bottle: Resource,
            amount_kind: str,
            extra_sources: Optional[List[Dict[str, Any]]] = None,
        ) -> Tuple[Optional[float], Optional[float]]:
            def _parse_amount(text: Any) -> Tuple[Optional[float], Optional[str]]:
                if text is None:
                    return None, None
                if isinstance(text, (int, float)):
                    return float(text), None
                match = re.search(r"([0-9]+(?:\\.[0-9]+)?)\\s*([a-zA-Zμµ]+)?", str(text))
                if not match:
                    return None, None
                value = float(match.group(1))
                unit = match.group(2) or ""
                unit = unit.replace("µ", "μ").strip()
                return value, unit

            def _normalize_amount(value: float, unit: Optional[str], amount_kind: str) -> float:
                unit = (unit or "").lower()
                if amount_kind == "volume":
                    if unit in ("l", "liter"):
                        return value * 1000.0
                    if unit in ("μl", "ul"):
                        return value / 1000.0
                    return value
                if amount_kind == "weight":
                    if unit in ("kg", "kilogram"):
                        return value * 1_000_000.0
                    if unit in ("g", "gram"):
                        return value * 1000.0
                    return value
                return value

            extra = getattr(bottle, "unilabos_extra", {}) or {}
            state = getattr(bottle, "_unilabos_state", {}) or {}
            data = getattr(bottle, "data", {}) or {}
            if not isinstance(extra, dict):
                extra = {}
            if not isinstance(state, dict):
                state = {}
            if not isinstance(data, dict):
                data = {}
            sources: List[Dict[str, Any]] = [extra, state, data]
            if extra_sources:
                sources.extend([src for src in extra_sources if isinstance(src, dict)])
            params = _load_parameters(*sources)
            sources = [extra, params, state, data] + (extra_sources or [])
            for source in sources:
                for key in ("initial_volume", "initial_weight", "volume", "weight"):
                    if key in source:
                        try:
                            val = float(source[key])
                        except Exception:
                            val = None
                        if val is None:
                            continue
                        if "volume" in key:
                            return val, None
                        return None, val
            for source in (params, extra, state, data, *(extra_sources or [])):
                raw = source.get("value") or source.get("amount") or source.get("quantity")
                unit = source.get("unit")
                if raw is None:
                    continue
                if unit:
                    raw = f"{raw}{unit}"
                value, unit = _parse_amount(raw)
                if value is None:
                    continue
                value = _normalize_amount(value, unit, amount_kind)
                if amount_kind == "volume":
                    return value, None
                if amount_kind == "weight":
                    return None, value
            liquid_sources: List[Any] = [data, state] + (extra_sources or [])
            tracker = getattr(bottle, "tracker", None)
            if tracker is not None:
                liquid_sources.append(tracker)
            for source in liquid_sources:
                liquids = _get_liquids_from_source(source)
                if not isinstance(liquids, list) or not liquids:
                    continue
                first = liquids[0]
                if isinstance(first, (list, tuple)) and len(first) > 1:
                    value, unit = _parse_amount(first[1])
                elif isinstance(first, dict):
                    raw = first.get("value") or first.get("amount") or first.get("volume") or first.get("weight")
                    value, unit = _parse_amount(raw)
                else:
                    value, unit = (None, None)
                if value is None:
                    continue
                value = _normalize_amount(value, unit, amount_kind)
                if amount_kind == "volume":
                    return value, None
                if amount_kind == "weight":
                    return None, value
            for key in ("value", "amount", "quantity"):
                raw = extra.get(key)
                if raw is None:
                    raw = state.get(key)
                if raw is None:
                    raw = data.get(key)
                if raw is None:
                    continue
                value, unit = _parse_amount(raw)
                if value is None:
                    continue
                value = _normalize_amount(value, unit, amount_kind)
                if amount_kind == "volume":
                    return value, None
                if amount_kind == "weight":
                    return None, value
            return None, None

        carrier = resource
        if getattr(resource, "category", None) != "bottle_carrier":
            if resource.parent and getattr(resource.parent, "category", None) == "bottle_carrier":
                carrier = resource.parent
            elif resource.parent and resource.parent.parent and getattr(resource.parent.parent, "category", None) == "bottle_carrier":
                carrier = resource.parent.parent

        extra = getattr(carrier, "unilabos_extra", {}) or {}
        eit_code = extra.get("eit_staging_code")
        if not eit_code and self.workstation.is_in_staging(carrier):
            eit_code = self.workstation.get_slot_layout_code(carrier)
            if eit_code:
                if not hasattr(carrier, "unilabos_extra"):
                    carrier.unilabos_extra = {}
                carrier.unilabos_extra["eit_staging_code"] = eit_code
        if not eit_code:
            logger.warning(f"[同步→硬件] 非入口位点或缺少入口坐标，跳过: {carrier.name}")
            return False

        tray_code = None
        model_val = getattr(carrier, "model", None)
        if model_val is not None:
            try:
                tray_code = int(str(model_val).strip())
            except Exception:
                tray_code = None
        if tray_code is None:
            extra_code = extra.get("eit_resource_type")
            if extra_code is not None:
                try:
                    tray_code = int(str(extra_code).strip())
                except Exception:
                    tray_code = None
        if tray_code is None:
            desc = str(getattr(carrier, "description", "") or "").strip()
            name = str(getattr(carrier, "name", "") or "").strip()
            for key, label in TRAY_CODE_DISPLAY_NAME.items():
                if desc == label or name == label:
                    tray_code = int(key)
                    break
        if tray_code is None:
            logger.warning(f"[同步→硬件] 无法解析托盘类型，跳过: {carrier.name}")
            return False

        tray_spec = None
        try:
            tray_spec = getattr(TraySpec, ResourceCode(tray_code).name, None)
        except Exception:
            tray_spec = None

        tray_to_media = {
            int(ResourceCode.REAGENT_BOTTLE_TRAY_2ML): (str(int(ResourceCode.REAGENT_BOTTLE_2ML)), True, "volume", "mL"),
            int(ResourceCode.REAGENT_BOTTLE_TRAY_8ML): (str(int(ResourceCode.REAGENT_BOTTLE_8ML)), True, "volume", "mL"),
            int(ResourceCode.REAGENT_BOTTLE_TRAY_40ML): (str(int(ResourceCode.REAGENT_BOTTLE_40ML)), True, "volume", "mL"),
            int(ResourceCode.REAGENT_BOTTLE_TRAY_125ML): (str(int(ResourceCode.REAGENT_BOTTLE_125ML)), True, "volume", "mL"),
            int(ResourceCode.POWDER_BUCKET_TRAY_30ML): (str(int(ResourceCode.POWDER_BUCKET_30ML)), False, "weight", "mg"),
        }

        resource_list: List[Dict[str, Any]] = [{
            "layout_code": f"{eit_code}:-1",
            "resource_type": str(tray_code),
        }]

        media_code, with_cap, amount_kind, default_unit = tray_to_media.get(
            tray_code,
            (str(tray_code), False, "volume", "mL"),
        )
        holder_by_slot: Dict[str, Any] = {}
        for child in getattr(carrier, "children", []):
            if isinstance(child, ResourceHolder):
                holder_by_slot[getattr(child, "name", "")] = child

        name_map = self._chemical_name_map
        chem_cache: Dict[str, Tuple[Optional[int], Optional[str]]] = {}

        def _resolve_chemical(sub_name: str) -> Tuple[Optional[int], Optional[str]]:
            """对齐化学品库，优先完全匹配与中文名。"""
            raw_name = str(sub_name or "").strip()
            if raw_name == "":
                return None, None
            cached = chem_cache.get(raw_name)
            if cached is not None:
                return cached
            nonlocal name_map
            if name_map is None:
                name_map = {}
                sheet_path = Path(__file__).resolve().parent / "sheet" / "chemical_list.xlsx"
                if not sheet_path.exists():
                    logger.warning(f"[同步→硬件] 未找到化学品映射表: {sheet_path}")
                else:
                    try:
                        with zipfile.ZipFile(sheet_path) as zf:
                            shared = zf.read("xl/sharedStrings.xml") if "xl/sharedStrings.xml" in zf.namelist() else None
                            sheet = zf.read("xl/worksheets/sheet1.xml")
                    except Exception as exc:
                        logger.warning(f"[同步→硬件] 读取化学品映射表失败: {exc}")
                    else:
                        ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                        if shared:
                            try:
                                shared_root = ET.fromstring(shared)
                                strings = [
                                    (si.find(".//a:t", ns).text if si.find(".//a:t", ns) is not None else "")
                                    for si in shared_root.findall("a:si", ns)
                                ]
                            except Exception:
                                strings = []
                        else:
                            strings = []
                        sheet_root = ET.fromstring(sheet)
                        sheet_data = sheet_root.find(".//a:sheetData", ns)
                        if sheet_data is not None:
                            header: Dict[int, str] = {}
                            english_idx = None
                            substance_idx = None
                            for row_idx, row in enumerate(sheet_data.findall("a:row", ns), start=1):
                                row_values: Dict[int, str] = {}
                                for cell in row.findall("a:c", ns):
                                    ref = cell.get("r") or ""
                                    if ref == "":
                                        continue
                                    letters = "".join(ch for ch in ref if ch.isalpha()).upper()
                                    col_idx = 0
                                    for ch in letters:
                                        col_idx = col_idx * 26 + (ord(ch) - ord("A") + 1)
                                    col_idx -= 1
                                    cell_type = cell.get("t")
                                    if cell_type == "inlineStr":
                                        inline = cell.find("a:is/a:t", ns)
                                        value = inline.text if inline is not None else None
                                    else:
                                        value_node = cell.find("a:v", ns)
                                        if value_node is None:
                                            value = None
                                        else:
                                            text = value_node.text or ""
                                            if cell_type == "s":
                                                try:
                                                    value = strings[int(text)]
                                                except Exception:
                                                    value = None
                                            else:
                                                value = text
                                    if value is None:
                                        continue
                                    row_values[col_idx] = str(value).strip()

                                if not row_values:
                                    continue

                                if row_idx == 1:
                                    header = {idx: val for idx, val in row_values.items() if val != ""}
                                    for idx, name in header.items():
                                        if name == "substance_english_name":
                                            english_idx = idx
                                        elif name == "substance":
                                            substance_idx = idx
                                    if english_idx is None or substance_idx is None:
                                        logger.warning("[同步→硬件] 化学品映射表缺少 substance_english_name 或 substance 列")
                                        break
                                    continue

                                if english_idx is None or substance_idx is None:
                                    break
                                english = row_values.get(english_idx, "").strip()
                                substance = row_values.get(substance_idx, "").strip()
                                if english and substance:
                                    name_map.setdefault(english, substance)
                                    name_map.setdefault(english.lower(), substance)
                self._chemical_name_map = name_map
            name = raw_name
            mapped = name_map.get(name) or name_map.get(name.lower())
            if mapped:
                name = mapped
            try:
                resp = self.manager.get_chemical_list(query_key=name, limit=10)
            except Exception as exc:
                logger.warning(f"[同步→硬件] 化学品查询失败: {name}, err={exc}")
                chem_cache[raw_name] = (None, name)
                return chem_cache[raw_name]
            candidates = resp.get("chemical_list") or []
            exact = None
            for item in candidates:
                item_name = str(item.get("name") or "").strip()
                if item_name == name:
                    exact = item
                    break
            chosen = exact
            if chosen is None:
                for item in candidates:
                    item_name = str(item.get("name") or "").strip()
                    if item_name != "" and re.search(r"[\u4e00-\u9fff]", item_name):
                        chosen = item
                        break
            if chosen is None and candidates:
                chosen = candidates[0]
            if chosen is None:
                logger.warning(f"[同步→硬件] 未找到化学品: {name}")
                chem_cache[raw_name] = (None, name)
                return chem_cache[raw_name]
            fid = chosen.get("fid") or chosen.get("chemical_id")
            chosen_name = str(chosen.get("name") or name).strip() or name
            chem_cache[raw_name] = (fid, chosen_name)
            return chem_cache[raw_name]

        bottle_debug: List[Dict[str, Any]] = []
        seen_slots: set = set()
        bottle_items: List[Tuple[Optional[str], Optional[Resource]]] = []
        if hasattr(carrier, "sites"):
            for idx in range(len(carrier.sites)):
                site = carrier.get_item(idx)
                slot_label = None
                bottle = None
                if isinstance(site, ResourceHolder):
                    slot_label = getattr(site, "name", None)
                    bottle = site.children[0] if site.children else site
                else:
                    bottle = site
                    slot_label = _resolve_slot_label_from_name(getattr(bottle, "name", None))
                bottle_items.append((slot_label, bottle))
        for child in getattr(carrier, "children", []):
            if isinstance(child, ResourceHolder):
                slot_label = getattr(child, "name", None)
                bottle = child.children[0] if child.children else child
                bottle_items.append((slot_label, bottle))
            else:
                slot_label = _resolve_slot_label_from_name(getattr(child, "name", None))
                bottle_items.append((slot_label, child))

        for slot_label, bottle in bottle_items:
            if not bottle:
                continue
            bottle_extra = getattr(bottle, "unilabos_extra", {}) or {}
            if slot_label and "@" in slot_label:
                slot_label = slot_label.split("@", 1)[0].strip()
            if not slot_label:
                slot_label = bottle_extra.get("well") or _resolve_slot_label_from_name(getattr(bottle, "name", None)) or ""
            slot_label = slot_label or ""
            slot_idx = None
            if tray_spec and slot_label and len(slot_label) > 1 and slot_label[0].isalpha():
                try:
                    col_count, row_count = tray_spec
                    col_index = int(slot_label[1:])
                    row_index = ord(slot_label[0].upper()) - ord("A")
                    if 1 <= col_index <= col_count and 0 <= row_index < row_count:
                        slot_idx = row_index * col_count + (col_index - 1)
                except Exception:
                    slot_idx = None
            if slot_idx is None:
                slot_raw = bottle_extra.get("slot") or getattr(bottle, "slot", None)
                if slot_raw is not None:
                    try:
                        slot_idx = int(str(slot_raw).strip())
                    except Exception:
                        slot_idx = None
            if slot_idx is None:
                continue
            if slot_idx in seen_slots:
                continue
            seen_slots.add(slot_idx)

            holder = holder_by_slot.get(slot_label)
            holder_extra = getattr(holder, "unilabos_extra", {}) or {} if holder else {}
            holder_state = getattr(holder, "_unilabos_state", {}) or {} if holder else {}
            holder_data = getattr(holder, "data", {}) or {} if holder else {}
            bottle_state: Dict[str, Any] = {}
            for attr in ("serialize_state", "serialize_all_state"):
                func = getattr(bottle, attr, None)
                if not callable(func):
                    continue
                try:
                    state = func()
                except Exception:
                    continue
                if not isinstance(state, dict):
                    continue
                if attr == "serialize_all_state":
                    name = getattr(bottle, "name", None)
                    if isinstance(name, str) and isinstance(state.get(name), dict):
                        bottle_state = state[name]
                        break
                bottle_state = state
                break

            bottle_debug.append({
                "type": type(bottle).__name__,
                "name": getattr(bottle, "name", None) if hasattr(bottle, "name") else str(bottle),
                "slot_label": slot_label,
                "slot_idx": slot_idx,
                "unilabos_extra": bottle_extra if isinstance(bottle_extra, dict) else {},
                "data": getattr(bottle, "data", None) if hasattr(bottle, "data") else None,
                "_unilabos_state": getattr(bottle, "_unilabos_state", None) if hasattr(bottle, "_unilabos_state") else None,
                "serialized_state": bottle_state,
                "tracker_liquids": getattr(getattr(bottle, "tracker", None), "liquids", None),
                "holder_unilabos_extra": holder_extra,
                "holder_data": holder_data,
                "holder_unilabos_state": holder_state,
            })

            extra_sources = [holder_extra, holder_state, holder_data, bottle_state]
            substance = _extract_substance(bottle, slot_label, extra_sources)
            init_vol, init_wt = _extract_amount(bottle, amount_kind, extra_sources)
            if not substance and init_vol is None and init_wt is None:
                continue

            bottle_type = None
            bottle_model = getattr(bottle, "model", None)
            if bottle_model is not None:
                try:
                    bottle_type = int(str(bottle_model).strip())
                except Exception:
                    bottle_type = None
            resource_type = str(bottle_type) if bottle_type is not None else media_code
            resolved_substance = substance
            chemical_id = None
            if substance:
                liquids_present = False
                for source in (bottle_state, getattr(bottle, "data", None), getattr(bottle, "_unilabos_state", None)):
                    liquids = _get_liquids_from_source(source)
                    if liquids:
                        liquids_present = True
                        break
                if not liquids_present:
                    tracker = getattr(bottle, "tracker", None)
                    liquids_present = bool(_get_liquids_from_source(tracker))
                if liquids_present:
                    chemical_id, resolved_substance = _resolve_chemical(substance)
            entry: Dict[str, Any] = {
                "layout_code": f"{eit_code}:{slot_idx}",
                "resource_type": resource_type,
                "with_cap": with_cap,
            }
            if resolved_substance:
                entry["substance"] = resolved_substance
            if chemical_id is not None:
                entry["chemical_id"] = chemical_id
            if amount_kind == "weight":
                if init_wt is not None:
                    entry["initial_weight"] = init_wt
                    entry["unit"] = default_unit
                elif init_vol is not None:
                    entry["initial_volume"] = init_vol
                    entry["unit"] = "mL"
            else:
                if init_vol is not None:
                    entry["initial_volume"] = init_vol
                    entry["unit"] = default_unit
                elif init_wt is not None:
                    entry["initial_weight"] = init_wt
                    entry["unit"] = "mg"
            resource_list.append(entry)

        logger.info(f"同步至硬件: {carrier.name} -> {eit_code}")
        if bottle_debug:
            logger.info(f"[同步→硬件] Bottle raw data: {bottle_debug}")
        else:
            logger.info(
                "[同步→硬件] Bottle raw data: [] (children=%s, sites=%s)",
                [(getattr(c, "name", None), getattr(c, "category", None), type(c).__name__) for c in getattr(carrier, "children", [])],
                len(getattr(carrier, "sites", []) or []),
            )
        resource_req_list = [{
            "remark": "",
            "resource_list": resource_list,
        }]
        logger.info(f"[同步→硬件] BatchInTray payload: {resource_req_list}")
        try:
            resp = self.manager.batch_in_tray(resource_req_list)
            if resp is not None and hasattr(carrier, "unilabos_extra"):
                carrier.unilabos_extra.pop("eit_staging_code", None)
            return resp is not None
        except Exception as e:
            logger.error(f"[同步→硬件] BatchInTray 调用失败: {e}")
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
            env = self.manager.get_glovebox_env()
            state = self.manager.station_state()
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

    def get_slot_layout_code(self, res: Resource) -> str:
        slot = res if isinstance(res, ResourceHolder) else res.parent
        if slot is None:
            return ""
        slot_name = normalize_layout_code(getattr(slot, "name", "") or "")
        if slot_name:
            return slot_name
        return self._resolve_eit_code_by_slot(slot)

    def is_in_staging(self, res: Resource) -> bool:
        slot = res if isinstance(res, ResourceHolder) else res.parent
        if slot is None or not slot.parent:
            return False
        warehouse = slot.parent
        slot_code = normalize_layout_code(getattr(slot, "name", "") or "")
        if getattr(warehouse, "name", "") == "TB":
            return True
        if getattr(warehouse, "name", "") == "W":
            return bool(re.match(r"^W-1-[1-8]$", slot_code))
        return False

    def _get_eit_layout_code(self, res: Resource) -> str:
        """优先使用硬件回写位置，其次使用前端更新位置或槽位推导。"""
        extra = getattr(res, "unilabos_extra", {}) or {}
        eit_code = extra.get("eit_layout_code") or extra.get("update_resource_site")
        if eit_code:
            return eit_code
        if res.parent:
            return self._resolve_eit_code_by_slot(res.parent)
        return ""
    
    def resource_tree_add(self, resources: List[Resource]):
        """处理前端物料添加请求（上料）"""
        for res in resources:
            tray = res
            if getattr(res, "category", None) != "bottle_carrier":
                if res.parent and getattr(res.parent, "category", None) == "bottle_carrier":
                    tray = res.parent
                elif res.parent and res.parent.parent and getattr(res.parent.parent, "category", None) == "bottle_carrier":
                    tray = res.parent.parent
            if tray.parent and self.is_in_staging(tray):
                staging_code = self.get_slot_layout_code(tray)
                if staging_code:
                    if not hasattr(tray, "unilabos_extra"):
                        tray.unilabos_extra = {}
                    tray.unilabos_extra["eit_staging_code"] = staging_code
                    self.resource_synchronizer.sync_to_external(tray)
            if getattr(res, "category", None) == "bottle_carrier":
                self.resource_synchronizer._update_resource_flattened([res])

    def resource_tree_remove(self, resources: List[Resource]):
        """处理资源删除时的同步（出库操作）

        当 UniLab 前端删除物料时，需要将删除操作同步到 EIT 系统（出库）

        Args:
            resources: 要删除的资源列表
        """
        top_level_names = {res.name for res in resources}
        processed_codes = set()
        layout_list = []

        for res in resources:
            eit_code = self._get_eit_layout_code(res)
            if eit_code and eit_code not in processed_codes:
                parent_resource = res.parent
                if parent_resource and parent_resource.name not in top_level_names:
                    logger.info(f"[EIT] 真正触发硬件下料动作: {eit_code}")
                    layout_list.append({"layout_code": eit_code})
                    processed_codes.add(eit_code)

        if layout_list:
            def _run_batch_out():
                try:
                    self.manager.batch_out_tray(layout_list)
                except Exception as e:
                    logger.error(f"[EIT] batch_out_tray 异步执行失败: {e}")

            threading.Thread(target=_run_batch_out, daemon=True).start()

    def resource_tree_transfer(self, old_parent: Optional[Resource], resource: Resource, new_parent: Resource):
        """处理资源在设备间迁移时的同步

        当资源从一个设备迁移到 Workstation 时,只创建物料（不入库）
        入库操作由后续的 resource_tree_add 完成

        Args:
            old_parent: 资源的原父节点（可能为 None）
            resource: 要迁移的资源
            new_parent: 资源的新父节点
        """
        logger.info(f"[Transfer] 资源 {resource.name} 移入工站")
        self.resource_tree_add([resource])
        
    def resource_tree_update(self, resources: List[Resource]):
        """处理资源更新时的同步（位置移动、属性修改等）

        当 UniLab 前端更新物料信息时（如修改位置），需要将更新操作同步到 Bioyond 系统

        Args:
            resources: 要更新的资源列表
        """
        for res in resources:
            tray = res
            if getattr(res, "category", None) != "bottle_carrier":
                if res.parent and getattr(res.parent, "category", None) == "bottle_carrier":
                    tray = res.parent
                elif res.parent and res.parent.parent and getattr(res.parent.parent, "category", None) == "bottle_carrier":
                    tray = res.parent.parent
            if not tray.parent:
                continue
            extra = getattr(tray, "unilabos_extra", {}) or {}
            old_staging_code = extra.get("eit_staging_code")
            old_in_staging = bool(old_staging_code)
            new_in_staging = self.is_in_staging(tray)
            if not old_in_staging and new_in_staging:
                staging_code = self.get_slot_layout_code(tray)
                if staging_code:
                    if not hasattr(tray, "unilabos_extra"):
                        tray.unilabos_extra = {}
                    tray.unilabos_extra["eit_staging_code"] = staging_code
                    logger.info(f"[EIT] 进入入口位点，上料触发: {staging_code}")
                    self.resource_synchronizer.sync_to_external(tray)
                continue
            if old_in_staging and not new_in_staging:
                if hasattr(tray, "unilabos_extra"):
                    tray.unilabos_extra.pop("eit_staging_code", None)
