import type { ResolvedConfig } from './config';
import type { DeliveryReport, EventContext, TrackEvent } from './types';
import { Transport } from './transport';
import { OfflineStorage } from './storage';
import type { Scrubber } from '../privacy/scrubber';
import type { ConsentManager } from '../privacy/consent';
import { sanitizeAutoCaptureEvent } from '../privacy/auto-capture-safety';
import {
  assertSerializedEventSize,
  canonicalizeTrackEvent,
  MAX_EVENTS_PER_BATCH,
  MAX_SERIALIZED_REQUEST_BYTES,
  serializedJsonBytes,
} from './event-validation';
import type { TransportOutcome } from './transport';

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

interface DeliveryAccumulator {
  delivered: number;
  persisted: number;
  permanentRejections: TrackEvent[];
  discardedForConsent: number;
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
  private visibilityHandler: (() => void) | null = null;
  private unloadHandler: (() => void) | null = null;
  private started = false;
  private ingestionUrl: string;
  private accepting = true;
  private flushPromise: Promise<DeliveryReport> | null = null;
  private startPromise: Promise<void> | null = null;
  private startComplete = false;
  private shutdownPromise: Promise<DeliveryReport> | null = null;
  private activeDeliveryController: AbortController | null = null;
  private inFlightEventCount = 0;
  private consentEpoch = 0;
  private consentFence: Promise<void> = Promise.resolve();
  private consentFencePending = false;
  private consentDiscardedSinceReport = 0;

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
    if (!this.accepting) {
      throw new Error('APDL: client is shut down');
    }

    // Check consent for analytics
    if (!this.consentManager.isGranted('analytics')) {
      if (this.config.debug) {
        console.debug('APDL: Event dropped — analytics consent not granted');
      }
      return;
    }

    // Canonicalize before invoking the scrubber: recursive scrubbers cannot
    // safely traverse cyclic inputs, and JSON.stringify would otherwise fail
    // only after this event had already blocked a delivery batch.
    const canonicalInput = canonicalizeTrackEvent(event);

    // Mandatory safety rules run outside the configurable scrubber pipeline.
    const scrubbed = this.scrubber.scrub(
      sanitizeAutoCaptureEvent(canonicalInput)
    );
    if (scrubbed === null) {
      if (this.config.debug) {
        console.debug('APDL: Event dropped by scrubber pipeline');
      }
      return;
    }

    // A custom scrubber can return a new invalid value. Re-run both the
    // mandatory privacy boundary and canonical JSON validation before enqueue.
    const canonicalEvent = canonicalizeTrackEvent(
      sanitizeAutoCaptureEvent(scrubbed)
    );
    assertSerializedEventSize(this.toIngestionEvent(canonicalEvent));

    // Accepted events remain owned by the SDK until delivered, persisted, or
    // explicitly reported. Rejecting the new event preserves that ownership;
    // evicting an older event here would be silent data loss.
    if (
      this.queue.length + this.inFlightEventCount >=
      this.config.maxQueueSize
    ) {
      throw new Error('APDL: event queue is full');
    }

    this.queue.push(canonicalEvent);

