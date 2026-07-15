// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, it, expect, vi } from "vitest";
import type { DeviceSchema, DataSnapshot } from "@labos/card-sdk";
import { MockAdapter } from "../mock-adapter";

const schema: DeviceSchema = {
  deviceId: "pump_1",
  fields: [{ name: "pressure", type: "number", unit: "bar" }],
  actions: [{ name: "aspirate", params: {} }],
};

function makeAdapter() {
  return new MockAdapter({
    pump_1: { schema, initial: { pressure: 1.2 } },
  });
}

describe("MockAdapter", () => {
  it("getDeviceSchema 返回登记的 schema", async () => {
    const a = makeAdapter();
    expect((await a.getDeviceSchema("pump_1")).deviceId).toBe("pump_1");
  });

  it("未知设备抛错", async () => {
    const a = makeAdapter();
    await expect(a.getDeviceSchema("nope")).rejects.toThrow();
  });

  it("subscribeState 立即回推 initial 快照", () => {
    const a = makeAdapter();
    const onData = vi.fn();
    const sub = a.subscribeState("pump_1", ["pressure"], onData);
    expect(onData).toHaveBeenCalledTimes(1);
    const call = onData.mock.calls[0];
    if (call === undefined) throw new Error("缺少回调调用");
    const snap = call[0] as DataSnapshot;
    const pressure = snap.latest.pressure;
    if (pressure === undefined) throw new Error("缺少 pressure 快照");
    expect(pressure.value).toBe(1.2);
    sub.close();
  });

  it("callAction 返回 ack", async () => {
    const a = makeAdapter();
    const r = await a.callAction("pump_1", "aspirate", { volume: 100 });
    expect(r).toEqual({
      ack: true,
      action: "aspirate",
      params: { volume: 100 },
    });
  });
});
