import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from collections import OrderedDict

from unilabos.devices.workstation.eit_synthesis_station.config.constants import TaskStatus,StationState,DeviceModuleStatus, ResourceCode, TraySpec,TRAY_CODE_DISPLAY_NAME
from unilabos.devices.workstation.eit_synthesis_station.config.setting import Settings, configure_logging
from unilabos.devices.workstation.eit_synthesis_station.driver.api_client import ApiClient
from unilabos.devices.workstation.eit_synthesis_station.driver.exceptions import AuthorizationExpiredError, ValidationError
import re
import math

import uuid

JsonDict = Dict[str, Any]

class SynthesisStationController:
    """
    功能:
        【上层逻辑】面向用户的控制器，提供自动登录、401 自动重登、状态轮询等。
    """

    def __init__(self, settings: Optional[Settings] = None):
        self._settings = settings or Settings.from_env()
        self._client = ApiClient(self._settings)
        self._logger = logging.getLogger(self.__class__.__name__)

    @property
    def client(self) -> ApiClient:
        return self._client
    
    # ---------- 自动登录 -------------
    def login(self) -> Tuple[str, str]:
        """
        功能:
            登录并缓存 token.
        参数:
            无.
        返回:
            (token_type, access_token).
        """
        resp = self._client.login(self._settings.username, self._settings.password)
        token_type = str(resp.get("token_type", "Bearer"))
        access_token = str(resp.get("access_token", ""))
        self._client.set_token(token_type, access_token)
        self._logger.debug("登录成功, token_type=%s", token_type)
        return token_type, access_token

    def ensure_login(self) -> None:
        """
        功能:
            确保已登录，未登录则自动登录。
        参数:
            无.
        返回:
            无.
        """
        if not self._client.access_token:
            self.login()

    def _call_with_relogin(self, func: Callable, *args, **kwargs):
        """
        功能:
            捕获 401，自动重登后重试一次。
        参数:
            func: 需要包装的函数.
            *args, **kwargs: 透传参数.
        返回:
            func 的返回值.
        """
        self.ensure_login()
        try:
            return func(*args, **kwargs)
        except AuthorizationExpiredError:
            self._logger.warning("检测到登录失效, 自动重新登录并重试一次")
            self.login()
            return func(*args, **kwargs)
        
    def _extract_station_state(self, state_info: JsonDict) -> Optional[int]:
        """
        功能:
            从站点状态响应中提取状态码。
        参数:
            state_info: station_state 响应。
        返回:
            Optional[int], 状态码。
        """
        for key in ("state", "status"):
            if key in state_info and isinstance(state_info.get(key), int):
                return int(state_info[key])
        for outer in ("result", "data"):
            obj = state_info.get(outer)
            if isinstance(obj, dict):
                for key in ("state", "status"):
                    if isinstance(obj.get(key), int):
                        return int(obj[key])
        return None

    def _assert_success(self, resp: JsonDict, action: str) -> None:
            """
            功能:
                校验接口响应的 code 与 msg 是否成功。
            参数:
                resp: 接口返回字典.
                action: 当前动作名称, 用于异常信息.
            返回:
                无, 不满足条件则抛出 ValidationError.
            """
            code = resp.get("code", 200)
            msg = resp.get("msg", "")
            if code != 200 or str(msg).lower() != "success":
                raise ValidationError(f"{action}失败, code={code}, msg={msg}, resp={resp}")
        
    # ---------- 设备初始化 ----------
    def device_init(
        self,
        device_id: Optional[List[str]] = None,
        *,
        poll_interval_s: float = 1.0,
        timeout_s: float = 600.0,
    ) -> JsonDict:
        """
        功能:
            触发设备初始化, 然后轮询站点状态直到空闲。
        参数:
            device_id: 设备id列表, 当前忽略, 始终传空JSON。
            poll_interval_s: 轮询间隔秒数。
            timeout_s: 超时秒数, 超时抛出 TimeoutError。
        返回:
            Dict, 初始化接口原始响应。
        """
        resp = self._call_with_relogin(self._client.device_init, {})  # 传空JSON
        self._assert_success(resp, "设备初始化") 
        self._logger.info("设备开始初始化")
        start_ts = time.time()

        while True:
            state = self.station_state()
            if state is not None:
                if state == int(StationState.IDLE):
                    self._logger.info("设备初始化完成")
                    return resp
            else:
                self._logger.warning("无法解析站点状态, resp=%s", state)

            if time.time() - start_ts > timeout_s:
                raise TimeoutError(f"设备初始化等待空闲超时, last_state={state}")

            time.sleep(poll_interval_s)

    # ---------- 获取设备状态 ------------
    def station_state(self) -> int:
        """
        功能:
            获取工站整体状态码.
        参数:
            无.
        返回:
            int, 工站状态码.
        """
        resp = self._call_with_relogin(self._client.station_state)
        # 兼容不同层级的状态字段
        for key in ("state", "status"):
            if isinstance(resp.get(key), int):
                return int(resp[key])
        for outer in ("result", "data"):
            obj = resp.get(outer)
            if isinstance(obj, dict):
                for key in ("state", "status"):
                    if isinstance(obj.get(key), int):
                        return int(obj[key])
                    
        raise ValidationError(f"无法解析站点状态码, resp={resp}")

    def get_glovebox_env(self) -> JsonDict:
        """
        功能:
            调用 batch_list_device_runtimes 获取手套箱环境数据，并提取时间、箱压、水值、氧值
        参数:
            无
        返回:
            Dict[str, Any], 包含 time、box_pressure、water_content、oxygen_content
        """
        # 固定查询设备代码 352（手套箱环境）
        resp = self._call_with_relogin(self._client.batch_list_device_runtimes, ["352"])
        data_container = resp.get("result") or resp.get("data") or resp

        if not isinstance(data_container, list) or len(data_container) == 0:
            raise ValidationError(f"响应缺少环境数据, resp={resp}")

        first_item = data_container[0]  # 多组相同数据，取第一组
        time_val = first_item.get("time")
        box_pressure = first_item.get("box_pressure")
        water_content = first_item.get("water_content")
        oxygen_content = first_item.get("oxygen_content")

        result = {
            "time": time_val,
            "box_pressure": box_pressure,
            "water_content": water_content,
            "oxygen_content": oxygen_content,
        }
        self._logger.info("手套箱环境数据: %s", result)
        return result

    def list_device_info(self) -> JsonDict:
        """
        功能:
            获取站点设备模块列表。暂不使用,使用get_all_device_info.
        参数:
            无.
        返回:
            Dict, 接口响应.
        """
        resp = self._call_with_relogin(self._client.list_device_info)
        self._assert_success(resp, "获取站点设备模块列表")
        return resp

    def get_all_device_info(self) -> JsonDict:
        """
        功能:
            获取全部设备信息，仅返回 station_data 字段
        参数:
            无
        返回:
            Dict[str, Any], 包含 station_data 的字典
        """
        resp = self._call_with_relogin(self._client.get_all_device_info)
        self._assert_success(resp, "获取全部设备信息")

        station_data = resp.get("station_data") or resp.get("data") or resp.get("result")
        if not isinstance(station_data, list):
            raise ValidationError(f"响应缺少 station_data, resp={resp}")

        return {"station_data": station_data}
    
    def list_device_status(self) -> List[JsonDict]:
        """
        功能:
            基于 get_all_device_info 提取设备名称与状态(状态名替换数值).
        参数:
            无.
        返回:
            List[Dict], 包含 device_name、status(名称)、status_code(数值).
        """
        raw = self.get_all_device_info()
        station_list = raw.get("station_data") or raw.get("data") or raw.get("result") or []
        if not isinstance(station_list, list):
            raise ValidationError(f"station_data 格式异常, resp={raw}")

        device_status: List[JsonDict] = []
        for station_item in station_list:
            for dev in station_item.get("device_info", []):
                status_val = dev.get("status")
                status_name = (
                    DeviceModuleStatus(status_val).name
                    if isinstance(status_val, int) and status_val in DeviceModuleStatus._value2member_map_
                    else "UNKNOWN"
                )
                device_status.append(
                    {
                        "device_name": dev.get("device_name"),
                        "status": status_name,       # 如 AVAILABLE
                        "status_code": status_val,   # 数值保留以便排查
                    }
                )
        self._logger.info("设备状态汇总完成, 数量=%s", len(device_status))
        return device_status

    # ---------- 获取站内资源信息 ----------
    def get_resource_info(self) -> List[JsonDict]:
        """
        功能:
            调用资源详情接口, 将打平的 resource_list 聚合为按资源位置展示的表格行.
            聚合协议:
                1) 资源位置: 取 layout_code 或 source_layout_code 的冒号前缀, 例如 "N-1:-1" 与 "N-1:0" 聚合为 "N-1".
                2) slot == -1: 表示托盘本体, 读取 resource_type 作为托盘型号, 并补充 resource_type_name.
                3) slot != -1: 表示托盘坑位, 统计数量作为 count.
                4) substance_details: 仅收集 substance 非空的坑位, 使用列表返回; 若无物质, 返回空列表.
                   每个元素包含 slot, well, substance, value 字段.
        参数:
            无.
        返回:
            List[JsonDict], 每个元素结构如下:
                {
                    "layout_code": str, 资源位置,
                    "count": int, 坑位数量,
                    "substance_details": List[Dict], 物质详情列表, 无则 [],
                    "resource_type": int or None, 托盘型号编码,
                    "resource_type_name": str, 托盘型号中文名,
                }
        """
        response = self._call_with_relogin(self._client.get_resource_info, {})
        self._assert_success(response, "获取资源信息")

        resource_list = self._extract_resource_list(response)
        rows = self._format_resource_rows(resource_list)

        self._logger.info("获取资源信息成功")
        return rows

    def _extract_resource_list(self, response: JsonDict) -> List[JsonDict]:
        """
        功能:
            从不同响应包裹层中提取 resource_list, 兼容直接返回或嵌套在 result/data 中的情况.
        参数:
            response: JsonDict, 接口原始响应.
        返回:
            List[JsonDict], 资源明细列表.
        """
        if "resource_list" in response:
            resource_list = response.get("resource_list")
            if resource_list is not None:
                return resource_list

        for outer_key in ("result", "data"):
            outer_obj = response.get(outer_key)
            if isinstance(outer_obj, dict) and "resource_list" in outer_obj:
                resource_list = outer_obj.get("resource_list")
                if resource_list is not None:
                    return resource_list

        raise ValidationError(f"响应缺少 resource_list 字段, resp={response}")

    def _format_resource_rows(self, resource_list: List[JsonDict]) -> List[JsonDict]:
        """
        功能:
            将资源明细列表按资源位置聚合为返回行, 并生成 substance_details 列表.
        参数:
            resource_list: List[JsonDict], 资源明细列表.
        返回:
            List[JsonDict], 聚合后的行列表.
        """
        grouped_by_layout: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

        for item in resource_list:
            layout_code = self._get_layout_code(item)
            if layout_code is None:
                continue
            if layout_code == "":
                continue
            if ":" not in layout_code:
                continue

            layout_prefix, slot_text = layout_code.split(":", 1)
            slot_index = self._safe_int(slot_text)
            if slot_index is None:
                continue

            if layout_prefix not in grouped_by_layout:
                grouped_by_layout[layout_prefix] = {"tray_item": None, "media_items": []}

            if slot_index == -1:
                grouped_by_layout[layout_prefix]["tray_item"] = item
            else:
                grouped_by_layout[layout_prefix]["media_items"].append(item)

        rows: List[JsonDict] = []
        for layout_prefix, group in grouped_by_layout.items():
            tray_item = group.get("tray_item")
            media_items: List[JsonDict] = group.get("media_items", [])

            tray_code = self._get_tray_code(tray_item)
            tray_name = self._get_tray_name(tray_code)

            rows.append(
                {
                    "layout_code": layout_prefix,
                    "count": len(media_items),
                    "substance_details": self._build_substance_details(tray_code, media_items),
                    "resource_type": tray_code,
                    "resource_type_name": tray_name,
                }
            )

        return rows

    def _get_layout_code(self, item: JsonDict) -> Optional[str]:
        """
        功能:
            获取明细中的布局编码字段, 兼容 layout_code 与 source_layout_code.
        参数:
            item: JsonDict, 单条资源明细.
        返回:
            Optional[str], 布局编码字符串.
        """
        layout_code = item.get("layout_code")
        if layout_code is not None and str(layout_code) != "":
            return str(layout_code)

        source_layout_code = item.get("source_layout_code")
        if source_layout_code is not None and str(source_layout_code) != "":
            return str(source_layout_code)

        return None

    def _get_tray_code(self, tray_item: Optional[JsonDict]) -> Optional[int]:
        """
        功能:
            从托盘本体记录(slot == -1)中解析托盘编码(resource_type).
        参数:
            tray_item: Optional[JsonDict], 托盘本体记录.
        返回:
            Optional[int], 托盘编码.
        """
        if tray_item is None:
            return None
        return self._safe_int(tray_item.get("resource_type"))

    def _get_tray_name(self, tray_code: Optional[int]) -> str:
        """
        功能:
            根据托盘编码获取中文名称, 未命中时返回空字符串.
        参数:
            tray_code: Optional[int], 托盘编码.
        返回:
            str, 托盘中文名.
        """
        if tray_code is None:
            return ""
        tray_name = TRAY_CODE_DISPLAY_NAME.get(tray_code)
        if tray_name is None:
            return ""
        return tray_name

    def _build_substance_details(self, tray_code: Optional[int], media_items: List[JsonDict]) -> List[JsonDict]:
        """
        功能:
            生成 substance_details 列表, 每个元素表示一个有物质的坑位.
            若无物质, 返回空列表.
        参数:
            tray_code: Optional[int], 托盘编码, 用于 slot 到 well 的映射.
            media_items: List[JsonDict], 坑位明细列表.
        返回:
            List[JsonDict], 物质详情列表, 结构:
                {
                    "slot": int or None,
                    "well": str,
                    "substance": str,
                    "value": str,
                }
        """
        if tray_code is None:
            return []

        tray_spec = self._get_tray_spec(tray_code)
        details: List[JsonDict] = []

        for item in media_items:
            # 只输出实际有物质的坑位, 避免空坑位污染返回数据.
            substance = item.get("substance")
            if substance is None:
                continue
            if str(substance).strip() == "":
                continue

            slot_index = self._extract_slot_index(item)
            well_text = self._slot_to_well_text(slot_index, tray_spec)
            value_text = self._extract_amount_with_unit(item, tray_code) 

            details.append(
                {
                    "slot": slot_index,
                    "well": well_text,
                    "substance": str(substance).strip(),
                    "value": value_text,
                }
            )

        return details

    def _get_tray_spec(self, tray_code: int) -> Optional[Tuple[int, int]]:
        """
        功能:
            根据托盘编码获取 TraySpec 中的规格定义, 规格格式为 (col, row).
        参数:
            tray_code: int, 托盘编码.
        返回:
            Optional[Tuple[int, int]], 托盘规格(列数, 行数), 未匹配则 None.
        """
        try:
            enum_name = ResourceCode(tray_code).name
        except Exception:
            return None

        tray_spec = getattr(TraySpec, enum_name, None)
        if tray_spec is None:
            return None
        return tray_spec

    def _extract_slot_index(self, item: JsonDict) -> Optional[int]:
        """
        功能:
            从 layout_code/source_layout_code 中提取 slot 序号.
        参数:
            item: JsonDict, 单条坑位明细.
        返回:
            Optional[int], slot 序号.
        """
        layout_code = self._get_layout_code(item)
        if layout_code is None:
            return None
        if layout_code == "":
            return None
        if ":" not in layout_code:
            return None

        _, slot_text = layout_code.split(":", 1)
        return self._safe_int(slot_text)

    def _slot_to_well_text(self, slot_index: Optional[int], tray_spec: Optional[Tuple[int, int]]) -> str:
        """
        功能:
            将 slot 序号按行优先映射为井位文本, 规则 A1, A2...B1.
        参数:
            slot_index: Optional[int], slot 序号.
            tray_spec: Optional[Tuple[int, int]], (col, row) 托盘规格.
        返回:
            str, 井位文本, 无法映射时返回 "-".
        """
        if slot_index is None:
            return "-"
        if tray_spec is None:
            return str(slot_index)

        col_count, row_count = tray_spec
        if col_count <= 0:
            return str(slot_index)
        if row_count <= 0:
            return str(slot_index)

        row_index = slot_index // col_count
        col_index = slot_index % col_count + 1

        if row_index >= row_count:
            return str(slot_index)

        return f"{chr(ord('A') + row_index)}{col_index}"

    def _extract_amount_with_unit(self, item: JsonDict, tray_code: Optional[int] = None) -> str:
        """
        功能:
            根据托盘类型提取可展示的数值与单位; 粉桶托盘优先读取重量, 试剂瓶托盘优先读取体积, 其他类型按通用顺序。
        参数:
            item: JsonDict, 单条槽位明细.
            tray_code: Optional[int], 托盘资源编码, 用于确定重量或体积优先级。
        返回:
            str, 数值与单位拼接后的字符串, 例如 "5000mg".
        """
        unit = item.get("unit")
        unit_text = ""
        if unit is not None:
            if str(unit).strip() != "":
                unit_text = str(unit).strip()

        powder_tray_code = int(ResourceCode.POWDER_BUCKET_TRAY_30ML)
        bottle_tray_codes = {
            int(ResourceCode.REAGENT_BOTTLE_TRAY_2ML),
            int(ResourceCode.REAGENT_BOTTLE_TRAY_8ML),
            int(ResourceCode.REAGENT_BOTTLE_TRAY_40ML),
            int(ResourceCode.REAGENT_BOTTLE_TRAY_125ML),
        }

        if tray_code is not None and tray_code == powder_tray_code:
            amount_fields = (
                "available_weight",
                "cur_weight",
                "initial_weight"
            )
        elif tray_code is not None and tray_code in bottle_tray_codes:
            amount_fields = (
                "available_volume",
                "cur_volume",
                "initial_volume",
            )
        else:
            amount_fields = (
                "available_weight",
                "available_volume",
                "cur_weight",
                "cur_volume",
                "initial_weight",
                "initial_volume",
            )

        amount_value = None
        for field_name in amount_fields:
            if field_name in item:
                candidate = item.get(field_name)
                if candidate is not None:
                    amount_value = candidate
                    break

        amount_text = self._format_number(amount_value)
        if unit_text == "":
            return amount_text
        return f"{amount_text}{unit_text}"

    def _format_number(self, value: Any) -> str:
        """
        功能:
            将数值格式化为展示字符串, 整数不带小数, 小数去除尾随 0.
        参数:
            value: Any, 输入数值.
        返回:
            str, 格式化后的数字字符串, value 为空时返回 "0".
        """
        if value is None:
            return "0"

        try:
            number_value = float(value)
        except Exception:
            return str(value)

        if abs(number_value - round(number_value)) < 1e-9:
            return str(int(round(number_value)))

        text = f"{number_value:.6f}".rstrip("0").rstrip(".")
        if text == "":
            return "0"
        return text

    def _safe_int(self, value: Any) -> Optional[int]:
        """
        功能:
            安全转换为 int, 转换失败返回 None.
        参数:
            value: Any, 输入值.
        返回:
            Optional[int], 转换结果.
        """
        try:
            return int(value)
        except Exception:
            return None

    # ---------- 编辑站内化学品 ----------
    def get_chemical_list(
        self,
        *,
        query_key: Optional[str] = None,
        limit: int = 20,
    ) -> JsonDict:
        """
        功能:
            调用底层接口获取化学品列表，返回 chemical_sums 和 chemical_list
        参数:
            query_key: 可选字符串，用于模糊查询
            limit: 返回条数，传负数则不传该参数
        返回:
            Dict[str, Any]，包含 chemical_sums 和 chemical_list
        """
        params = {
            "query_key": query_key,
            "sort": "desc",   # 固定默认排序
            "offset": 0,      # 固定起始偏移
            "limit": limit,
        }
        filtered_params = {
            key: val
            for key, val in params.items()
            if val is not None and not (key == "limit" and isinstance(val, int) and val < 0)
        }

        resp = self._call_with_relogin(self._client.get_chemical_list, **filtered_params)
        data = resp if "chemical_list" in resp else (resp.get("result") or resp.get("data") or resp)

        chemical_sums = data.get("chemical_sums")
        chemical_list = data.get("chemical_list", [])
        return {"chemical_sums": chemical_sums, "chemical_list": chemical_list}

    def get_all_chemical_list(self) -> JsonDict:
        """
        功能:
            一次性获取全部化学品列表并返回包含 chemical_sums 和 chemical_list 的字典
        参数:
            无
        返回:
            Dict[str, Any], 包含 chemical_sums 和 chemical_list
        """
        first_resp = self.get_chemical_list()  # 默认 limit=20
        first_data = first_resp.get("result") or first_resp.get("data") or first_resp
        total = first_data.get("chemical_sums")
        if not isinstance(total, int):
            raise ValidationError(f"响应缺少 chemical_sums, resp={first_resp}")

        first_list = first_data.get("chemical_list") or []
        if len(first_list) >= total:
            return {"chemical_sums": total, "chemical_list": first_list}

        full_resp = self.get_chemical_list(limit=total)  # 用总数作为 limit
        full_data = full_resp.get("result") or full_resp.get("data") or full_resp
        full_list = full_data.get("chemical_list") or []
        return {"chemical_sums": total, "chemical_list": full_list}

    def add_chemical(self, payload: JsonDict) -> JsonDict:
        return self._call_with_relogin(self._client.add_chemical, payload)

    def update_chemical(self, payload: JsonDict) -> JsonDict:
        return self._call_with_relogin(self._client.update_chemical, payload)
    
    def delete_chemical(self, chemical_id: int) -> JsonDict:
        """
        功能:
            删除单个化学品
        参数:
            chemical_id: int, 化学品 id
        返回:
            Dict[str, Any], 接口响应
        """
        resp = self._call_with_relogin(self._client.delete_chemical, chemical_id)
        try:
            self._assert_success(resp, "删除化学品")
            self._logger.info("删除化学品成功: chemical_id=%s, resp=%s", chemical_id, resp)
        except ValidationError as exc:
            self._logger.info("删除化学品失败: chemical_id=%s, resp=%s", chemical_id, resp)
            raise exc
        return resp

    def sync_chemicals_from_data(self, items: List[JsonDict], *, overwrite: bool = False, limit: int = 20000) -> None:
        """
        功能:
            接收化学品数据列表，逐条查询是否存在，按需新增或更新
        参数:
            items: List[Dict], 包含 name, cas, state 等字段的字典列表
            overwrite: Bool, 是否覆盖更新
            limit: 查询 limit
        返回:
            None
        """
        if not items:
            self._logger.info("输入数据为空, 退出同步")
            return

        for item in items:
            name = item.get("name")
            cas = item.get("cas")
            candidates: List[JsonDict] = []

            # 先按名称查询
            if name:
                name_resp = self.get_chemical_list(query_key=name, limit=limit)
                candidates.extend(name_resp.get("chemical_list", []))

            # 如有 CAS 再按 CAS 查询
            if cas:
                cas_resp = self.get_chemical_list(query_key=cas, limit=limit)
                candidates.extend(cas_resp.get("chemical_list", []))

            # 按 fid 去重
            unique = {}
            for chem in candidates:
                fid = chem.get("fid")
                if fid is not None:
                    unique[fid] = chem
            matched = list(unique.values())

            if not matched:
                self.add_chemical(item)
                self._logger.info("新增化学品: %s", name)
                continue

            if not overwrite:
                self._logger.info("已存在化学品, 跳过: %s", name)
                continue

            target = matched[0]
            payload = dict(item)
            payload["fid"] = target["fid"]
            self.update_chemical(payload)
            self._logger.info("已覆盖更新化学品: %s, fid=%s", name, target["fid"])

    # ---------- 化合物库管理 ----------
    def check_chemical_library_data(self, rows: List[JsonDict], headers: List[str]) -> Dict[str, List[str]]:
        """
        功能:
            校验化学品库数据完整性，检查表头、重复项与必填字段，返回错误和警告列表
        参数:
            rows: List[Dict[str, Any]], 文件数据行，键名需与表头一致
            headers: List[str], 原始表头列表
        返回:
            Dict[str, List[str]], 包含 errors 与 warnings
        """
        required_headers = [
            "cas_number", "substance", "storage_location",
            "molecular_weight", "physical_state", "density (g/mL)"
        ]
        header= [str(col).strip() for col in headers]

        errors: List[str] = []
        warnings: List[str] = []

        # 汇总缺失表头
        missing_headers = [col for col in required_headers if col not in header]
        if len(missing_headers) > 0:
            errors.append(f"表头缺少字段: {', '.join(missing_headers)}")

        substance_count: Dict[str, int] = {}
        missing_state_names: List[str] = []
        solid_missing_mw: List[str] = []
        liquid_missing: List[str] = []

        for idx, row in enumerate(rows, start=2):
            # 逐行检查必填项
            substance = str(row.get("substance") or "").strip()
            physical_state = str(row.get("physical_state") or "").strip()
            cas_number = str(row.get("cas_number") or "").strip()
            molecular_weight = str(row.get("molecular_weight") or "").strip()
            density = str(row.get("density (g/mL)") or "").strip()

            if substance != "":
                substance_count[substance] = substance_count.get(substance, 0) + 1

            if physical_state == "":
                label = substance if substance != "" else f"第{idx}行"
                missing_state_names.append(label)

            state_lower = physical_state.lower()
            if cas_number != "" and state_lower == "solid" and molecular_weight == "":
                target = substance if substance != "" else cas_number
                solid_missing_mw.append(target)

            if state_lower == "liquid":
                if molecular_weight == "" or density == "":
                    target = substance if substance != "" else cas_number if cas_number != "" else f"第{idx}行"
                    liquid_missing.append(target)

        duplicated_substance = [name for name, count in substance_count.items() if count > 1]
        if len(duplicated_substance) > 0:
            errors.append(f"substance 出现重复: {', '.join(duplicated_substance)}")

        if len(missing_state_names) > 0:
            errors.append(f"physical_state 未填写: {', '.join(missing_state_names)}")

        if len(solid_missing_mw) > 0:
            warnings.append(f"存在 solid 且有 CAS 但缺少 molecular_weight: {', '.join(solid_missing_mw)}")

        if len(liquid_missing) > 0:
            warnings.append(f"physical_state 为 liquid 但缺少 molecular_weight 或 density: {', '.join(liquid_missing)}")

        if len(errors) > 0:
            self._logger.error("化学品库校验失败, 错误=%s, 警告=%s", len(errors), len(warnings))
        else:
            self._logger.info("化学品库校验通过, 警告=%s", len(warnings))

        return {"errors": errors, "warnings": warnings}

    def deduplicate_chemical_library_data(self, rows: List[JsonDict], headers: List[str]) -> List[JsonDict]:
        """
        功能:
            依据 substance 自动去重化学品库，合并 brand/package_size/storage_location，处理其他字段差异
        参数:
            rows: List[Dict[str, Any]], 表格行数据，键名与表头一致
            headers: List[str], 原始表头列表，用于保持输出列顺序
        返回:
            List[Dict[str, Any]], 去重后的数据
        """
        if not headers or "substance" not in [str(h).strip().lower() for h in headers]:
            self._logger.error("表头缺少 substance，无法去重")
            return rows

        brand_fields = {"brand", "package_size", "storage_location"}
        header_info = [(str(h).strip(), str(h).strip().lower()) for h in headers]
        dedup_map: Dict[str, Dict[str, List[str]]] = {}
        order: List[str] = []

        def _clean(val: Any) -> str:
            if val is None:
                return ""
            if isinstance(val, float):
                if math.isnan(val):
                    return ""
                if val.is_integer():
                    return str(int(val))  # 374.0 -> "374"
                return str(val).rstrip("0").rstrip(".")  # 去掉尾随0或小数点
            if isinstance(val, int):
                return str(val)
            text = str(val).strip()
            return "" if text.lower() == "nan" else text

        for row in rows:
            substance = _clean(row.get("substance") or row.get("Substance"))
            if substance == "":
                self._logger.warning("发现缺少 substance 的行，已跳过")
                continue
            if substance not in dedup_map:
                dedup_map[substance] = {info[1]: [] for info in header_info}
                order.append(substance)

            field_store = dedup_map[substance]
            for orig, key in header_info:
                val = _clean(row.get(orig))
                if val == "":
                    continue
                # 合并品牌/包装/库位，跳过空值
                if key in brand_fields:
                    if val not in field_store[key]:
                        field_store[key].append(val)
                    continue
                # 其他字段保留不同值以便冲突时加括号
                if val not in field_store[key]:
                    field_store[key].append(val)

        result: List[JsonDict] = []
        for substance in order:
            field_store = dedup_map[substance]
            out_row: JsonDict = {}
            for orig, key in header_info:
                vals = field_store.get(key, [])
                if key == "substance":
                    out_row[orig] = substance
                elif key in brand_fields:
                    out_row[orig] = ";".join(vals)
                else:
                    if len(vals) == 0:
                        out_row[orig] = ""
                    elif len(vals) == 1:
                        out_row[orig] = vals[0]
                    else:
                        out_row[orig] = f"({';'.join(vals)})"
            result.append(out_row)

        self._logger.info("化合物库去重完成，原始行数=%s，去重后=%s", len(rows), len(result))
        return result

    def align_chemicals_from_data(self, rows: List[JsonDict], *, auto_delete: bool = False) -> List[JsonDict]:
        """
        功能:
            对齐工站化学品: 以传入数据为准校正工站数据，并回填 chemical_id/fid 到返回数据中
        参数:
            rows: List[Dict], 包含 substance/name, cas, physical_state 等字段
            auto_delete: bool, 是否删除工站内多余的化学品
        返回:
            List[Dict]: 更新后的数据列表 (包含回填的 ID)
        """
        if not rows:
            return []

        # 获取工站所有数据
        station_data = self.get_all_chemical_list()
        station_list = station_data.get("chemical_list") or []
        station_by_name = {
            str(item.get("name") or "").strip(): item for item in station_list if item.get("name")
        }

        updated = 0
        added = 0
        fid_by_substance: Dict[str, int] = {}

        # 遍历输入行进行同步
        for row in rows:
            # 兼容字段名：substance 或 name
            name = str(row.get("substance") or row.get("name") or "").strip()
            if not name:
                continue
            
            cas_file = str(row.get("cas_number") or row.get("cas") or "").strip()
            state_file = str(row.get("physical_state") or row.get("state") or "").strip()

            existing = station_by_name.get(name)
            
            # Case A: 工站不存在 -> 新增
            if existing is None:
                payload = {"name": name}
                if cas_file: payload["cas"] = cas_file
                if state_file: payload["state"] = state_file
                
                try:
                    resp = self.add_chemical(payload)
                    fid = resp.get("fid") or resp.get("chemical_id")
                    if isinstance(fid, int):
                        fid_by_substance[name] = fid
                    added += 1
                except Exception as e:
                    self._logger.error(f"新增失败 {name}: {e}")
                continue

            # Case B: 工站存在 -> 检查更新
            fid = existing.get("fid")
            fid_by_substance[name] = fid if isinstance(fid, int) else None

            payload = {k: v for k, v in existing.items() if v is not None}
            payload["fid"] = fid
            payload["name"] = existing.get("name") # 保持原名

            need_update = False
            cas_station = str(existing.get("cas") or "").strip()
            if cas_file and cas_file != cas_station:
                payload["cas"] = cas_file
                need_update = True

            state_station = str(existing.get("state") or "").strip()
            if state_file and state_file != state_station:
                payload["state"] = state_file
                need_update = True

            if need_update:
                self.update_chemical(payload)
                updated += 1

        # 自动删除逻辑
        if auto_delete:
            file_names = {
                str(r.get("substance") or r.get("name") or "").strip() for r in rows
            }
            for item in station_list:
                s_name = str(item.get("name") or "").strip()
                s_fid = item.get("fid")
                if s_name and s_name not in file_names and isinstance(s_fid, int):
                    self.delete_chemical(s_fid)

        # 回写 ID 到原数据结构
        result_rows = []
        for row in rows:
            new_row = row.copy()
            name = str(new_row.get("substance") or new_row.get("name") or "").strip()
            fid = fid_by_substance.get(name)
            if isinstance(fid, int):
                new_row["chemical_id"] = fid # 统一回填到 chemical_id 字段
            result_rows.append(new_row)

        self._logger.info("化学品对齐逻辑执行完毕: 更新=%s, 新增=%s", updated, added)
        return result_rows
    
    # ---------- 上料函数 ----------
    def _well_to_slot_index(self, well: str, tray_spec: Optional[Tuple[int, int]]) -> Optional[int]:
        """
        功能:
            将井位文本（如 A1、B2）转换为 slot 序号，按行优先编号。
        参数:
            well: str, 井位文本，格式 字母+数字，如 A1。
            tray_spec: Optional[Tuple[int, int]], (列数, 行数)。
        返回:
            Optional[int], 对应的 slot 序号，无法解析时返回 None。
        """
        if tray_spec is None:
            return None
        if not well or len(well) < 2:
            return None
        row_char = well[0].upper()
        if not row_char.isalpha():
            return None
        try:
            col_count, row_count = tray_spec
            col_index = int(well[1:])  # 1-based
            row_index = ord(row_char) - ord("A")  # 0-based
            if col_index < 1 or col_index > col_count:
                return None
            if row_index < 0 or row_index >= row_count:
                return None
            return row_index * col_count + (col_index - 1)
        except Exception:
            return None

    def _normalize_tray_code_text(self, raw: Any) -> str:
        """
        功能:
            从类似“50 μL Tip 头托盘(201000815)”提取括号内的编码; 如果没有括号则返回原文本.
        参数:
            raw: Any, 单元格原始值.
        返回:
            str, 纯数字编码字符串或原文本.
        """
        if raw is None:
            return ""
        text = str(raw).strip()
        if "(" in text and ")" in text:
            inside = text[text.rfind("(") + 1:text.rfind(")")]
            digits = "".join(ch for ch in inside if ch.isdigit())
            if digits != "":
                return digits
        return text

    def _split_amount_unit(self, text: str) -> Tuple[float, str]:
        """
        功能:
            将类似 '2mL' 或 '500mg' 的文本拆为数值和单位.
        参数:
            text: str, 输入文本.
        返回:
            (float, str), 数值与单位, 无法解析数值时返回 0.
        """
        number_part = ""
        unit_part = ""
        for ch in str(text):
            if ch.isdigit() or ch == ".":
                number_part += ch
            else:
                unit_part += ch
        try:
            value = float(number_part) if number_part != "" else 0.0
        except Exception:
            value = 0.0
        unit = unit_part.strip() if unit_part.strip() != "" else "mL"
        return value, unit

    def batch_in_tray(self, resource_req_list: List[JsonDict]) -> JsonDict:
        return self._call_with_relogin(self._client.batch_in_tray, resource_req_list)
    
    def build_batch_in_tray_payload(self, rows: List[Tuple[str, str, str]]) -> List[JsonDict]:
        """
        功能:
            将清洗后的上料数据转换为 API Payload
        参数:
            rows: List[Tuple], 每个元素为 (position, tray_type_text, content_text)
        返回:
            List[Dict]: resource_req_list
        """
        # 内部映射表
        tray_to_media = {
            int(ResourceCode.REAGENT_BOTTLE_TRAY_2ML): (
                str(int(ResourceCode.REAGENT_BOTTLE_2ML)), True, "volume", "mL"
            ),
            int(ResourceCode.REAGENT_BOTTLE_TRAY_8ML): (
                str(int(ResourceCode.REAGENT_BOTTLE_8ML)), True, "volume", "mL"
            ),
            int(ResourceCode.REAGENT_BOTTLE_TRAY_40ML): (
                str(int(ResourceCode.REAGENT_BOTTLE_40ML)), True, "volume", "mL"
            ),
            int(ResourceCode.REAGENT_BOTTLE_TRAY_125ML): (
                str(int(ResourceCode.REAGENT_BOTTLE_125ML)), True, "volume", "mL"
            ),
            int(ResourceCode.POWDER_BUCKET_TRAY_30ML): (
                str(int(ResourceCode.POWDER_BUCKET_30ML)), False, "weight", "mg"
            ),
        }
        no_substance_trays = {
            int(ResourceCode.TIP_TRAY_50UL), int(ResourceCode.TIP_TRAY_1ML), int(ResourceCode.TIP_TRAY_5ML),
            int(ResourceCode.REACTION_SEAL_CAP_TRAY), int(ResourceCode.FLASH_FILTER_INNER_BOTTLE_TRAY),
            int(ResourceCode.FLASH_FILTER_OUTER_BOTTLE_TRAY),
            int(ResourceCode.REACTION_TUBE_TRAY_2ML), int(ResourceCode.TEST_TUBE_MAGNET_TRAY_2ML),
        }
        consumable_map = {
            int(ResourceCode.TIP_TRAY_50UL): int(ResourceCode.TIP_50UL),
            int(ResourceCode.TIP_TRAY_1ML): int(ResourceCode.TIP_1ML),
            int(ResourceCode.TIP_TRAY_5ML): int(ResourceCode.TIP_5ML),
            int(ResourceCode.REACTION_SEAL_CAP_TRAY): int(ResourceCode.REACTION_SEAL_CAP),
            int(ResourceCode.FLASH_FILTER_INNER_BOTTLE_TRAY): int(ResourceCode.FLASH_FILTER_INNER_BOTTLE),
            int(ResourceCode.FLASH_FILTER_OUTER_BOTTLE_TRAY): int(ResourceCode.FLASH_FILTER_OUTER_BOTTLE),
            int(ResourceCode.REACTION_TUBE_TRAY_2ML): int(ResourceCode.REACTION_TUBE_2ML),
            int(ResourceCode.TEST_TUBE_MAGNET_TRAY_2ML): int(ResourceCode.TEST_TUBE_MAGNET_2ML),
        }

        # 简单的内存缓存
        chem_cache: Dict[str, Optional[int]] = {}

        def _resolve_fid(sub_name: str) -> int:
            if sub_name in chem_cache:
                if chem_cache[sub_name] is None: raise ValidationError(f"未找到化学品: {sub_name}")
                return chem_cache[sub_name]
            
            # 调用 API 查询
            resp = self.get_chemical_list(query_key=sub_name, limit=10)
            lst = resp.get("chemical_list", [])
            for c in lst:
                if str(c.get("name")).strip() == sub_name:
                    fid = c.get("fid") or c.get("chemical_id")
                    chem_cache[sub_name] = fid
                    return fid
            
            # 模糊匹配 fallback
            if lst:
                fid = lst[0].get("fid") or lst[0].get("chemical_id")
                chem_cache[sub_name] = fid
                return fid
            
            chem_cache[sub_name] = None
            raise ValidationError(f"未找到化学品: {sub_name}")

        resource_req_list: List[JsonDict] = []

        for position, tray_type_raw, content in rows:
            tray_layout = str(position).strip()
            if not tray_layout: continue
            
            tray_code_text = self._normalize_tray_code_text(tray_type_raw)
            tray_code_int = self._safe_int(tray_code_text)
            if tray_code_int is None: continue

            resource_list: List[JsonDict] = []
            # 托盘本体
            resource_list.append({
                "layout_code": f"{tray_layout}:-1",
                "resource_type": str(tray_code_int),
            })

            # 分支 A: 无物质耗材 (Tip头等)
            if tray_code_int in no_substance_trays:
                qty = self._safe_int(content) or 0
                if qty <= 0: continue
                
                # 限制最大容量
                spec = self._get_tray_spec(tray_code_int)
                cap = (spec[0] * spec[1]) if spec else qty
                qty = min(qty, cap)
                
                res_type = str(consumable_map.get(tray_code_int, tray_code_int))
                for idx in range(qty):
                    resource_list.append({
                        "layout_code": f"{tray_layout}:{idx}",
                        "resource_type": res_type,
                        "with_cap": False
                    })
            
            # 分支 B: 有物质容器 (试剂瓶/粉桶)
            else:
                entries = [seg.strip() for seg in str(content).split(";") if seg.strip()]
                media_code, with_cap, amt_kind, def_unit = tray_to_media.get(
                    tray_code_int, (str(tray_code_int), False, "volume", "mL")
                )
                tray_spec = self._get_tray_spec(tray_code_int)

                for seg in entries:
                    parts = [p.strip() for p in seg.split("|")]
                    if len(parts) < 3: continue
                    
                    slot_raw, substance, amt_str = parts[0], parts[1], parts[2]
                    
                    slot_idx = self._safe_int(slot_raw)
                    if slot_idx is None:
                        slot_idx = self._well_to_slot_index(slot_raw, tray_spec)
                    if slot_idx is None: continue

                    val, unit = self._split_amount_unit(amt_str)
                    if not unit: unit = def_unit

                    fid = _resolve_fid(substance)

                    media_item = {
                        "layout_code": f"{tray_layout}:{slot_idx}",
                        "resource_type": media_code,
                        "with_cap": with_cap,
                        "substance": substance,
                        "unit": unit,
                        "chemical_id": fid
                    }
                    
                    if amt_kind == "volume":
                        media_item["initial_volume"] = val
                    else:
                        media_item["initial_weight"] = val
                    
                    resource_list.append(media_item)

            resource_req_list.append({
                "remark": "",
                "resource_list": resource_list
            })

        self._logger.info("已生成上料 Payload, 包含 %s 个托盘", len(resource_req_list))
        return resource_req_list

    # ---------- 下料函数 ----------
    def batch_out_tray(self, layout_codes: List[str], move_type: str = "main_out") -> JsonDict:
        """
        功能:
            批量下料, 接收托盘位置编码列表并调用出料接口.
        参数:
            layout_codes: List[str], 资源位置编码列表, 如 ["N-4", "N-5"].
            move_type: 下料方式, 默认 "main_out".
        返回:
            Dict, 接口响应.
        """
        if layout_codes is None:
            raise ValidationError("layout_codes 不能为空")
        if len(layout_codes) == 0:
            raise ValidationError("layout_codes 不能为空")

        layout_list: List[JsonDict] = []
        for code in layout_codes:
            if code is None:
                continue
            text = str(code).strip()
            if text == "":
                continue
            layout_list.append({"layout_code": text})  # 构造 API 需要的格式

        if len(layout_list) == 0:
            raise ValidationError("layout_codes 解析后为空")

        resp = self._call_with_relogin(self._client.batch_out_tray, layout_list, move_type)
        self._assert_success(resp, "批量下料")
        self._logger.info("批量下料开始执行")
        return resp

    # ---------- 清空站内资源（慎用） ----------
    def clear_tray_shelf(self) -> JsonDict:
        """
        功能:
            清空站内托盘货架.
        参数:
            无.
        返回:
            Dict, 接口响应.
        """
        return self._call_with_relogin(self._client.clear_tray_shelf)

    # ---------- 开关外舱门 ----------
    def open_close_door(self, op: str, *, station: str = "FSY", door_num: int = 0) -> JsonDict:
        """
        功能:
            打开/关闭过渡舱门
        参数:
            op: "open" 或 "close".
            station: 站点编码，默认 "FSY".
            door_num: 门编号，默认 0.
        返回:
            Dict, 接口响应.
        """
        return self._call_with_relogin(self._client.open_close_door, station, op, door_num)

    # ---------- 任务模块 ----------  未完成
    def add_task(self, payload: JsonDict) -> JsonDict:
        return self._call_with_relogin(self._client.add_task, payload)

    def start_task(self, task_id: int) -> JsonDict:
        return self._call_with_relogin(self._client.start_task, task_id)

    def stop_task(self, task_id: int) -> JsonDict:
        return self._call_with_relogin(self._client.stop_task, task_id)

    def cancel_task(self, task_id: int) -> JsonDict:
        return self._call_with_relogin(self._client.cancel_task, task_id)

    def delete_task(self, task_id: int) -> JsonDict:
        return self._call_with_relogin(self._client.delete_task, task_id)
    
    def get_task_info(self, task_id: int) -> JsonDict:
        return self._call_with_relogin(self._client.get_task_info, task_id)

    def get_task_list(
        self,
        *,
        sort: str = "desc",
        offset: int = 0,
        limit: int = 20,
        status: Optional[List[int]] = None,
    ) -> JsonDict:
        """
        功能:
            获取任务列表, 对应 GetTaskList
        参数:
            sort: 排序方式, 默认按创建时间倒序
            offset: 数据起点
            limit: 数据限制
            status: 任务状态列表, 例如 [0, 1]
        返回:
            Dict, 接口响应
        """
        body: JsonDict = {
            "sort": sort,
            "offset": offset,
            "limit": limit,
        }
        if status is not None:
            body["status"] = status  # 传递状态过滤
        return self._call_with_relogin(self._client.get_task_list, body)

    def _extract_task_sums(self, resp: JsonDict) -> Optional[int]:
        """
        功能:
            从任务列表响应中提取 task_sums 总数
        参数:
            resp: 任务列表接口响应
        返回:
            Optional[int], 任务总数
        """
        if "task_sums" in resp and isinstance(resp.get("task_sums"), int):
            return int(resp["task_sums"])
        for outer in ("result", "data"):
            outer_obj = resp.get(outer)
            if isinstance(outer_obj, dict) and isinstance(outer_obj.get("task_sums"), int):
                return int(outer_obj["task_sums"])
        return None

    def get_all_tasks(self) -> JsonDict:
        """
        功能:
            获取全部任务列表, 先用一次 GetTaskList 读取 task_sums, 再用 limit 拉全量
        参数:
            无
        返回:
            Dict, 包含完整任务列表
        """
        first_resp = self.get_task_list(limit=1, offset=0, sort="desc")
        task_sums = self._extract_task_sums(first_resp)
        if task_sums is None:
            raise ValidationError(f"GetTaskList 未返回 task_sums, resp={first_resp}")
        self._logger.info("开始获取全部任务列表, total=%s", task_sums)  # 记录预期条数
        return self.get_task_list(limit=task_sums, offset=0, sort="desc")

    def _extract_task_status(self, task_info: JsonDict) -> Optional[int]:
        """
        功能:
            从 GetTaskInfo 返回中提取 status, 兼容不同字段层级。
        参数:
            task_info: 任务详情响应.
        返回:
            Optional[int], 解析出的状态码.
        """
        if "status" in task_info and isinstance(task_info.get("status"), int):
            return int(task_info["status"])

        for key in ("result", "data"):
            obj = task_info.get(key)
            if isinstance(obj, dict) and isinstance(obj.get("status"), int):
                return int(obj["status"])

        return None

    def wait_task(
        self,
        task_id: int,
        *,
        timeout_s: float = 3600.0,
        poll_interval_s: float = 2.0,
        done_status: Optional[List[int]] = None,
        fail_status: Optional[List[int]] = None,
    ) -> int:
        """
        功能:
            轮询任务状态直到结束或超时。
        参数:
            task_id: 任务 id.
            timeout_s: 超时秒数.
            poll_interval_s: 轮询间隔秒数.
            done_status: 视为完成的状态集合, 默认 [COMPLETED].
            fail_status: 视为失败的状态集合, 默认 [FAILED, STOPPED].
        返回:
            int, 最终 status.
        """
        done = done_status or [int(TaskStatus.COMPLETED)]
        fail = fail_status or [int(TaskStatus.FAILED), int(TaskStatus.STOPPED)]

        start_ts = time.time()
        last_status = None

        while True:
            info = self.get_task_info(task_id)
            status = self._extract_task_status(info)

            if status is not None and status != last_status:
                self._logger.info("任务状态变化 task_id=%s, status=%s", task_id, status)
                last_status = status

            if status in done:
                return int(status)
            if status in fail:
                return int(status)

            if time.time() - start_ts > timeout_s:
                raise TimeoutError(f"任务超时, task_id={task_id}, last_status={status}")

            time.sleep(poll_interval_s)

    def create_and_start_task(self, payload: JsonDict) -> int:
        """                 
        28
        功能:
            创建任务并启动, 返回 task_id.
        参数:
            payload: AddTask 请求体.
        返回:
            int, task_id.
        """
        resp = self.add_task(payload)
        task_id = resp.get("task_id") or resp.get("result", {}).get("task_id")
        if not isinstance(task_id, int):
            raise ValidationError(f"AddTask 未返回 task_id, resp={resp}")
        self.start_task(int(task_id))
        return int(task_id)

    # ---------- 任务生成 (Excel/CSV 模板) ----------
    def build_task_payload(self, params: Dict[str, Any], headers: List[str], 
                          data_rows: List[List[Any]], chemical_db: Dict[str, Any]) -> JsonDict:
        """
        功能:.
            将结构化的实验数据转换为 AddTask API Payload
        参数:
            params: 实验全局参数 (反应时间、温度等)
            headers: 实验数据表头列表
            data_rows: 实验数据行 (每行为值列表)
            chemical_db: 化学品信息字典
        返回:
            Dict: AddTask 请求体
        """
        # 1. 参数预处理
        try:
            weighing_error_pct = float(str(params.get("称量误差(%)", 1)).replace('%', ''))
        except:
            weighing_error_pct = 1.0

        try:
            max_error_mg = float(str(params.get("最大称量误差(mg)", 1)).strip())
        except:
            max_error_mg = 1.0

        auto_magnet = str(params.get("自动加磁子", "是")).strip() == "是"
        fixed_order = str(params.get("固定加料顺序", "否")).strip() == "是"
        exp_count = len(data_rows)
        
        if exp_count not in [12, 24, 36, 48]:
            # 这里的限制取决于硬件，暂时警告
            self._logger.warning(f"实验数量 {exp_count} 非标准 (12/24/36/48)")

        # 2. 全局列分析 (Global Mapping) - 不依赖 Pandas
        # col_metadata 结构: {col_idx, type, max_vol, name, is_reagent_group}
        col_metadata = []
        col_idx = 0
        
        while col_idx < len(headers):
            header = headers[col_idx]
            
            if "试剂" in header:
                # 这一组是 (试剂名称, 试剂量)
                # 扫描该列所有行确定类型
                is_liquid, is_solid, is_magnet_manual = False, False, False
                max_vol = 0.0

                for row in data_rows:
                    if col_idx >= len(row): continue
                    c_name = str(row[col_idx]).strip()
                    if not c_name or c_name == "0": continue
                    
                    if c_name == "加磁子":
                        is_magnet_manual = True
                    elif c_name in chemical_db:
                        state = chemical_db[c_name].get('physical_state', '').lower()
                        if 'liquid' in state: is_liquid = True
                        if 'solid' in state: is_solid = True
                        
                        # 计算最大体积用于排序
                        if 'liquid' in state and (col_idx + 1 < len(row)):
                            amt_val, amt_unit = self._split_amount_unit(str(row[col_idx+1]))
                            vol_ml = amt_val if amt_unit in ['ml', 'mL'] else amt_val / 1000.0
                            if vol_ml > max_vol: max_vol = vol_ml

                final_type = 'other'
                if is_magnet_manual: final_type = 'magnet_manual'
                elif is_liquid: final_type = 'liquid'
                elif is_solid: final_type = 'solid'

                col_metadata.append({
                    "col_idx": col_idx,
                    "type": final_type,
                    "max_vol": max_vol,
                    "is_reagent_group": True
                })
                col_idx += 2 # 跳过 Amount 列

            elif "加磁子" in header:
                col_metadata.append({
                    "col_idx": col_idx,
                    "type": "magnet_manual",
                    "max_vol": 0,
                    "is_reagent_group": False
                })
                col_idx += 1
            else:
                col_idx += 1

        # 3. 确定执行顺序
        VIRTUAL_MAGNET_COL_IDX = -999
        ordered_cols = []

        if not fixed_order:
            # 排序策略: 固体 -> 自动磁子 -> 手动磁子 -> 液体(体积降序) -> 其他
            solids = [c for c in col_metadata if c['type'] == 'solid']
            manual_magnets = [c for c in col_metadata if c['type'] == 'magnet_manual']
            liquids = [c for c in col_metadata if c['type'] == 'liquid']
            liquids.sort(key=lambda x: x['max_vol'], reverse=True)
            others = [c for c in col_metadata if c['type'] not in ['solid', 'liquid', 'magnet_manual']]

            ordered_cols.extend(solids)
            if auto_magnet:
                ordered_cols.append({"col_idx": VIRTUAL_MAGNET_COL_IDX, "type": "magnet_auto"})
            ordered_cols.extend(manual_magnets)
            ordered_cols.extend(liquids)
            ordered_cols.extend(others)
        else:
            # 固定顺序策略: 保持原序，首个液体前插磁子
            inserted_magnet = False
            for c in col_metadata:
                if auto_magnet and not inserted_magnet and c['type'] == 'liquid':
                    ordered_cols.append({"col_idx": VIRTUAL_MAGNET_COL_IDX, "type": "magnet_auto"})
                    inserted_magnet = True
                ordered_cols.append(c)
            if auto_magnet and not inserted_magnet:
                ordered_cols.append({"col_idx": VIRTUAL_MAGNET_COL_IDX, "type": "magnet_auto"})

        # 4. 行号映射
        col_to_row_map = {}
        curr_row = 0
        for item in ordered_cols:
            col_to_row_map[item['col_idx']] = curr_row
            curr_row += 1
        
        # 固定操作行号
        ROW_IDX_REACTION = curr_row + 1
        ROW_IDX_INT_STD = curr_row + 2
        ROW_IDX_STIR_AFTER = curr_row + 3
        ROW_IDX_FILTER = curr_row + 4

        # 5. 生成 Layout List
        layout_list = []
        common_fields = {
            "layout_code": "", "src_layout_code": "", "resource_type": "551000502",  
            "status": 0, "tray_QR_code": "", "QR_code": ""                   
        }

        for exp_idx, row_vals in enumerate(data_rows):
            unit_column = exp_idx 
            
            # 遍历排好序的列定义
            for item in ordered_cols:
                c_idx = item['col_idx']
                target_row = col_to_row_map[c_idx]

                # A. 自动加磁子
                if c_idx == VIRTUAL_MAGNET_COL_IDX:
                    has_explicit = False
                    for val in row_vals:
                        if str(val).strip() == "加磁子":
                            has_explicit = True
                            break
                    if not has_explicit:
                        self._add_unit_magnet(layout_list, common_fields, unit_column, target_row)
                    continue

                # B. 真实数据列
                if c_idx >= len(row_vals): continue
                val_name = str(row_vals[c_idx]).strip()
                
                if not val_name or val_name == "0": continue
                
                if val_name == "加磁子":
                    self._add_unit_magnet(layout_list, common_fields, unit_column, target_row)
                    continue
                
                if val_name not in chemical_db:
                    raise ValidationError(f"实验 {exp_idx+1}: 未知化学品 '{val_name}'")
                
                chem_info = chemical_db[val_name]
                
                if item.get('is_reagent_group'):
                    # 获取 Amount
                    val_amt_str = str(row_vals[c_idx+1]) if (c_idx+1 < len(row_vals)) else "0"
                    amt_val, amt_unit = self._split_amount_unit(val_amt_str)
                    
                    if amt_val > 0:
                        self._add_reagent_unit(
                            layout_list, common_fields, unit_column, target_row,
                            val_name, chem_info, amt_val, amt_unit, weighing_error_pct,max_error_mg
                        )

            # C. 后续固定操作
            # 反应
            self._add_reaction_unit(layout_list, common_fields, unit_column, ROW_IDX_REACTION, params)
            
            # 内标
            std_name = str(params.get("内标种类", "")).strip()
            if std_name:
                self._add_internal_std_unit(
                    layout_list, common_fields, unit_column, ROW_IDX_INT_STD,
                    std_name, chemical_db, params, weighing_error_pct,max_error_mg
                )
            
            # 搅拌
            stir_t = str(params.get("加入内标后搅拌时间(min)", "")).strip()
            if stir_t:
                 self._add_stir_unit(
                    layout_list, common_fields, unit_column, ROW_IDX_STIR_AFTER,
                    float(stir_t), params
                )
            
            # 过滤
            dil_name = str(params.get("稀释液种类", "")).strip()
            if dil_name:
                self._add_filter_unit(
                    layout_list, common_fields, unit_column, ROW_IDX_FILTER,
                    dil_name, chemical_db, params
                )

        # 6. 组装 Payload
        return {
            "task_id": 0,
            "task_name": str(params.get("实验名称", "AutoTask")),
            "layout_list": layout_list,
            "task_setup": {
                "subtype": None,
                "experiment_num": exp_count,
                "vessel": "551000502",
                "added_slots": ""
            },
            "is_audit_log": 1,
            "is_copy": False
        }
    
    # ---------- 任务生成辅助函数：添加各类 Unit----------
    def _add_reagent_unit(self, layout_list: List[Dict], common_fields: Dict, col: int, row: int, 
                          name: str, chem_info: Dict, amt_val: float, amt_unit: str, error_pct: float, max_error_mg: float):
        """功能: 添加加粉或加液操作"""
        unit_dict = common_fields.copy()
        unit_dict.update({
            "unit_column": col, "unit_row": row, "unit_id": f"unit-{uuid.uuid4().hex[:8]}"
        })

        state = chem_info.get('physical_state', 'unknown').lower()
        mw = float(chem_info.get('molecular_weight', 0) or 0)
        density = float(chem_info.get('density (g/mL)', 0) or 0)
        
        if 'solid' in state:
            target_mg = 0.0
            if amt_unit.lower() == 'mmol': target_mg = amt_val * mw
            elif amt_unit.lower() == 'g': target_mg = amt_val * 1000.0
            elif amt_unit.lower() == 'mg': target_mg = amt_val
            
            calc_offset = target_mg * (error_pct / 100.0)
            final_offset = max(0.1, min(calc_offset, max_error_mg))

            unit_dict.update({
                "unit_type": "exp_add_powder",
                "process_json": {
                    "offset": round(final_offset, 1),
                    "custom": {"unit": "mg", "unitOptions": ["mg", "g"]},
                    "substance": name,
                    "chemical_id": chem_info['chemical_id'],
                    "add_weight": round(target_mg, 1)
                }
            })
            
        elif 'liquid' in state:
            target_vol_ml = 0.0
            if amt_unit.lower() == 'mmol':
                mass_mg = amt_val * mw
                if density > 0: target_vol_ml = (mass_mg / 1000.0) / density
            elif amt_unit.lower() == 'ml': target_vol_ml = amt_val
            elif amt_unit.lower() == 'ul': target_vol_ml = amt_val / 1000.0

            unit_dict.update({
                "unit_type": "exp_pipetting",
                "process_json": {
                    "custom": {"unit": "mL","unitOptions": ["mL","µL","L"]},
                    "substance": name,
                    "chemical_id": chem_info['chemical_id'],
                    "add_volume": round(target_vol_ml,3)
                }
            })
        layout_list.append(unit_dict)

    def _add_unit_magnet(self, layout_list, common_fields, col, row):
        """功能: 添加加磁子操作"""
        unit_dict = common_fields.copy()
        unit_dict.update({
            "unit_type": "exp_add_magnet",
            "unit_column": col, "unit_row": row, "unit_id": f"unit-{uuid.uuid4().hex[:8]}",
            "process_json": {"custom": {"unit": ""}}
        })
        layout_list.append(unit_dict)

    def _add_reaction_unit(self, layout_list: List[JsonDict], common_fields: JsonDict, col: int, row: int, params: JsonDict) -> None:
        """
        功能:
            添加反应操作单元 (Unit), 处理温度与加热状态.
        参数:
            layout_list: List[JsonDict], 任务布局列表.
            common_fields: JsonDict, 通用字段模板.
            col: int, 单元所在列号.
            row: int, 单元所在行号.
            params: JsonDict, 实验参数字典.
        返回:
            无.
        """
        rxn_temp_raw = params.get("反应温度(°C)")
        
        # Determine target temperature from params
        tgt_temp_raw = None
        for key in params.keys():
            if "搅拌后" in str(key) and "温度" in str(key):
                tgt_temp_raw = params[key]
                break

        rxn_time_h = float(params.get("反应时间(h)", 0))
        rxn_rpm = int(params.get("转速(rpm)", 0))
        is_wait = str(params.get("等待目标温度", "否")) == "是"

        process_data = {
            "rotation_speed": rxn_rpm,
            "reaction_duration": int(rxn_time_h * 3600),
            "is_wait": is_wait,
            "custom": {"unit": ""}
        }

        # 1. Reaction Temperature logic: Replace pd.isna with None/Empty check
        if rxn_temp_raw is None or str(rxn_temp_raw).strip() == "":
             process_data["temperature"] = 25
        else:
             process_data["temperature"] = float(rxn_temp_raw)

        # 2. Target Temperature logic: Replace pd.isna with None/Empty check
        if tgt_temp_raw is None or str(tgt_temp_raw).strip() == "":
            process_data["is_heating"] = False
        else:
            try:
                target_t = float(tgt_temp_raw)
                process_data["is_heating"] = True
                process_data["target_temperature"] = target_t
            except ValueError:
                process_data["is_heating"] = False

        unit_dict = common_fields.copy()
        unit_dict.update({
            "unit_type": "exp_magnetic_stirrer",
            "unit_column": col,
            "unit_row": row,
            "unit_id": f"unit-{uuid.uuid4().hex[:8]}",
            "process_json": process_data
        })
        layout_list.append(unit_dict)

    def _add_internal_std_unit(self, layout_list: List[JsonDict], common_fields: JsonDict, col: int, row: int, name: str, db: Dict[str, Any], params: JsonDict, error_pct: float, max_error_mg: float) -> None:
        """
        功能:
            添加内标加料单元.
        参数:
            layout_list: List[JsonDict], 任务布局列表.
            common_fields: JsonDict, 通用字段模板.
            col/row: int, 坐标.
            name: str, 内标物质名称.
            db: Dict, 化学品数据库.
            params: JsonDict, 参数.
            error_pct: float, 允许误差百分比.
        返回:
            无.
        """
        if name not in db:
            return
            
        chem_info = db[name]
        state = chem_info.get('physical_state', 'unknown').lower()
        chem_id = chem_info['chemical_id']
        
        unit_dict = common_fields.copy()
        unit_dict.update({
            "unit_column": col,
            "unit_row": row,
            "unit_id": f"unit-{uuid.uuid4().hex[:8]}"
        })

        if 'solid' in state:
            target_mg = float(params.get("内标用量(μL/mg)", 10.0))
            calc_offset = target_mg * (error_pct / 100.0)
            final_offset = max(0.1, min(calc_offset, min(calc_offset, max_error_mg)))
            
            unit_dict.update({
                "unit_type": "exp_add_powder",
                "process_json": {
                    "offset": round(final_offset, 1),
                    "custom": {"unit": "mg", "unitOptions": ["mg", "g"]},
                    "substance": name,
                    "chemical_id": chem_id,
                    "add_weight": round(target_mg, 1)
                }
            })
        elif 'liquid' in state:
            target_vol_ml = 0.1 
            
            # Replacement for pd.notna: Check if key exists and value is not empty
            val_ul = params.get("内标用量(μL/mg)")

            if val_ul is not None and str(val_ul).strip() != "":
                target_vol_ml = float(val_ul) / 1000.0

            unit_dict.update({
                "unit_type": "exp_pipetting",
                "process_json": {
                    "custom": {"unit": "mL","unitOptions": ["mL","µL","L"]},
                    "substance": name,
                    "chemical_id": chem_id,
                    "add_volume": round(target_vol_ml,3)
                }
            })
        layout_list.append(unit_dict)

    def _add_stir_unit(self, layout_list, common_fields, col, row, time_min, params):
        """功能: 添加搅拌"""
        rxn_rpm = int(params.get("转速(rpm)", 600))
        unit_dict = common_fields.copy()
        unit_dict.update({
            "unit_type": "exp_magnetic_stirrer",
            "unit_column": col, "unit_row": row, "unit_id": f"unit-{uuid.uuid4().hex[:8]}",
            "process_json": {
                "temperature": 25, "rotation_speed": rxn_rpm, "reaction_duration": int(time_min * 60),
                "is_wait": False, "is_heating": False, "target_temperature": 25, "custom": {"unit": ""}
            }
        })
        layout_list.append(unit_dict)

    def _add_filter_unit(self, layout_list, common_fields, col, row, diluent_name, db, params):
        """功能: 添加过滤"""
        if diluent_name not in db: return
        chem_id = db[diluent_name]['chemical_id']
        dilution_vol_ul = float(params.get("稀释量(μL)", 0))
        sample_vol_ul = float(params.get("取样量(μL)", 0))

        unit_dict = common_fields.copy()
        unit_dict.update({
            "unit_type": "exp_filtering_sample",
            "unit_column": col, "unit_row": row, "unit_id": f"unit-{uuid.uuid4().hex[:8]}",
            "process_json": {
                "single_press_num": 6, "substance": diluent_name, "chemical_id": chem_id,
                "add_volume": dilution_vol_ul/1000, "sampling_volume": sample_vol_ul/1000 
            }
        })
        layout_list.append(unit_dict)
  
    def _parse_amount_string(self, amt_str: Any) -> Tuple[float, str]:
        """
        功能:
            解析 '100mg', '5 mmol' 等字符串, 分离数值与单位.
        参数:
            amt_str: Any, 输入的金额字符串或数值.
        返回:
            Tuple[float, str], (数值, 单位).
        """
        # Replacement for pd.isna: check None or empty string
        if amt_str is None:
            return 0, ""
        
        text = str(amt_str).strip().lower()
        if text == "" or text == "0":
            return 0, ""
        
        # Regex matching number + unit
        match = re.match(r"([0-9.]+)\s*([a-z%]+)", text)
        if match:
            return float(match.group(1)), match.group(2)
        
        try:
            return float(text), "unknown"
        except Exception:
            return 0, "error"

    # ---------- 消息通知与故障恢复 ----------
    def notice(self, types: Optional[List[int]] = None) -> JsonDict:
        return self._call_with_relogin(self._client.notice, types)

    def fault_recovery(
        self,
        *,
        ids: Optional[List[int]] = None,
        recovery_type: int = 0,
        resume_task: int = 1,
    ) -> JsonDict:
        return self._call_with_relogin(
            self._client.fault_recovery,
            ids=ids,
            recovery_type=recovery_type,
            resume_task=resume_task,
        )

    # ---------- 方法模块 ---------- 未完成
    def create_method(self, payload: JsonDict) -> JsonDict:
        return self._call_with_relogin(self._client.create_method, payload)

    def update_method(self, task_template_id: int, payload: JsonDict) -> JsonDict:
        return self._call_with_relogin(self._client.update_method, task_template_id, payload)

    def delete_method(self, task_template_id: int) -> JsonDict:
        return self._call_with_relogin(self._client.delete_method, task_template_id)

    def get_method_detail(self, task_template_id: int) -> JsonDict:
        return self._call_with_relogin(self._client.get_method_detail, task_template_id)

    def get_method_list(self, *, limit: int = 20, offset: int = 0, sort: str = "desc") -> JsonDict:
        return self._call_with_relogin(self._client.get_method_list, limit=limit, offset=offset, sort=sort)

    def get_latest_method_detail(self) -> JsonDict:
        """
        功能:
            获取最近一个方法详情, 通过 list(limit=1) 再 detail 的方式实现。
        参数:
            无.
        返回:
            Dict, 方法详情.
        """
        lst = self.get_method_list(limit=1, offset=0, sort="desc")
        data = lst.get("result") or lst.get("data") or lst
        items = data.get("list") if isinstance(data, dict) else None
        if not items:
            raise ValidationError(f"方法列表为空, resp={lst}")
        tid = items[0].get("task_template_id")
        if not isinstance(tid, int):
            raise ValidationError(f"无法解析 task_template_id, item={items[0]}")
        return self.get_method_detail(int(tid))

