from pylabrobot.resources import (
    ResourceHolder, 
    Coordinate, 
    create_ordered_items_2d
)
from unilabos.resources.itemized_carrier import BottleCarrier
from unilabos.devices.workstation.eit_synthesis_station.config.constants import TraySpec, ResourceCode
import uuid

def _create_eit_tray(name: str, tray_type_enum: str, size: tuple, model_code: str) -> BottleCarrier:
    """通用的 EIT 托盘创建工厂函数"""
    cols, rows = getattr(TraySpec, tray_type_enum) # 从 TraySpec 获取 (8, 6) 等规格
    
    spacing_x, spacing_y = 15.0, 15.0  # 孔位中心间距
    site_size_z = 10.0
    offset_x, offset_y = 10.0, 10.0    # A1 孔位相对于左下角的偏移
    carrier_size_z = 50.0
    bottle_diameter = 5.0

    sites = create_ordered_items_2d(
            klass=ResourceHolder,
            num_items_x=cols,
            num_items_y=rows,
            item_dx=spacing_x,           
            item_dy=spacing_y,
            dz=site_size_z,                
            dx=offset_x,
            dy=offset_y,
            size_x=bottle_diameter,
            size_y=bottle_diameter,
            size_z=carrier_size_z,
        )

    carrier = BottleCarrier(
        name=name,
        size_x=size[0],
        size_y=size[1],
        size_z=size[2],
        sites=sites,
        model=str(int(model_code)),
        category="bottle_carrier"
    )

    carrier.unilabos_uuid = str(uuid.uuid4())
    for site in carrier.children:
        site.unilabos_uuid = str(uuid.uuid4())
    
    return carrier

def EIT_REAGENT_BOTTLE_TRAY_2ML(name: str) -> BottleCarrier:
    """创建 2 mL 试剂瓶托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="REAGENT_BOTTLE_TRAY_2ML",
        size=(127.8, 85.5, 20.0),
        model_code=ResourceCode.REAGENT_BOTTLE_TRAY_2ML
    )

def EIT_REAGENT_BOTTLE_TRAY_8ML(name: str) -> BottleCarrier:
    """创建 8 mL 试剂瓶托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="REAGENT_BOTTLE_TRAY_8ML",
        size=(127.8, 85.5, 20.0),
        model_code=ResourceCode.REAGENT_BOTTLE_TRAY_8ML
    )

def EIT_REAGENT_BOTTLE_TRAY_40ML(name: str) -> BottleCarrier:
    """创建 40 mL 试剂瓶托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="REAGENT_BOTTLE_TRAY_40ML",
        size=(127.8, 85.5, 30.0),
        model_code=ResourceCode.REAGENT_BOTTLE_TRAY_40ML
    )

def EIT_REAGENT_BOTTLE_TRAY_125ML(name: str) -> BottleCarrier:
    """创建 125 mL 试剂瓶托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="REAGENT_BOTTLE_TRAY_125ML",
        size=(127.8, 85.5, 40.0),
        model_code=ResourceCode.REAGENT_BOTTLE_TRAY_125ML
    )

def EIT_POWDER_BUCKET_TRAY_30ML(name: str) -> BottleCarrier:
    """创建 30 mL 粉桶托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="POWDER_BUCKET_TRAY_30ML",
        size=(127.8, 85.5, 30.0),
        model_code=ResourceCode.POWDER_BUCKET_TRAY_30ML
    )

def EIT_TIP_TRAY_1ML(name: str) -> BottleCarrier:
    """创建 1 mL Tip 头托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="TIP_TRAY_1ML",
        size=(127.8, 85.5, 40.0),
        model_code=ResourceCode.TIP_TRAY_1ML
    )

def EIT_TIP_TRAY_5ML(name: str) -> BottleCarrier:
    """创建 5 mL Tip 头托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="TIP_TRAY_5ML",
        size=(127.8, 85.5, 40.0),
        model_code=ResourceCode.TIP_TRAY_5ML
    )  

def EIT_TIP_TRAY_50UL(name: str) -> BottleCarrier:
    """创建 50 μL Tip 头托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="TIP_TRAY_50UL",
        size=(127.8, 85.5, 40.0),
        model_code=ResourceCode.TIP_TRAY_50UL
    )

def EIT_REACTION_TUBE_TRAY_2ML(name: str) -> BottleCarrier:
    """创建 2 mL 反应试管托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="REACTION_TUBE_TRAY_2ML",
        size=(127.8, 85.5, 30.0),
        model_code=ResourceCode.REACTION_TUBE_TRAY_2ML
    )  

def EIT_TEST_TUBE_MAGNET_TRAY_2ML(name: str) -> BottleCarrier:
    """创建 2 mL 试管磁子托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="TEST_TUBE_MAGNET_TRAY_2ML",
        size=(127.8, 85.5, 30.0),
        model_code=ResourceCode.TEST_TUBE_MAGNET_TRAY_2ML
    )

def EIT_REACTION_SEAL_CAP_TRAY(name: str) -> BottleCarrier:
    """创建 反应密封盖托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="REACTION_SEAL_CAP_TRAY",
        size=(127.8, 85.5, 20.0),
        model_code=ResourceCode.REACTION_SEAL_CAP_TRAY
    )

def EIT_FLASH_FILTER_INNER_BOTTLE_TRAY(name: str) -> BottleCarrier:
    """创建 闪滤瓶内瓶托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="FLASH_FILTER_INNER_BOTTLE_TRAY",
        size=(127.8, 85.5, 30.0),
        model_code=ResourceCode.FLASH_FILTER_INNER_BOTTLE_TRAY
    )

def EIT_FLASH_FILTER_OUTER_BOTTLE_TRAY(name: str) -> BottleCarrier:
    """创建 闪滤瓶外瓶托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="FLASH_FILTER_OUTER_BOTTLE_TRAY",
        size=(127.8, 85.5, 30.0),
        model_code=ResourceCode.FLASH_FILTER_OUTER_BOTTLE_TRAY
    )

