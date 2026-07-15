// SPDX-License-Identifier: GPL-3.0-or-later
// 归一化设备模型 — 所有 adapter 填充它,所有 host/codegen 只认它。
export type JsonSchema = Record<string, unknown>

export interface DeviceField {
  name: string
  type: 'number' | 'bool' | 'enum' | 'string'
  unit?: string
  min?: number
  max?: number
  readonly?: boolean
  enum?: string[]
  timeseries?: boolean
}

export interface DeviceAction {
  name: string
  params: JsonSchema
  confirm?: boolean
}

export interface DeviceSchema {
  deviceId: string
  fields: DeviceField[]
  actions: DeviceAction[]
}
