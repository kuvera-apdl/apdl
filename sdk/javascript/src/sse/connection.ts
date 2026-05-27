type MessageCallback = (event: { type: string; data: string; id?: string }) => void;

const INITIAL_RECONNECT_DELAY = 1000;
const MAX_RECONNECT_DELAY = 30000;
const HEARTBEAT_INTERVAL = 30000;
const HEARTBEAT_GRACE = 10000; // Extra time before considering heartbeat missed

/**
 * EventSource wrapper with auto-reconnection, Last-Event-ID resumption,
 * and heartbeat monitoring.
 */
export class SSEConnection {
  private url: string;
  private apiKey: string;
  private eventSource: EventSource | null = null;
  private messageCallback: MessageCallback | null = null;
  private lastEventId: string | undefined;
  private reconnectDelay = INITIAL_RECONNECT_DELAY;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeatTimer: ReturnType<typeof setTimeout> | null = null;
  private connected = false;
  private intentionalClose = false;
  private debug: boolean;

  constructor(url: string, apiKey: string, debug = false) {
    this.url = url;
    this.apiKey = apiKey;
    this.debug = debug;
  }

  /**
   * Opens the SSE connection. Reconnects automatically on failure.
   */
  connect(): void {
    this.intentionalClose = false;
    this.openConnection();
  }

  /**
   * Closes the SSE connection and stops reconnection attempts.
   */
  disconnect(): void {
    this.intentionalClose = true;
    this.cleanup();
  }

  /**
   * Registers a callback for incoming SSE messages.
   */
  onMessage(callback: MessageCallback): void {
    this.messageCallback = callback;
  }

  /**
   * Returns whether the connection is currently open.
   */
  isConnected(): boolean {
    return this.connected;
  }

  private openConnection(): void {
    if (typeof EventSource === 'undefined') {
      if (this.debug) {
        console.warn('APDL: EventSource not available in this environment');
      }
      return;
    }

    this.cleanup();

    // Build URL with API key and last event ID
    const connectUrl = new URL(this.url);
    connectUrl.searchParams.set('api_key', this.apiKey);
    if (this.lastEventId) {
      connectUrl.searchParams.set('last_event_id', this.lastEventId);
    }

    try {
      this.eventSource = new EventSource(connectUrl.toString());
    } catch (err) {
      if (this.debug) {
        console.error('APDL: Failed to create EventSource:', err);
      }
      this.scheduleReconnect();
      return;
    }

    this.eventSource.onopen = () => {
      this.connected = true;
      this.reconnectDelay = INITIAL_RECONNECT_DELAY;
      this.startHeartbeatMonitor();
      if (this.debug) {
        console.debug('APDL: SSE connection opened');
      }
    };

    this.eventSource.onmessage = (event: MessageEvent) => {
      this.handleMessage('message', event);
    };

    // Listen for typed events
    const eventTypes = [
      'config',
      'flag_update',
      'flags_update',
      'experiment_update',
      'ui_config',
      'heartbeat',
    ];
    for (const type of eventTypes) {
      this.eventSource.addEventListener(type, ((event: MessageEvent) => {
        this.handleMessage(type, event);
      }) as EventListener);
    }

    this.eventSource.onerror = () => {
      this.connected = false;
      if (this.debug) {
        console.warn('APDL: SSE connection error');
      }
      if (!this.intentionalClose) {
        this.cleanup();
        this.scheduleReconnect();
      }
    };
  }

  private handleMessage(type: string, event: MessageEvent): void {
    // Track last event ID for resumption
    if (event.lastEventId) {
      this.lastEventId = event.lastEventId;
    }

    // Reset heartbeat monitor on any message
    this.resetHeartbeatMonitor();

    if (this.messageCallback) {
      this.messageCallback({
        type,
        data: event.data as string,
        id: event.lastEventId || undefined,
      });
    }
  }

  private startHeartbeatMonitor(): void {
    this.clearHeartbeatTimer();
    this.heartbeatTimer = setTimeout(() => {
      if (this.debug) {
        console.warn('APDL: Heartbeat missed, reconnecting');
      }
      if (!this.intentionalClose) {
        this.cleanup();
        this.openConnection();
      }
    }, HEARTBEAT_INTERVAL + HEARTBEAT_GRACE);
  }

  private resetHeartbeatMonitor(): void {
    if (this.connected) {
      this.startHeartbeatMonitor();
    }
  }

  private clearHeartbeatTimer(): void {
    if (this.heartbeatTimer) {
      clearTimeout(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  private scheduleReconnect(): void {
    if (this.intentionalClose) return;

    if (this.debug) {
      console.debug(`APDL: Reconnecting in ${this.reconnectDelay}ms`);
    }

    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.openConnection();
    }, this.reconnectDelay);

    // Exponential backoff
    this.reconnectDelay = Math.min(
      this.reconnectDelay * 2,
      MAX_RECONNECT_DELAY
    );
  }

  private cleanup(): void {
    this.connected = false;
    this.clearHeartbeatTimer();

    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }

    if (this.eventSource) {
      this.eventSource.onopen = null;
      this.eventSource.onmessage = null;
      this.eventSource.onerror = null;
      this.eventSource.close();
      this.eventSource = null;
    }
  }
}
