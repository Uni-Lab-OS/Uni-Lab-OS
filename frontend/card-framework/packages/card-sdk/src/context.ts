// SPDX-License-Identifier: GPL-3.0-or-later
import type { DeviceSchema } from './schema'
import type { DataSnapshot } from './data'

// runtime 产出;卡片(任意代码)拿它接地。
export interface CardContext<TConfig = unknown> {
  mountEl: HTMLElement
  config: TConfig
  saveConfig(patch: Partial<TConfig>): void
  schema: DeviceSchema
  subscribe(deviceId: string, keys: string[]): void
  onData(cb: (snapshot: DataSnapshot) => void): void
  callAction(deviceId: string, action: string, params: unknown): Promise<unknown>
  destroy(): void
}

export interface CardDefinition<TConfig = unknown> {
  type: string
  title: string
  defaultConfig: TConfig
  init(ctx: CardContext<TConfig>): { destroy(): void }
}
