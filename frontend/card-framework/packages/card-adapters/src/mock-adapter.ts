// SPDX-License-Identifier: GPL-3.0-or-later
import type {
  DataAdapter, DeviceSchema, DataSnapshot, Subscription, TsValue,
} from '@labos/card-sdk'

export interface MockDevice {
  schema: DeviceSchema
  initial: Record<string, number | string | boolean>
}

// 无后端、无 Edge、无 Python 的最快数据源 — 本地开发 + 契约测试用。
export class MockAdapter implements DataAdapter {
  constructor(private devices: Record<string, MockDevice>) {}

  private get(deviceId: string): MockDevice {
    const d = this.devices[deviceId]
    if (d === undefined) throw new Error(`MockAdapter: 未知设备 ${deviceId}`)
    return d
  }

  async getDeviceSchema(deviceId: string): Promise<DeviceSchema> {
    return this.get(deviceId).schema
  }

  subscribeState(
    deviceId: string,
    keys: string[],
    onData: (snapshot: DataSnapshot) => void,
  ): Subscription {
    const dev = this.get(deviceId)
    const ts = 0 // 确定性时间戳,避免测试依赖 Date.now
    const latest: Record<string, TsValue> = {}
    for (const k of keys) {
      const v = dev.initial[k]
      if (v !== undefined) latest[k] = { ts, value: v }
    }
    onData({ deviceId, latest, series: {} })
    return { close: () => {} }
  }

  async callAction(_deviceId: string, action: string, params: unknown): Promise<unknown> {
    return { ack: true, action, params }
  }
}
