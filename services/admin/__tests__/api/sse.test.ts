import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

import { FlagStream, reconnectDelayMs, streamUrl, type StreamState } from '../../src/api/sse'

type Listener = (event: MessageEvent) => void

class FakeEventSource {
  static instances: FakeEventSource[] = []
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  closed = false
  private listeners = new Map<string, Listener[]>()

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this)
  }

  addEventListener(name: string, listener: Listener): void {
    const existing = this.listeners.get(name) ?? []
    this.listeners.set(name, [...existing, listener])
  }

  close(): void {
    this.closed = true
  }

  emit(name: string, data: unknown): void {
    for (const listener of this.listeners.get(name) ?? []) {
      listener({ data: JSON.stringify(data) } as MessageEvent)
    }
  }
}

function createStream(events: { name: string; data: unknown }[], states: StreamState[]) {
  return new FlagStream(
    'http://config.test/v1/stream?api_key=k',
    {
      onEvent: (name, data) => events.push({ name, data }),
      onState: (state) => states.push(state),
    },
    (url) => new FakeEventSource(url) as unknown as EventSource,
  )
}

describe('streamUrl', () => {
  test('builds the query-param auth URL (EventSource cannot set headers)', () => {
    expect(streamUrl('http://localhost:8081/', 'proj_demo_0123456789abcdef')).toBe(
      'http://localhost:8081/v1/stream?api_key=proj_demo_0123456789abcdef',
    )
  })
})

describe('reconnectDelayMs', () => {
  test('doubles from 1s and caps at 30s', () => {
    expect(reconnectDelayMs(0)).toBe(1000)
    expect(reconnectDelayMs(1)).toBe(2000)
    expect(reconnectDelayMs(4)).toBe(16_000)
    expect(reconnectDelayMs(10)).toBe(30_000)
  })
})

describe('FlagStream', () => {
  beforeEach(() => {
    FakeEventSource.instances = []
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  test('delivers parsed events and tracks open state', () => {
    const events: { name: string; data: unknown }[] = []
    const states: StreamState[] = []
    const stream = createStream(events, states)
    stream.start()

    const source = FakeEventSource.instances[0]!
    source.onopen?.()
    expect(stream.getState().status).toBe('open')

    source.emit('flag_update', { action: 'flag_removed', key: 'x' })
    expect(events).toEqual([{ name: 'flag_update', data: { action: 'flag_removed', key: 'x' } }])
    stream.stop()
  })

  test('reconnects with backoff after errors', () => {
    const stream = createStream([], [])
    stream.start()

    const first = FakeEventSource.instances[0]!
    first.onopen?.()
    first.onerror?.()

    expect(stream.getState().status).toBe('reconnecting')
    expect(stream.getState().reconnects).toBe(1)
    expect(first.closed).toBe(true)

    vi.advanceTimersByTime(1500)
    expect(FakeEventSource.instances).toHaveLength(2)
    stream.stop()
  })

  test('forces a reconnect when the heartbeat goes quiet for 90s', () => {
    const stream = createStream([], [])
    stream.start()

    const source = FakeEventSource.instances[0]!
    source.onopen?.()
    expect(stream.getState().status).toBe('open')

    // No events arrive; watchdog (10s cadence) should trip after 90s.
    vi.advanceTimersByTime(101_000)
    expect(stream.getState().status).toBe('reconnecting')
    stream.stop()
  })

  test('stop() closes the source and resets state', () => {
    const stream = createStream([], [])
    stream.start()
    const source = FakeEventSource.instances[0]!
    stream.stop()
    expect(source.closed).toBe(true)
    expect(stream.getState()).toEqual({ status: 'idle', lastEventAt: null, reconnects: 0 })
  })
})
