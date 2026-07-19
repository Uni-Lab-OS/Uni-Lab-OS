#!/usr/bin/env python3
"""离线从 OS registry device YAML 抽取 DeviceSchema(映射与 OsAdapter.getDeviceSchema 一致)。"""
import yaml, glob, json, os, sys

REG = "/Users/dp/Design_projects/LabOS/Uni-Lab-OS/unilabos/registry/devices"

def map_type(t):
    t = (t or "").strip().lower()
    if t in ("float", "int", "integer", "number", "double"): return "number"
    if t in ("bool", "boolean"): return "bool"
    return "string"

def extract():
    out = []
    for f in sorted(glob.glob(os.path.join(REG, "*.yaml"))):
        try:
            d = yaml.safe_load(open(f))
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        for cls, body in d.items():
            if not isinstance(body, dict):
                continue
            c = body.get("class") or {}
            st = c.get("status_types") or {}
            av = c.get("action_value_mappings") or {}
            if not st and not av:
                continue
            fields = []
            for name, tp in st.items():
                ft = map_type(tp)
                field = {"name": name, "type": ft}
                if ft == "number":
                    field["timeseries"] = True
                fields.append(field)
            actions = []
            for aname, amap in av.items():
                params = {}
                required = []
                try:
                    goal = ((amap.get("schema") or {}).get("properties") or {}).get("goal", {})
                    params = goal.get("properties", {}) or {}
                    required = goal.get("required", []) or []
                except AttributeError:
                    params = {}
                actions.append({"name": aname, "params": params, "required": required})
            out.append({
                "deviceId": cls,
                "category": body.get("category") or "",
                "description": (body.get("description") or "")[:160],
                "fields": fields,
                "actions": actions,
            })
    return out

if __name__ == "__main__":
    schemas = extract()
    dst = os.path.join(os.path.dirname(__file__), "out", "schemas.json")
    json.dump(schemas, open(dst, "w"), ensure_ascii=False, indent=1)
    print(f"抽取 {len(schemas)} 个设备类 → {dst}")
    # 摘要
    for s in schemas[:6]:
        print(f"  {s['deviceId']}: {len(s['fields'])} fields, {len(s['actions'])} actions")
