// SPDX-License-Identifier: GPL-3.0-or-later
import type { DataAdapter, CardContext, DataSnapshot } from '@labos/card-sdk'
import { SubscriptionManager } from './subscription-manager'

export interface CreateContextOptions<T> {
  deviceId: string
  mountEl: HTMLElement
  config: T
  onConfigChange?: (config: T) => void
}

export interface Runtime {
  createContext<T>(opts: CreateContextOptions<T>): Promise<CardContext<T>>
}

// 中立底座:产出 CardContext,管理订阅生命周期,不涉及任何渲染。
export function createRuntime(adapter: DataAdapter): Runtime {
  const mgr = new SubscriptionManager(adapter)

  return {
    async createContext<T>(opts: CreateContextOptions<T>): Promise<CardContext<T>> {
      const schema = await adapter.getDeviceSchema(opts.deviceId)
      let config = opts.config
      const dataListeners = new Set<(s: DataSnapshot) => void>()
      const offs: Array<() => void> = []

      const ctx: CardContext<T> = {
        mountEl: opts.mountEl,
        get config() {
          return config
        },
        saveConfig(patch) {
          config = { ...config, ...patch }
          opts.onConfigChange?.(config)
        },
        schema,
        subscribe(deviceId, keys) {
          const off = mgr.subscribe(deviceId, keys, (snap) => {
            for (const l of dataListeners) l(snap)
          })
          offs.push(off)
        },
        onData(cb) {
          dataListeners.add(cb)
        },
        callAction(deviceId, action, params) {
          return adapter.callAction(deviceId, action, params)
        },
        destroy() {
          for (const off of offs) off()
          offs.length = 0
          dataListeners.clear()
        },
      }
      return ctx
    },
  }
}
