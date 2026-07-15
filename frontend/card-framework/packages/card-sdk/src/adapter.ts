// SPDX-License-Identifier: GPL-3.0-or-later
import type { DeviceSchema } from './schema'
import type { DataSnapshot, Subscription } from './data'

export interface HistoryQuery {
  deviceId: string
  keys: string[]
  fromTs: number
  toTs: number
}

export interface HistoryResult {
  deviceId: string
  series: Record<string, { ts: number; value: number }[]>
}

// 三态可移植的唯一缝隙 — Mock/Os/Cloud 各实现一份。
export interface DataAdapter {
  getDeviceSchema(deviceId: string): Promise<DeviceSchema>
  subscribeState(
    deviceId: string,
    keys: string[],
    onData: (snapshot: DataSnapshot) => void,
  ): Subscription
  callAction(deviceId: string, action: string, params: unknown): Promise<unknown>
  queryHistory?(req: HistoryQuery): Promise<HistoryResult>
}
