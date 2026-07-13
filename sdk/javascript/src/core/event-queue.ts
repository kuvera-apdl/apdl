import type { ResolvedConfig } from './config';
import type { EventContext, TrackEvent } from './types';
import { Transport } from './transport';
import { OfflineStorage } from './storage';
import type { Scrubber } from '../privacy/scrubber';
import type { ConsentManager } from '../privacy/consent';
import { sanitizeAutoCaptureEvent } from '../privacy/auto-capture-safety';

interface IngestionEvent {
  event: string;
  type: TrackEvent['type'];
  user_id?: string;
  anonymous_id: string;
  group_id?: string;
  properties?: Record<string, unknown>;
  traits?: Record<string, unknown>;
  context: EventContext;
  timestamp: string;
  message_id: string;
  session_id: string;
}

/**
 * Batching event queue with offline fallback.
 * Collects events, batches them, and sends via Transport.
 * On failure, persists to IndexedDB for retry on next session.
 */
export class EventQueue {
  private queue: TrackEvent[] = [];
  private transport: Transport;
  private storage: OfflineStorage;
  private config: ResolvedConfig;
  private scrubber: Scrubber;
  private consentManager: ConsentManager;
  private flushTimer: ReturnType<typeof setInterval> | null = null;
  private isFlushing = false;
  private visibilityHandler: (() => void) | null = null;
  private unloadHandler: (() => void) | null = null;
  private started = false;
  private ingestionUrl: string;

  constructor(
    config: ResolvedConfig,
    transport: Transport,
    storage: OfflineStorage,
    scrubber: Scrubber,
    consentManager: ConsentManager
  ) {
    this.config = config;
    this.transport = transport;
    this.storage = storage;
    this.scrubber = scrubber;
    this.consentManager = consentManager;
    this.ingestionUrl = `${config.endpoint}/v1/events`;
  }

  /**
   * Adds an event to the queue. Runs scrubbing, checks consent,
   * and auto-flushes when batch size is reached.
   */
  enqueue(event: TrackEvent): void {
    // Check consent for analytics
    if (!this.consentManager.isGranted('analytics')) {
      if (this.config.debug) {
        console.debug('APDL: Event dropped — analytics consent not granted');
      }
      return;
    }

    // Mandatory safety rules run outside the configurable scrubber pipeline.
    const scrubbed = this.scrubber.scrub(sanitizeAutoCaptureEvent(event));
    if (scrubbed === null) {
      if (this.config.debug) {
        console.debug('APDL: Event dropped by scrubber pipeline');
      }
      return;
    }

    // Enforce max queue size
    if (this.queue.length >= this.config.maxQueueSize) {
      if (this.config.debug) {
        console.warn('APDL: Queue full, dropping oldest event');
      }
      this.queue.shift();
    }

    // Re-apply the mandatory rules in case a custom scrubber reintroduced a
    // forbidden reserved-event property.
    this.queue.push(sanitizeAutoCaptureEvent(scrubbed));

    // Auto-flush when batch size is reached
    if (this.queue.length >= this.config.batchSize) {
      void this.flush();
    }
  }

  /**
   * Flushes the current queue by sending events via the transport.
   * On failure, persists events to offline storage.
   */
  async flush(): Promise<void> {
    if (this.isFlushing || this.queue.length === 0) {
      return;
    }

    this.isFlushing = true;

    // Snapshot and clear queue atomically
    const batch = this.queue
      .splice(0, this.config.batchSize)
      .map(sanitizeAutoCaptureEvent);

    try {
      const payload = {
        events: batch.map((event) => this.toIngestionEvent(event)),
      };

      const success = await this.transport.send(this.ingestionUrl, payload);

      if (!success) {
        // Persist failed batch to offline storage
        if (this.config.debug) {
          console.warn('APDL: Event send failed, persisting to offline storage');
        }
        await this.storage.store(batch);
      }
    } catch (err) {
      if (this.config.debug) {
        console.error('APDL: Unexpected error during flush:', err);
      }
      await this.storage.store(batch);
    } finally {
      this.isFlushing = false;
    }

    // If there are more events remaining, flush again
    if (this.queue.length >= this.config.batchSize) {
      void this.flush();
    }
  }

