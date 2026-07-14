import {
  API_KEY_HEADER,
  SDK_IDENTIFIER,
  SDK_IDENTIFIER_HEADER,
} from '../core/constants';

type MessageCallback = (event: { type: string; data: string; id?: string }) => void;

const INITIAL_RECONNECT_DELAY = 1000;
const MAX_RECONNECT_DELAY = 30000;
const HEARTBEAT_INTERVAL = 30000;
const HEARTBEAT_GRACE = 10000;

/**
 * Header-authenticated SSE client built on the Fetch streaming API.
 *
 * Browser EventSource cannot set authorization headers, which would force a
 * credential into the URL. Fetch keeps the client credential out of browser
 * history, proxy logs, referrers, and screenshots while retaining reconnect
 * and Last-Event-ID semantics.
 */
export class SSEConnection {
  private readonly url: string;
  private readonly clientKey: string;
  private readonly debug: boolean;
  private messageCallback: MessageCallback | null = null;
  private lastEventId: string | undefined;
  private reconnectDelay = INITIAL_RECONNECT_DELAY;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeatTimer: ReturnType<typeof setTimeout> | null = null;
  private controller: AbortController | null = null;
  private connected = false;
  private intentionalClose = true;

  constructor(url: string, clientKey: string, debug = false) {
    this.url = url;
    this.clientKey = clientKey;
    this.debug = debug;
  }

  /** Opens the stream. Calling connect while open is idempotent. */
  connect(): void {
    this.intentionalClose = false;
    if (this.controller !== null || this.reconnectTimer !== null) return;
    this.openConnection();
  }

  /** Aborts the active fetch and cancels every future reconnect. */
  disconnect(): void {
    this.intentionalClose = true;
    this.connected = false;
    this.clearHeartbeatTimer();
    this.clearReconnectTimer();
    const controller = this.controller;
    this.controller = null;
    controller?.abort();
  }

  onMessage(callback: MessageCallback): void {
    this.messageCallback = callback;
  }

  isConnected(): boolean {
    return this.connected;
  }

  private openConnection(): void {
    if (this.intentionalClose || this.controller !== null) return;

    const controller = new AbortController();
    this.controller = controller;
    void this.consumeStream(controller)
      .catch((error: unknown) => {
        if (!controller.signal.aborted && this.debug) {
          console.warn('APDL: SSE connection error', error);
        }
      })
      .finally(() => {
        if (this.controller !== controller) return;
        this.controller = null;
        this.connected = false;
        this.clearHeartbeatTimer();
        if (!this.intentionalClose) {
          this.scheduleReconnect();
        }
      });
  }

  private async consumeStream(controller: AbortController): Promise<void> {
    const headers: Record<string, string> = {
      Accept: 'text/event-stream',
      [API_KEY_HEADER]: this.clientKey,
      [SDK_IDENTIFIER_HEADER]: SDK_IDENTIFIER,
    };
    if (this.lastEventId) {
      headers['Last-Event-ID'] = this.lastEventId;
    }

    const response = await fetch(this.url, {
      method: 'GET',
      headers,
      cache: 'no-store',
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new Error(`SSE request failed with status ${response.status}`);
    }
    if (response.body === null) {
      throw new Error('SSE response did not provide a readable stream');
    }

    this.connected = true;
    this.reconnectDelay = INITIAL_RECONNECT_DELAY;
    this.startHeartbeatMonitor(controller);
    if (this.debug) {
      console.debug('APDL: SSE connection opened');
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value === undefined) continue;
        buffer += decoder.decode(value, { stream: true });
        this.resetHeartbeatMonitor(controller);
        buffer = this.dispatchCompleteMessages(buffer);
      }
      buffer += decoder.decode();
      this.dispatchCompleteMessages(buffer);
    } finally {
      reader.releaseLock();
    }
  }

  private dispatchCompleteMessages(input: string): string {
    let buffer = input;
    while (true) {
      const boundary = /(?:\r\n|\r|\n){2}/.exec(buffer);
      if (boundary === null) return buffer;
      const block = buffer.slice(0, boundary.index);
      buffer = buffer.slice(boundary.index + boundary[0].length);
      this.dispatchMessage(block);
    }
  }

  private dispatchMessage(block: string): void {
    let type = 'message';
    const data: string[] = [];
    let eventId: string | undefined;

    for (const line of block.split(/\r\n|\r|\n/)) {
      if (line === '' || line.startsWith(':')) continue;
      const separator = line.indexOf(':');
      const field = separator === -1 ? line : line.slice(0, separator);
      let value = separator === -1 ? '' : line.slice(separator + 1);
      if (value.startsWith(' ')) value = value.slice(1);

      if (field === 'event') {
        type = value || 'message';
      } else if (field === 'data') {
        data.push(value);
      } else if (field === 'id' && !value.includes('\0')) {
        eventId = value;
      } else if (field === 'retry' && /^\d+$/.test(value)) {
        const delay = Number(value);
        if (Number.isInteger(delay) && delay >= 0) {
          this.reconnectDelay = Math.min(
            Math.max(delay, INITIAL_RECONNECT_DELAY),
            MAX_RECONNECT_DELAY
          );
        }
      }
    }

    if (eventId !== undefined) {
      this.lastEventId = eventId;
    }
    if (data.length === 0) return;

    try {
      this.messageCallback?.({
        type,
        data: data.join('\n'),
        ...(eventId === undefined ? {} : { id: eventId }),
      });
    } catch (error) {
      if (this.debug) {
        console.warn('APDL: SSE message handler failed', error);
      }
    }
  }

  private startHeartbeatMonitor(controller: AbortController): void {
    this.clearHeartbeatTimer();
    this.heartbeatTimer = setTimeout(() => {
      if (this.controller !== controller || this.intentionalClose) return;
      if (this.debug) {
        console.warn('APDL: Heartbeat missed, reconnecting');
      }
      this.connected = false;
      controller.abort();
    }, HEARTBEAT_INTERVAL + HEARTBEAT_GRACE);
  }

  private resetHeartbeatMonitor(controller: AbortController): void {
    if (this.connected && this.controller === controller) {
      this.startHeartbeatMonitor(controller);
    }
  }

  private scheduleReconnect(): void {
    if (this.intentionalClose || this.reconnectTimer !== null) return;
    const delay = this.reconnectDelay;
    if (this.debug) {
      console.debug(`APDL: Reconnecting in ${delay}ms`);
    }
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.openConnection();
    }, delay);
    this.reconnectDelay = Math.min(delay * 2, MAX_RECONNECT_DELAY);
  }

  private clearHeartbeatTimer(): void {
    if (this.heartbeatTimer !== null) {
      clearTimeout(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }
}
