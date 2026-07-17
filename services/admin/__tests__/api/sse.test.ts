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
    '/api/projects/demo/config/v1/stream',
    {
      onEvent: (name, data) => events.push({ name, data }),
      onState: (state) => states.push(state),
    },
    (url) => new FakeEventSource(url) as unknown as EventSource,
  )
}

describe('streamUrl', () => {
  test('builds a credential-free same-origin stream URL', () => {
    expect(streamUrl('/api/projects/demo/config/')).toBe(
      '/api/projects/demo/config/v1/stream',
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

  test('delivers server-side session expiry events', () => {
    const events: { name: string; data: unknown }[] = []
    const stream = createStream(events, [])
    stream.start()

    FakeEventSource.instances[0]!.emit('auth_expired', {})

    expect(events).toEqual([{ name: 'auth_expired', data: {} }])
    stream.stop()
  })

  test('delivers project-scoped authority revocation separately', () => {
    const events: { name: string; data: unknown }[] = []
    const stream = createStream(events, [])
    stream.start()

    FakeEventSource.instances[0]!.emit('project_access_revoked', {
      project_id: 'demo',
      required_role: 'config:read',
    })

    expect(events).toEqual([
      {
        name: 'project_access_revoked',
        data: { project_id: 'demo', required_role: 'config:read' },
      },
    ])
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
