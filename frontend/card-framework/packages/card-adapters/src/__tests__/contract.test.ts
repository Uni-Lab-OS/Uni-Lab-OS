// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, it, expect, vi } from "vitest";
import { isDeviceSchema, type DeviceSchema } from "@labos/card-sdk";
import { MockAdapter } from "../mock-adapter";
import { OsAdapter } from "../os-adapter";

function jsonResponse(body: unknown): Response {
  return { ok: true, json: async () => body } as unknown as Response;
}

// 同一逻辑设备的两种来源
const mockSchema: DeviceSchema = {
  deviceId: "pump_1",
  fields: [{ name: "pressure", type: "number", unit: "bar", timeseries: true }],
  actions: [{ name: "aspirate", params: { volume: { type: "float" } } }],
};

function osFetch(): typeof fetch {
  return vi.fn(async (url: string | URL) => {
    const u = String(url);
    if (u.endsWith("/devices")) {
      return jsonResponse({
        data: [
          {
            id: "pump_1",
            status: { pressure: { type: "float", unit: "bar" } },
          },
        ],
      });
    }
    if (u.endsWith("/devices/pump_1/actions")) {
      return jsonResponse({
        data: [{ name: "aspirate", schema: { volume: { type: "float" } } }],
      });
    }
    throw new Error(u);
  }) as unknown as typeof fetch;
}

function sortedKeys(s: DeviceSchema) {
  return {
    deviceId: s.deviceId,
    fields: s.fields.map((f) => f.name).sort(),
    actions: s.actions.map((a) => a.name).sort(),
    fieldTypes: Object.fromEntries(s.fields.map((f) => [f.name, f.type])),
    // 锁定 adapter 归一后每个 field 的 type/unit/timeseries,防止细节漂移
    fieldMeta: Object.fromEntries(
      s.fields.map((f) => [
        f.name,
        { type: f.type, unit: f.unit, timeseries: f.timeseries },
      ]),
    ),
    // 锁定每个 action 的 params 投影
    actionParams: Object.fromEntries(s.actions.map((a) => [a.name, a.params])),
  };
}

describe("adapter 契约一致性", () => {
  it("Mock 与 Os 归一到结构一致的 DeviceSchema", async () => {
    const mock = new MockAdapter({
      pump_1: { schema: mockSchema, initial: { pressure: 1.2 } },
    });
    const os = new OsAdapter({ baseUrl: "http://x", fetchFn: osFetch() });

    const a = await mock.getDeviceSchema("pump_1");
    const b = await os.getDeviceSchema("pump_1");

    expect(isDeviceSchema(a)).toBe(true);
    expect(isDeviceSchema(b)).toBe(true);
    expect(sortedKeys(a)).toEqual(sortedKeys(b));
  });
});
