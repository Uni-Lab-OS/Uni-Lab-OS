#!/usr/bin/env python3
"""
EIT 跨设备工作流数据传递 & AGV Deck 扁平化上报 本地测试脚本

测试目标：
  问题1: 验证 result/handles 注册表声明是否正确，模拟跨设备 manifest 数据流
  问题2: 验证 AGV Deck 扁平化上报逻辑（有物料时显示，空位不显示）

运行方式：
  cd /Users/fish/Documents/GitHub/Uni-Lab-OS
  python3 test_eit_workflow.py

不发送任何真实硬件命令，不启动 ROS，不连接工站/AGV。
"""

import sys
import os
import json
import yaml
import logging

sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("test_eit_workflow")

PASS = "\033[92m✅ PASS\033[0m"
FAIL = "\033[91m❌ FAIL\033[0m"
INFO = "\033[94mℹ️\033[0m"

results = []

def check(name: str, condition: bool, detail: str = ""):
    status = PASS if condition else FAIL
    results.append(condition)
    msg = f"  {status} {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return condition


# =====================================================================
# 问题 1：跨设备注册表 result/handles 数据流
# =====================================================================
print("\n" + "=" * 70)
print("问题 1：跨设备注册表 result/handles 数据流验证")
print("=" * 70)

# 1.1 加载注册表
print(f"\n{INFO} 加载注册表...")
with open("unilabos/registry/devices/eit_synthesis_station.yaml") as f:
    ss_reg = yaml.safe_load(f)
with open("unilabos/registry/devices/eit_agv.yaml") as f:
    agv_reg = yaml.safe_load(f)

ss_avm = ss_reg["eit_synthesis_station"]["class"]["action_value_mappings"]
agv_avm = agv_reg["eit_agv"]["class"]["action_value_mappings"]

# 1.2 检查 prepare_batch_in_with_agv_manifest 的 output handle
print(f"\n{INFO} [Step 1] 检查 prepare_batch_in_with_agv_manifest...")
prepare = ss_avm["prepare_batch_in_with_agv_manifest"]
check("result 声明包含 manifest",
      prepare.get("result", {}).get("manifest") == "manifest")
check("handles.output 存在且非空",
      len(prepare.get("handles", {}).get("output", [])) > 0)
if prepare.get("handles", {}).get("output"):
    out = prepare["handles"]["output"][0]
    check("output.handler_key == 'manifest'", out["handler_key"] == "manifest")
    check("output.data_source == 'executor'", out["data_source"] == "executor")
    check("output.data_type == 'object'", out["data_type"] == "object")

# 1.3 检查 transfer_manifest 的 input/output handle
print(f"\n{INFO} [Step 2] 检查 transfer_manifest...")
transfer = agv_avm["transfer_manifest"]
check("result 声明包含 transfer_result",
      transfer.get("result", {}).get("transfer_result") == "transfer_result")
check("handles.input 存在且非空",
      len(transfer.get("handles", {}).get("input", [])) > 0)
if transfer.get("handles", {}).get("input"):
    inp = transfer["handles"]["input"][0]
    check("input.handler_key == 'manifest'", inp["handler_key"] == "manifest")
    check("input.data_source == 'handle'", inp["data_source"] == "handle")
    check("input.data_type == 'object'", inp["data_type"] == "object")
check("handles.output 存在且非空",
      len(transfer.get("handles", {}).get("output", [])) > 0)

# 1.4 检查 execute_batch_in_payload 的 input handle
print(f"\n{INFO} [Step 3] 检查 execute_batch_in_payload...")
execute = ss_avm["execute_batch_in_payload"]
check("result 声明包含 success",
      execute.get("result", {}).get("success") == "success")
check("handles.input 存在且非空",
      len(execute.get("handles", {}).get("input", [])) > 0)
if execute.get("handles", {}).get("input"):
    inp = execute["handles"]["input"][0]
    check("input.handler_key == 'batch_in_payload'",
          inp["handler_key"] == "batch_in_payload")
    check("input.data_source == 'handle'", inp["data_source"] == "handle")

# 1.5 检查 unload_task_and_empty_trays_return_manifest 的 output handle
print(f"\n{INFO} [Step 4] 检查 unload_task_and_empty_trays_return_manifest...")
unload = ss_avm["unload_task_and_empty_trays_return_manifest"]
check("result 声明包含 manifest",
      unload.get("result", {}).get("manifest") == "manifest")
check("handles.output 存在且非空",
      len(unload.get("handles", {}).get("output", [])) > 0)

# 1.6 模拟工作流连线验证
print(f"\n{INFO} [Step 5] 模拟前端工作流连线...")

