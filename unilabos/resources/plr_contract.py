"""Uni-Lab 资源事实进入物理位置资源（PLR）驱动对象时共享的只读契约。"""

from __future__ import annotations

from typing import Final, TypeAlias

# 父物料 ``unilabos_extra`` 中稳定库位（Site）UUID 到设备局部名称的只读映射键。
SITE_NAME_BY_UUID_EXTRA_KEY: Final = "unilabos_site_name_by_uuid"

# 映射结构严格只含稳定库位 UUID 字符串与设备局部库位名称字符串，不承载其他元数据。
SiteNameByUuid: TypeAlias = dict[str, str]

__all__ = ["SITE_NAME_BY_UUID_EXTRA_KEY", "SiteNameByUuid"]
