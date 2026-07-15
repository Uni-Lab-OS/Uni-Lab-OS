// SPDX-License-Identifier: GPL-3.0-or-later
import type { DeviceSchema } from './schema'

// 运行时类型守卫 — adapter 返回值/外部输入的边界校验。
export function isDeviceSchema(v: unknown): v is DeviceSchema {
  if (typeof v !== 'object' || v === null) return false
  const o = v as Record<string, unknown>
  return typeof o.deviceId === 'string' && Array.isArray(o.fields) && Array.isArray(o.actions)
}