    // Auto-flush when batch size is reached
    if (this.queue.length >= this.config.batchSize) {
      void this.flush();
    }
  }

  /**
   * Flushes the current queue by sending events via the transport.
   * On failure, persists events to offline storage.
   */
  flush(): Promise<DeliveryReport> {
    return this.startDrain(false);
  }

  /** Sends a keepalive request for reliable delivery during page unload. */
  flushOnUnload(): Promise<DeliveryReport> {
    return this.startDrain(true);
  }

  /**
   * Starts the flush interval, registers page lifecycle listeners,
   * and drains any events from offline storage.
   */
  start(): Promise<void> {
    if (this.startPromise !== null) return this.startPromise;
    if (this.started) return Promise.resolve();
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

    this.startPromise = this.restoreOfflineEvents();
    void this.startPromise.then(() => {
      this.startComplete = true;
      if (this.queue.length >= this.config.batchSize) {
        void this.flush();
      }
    });
    return this.startPromise;
  }

  private async restoreOfflineEvents(): Promise<void> {
    // Drain offline storage and re-enqueue events. Consent is checked before
    // and after the asynchronous read because revocation is an immediate fence.
    try {
      if (this.consentFencePending) {
        await this.consentFence;
      }
      if (!this.consentManager.isGranted('analytics')) {
        await this.storage.clear();
        return;
      }

      const restoreEpoch = this.consentEpoch;
      const offlineEvents = await this.storage.drain();
      if (
        restoreEpoch !== this.consentEpoch ||
        !this.consentManager.isGranted('analytics')
      ) {
        this.consentDiscardedSinceReport += offlineEvents.length;
        if (offlineEvents.length > 0 && this.config.debug) {
          console.debug(
            `APDL: Discarded ${offlineEvents.length} offline events — analytics consent not granted`
          );
        }
        await this.storage.clear();
        return;
      }

      if (offlineEvents.length > 0) {
        if (this.config.debug) {
          console.debug(`APDL: Drained ${offlineEvents.length} events from offline storage`);
        }
        // Offline events already passed through configurable scrubbers, but
        // legacy records can predate mandatory validation and auto-capture
        // safety rules. Permanently invalid records are quarantined here.
        for (const event of offlineEvents) {
          try {
            const canonical = canonicalizeTrackEvent(
              sanitizeAutoCaptureEvent(event)
            );
            assertSerializedEventSize(this.toIngestionEvent(canonical));
            this.queue.push(canonical);
          } catch (err) {
            if (this.config.debug) {
              console.warn('APDL: Discarded invalid offline event:', err);
            }
          }
        }
      }
    } catch (err) {
      if (this.config.debug) {
        console.error('APDL: Failed to drain offline storage:', err);
      }
    }
  }

  /**
   * Applies an immediate analytics-consent fence.
   *
   * The in-memory queue is cleared synchronously, an in-flight fetch is
   * aborted, and project-owned offline records are cleared before any later
   * delivery can begin.
   */
  revokeAnalyticsConsent(): Promise<void> {
    this.consentEpoch += 1;
    this.consentDiscardedSinceReport += this.queue.length;
    this.queue.splice(0);
    this.activeDeliveryController?.abort();

    const clear = async () => {
      await this.storage.clear();
    };
    const fence = this.consentFence.then(clear, clear);
    this.consentFence = fence;
    this.consentFencePending = true;
    const markSettled = () => {
      if (this.consentFence === fence) {
        this.consentFencePending = false;
      }
    };
    void fence.then(markSettled, markSettled);
    return fence;
  }

  /** Stops accepting new events and waits for every accepted event to drain. */
  shutdown(): Promise<DeliveryReport> {
    if (this.shutdownPromise !== null) return this.shutdownPromise;

    this.accepting = false;
    this.stop();
    this.shutdownPromise = (async () => {
      if (this.startPromise !== null && !this.startComplete) {
        await this.startPromise;
      }
      return this.flush();
    })();
    return this.shutdownPromise;
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

  private startDrain(useKeepalive: boolean): Promise<DeliveryReport> {
    if (this.flushPromise !== null) return this.flushPromise;

    const drain = this.drainQueue(useKeepalive);
    this.flushPromise = drain.finally(() => {
      this.activeDeliveryController = null;
      this.flushPromise = null;
    });
    return this.flushPromise;
  }

  private async drainQueue(useKeepalive: boolean): Promise<DeliveryReport> {
    const report: DeliveryAccumulator = {
      delivered: 0,
      persisted: 0,
      permanentRejections: [],
      discardedForConsent: 0,
    };
    if (this.startPromise !== null && !this.startComplete) {
      await this.startPromise;
    }

    while (this.queue.length > 0) {
      if (this.consentFencePending) {
        await this.consentFence;
      }
      if (!this.consentManager.isGranted('analytics')) {
        report.discardedForConsent += this.queue.length;
        this.queue.splice(0);
        break;
      }

      const epoch = this.consentEpoch;
      const prepared = this.dequeueBatch();
      if (prepared === null) continue;
      this.inFlightEventCount = prepared.events.length;

      // There is no await between this final consent check and fetch creation,
      // so revocation cannot cross the send boundary without aborting it.
      if (
        epoch !== this.consentEpoch ||
        !this.consentManager.isGranted('analytics')
      ) {
        report.discardedForConsent += prepared.events.length;
        this.inFlightEventCount = 0;
        continue;
      }

      const controller = new AbortController();
      this.activeDeliveryController = controller;
      let outcome: TransportOutcome;
      try {
        outcome = useKeepalive
          ? await this.transport.sendKeepalive(
              this.ingestionUrl,
              prepared.payload,
              controller.signal
            )
          : await this.transport.send(
              this.ingestionUrl,
              prepared.payload,
              controller.signal
            );
      } catch (err) {
        if (this.config.debug) {
          console.error('APDL: Unexpected error during flush:', err);
        }
        outcome = 'retryable';
      } finally {
        if (this.activeDeliveryController === controller) {
          this.activeDeliveryController = null;
        }
      }

      if (
        epoch !== this.consentEpoch ||
        !this.consentManager.isGranted('analytics')
      ) {
        report.discardedForConsent += prepared.events.length;
        this.inFlightEventCount = 0;
        continue;
      }

      if (outcome === 'retryable') {
        if (this.config.debug) {
          console.warn(
            'APDL: Retryable event send failure, persisting to offline storage'
          );
        }
        try {
          await this.storage.store(prepared.events);
          if (
            epoch !== this.consentEpoch ||
            !this.consentManager.isGranted('analytics')
          ) {
            await this.storage.clear();
            report.discardedForConsent += prepared.events.length;
          } else {
            report.persisted += prepared.events.length;
          }
          this.inFlightEventCount = 0;
        } catch (err) {
          if (this.config.debug) {
            console.error('APDL: Failed to persist retryable event batch:', err);
          }
          if (
            epoch !== this.consentEpoch ||
            !this.consentManager.isGranted('analytics')
          ) {
            report.discardedForConsent += prepared.events.length;
            this.inFlightEventCount = 0;
          } else {
            // Keep ownership in memory and stop this drain. Continuing would
            // immediately retry the same failed batch forever.
            this.queue.unshift(...prepared.events);
            this.inFlightEventCount = 0;
            break;
          }
        }
      } else if (outcome === 'permanent_rejection') {
        report.permanentRejections.push(...prepared.events);
        if (this.config.debug) {
          console.warn(
            `APDL: Server permanently rejected ${prepared.events.length} event(s); dropping batch`
          );
        }
        this.inFlightEventCount = 0;
      } else {
        report.delivered += prepared.events.length;
        this.inFlightEventCount = 0;
      }
    }

    report.discardedForConsent += this.consentDiscardedSinceReport;
    this.consentDiscardedSinceReport = 0;
    return this.immutableReport(report);
  }

  private immutableReport(report: DeliveryAccumulator): DeliveryReport {
    const permanentRejections = report.permanentRejections.map((event) =>
      deepFreeze(canonicalizeTrackEvent(event))
    );
    const pending = this.queue.map((event) =>
      deepFreeze(canonicalizeTrackEvent(event))
    );

    return Object.freeze({
      delivered: report.delivered,
      persisted: report.persisted,
      permanentRejections: Object.freeze(permanentRejections),
      discardedForConsent: report.discardedForConsent,
      pending: Object.freeze(pending),
    });
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
    return event.context;
  }

  /**
   * Removes a bounded request from the in-memory queue.
   *
   * Every record is validated again so a debug reference or custom integration
   * cannot mutate an already-queued event into a permanent poison record.
   */
  private dequeueBatch(): {
    events: TrackEvent[];
    payload: { events: IngestionEvent[] };
  } | null {
    const events: TrackEvent[] = [];
    const ingestionEvents: IngestionEvent[] = [];
    const maxEvents = Math.min(this.config.batchSize, MAX_EVENTS_PER_BATCH);

    while (this.queue.length > 0 && events.length < maxEvents) {
      const queued = this.queue.shift()!;
      let canonical: TrackEvent;
      let ingestionEvent: IngestionEvent;

      try {
        canonical = canonicalizeTrackEvent(
          sanitizeAutoCaptureEvent(queued)
        );
        ingestionEvent = this.toIngestionEvent(canonical);
        assertSerializedEventSize(ingestionEvent);
      } catch (err) {
        if (this.config.debug) {
          console.warn('APDL: Discarded permanently invalid queued event:', err);
        }
        continue;
      }

      const candidatePayload = {
        events: [...ingestionEvents, ingestionEvent],
      };
      if (serializedJsonBytes(candidatePayload) > MAX_SERIALIZED_REQUEST_BYTES) {
        if (events.length === 0) {
          // This should be unreachable because a single event is capped at 64
          // KiB, but fail closed rather than create an unsendable queue head.
          if (this.config.debug) {
            console.warn('APDL: Discarded event that exceeds request size limit');
          }
          continue;
        }
        this.queue.unshift(canonical);
        break;
      }

      events.push(canonical);
      ingestionEvents.push(ingestionEvent);
    }

    if (events.length === 0) return null;
    return { events, payload: { events: ingestionEvents } };
  }
}

function deepFreeze<T>(value: T): T {
  if (value !== null && typeof value === 'object' && !Object.isFrozen(value)) {
    for (const nested of Object.values(value as Record<string, unknown>)) {
      deepFreeze(nested);
    }
    Object.freeze(value);
  }
  return value;
}