# 模拟 test_mode 下 _build_test_mode_return 的逻辑
def build_mock_return(action_mapping: dict, action_name: str) -> dict:
    """复刻 host_node._build_test_mode_return 的逻辑"""
    mock_return = {"test_mode": True, "action_name": action_name}
    handles = action_mapping.get("handles", {})
    if isinstance(handles, dict):
        for output_handle in handles.get("output", []):
            data_key = output_handle.get("data_key", "")
            handler_key = output_handle.get("handler_key", "")
            flatten_count = data_key.count("@flatten")
            value = {}
            for _ in range(flatten_count):
                value = [value]
            mock_return[handler_key] = value
    return mock_return

# 模拟上料流程
print(f"\n  {'─' * 50}")
print(f"  模拟上料流程: prepare → transfer → execute")
print(f"  {'─' * 50}")

# Step A: prepare_batch_in_with_agv_manifest → 返回 manifest
mock_prepare = build_mock_return(prepare, "prepare_batch_in_with_agv_manifest")
logger.info(f"[prepare] test_mode mock return: {json.dumps(mock_prepare, ensure_ascii=False)}")
check("prepare mock 返回包含 manifest 键", "manifest" in mock_prepare,
      f"keys = {list(mock_prepare.keys())}")

# Step B: transfer_manifest ← 接收 manifest (from prepare)
# 框架会把上游 output handler_key='manifest' 的值传给下游 input handler_key='manifest'
downstream_goal = {"manifest": mock_prepare.get("manifest", {})}
logger.info(f"[transfer] 从上游接收 goal: {json.dumps(downstream_goal, ensure_ascii=False)}")
check("transfer 接收到的 manifest 不为 None", downstream_goal["manifest"] is not None)

mock_transfer = build_mock_return(transfer, "transfer_manifest")
logger.info(f"[transfer] test_mode mock return: {json.dumps(mock_transfer, ensure_ascii=False)}")
check("transfer mock 返回包含 transfer_result 键", "transfer_result" in mock_transfer)

# Step C: execute_batch_in_payload ← 接收 batch_in_payload
# 注意: batch_in_payload 来自 manifest 内部，前端连线时用户需要从 prepare 输出中绑定
execute_goal = {"batch_in_payload": [{"tray_code": "REACTION_TUBE_TRAY_2ML", "layout_code": "TB-1-1"}]}
logger.info(f"[execute] goal: {json.dumps(execute_goal, ensure_ascii=False)}")
check("execute 接收到的 batch_in_payload 是 list", isinstance(execute_goal["batch_in_payload"], list))

# 模拟下料流程
print(f"\n  {'─' * 50}")
print(f"  模拟下料流程: unload → transfer")
print(f"  {'─' * 50}")

mock_unload = build_mock_return(unload, "unload_task_and_empty_trays_return_manifest")
logger.info(f"[unload] test_mode mock return: {json.dumps(mock_unload, ensure_ascii=False)}")
check("unload mock 返回包含 manifest 键", "manifest" in mock_unload)

downstream_goal_2 = {"manifest": mock_unload.get("manifest", {})}
logger.info(f"[transfer] 从 unload 接收 goal: {json.dumps(downstream_goal_2, ensure_ascii=False)}")
check("下料 transfer 接收到的 manifest 不为 None", downstream_goal_2["manifest"] is not None)

# 1.7 验证框架 convert_from_json 能正确解析 handles
print(f"\n{INFO} [Step 6] 验证框架 handle 解析逻辑...")
try:
    # 复刻 convert_from_json.py 中的 get_action_handles 逻辑
    def get_action_handles(action_mapping: dict) -> dict:
        result = {"source": [], "target": []}
        handles = action_mapping.get("handles", {})
        if isinstance(handles, dict):
            for handle in handles.get("input", []):
                hk = handle.get("handler_key", "")
                if hk:
                    result["source"].append(hk)
            for handle in handles.get("output", []):
                hk = handle.get("handler_key", "")
                if hk:
                    result["target"].append(hk)
        return result

    prepare_handles = get_action_handles(prepare)
    check("prepare output handles: ['manifest']",
          prepare_handles["target"] == ["manifest"],
          f"实际: {prepare_handles}")

    transfer_handles = get_action_handles(transfer)
    check("transfer input handles: ['manifest']",
          transfer_handles["source"] == ["manifest"],
          f"实际: {transfer_handles}")
    check("transfer output handles: ['transfer_result']",
          transfer_handles["target"] == ["transfer_result"],
          f"实际: {transfer_handles}")

    execute_handles = get_action_handles(execute)
    check("execute input handles: ['batch_in_payload']",
          execute_handles["source"] == ["batch_in_payload"],
          f"实际: {execute_handles}")

    unload_handles = get_action_handles(unload)
    check("unload output handles: ['manifest']",
          unload_handles["target"] == ["manifest"],
          f"实际: {unload_handles}")

