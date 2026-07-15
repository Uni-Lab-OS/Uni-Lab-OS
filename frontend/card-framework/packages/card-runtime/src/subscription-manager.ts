// SPDX-License-Identifier: GPL-3.0-or-later
import type { DataAdapter, DataSnapshot, Subscription } from '@labos/card-sdk'

interface Entry {
  sub: Subscription
  listeners: Set<(s: DataSnapshot) => void>
}

// 多卡订阅同一 device+keys 时合并为一个底层订阅(引用计数)。
export class SubscriptionManager {
  private entries = new Map<string, Entry>()

  constructor(private adapter: DataAdapter) {}

  private key(deviceId: string, keys: string[]): string {
    return `${deviceId}::${[...keys].sort().join(',')}`
  }

  subscribe(deviceId: string, keys: string[], onData: (s: DataSnapshot) => void): () => void {
    const k = this.key(deviceId, keys)
    let entry = this.entries.get(k)
    if (entry === undefined) {
      const listeners = new Set<(s: DataSnapshot) => void>()
      const sub = this.adapter.subscribeState(deviceId, keys, (snap) => {
        for (const l of listeners) l(snap)
      })
      entry = { sub, listeners }
      this.entries.set(k, entry)
    }
    entry.listeners.add(onData)
    return () => this.unsubscribe(k, onData)
  }

  private unsubscribe(k: string, onData: (s: DataSnapshot) => void): void {
    const entry = this.entries.get(k)
    if (entry === undefined) return
    entry.listeners.delete(onData)
    if (entry.listeners.size === 0) {
      entry.sub.close()
      this.entries.delete(k)
    }
  }

  activeCount(): number {
    return this.entries.size
  }
}
