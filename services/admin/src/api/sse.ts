// EventSource lifecycle for the same-origin admin proxy: exponential backoff with
// jitter (1s → 30s cap) and a heartbeat watchdog (server emits every 15s; no
// event for 90s forces a reconnect). The browser sends its HttpOnly session
// cookie; the proxy injects the service credential server-side.
import { normalizeBaseUrl } from './http'

export type StreamEventName =
  | 'config'
  | 'flag_update'
  | 'experiment_update'
  | 'heartbeat'
  | 'auth_expired'
  | 'project_access_revoked'

export type StreamStatus = 'idle' | 'connecting' | 'open' | 'reconnecting'

export interface StreamState {
  status: StreamStatus
  lastEventAt: number | null
  reconnects: number
}

export interface FlagStreamHandlers {
  onEvent?: (name: StreamEventName, data: unknown) => void
  onState?: (state: StreamState) => void
}

export type EventSourceFactory = (url: string) => EventSource

const STREAM_EVENTS: StreamEventName[] = [
  'config',
  'flag_update',
  'experiment_update',
  'heartbeat',
  'auth_expired',
  'project_access_revoked',
]
const HEARTBEAT_TIMEOUT_MS = 90_000
const WATCHDOG_INTERVAL_MS = 10_000

export function streamUrl(configUrl: string): string {
  return `${normalizeBaseUrl(configUrl)}/v1/stream`
}

export function reconnectDelayMs(attempt: number): number {
  return Math.min(1000 * 2 ** attempt, 30_000)
}

export class FlagStream {
  private source: EventSource | null = null
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private watchdog: ReturnType<typeof setInterval> | null = null
  private attempt = 0
  private stopped = true
  private state: StreamState = { status: 'idle', lastEventAt: null, reconnects: 0 }

  constructor(
    private readonly url: string,
    private readonly handlers: FlagStreamHandlers = {},
    private readonly factory: EventSourceFactory = (url) => new EventSource(url),
  ) {}

  start(): void {
    if (!this.stopped) return
    this.stopped = false
    this.connect('connecting')
    this.watchdog = setInterval(() => this.checkHeartbeat(), WATCHDOG_INTERVAL_MS)
  }

  stop(): void {
    this.stopped = true
    if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer)
    this.reconnectTimer = null
    if (this.watchdog !== null) clearInterval(this.watchdog)
    this.watchdog = null
    this.closeSource()
    this.setState({ status: 'idle', lastEventAt: null, reconnects: 0 })
  }

  getState(): StreamState {
    return this.state
  }

  private connect(status: 'connecting' | 'reconnecting'): void {
    this.closeSource()
    this.setState({ ...this.state, status })
    let source: EventSource
    try {
      source = this.factory(this.url)
    } catch {
      this.scheduleReconnect()
      return
    }
    this.source = source
    source.onopen = () => {
      this.attempt = 0
      this.setState({ ...this.state, status: 'open', lastEventAt: Date.now() })
    }
    source.onerror = () => this.scheduleReconnect()
    for (const name of STREAM_EVENTS) {
      source.addEventListener(name, (event) => this.receive(name, event as MessageEvent))
    }
  }

  private receive(name: StreamEventName, event: MessageEvent): void {
    this.setState({ ...this.state, status: 'open', lastEventAt: Date.now() })
    let data: unknown = null
    try {
      data = JSON.parse(String(event.data))
    } catch {
      data = null
    }
    this.handlers.onEvent?.(name, data)
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.reconnectTimer !== null) return
    this.closeSource()
    const delay = reconnectDelayMs(this.attempt) + Math.random() * 250
    this.attempt += 1
    this.setState({
      ...this.state,
      status: 'reconnecting',
      reconnects: this.state.reconnects + 1,
    })
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      if (!this.stopped) this.connect('reconnecting')
    }, delay)
  }

  private checkHeartbeat(): void {
    if (this.state.status !== 'open' || this.state.lastEventAt === null) return
    if (Date.now() - this.state.lastEventAt > HEARTBEAT_TIMEOUT_MS) this.scheduleReconnect()
  }

  private closeSource(): void {
    if (this.source) {
      this.source.onopen = null
      this.source.onerror = null
      this.source.close()
      this.source = null
    }
  }

  private setState(next: StreamState): void {
    this.state = next
    this.handlers.onState?.(next)
  }
}
