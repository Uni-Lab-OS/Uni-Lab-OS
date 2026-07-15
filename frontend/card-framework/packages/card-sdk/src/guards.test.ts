// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, it, expect } from 'vitest'
import { isDeviceSchema } from './guards'

describe('isDeviceSchema', () => {
  it('接受合法 schema', () => {
    const s = { deviceId: 'pump_1', fields: [], actions: [] }
    expect(isDeviceSchema(s)).toBe(true)
  })
  it('拒绝缺 fields 的对象', () => {
    expect(isDeviceSchema({ deviceId: 'x', actions: [] })).toBe(false)
  })
  it('拒绝 null / 非对象', () => {
    expect(isDeviceSchema(null)).toBe(false)
    expect(isDeviceSchema('pump')).toBe(false)
  })
})
