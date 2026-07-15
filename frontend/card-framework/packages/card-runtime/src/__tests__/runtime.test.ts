// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, it, expect, vi } from "vitest";
import type {
  DataAdapter,
  DeviceSchema,
  DataSnapshot,
  Subscription,
} from "@labos/card-sdk";
import { createRuntime } from "../index";

function makeAdapter(schema: DeviceSchema) {
  let push: ((s: DataSnapshot) => void) | undefined;
  const adapter = {
    getDeviceSchema: vi.fn(async () => schema),
    subscribeState: vi.fn(
      (
        _d: string,
        _k: string[],
        onData: (s: DataSnapshot) => void,
      ): Subscription => {
        push = onData;
        return { close: vi.fn() };
      },
    ),
    callAction: vi.fn(async () => ({ ok: true })),
  } as unknown as DataAdapter;
  return { adapter, emit: (s: DataSnapshot) => push?.(s) };
}

const schema: DeviceSchema = {
  deviceId: "pump_1",
  fields: [{ name: "pressure", type: "number", unit: "bar", timeseries: true }],
  actions: [{ name: "aspirate", params: {} }],
};

describe("createRuntime / createContext", () => {
  it("createContext 注入 schema", async () => {
    const { adapter } = makeAdapter(schema);
    const rt = createRuntime(adapter);
    const el = {} as unknown as HTMLElement;
    const ctx = await rt.createContext({
      deviceId: "pump_1",
      mountEl: el,
      config: {},
    });
    expect(ctx.schema.deviceId).toBe("pump_1");
    expect(adapter.getDeviceSchema).toHaveBeenCalledWith("pump_1");
  });

  it("subscribe→onData 收到快照", async () => {
    const { adapter, emit } = makeAdapter(schema);
    const rt = createRuntime(adapter);
    const ctx = await rt.createContext({
      deviceId: "pump_1",
      mountEl: {} as HTMLElement,
      config: {},
    });
    const seen = vi.fn();
    ctx.onData(seen);
    ctx.subscribe("pump_1", ["pressure"]);
    const snap: DataSnapshot = {
      deviceId: "pump_1",
      latest: { pressure: { ts: 1, value: 1.2 } },
      series: {},
    };
    emit(snap);
    expect(seen).toHaveBeenCalledWith(snap);
  });

  it("callAction 透传到 adapter", async () => {
    const { adapter } = makeAdapter(schema);
    const rt = createRuntime(adapter);
    const ctx = await rt.createContext({
      deviceId: "pump_1",
      mountEl: {} as HTMLElement,
      config: {},
    });
    await ctx.callAction("pump_1", "aspirate", { volume: 100 });
    expect(adapter.callAction).toHaveBeenCalledWith("pump_1", "aspirate", {
      volume: 100,
    });
  });

  it("重复 subscribe(相同参数)不导致 onData 重复投递", async () => {
    const { adapter, emit } = makeAdapter(schema);
    const rt = createRuntime(adapter);
    const ctx = await rt.createContext({
      deviceId: "pump_1",
      mountEl: {} as HTMLElement,
      config: {},
    });
    const seen = vi.fn();
    ctx.onData(seen);
    // 相同参数订阅两次:稳定扇出闭包应被 SubscriptionManager 的 Set 去重
    ctx.subscribe("pump_1", ["pressure"]);
    ctx.subscribe("pump_1", ["pressure"]);
    const snap: DataSnapshot = {
      deviceId: "pump_1",
      latest: { pressure: { ts: 1, value: 1.2 } },
      series: {},
    };
    emit(snap);
    expect(seen).toHaveBeenCalledTimes(1);
  });

  it("destroy 后底层订阅被关闭", async () => {
    const closes = vi.fn();
    const adapter = {
      getDeviceSchema: vi.fn(async () => schema),
      subscribeState: vi.fn((): Subscription => ({ close: closes })),
      callAction: vi.fn(),
    } as unknown as DataAdapter;
    const rt = createRuntime(adapter);
    const ctx = await rt.createContext({
      deviceId: "pump_1",
      mountEl: {} as HTMLElement,
      config: {},
    });
    ctx.subscribe("pump_1", ["pressure"]);
    ctx.destroy();
    expect(closes).toHaveBeenCalledTimes(1);
  });
});