except Exception as e:
    check("框架 handle 解析无异常", False, str(e))


# =====================================================================
# 问题 2：AGV Deck 物料同步 + 扁平化上报
# =====================================================================
print("\n" + "=" * 70)
print("问题 2：AGV Deck 结构 + 扁平化上报验证")
print("=" * 70)

# 2.1 验证 Deck 结构
print(f"\n{INFO} [Step 1] 验证 AGV Deck 结构...")
from unilabos.resources.eit_agv.decks import EIT_AGV_Deck

deck = EIT_AGV_Deck(setup=True)
check("Deck 有且仅有 1 个子节点（WareHouse）",
      len(deck.children) == 1,
      f"实际: {len(deck.children)} 个子节点")

wh = deck.children[0]
check(f"WareHouse 名称为 'AGV'",
      wh.name == "AGV",
      f"实际: {wh.name}")

check("WareHouse 有 4 个槽位（1×4 车载）",
      len(wh.children) == 4,
      f"实际: {len(wh.children)} 个")

slot_names = [c.name for c in wh.children]
expected_names = ["AGV-1", "AGV-2", "AGV-3", "AGV-4"]
check(f"槽位名称: {expected_names}",
      slot_names == expected_names,
      f"实际: {slot_names}")

# 验证坐标不重叠
coords = [str(c.location) for c in wh.children]
check("所有槽位坐标不重叠",
      len(set(coords)) == 4,
      f"坐标: {coords}")

# 2.2 验证扁平化上报逻辑（空 Deck）
print(f"\n{INFO} [Step 2] 验证扁平化上报 — 空 Deck（所有槽位无物料）...")

def flatten_deck(deck_obj) -> dict:
    """
    复刻 agv_controller._upload_agv_deck_flattened 的扁平化核心逻辑。
    不依赖框架内部序列化——只模拟结构遍历：
    遍历 Deck → WareHouse → ResourceHolder → Carrier，
    只提取有 Carrier 的 ResourceHolder 的子节点。
    """
    flat_wh_list = []
    for wh_obj in deck_obj.children:
        flat_carriers = []
        for rh in wh_obj.children:
            for carrier in rh.children:
                flat_carriers.append({"name": carrier.name, "type": type(carrier).__name__})
        flat_wh_list.append({"name": wh_obj.name, "children": flat_carriers})
    return {"name": deck_obj.name, "children": flat_wh_list}

flat_empty = flatten_deck(deck)
agv_wh_children = flat_empty["children"][0]["children"]  # WareHouse AGV 的扁平化子节点
check("空 Deck 扁平化后 WareHouse 无子节点",
      len(agv_wh_children) == 0,
      f"实际: {len(agv_wh_children)} 个子节点")
print(f"  {INFO} 前端效果: eit_agv → EIT_AGV_Deck → AGV → (空，不显示任何槽位)")

# 2.3 验证扁平化上报逻辑（有物料）
print(f"\n{INFO} [Step 3] 验证扁平化上报 — 模拟放置物料...")

from unilabos.resources.eit_synthesis_station import bottle_carriers

# 模拟给 AGV-1 放一个 FLASH_FILTER_OUTER_BOTTLE_TRAY
slot_1 = wh.children[0]  # AGV-1
carrier = bottle_carriers.EIT_FLASH_FILTER_OUTER_BOTTLE_TRAY(
    name=f"FLASH_FILTER_OUTER_BOTTLE_TRAY@{slot_1.name}"
)
slot_1.assign_child_resource(carrier)
check(f"AGV-1 已挂载物料",
      len(slot_1.children) == 1,
      f"carrier: {carrier.name}")

# 模拟给 AGV-3 放一个 REAGENT_BOTTLE_TRAY_125ML
slot_3 = wh.children[2]  # AGV-3
carrier_3 = bottle_carriers.EIT_REAGENT_BOTTLE_TRAY_125ML(
    name=f"REAGENT_BOTTLE_TRAY_125ML@{slot_3.name}"
)
slot_3.assign_child_resource(carrier_3)
check(f"AGV-3 已挂载物料",
      len(slot_3.children) == 1)

# 扁平化后：只有 AGV-1 和 AGV-3 的 Carrier 显示
flat_with_materials = flatten_deck(deck)
agv_wh_children = flat_with_materials["children"][0]["children"]
check("扁平化后 WareHouse 有 2 个子节点（仅有物料的槽位）",
      len(agv_wh_children) == 2,
      f"实际: {len(agv_wh_children)} 个")

carrier_names = [c["name"] for c in agv_wh_children]
check("子节点是 Carrier（非 ResourceHolder）",
      all("TRAY" in n or "BOTTLE" in n for n in carrier_names),
      f"名称: {carrier_names}")

