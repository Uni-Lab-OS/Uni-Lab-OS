// SPDX-License-Identifier: GPL-3.0-or-later
import type {
  DataAdapter, DeviceSchema, DeviceField, DeviceAction, DataSnapshot, Subscription,
} from '@labos/card-sdk'

export interface OsAdapterOptions {
  baseUrl: string
  fetchFn?: typeof fetch
}

interface OsDeviceEntry {
  id: string
  status?: Record<string, { type?: string; unit?: string }>
}
interface OsActionEntry {
  name: string
  schema?: Record<string, unknown>
}

// OS 状态类型字符串 → 归一 field type
function mapType(t: string | undefined): DeviceField['type'] {
  switch ((t ?? '').toLowerCase()) {
    case 'float': case 'int': case 'integer': case 'number': case 'double':
      return 'number'
    case 'bool': case 'boolean':
      return 'bool'
    default:
      return 'string'
  }
}

// 直连 OS FastAPI 桥(unilab --app_bridges fastapi)。
export class OsAdapter implements DataAdapter {
  private baseUrl: string
  private fetchFn: typeof fetch

  constructor(opts: OsAdapterOptions) {
    this.baseUrl = opts.baseUrl.replace(/\/$/, '')
    this.fetchFn = opts.fetchFn ?? fetch
  }

  private async getJson<T>(path: string): Promise<T> {
    const res = await this.fetchFn(`${this.baseUrl}${path}`)
    if (!res.ok) throw new Error(`OsAdapter: ${path} 请求失败 ${res.status}`)
    const body = (await res.json()) as { data: T }
    return body.data
  }

  async getDeviceSchema(deviceId: string): Promise<DeviceSchema> {
    const devices = await this.getJson<OsDeviceEntry[]>('/devices')
    const dev = devices.find((d) => d.id === deviceId)
    if (dev === undefined) throw new Error(`OsAdapter: 未知设备 ${deviceId}`)

    const fields: DeviceField[] = Object.entries(dev.status ?? {}).map(([name, meta]) => {
      const field: DeviceField = { name, type: mapType(meta.type) }
      if (meta.unit !== undefined) field.unit = meta.unit
      if (field.type === 'number') field.timeseries = true
      return field
    })

    const actionsRaw = await this.getJson<OsActionEntry[]>(`/devices/${deviceId}/actions`)
    const actions: DeviceAction[] = actionsRaw.map((a) => ({
      name: a.name,
      params: a.schema ?? {},
    }))

    return { deviceId, fields, actions }
  }

  subscribeState(
    _deviceId: string,
    _keys: string[],
    _onData: (snapshot: DataSnapshot) => void,
  ): Subscription {
    // 真实 WS(/ws/device_status)在计划2 端到端联调接入;此处占位。
    return { close: () => {} }
  }

  async callAction(deviceId: string, action: string, params: unknown): Promise<unknown> {
    const res = await this.fetchFn(`${this.baseUrl}/job/add`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device_id: deviceId, action, goal: params }),
    })
    if (!res.ok) throw new Error(`OsAdapter: job/add 失败 ${res.status}`)
    const body = (await res.json()) as { data: unknown }
    return body.data
  }
}
