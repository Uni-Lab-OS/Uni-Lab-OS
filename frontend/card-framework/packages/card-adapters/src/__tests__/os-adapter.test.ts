// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, it, expect, vi } from 'vitest'
import { OsAdapter } from '../os-adapter'

function jsonResponse(body: unknown): Response {
  return { ok: true, json: async () => body } as unknown as Response
}

describe('OsAdapter', () => {
  it('getDeviceSchema 归一 /devices + /actions 到 DeviceSchema', async () => {
    const fetchFn = vi.fn(async (url: string | URL) => {
      const u = String(url)
      if (u.endsWith('/devices')) {
        return jsonResponse({
          data: [
            { id: 'pump_1', status: { pressure: { type: 'float', unit: 'bar' }, valve: { type: 'str' } } },
          ],
        })
      }
      if (u.endsWith('/devices/pump_1/actions')) {
        return jsonResponse({ data: [{ name: 'aspirate', schema: { volume: { type: 'float' } } }] })
      }
      throw new Error(`unexpected url ${u}`)
    }) as unknown as typeof fetch

    const a = new OsAdapter({ baseUrl: 'http://localhost:8002', fetchFn })
    const schema = await a.getDeviceSchema('pump_1')
    expect(schema.deviceId).toBe('pump_1')
    expect(schema.fields.find((f) => f.name === 'pressure')).toMatchObject({
      type: 'number', unit: 'bar',
    })
    expect(schema.fields.find((f) => f.name === 'valve')?.type).toBe('string')
    expect(schema.actions[0]).toMatchObject({ name: 'aspirate' })
  })

  it('callAction POST /job/add', async () => {
    const fetchFn = vi.fn(async () => jsonResponse({ data: { jobId: 'j1' } })) as unknown as typeof fetch
    const a = new OsAdapter({ baseUrl: 'http://localhost:8002', fetchFn })
    const r = await a.callAction('pump_1', 'aspirate', { volume: 100 })
    expect(r).toEqual({ jobId: 'j1' })
    const call = (fetchFn as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    if (call === undefined) throw new Error('fetchFn 未被调用')
    expect(String(call[0])).toBe('http://localhost:8002/job/add')
    expect(JSON.parse((call[1] as RequestInit).body as string)).toEqual({
      device_id: 'pump_1', action: 'aspirate', goal: { volume: 100 },
    })
  })
})
