// SPDX-License-Identifier: GPL-3.0-or-later
// 内核契约出口 — AI 主要读这里。
export type {
  JsonSchema, DeviceField, DeviceAction, DeviceSchema,
} from './schema'
export type { TsValue, DataSnapshot, Subscription } from './data'
export type { HistoryQuery, HistoryResult, DataAdapter } from './adapter'
export type { CardContext, CardDefinition } from './context'
export { isDeviceSchema } from './guards'