  /** Sends a keepalive request for reliable delivery during page unload. */
  async flushOnUnload(): Promise<void> {
    if (this.queue.length === 0) return;

    const batch = this.queue.splice(0).map(sanitizeAutoCaptureEvent);
    const payload = {
      events: batch.map((event) => this.toIngestionEvent(event)),
    };

    const accepted = await this.transport.sendKeepalive(this.ingestionUrl, payload);

    if (!accepted) {
      await this.storage.store(batch);
    }
  }

  /**
   * Starts the flush interval, registers page lifecycle listeners,
   * and drains any events from offline storage.
   */
  async start(): Promise<void> {
    if (this.started) return;
    this.started = true;

    // Start periodic flush
    this.flushTimer = setInterval(() => {
      void this.flush();
    }, this.config.flushInterval);

    // Register visibility change listener for flush on hide
    if (typeof document !== 'undefined') {
      this.visibilityHandler = () => {
        if (document.visibilityState === 'hidden') {
          void this.flushOnUnload();
        }
      };
      document.addEventListener('visibilitychange', this.visibilityHandler);
    }

    // Register beforeunload handler
    if (typeof window !== 'undefined') {
      this.unloadHandler = () => {
        void this.flushOnUnload();
      };
      window.addEventListener('beforeunload', this.unloadHandler);
    }

    // Drain offline storage and re-enqueue events. Consent is checked again
    // because it may have changed since these events originally failed.
    try {
      const offlineEvents = await this.storage.drain();
      if (!this.consentManager.isGranted('analytics')) {
        if (offlineEvents.length > 0 && this.config.debug) {
          console.debug(
            `APDL: Discarded ${offlineEvents.length} offline events — analytics consent not granted`
          );
        }
        return;
      }

      if (offlineEvents.length > 0) {
        if (this.config.debug) {
          console.debug(`APDL: Drained ${offlineEvents.length} events from offline storage`);
        }
        // Offline events already passed through configurable scrubbers, but
        // legacy records can predate mandatory auto-capture safety rules.
        this.queue.push(...offlineEvents.map(sanitizeAutoCaptureEvent));
        if (this.queue.length >= this.config.batchSize) {
          void this.flush();
        }
      }
    } catch (err) {
      if (this.config.debug) {
        console.error('APDL: Failed to drain offline storage:', err);
      }
    }
  }

  /**
   * Stops the flush interval and removes lifecycle listeners.
   */
  stop(): void {
    this.started = false;

    if (this.flushTimer !== null) {
      clearInterval(this.flushTimer);
      this.flushTimer = null;
    }

    if (typeof document !== 'undefined' && this.visibilityHandler) {
      document.removeEventListener('visibilitychange', this.visibilityHandler);
      this.visibilityHandler = null;
    }

    if (typeof window !== 'undefined' && this.unloadHandler) {
      window.removeEventListener('beforeunload', this.unloadHandler);
      this.unloadHandler = null;
    }
  }

  /**
   * Returns a snapshot of the current queue (for debugging).
   */
  getQueue(): TrackEvent[] {
    return [...this.queue];
  }

  /**
   * Returns the current queue length.
   */
  get length(): number {
    return this.queue.length;
  }

  private toIngestionEvent(event: TrackEvent): IngestionEvent {
    const normalized: IngestionEvent = {
      event: event.event ?? event.type,
      type: event.type,
      anonymous_id: event.anonymousId,
      context: this.toIngestionContext(event),
      timestamp: event.timestamp,
      message_id: event.messageId,
      session_id: event.sessionId,
    };

    if (event.userId) {
      normalized.user_id = event.userId;
    }

    if (event.groupId) {
      normalized.group_id = event.groupId;
    }

    if (event.properties) {
      normalized.properties = event.properties;
    }

    if (event.traits) {
      normalized.traits = event.traits;
    }

    return normalized;
  }

  private toIngestionContext(event: TrackEvent): EventContext {
    return structuredCloneContext(event.context);
  }
}

function structuredCloneContext(context: EventContext): EventContext {
  return JSON.parse(JSON.stringify(context)) as EventContext;
}
