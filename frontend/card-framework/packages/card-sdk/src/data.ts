// SPDX-License-Identifier: GPL-3.0-or-later
// 实时/历史数据的最小载体。
export interface TsValue {
  ts: number // unix ms
  value: number | string | boolean
}

export interface DataSnapshot {
  deviceId: string
  latest: Record<string, TsValue>       // 最新值
  series: Record<string, TsValue[]>      // 时序点(仅 timeseries 字段)
}

export interface Subscription {
  close(): void
}