if __name__ == "__main__":

    settings = Settings.from_env()
    configure_logging(settings.log_level)
    logger = logging.getLogger("station_controller.main")

    controller = SynthesisStationController()

    # #设备输初始化
    # controller.device_init()

    #获取资源列表
    resource_info = controller.get_resource_info()
    out_path = Path("resource_info.json")
    out_path.write_text(json.dumps(resource_info, ensure_ascii=False, indent=2), encoding="utf-8")

    # #获取工站内设备所有信息
    # device_info = controller.list_device_status()
    # print(device_info)
    
    # #获取工站化合物库信息：
    # chemical_info = controller.get_all_chemical_list()
    # print(chemical_info)
    # output_csv = controller.export_chemical_list_to_csv(chemical_info, Path("station_chemical_list.csv"))

    # #从csv文件中新增化合物
    # controller.sync_chemicals_from_csv(Path("add_chemical_list.csv"), overwrite=False)

    # 删除特定ID的化合物
    # controller.delete_chemical(363)

    # #获取手套箱内气体氛围的情况
    # device_info = controller.get_glovebox_env()
    # print(device_info)

    # #批量下料测试
    # out_resp = controller.batch_out_tray(["N-4", "W-2-1","W-2-5","W-3-2"])

    # # 批量上料测试
    # resource_req_list = controller.build_batch_in_tray_payload_from_sheet("batch_in_tray.xlsx")
    # out_path = Path("resource_req_list.json")
    # out_path.write_text(json.dumps(resource_req_list, ensure_ascii=False, indent=2), encoding="utf-8")
    # result = controller.batch_in_tray(resource_req_list)

    #————————————————————————化合物库对齐————————————————————————

    # #检查化学品列表的合理性
    # controller.check_chemical_list_file("chemical_list.xlsx")

    # # 对齐化合物库和站内化学品列表
    # controller.align_chemicals_from_file("chemical_list.xlsx")

    #————————————————————————创建任务————————————————————————

    # #获取所有任务信息
    # result = controller.get_all_tasks()
    # out_path = Path("task_list.json")
    # out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # #从表格中创建任务
    # result = controller.create_task_from_template("reeaction_template.xlsx")
    # result2 = controller.add_task(result)
    # print(result2)
    # # controller.delete_task(571)

    # out_path = Path("reaction_template.json")
    # out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")