print(f"  {INFO} 前端效果:")
print(f"       eit_agv → EIT_AGV_Deck → AGV")
for c in agv_wh_children:
    print(f"         └─ {c['name']}")
print(f"       (AGV-2, AGV-4 为空，不显示)")

# 2.4 验证物料移除后扁平化
print(f"\n{INFO} [Step 4] 验证移除物料后扁平化...")
slot_1.unassign_child_resource(carrier)
check("AGV-1 物料已移除", len(slot_1.children) == 0)

flat_after_remove = flatten_deck(deck)
agv_wh_children = flat_after_remove["children"][0]["children"]
check("移除后 WareHouse 只剩 1 个子节点",
      len(agv_wh_children) == 1,
      f"剩余: {[c['name'] for c in agv_wh_children]}")

# 2.5 验证 CARRIER_FACTORY 全量覆盖
print(f"\n{INFO} [Step 5] 验证载架工厂全覆盖（13 种物料类型）...")

FACTORY_MAP = {
    # 与 ResourceCode 枚举中所有 *_TRAY_* 对应的载架工厂
    "REACTION_TUBE_TRAY_2ML":         bottle_carriers.EIT_REACTION_TUBE_TRAY_2ML,
    "TEST_TUBE_MAGNET_TRAY_2ML":      bottle_carriers.EIT_TEST_TUBE_MAGNET_TRAY_2ML,
    "REACTION_SEAL_CAP_TRAY":         bottle_carriers.EIT_REACTION_SEAL_CAP_TRAY,
    "FLASH_FILTER_INNER_BOTTLE_TRAY": bottle_carriers.EIT_FLASH_FILTER_INNER_BOTTLE_TRAY,
    "FLASH_FILTER_OUTER_BOTTLE_TRAY": bottle_carriers.EIT_FLASH_FILTER_OUTER_BOTTLE_TRAY,
    "TIP_TRAY_50UL":                  bottle_carriers.EIT_TIP_TRAY_50UL,
    "TIP_TRAY_1ML":                   bottle_carriers.EIT_TIP_TRAY_1ML,
    "TIP_TRAY_5ML":                   bottle_carriers.EIT_TIP_TRAY_5ML,
    "POWDER_BUCKET_TRAY_30ML":        bottle_carriers.EIT_POWDER_BUCKET_TRAY_30ML,
    "REAGENT_BOTTLE_TRAY_2ML":        bottle_carriers.EIT_REAGENT_BOTTLE_TRAY_2ML,
    "REAGENT_BOTTLE_TRAY_8ML":        bottle_carriers.EIT_REAGENT_BOTTLE_TRAY_8ML,
    "REAGENT_BOTTLE_TRAY_40ML":       bottle_carriers.EIT_REAGENT_BOTTLE_TRAY_40ML,
    "REAGENT_BOTTLE_TRAY_125ML":      bottle_carriers.EIT_REAGENT_BOTTLE_TRAY_125ML,
}

all_ok = True
for name, factory in FACTORY_MAP.items():
    try:
        c = factory(name=f"test@AGV-1")
        ok = c is not None and hasattr(c, "name")
        if not ok:
            all_ok = False
    except Exception as e:
        all_ok = False
        print(f"    {FAIL} {name}: {e}")

check(f"全部 {len(FACTORY_MAP)} 种物料类型创建成功", all_ok)


# =====================================================================
# 总结
# =====================================================================
print("\n" + "=" * 70)
total = len(results)
passed = sum(results)
failed = total - passed
if failed == 0:
    print(f"\033[92m全部通过: {passed}/{total} 项检查\033[0m")
else:
    print(f"\033[91m有 {failed} 项失败: {passed}/{total} 项通过\033[0m")
print("=" * 70)

print(f"""
上料连线图:
  ┌──────────────────────────────────────┐     ┌─────────────────────┐     ┌──────────────────────────────────┐
  │ eit_synthesis_station                │     │ eit_agv             │     │ eit_synthesis_station            │
  │ prepare_batch_in_with_agv_manifest   │     │ transfer_manifest   │     │ execute_batch_in_payload         │
  │                                      │     │                     │     │                                  │
  │  output: manifest ─────────────────────────→ input: manifest     │     │  input: batch_in_payload         │
  │                                      │     │  output: transfer   │     │                                  │
  └──────────────────────────────────────┘     └─────────────────────┘     └──────────────────────────────────┘

下料连线图:
  ┌──────────────────────────────────────────────┐     ┌─────────────────────┐
  │ eit_synthesis_station                        │     │ eit_agv             │
  │ unload_task_and_empty_trays_return_manifest  │     │ transfer_manifest   │
  │                                              │     │                     │
  │  output: manifest ─────────────────────────────────→ input: manifest     │
  └──────────────────────────────────────────────┘     └─────────────────────┘
""")
