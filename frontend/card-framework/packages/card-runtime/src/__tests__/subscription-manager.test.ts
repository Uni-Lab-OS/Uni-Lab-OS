// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, it, expect, vi } from 'vitest'
import type { DataAdapter, DataSnapshot, Subscription } from '@labos/card-sdk'
import { SubscriptionManager } from '../subscription-manager'

function makeAdapter() {
  const closes = vi.fn()
  const subscribeState = vi.fn(
    (deviceId: string, _keys: string[], _onData: (s: DataSnapshot) => void): Subscription => ({
      close: closes,
    }),
  )
  const adapter = {
    getDeviceSchema: vi.fn(),
    subscribeState,
    callAction: vi.fn(),
  } as unknown as DataAdapter
  return { adapter, subscribeState, closes }
}

describe('SubscriptionManager', () => {
  it('同一 device+keys 多订阅只建一个底层订阅', () => {
    const { adapter, subscribeState } = makeAdapter()
    const mgr = new SubscriptionManager(adapter)
    mgr.subscribe('pump_1', ['pressure'], () => {})
    mgr.subscribe('pump_1', ['pressure'], () => {})
    expect(subscribeState).toHaveBeenCalledTimes(1)
    expect(mgr.activeCount()).toBe(1)
  })

  it('最后一个取消才关闭底层订阅', () => {
    const { adapter, closes } = makeAdapter()
    const mgr = new SubscriptionManager(adapter)
    const off1 = mgr.subscribe('pump_1', ['pressure'], () => {})
    const off2 = mgr.subscribe('pump_1', ['pressure'], () => {})
    off1()
    expect(closes).not.toHaveBeenCalled()
    off2()
    expect(closes).toHaveBeenCalledTimes(1)
    expect(mgr.activeCount()).toBe(0)
  })

  it('底层数据 fan-out 给所有监听者', () => {
    const { adapter, subscribeState } = makeAdapter()
    const mgr = new SubscriptionManager(adapter)
    const a = vi.fn()
    const b = vi.fn()
    mgr.subscribe('pump_1', ['pressure'], a)
    mgr.subscribe('pump_1', ['pressure'], b)
    const onData = subscribeState.mock.calls[0][2] as (s: DataSnapshot) => void
    const snap: DataSnapshot = { deviceId: 'pump_1', latest: {}, series: {} }
    onData(snap)
    expect(a).toHaveBeenCalledWith(snap)
    expect(b).toHaveBeenCalledWith(snap)
  })
})
