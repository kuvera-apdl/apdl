import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { EventQueue } from '../../src/core/event-queue';
import { Transport } from '../../src/core/transport';
import { OfflineStorage } from '../../src/core/storage';
import { Scrubber } from '../../src/privacy/scrubber';
import { ConsentManager } from '../../src/privacy/consent';
import type { ResolvedConfig } from '../../src/core/config';
import type { TrackEvent } from '../../src/core/types';

function createConfig(overrides?: Partial<ResolvedConfig>): ResolvedConfig {
  return {
    apiKey: 'test-key',
    host: 'https://ingest.test.dev',
    configHost: 'https://config.test.dev',
    autoCapture: {
      pageViews: false,
      clicks: false,
      formSubmissions: false,
      inputChanges: false,
      scrollDepth: false,
      rage_clicks: false,
    },
    batchSize: 5,
    flushInterval: 3000,
    privacyMode: 'standard',
    consent: { analytics: true, personalization: true, experiments: true },
    persistence: 'memory',
    maxQueueSize: 100,
    debug: false,
    ...overrides,
  };
}

function createEvent(overrides?: Partial<TrackEvent>): TrackEvent {
  return {
    type: 'track',
    event: 'test_event',
    anonymousId: 'anon-1',
    context: {},
    timestamp: new Date().toISOString(),
    messageId: `msg-${Math.random()}`,
    sessionId: 'sess-1',
    ...overrides,
  };
}

describe('EventQueue', () => {
  let config: ResolvedConfig;
  let transport: Transport;
  let storage: OfflineStorage;
  let scrubber: Scrubber;
  let consentManager: ConsentManager;
  let queue: EventQueue;

  beforeEach(() => {
    vi.useFakeTimers();
    config = createConfig();
    transport = new Transport('test-key');
    storage = new OfflineStorage();
    scrubber = new Scrubber(false); // No built-in scrubbers for testing
    consentManager = new ConsentManager(
      { analytics: true, personalization: true, experiments: true },
      'memory'
    );
    queue = new EventQueue(config, transport, storage, scrubber, consentManager);
  });

  afterEach(() => {
    queue.stop();
    vi.useRealTimers();
  });

  describe('enqueue()', () => {
    it('should add events to the queue', () => {
      queue.enqueue(createEvent());
      expect(queue.length).toBe(1);
    });

    it('should increment queue length', () => {
      queue.enqueue(createEvent());
      queue.enqueue(createEvent());
      queue.enqueue(createEvent());
      expect(queue.length).toBe(3);
    });

    it('should drop events when analytics consent is not granted', () => {
      consentManager.update({ analytics: false });
      queue.enqueue(createEvent());
      expect(queue.length).toBe(0);
    });

    it('should drop events rejected by scrubber', () => {
      scrubber.addScrubber(() => null);
      queue.enqueue(createEvent());
      expect(queue.length).toBe(0);
    });

    it('should respect maxQueueSize', () => {
      const smallConfig = createConfig({ maxQueueSize: 3, batchSize: 100 });
      const smallQueue = new EventQueue(
        smallConfig,
        transport,
        storage,
        scrubber,
        consentManager
      );

      for (let i = 0; i < 5; i++) {
        smallQueue.enqueue(createEvent({ event: `event_${i}` }));
      }

      expect(smallQueue.length).toBe(3);
      smallQueue.stop();
    });
  });

  describe('auto-flush at batchSize', () => {
    it('should trigger flush when batch size is reached', () => {
      const sendSpy = vi
        .spyOn(transport, 'send')
        .mockResolvedValue(true);

      for (let i = 0; i < 5; i++) {
        queue.enqueue(createEvent());
      }

      // Flush is async, but should have been triggered
      expect(sendSpy).toHaveBeenCalled();
    });
  });

  describe('flush()', () => {
    it('should send batched events via transport', async () => {
      const sendSpy = vi
        .spyOn(transport, 'send')
        .mockResolvedValue(true);

      queue.enqueue(createEvent());
      queue.enqueue(createEvent());
      await queue.flush();

      expect(sendSpy).toHaveBeenCalledTimes(1);
      const call = sendSpy.mock.calls[0];
      expect(call[0]).toBe('https://ingest.test.dev/v1/events');
      const payload = call[1] as { events: Array<Record<string, unknown>> };
      expect(payload.events).toHaveLength(2);
      expect(payload.events[0]).toMatchObject({
        event: 'test_event',
        type: 'track',
        anonymous_id: 'anon-1',
        session_id: 'sess-1',
      });
      expect(payload.events[0]).toHaveProperty('message_id');
    });

    it('should normalize SDK camelCase event fields for ingestion', async () => {
      const sendSpy = vi
        .spyOn(transport, 'send')
        .mockResolvedValue(true);

      queue.enqueue(createEvent({
        event: undefined,
        type: 'identify',
        userId: 'user-1',
        anonymousId: 'anon-1',
        groupId: 'group-1',
        sessionId: 'sess-1',
        messageId: 'msg-1',
        traits: { plan: 'pro' },
        context: {
          browser: { name: 'Chrome', version: '123' },
          device: { type: 'desktop' },
        },
      }));
      await queue.flush();

      const payload = sendSpy.mock.calls[0][1] as { events: Array<Record<string, unknown>> };
      expect(payload.events[0]).toMatchObject({
        event: 'identify',
        type: 'identify',
        user_id: 'user-1',
        anonymous_id: 'anon-1',
        group_id: 'group-1',
        session_id: 'sess-1',
        message_id: 'msg-1',
        traits: { plan: 'pro' },
        context: {
          browser: 'Chrome',
          browser_version: '123',
          device_type: 'desktop',
        },
      });
    });

    it('should clear the queue after successful flush', async () => {
      vi.spyOn(transport, 'send').mockResolvedValue(true);

      queue.enqueue(createEvent());
      await queue.flush();

      expect(queue.length).toBe(0);
    });

    it('should not send if queue is empty', async () => {
      const sendSpy = vi
        .spyOn(transport, 'send')
        .mockResolvedValue(true);

      await queue.flush();

      expect(sendSpy).not.toHaveBeenCalled();
    });
  });

  describe('offline fallback', () => {
    it('should persist events to offline storage on send failure', async () => {
      vi.spyOn(transport, 'send').mockResolvedValue(false);
      const storeSpy = vi
        .spyOn(storage, 'store')
        .mockResolvedValue(undefined);

      queue.enqueue(createEvent());
      queue.enqueue(createEvent());
      await queue.flush();

      expect(storeSpy).toHaveBeenCalledTimes(1);
      const storedEvents = storeSpy.mock.calls[0][0] as TrackEvent[];
      expect(storedEvents).toHaveLength(2);
    });
  });

  describe('flushOnUnload()', () => {
    it('should use sendBeacon', () => {
      const beaconSpy = vi
        .spyOn(transport, 'sendBeacon')
        .mockReturnValue(true);

      queue.enqueue(createEvent());
      queue.flushOnUnload();

      expect(beaconSpy).toHaveBeenCalledTimes(1);
      expect(queue.length).toBe(0);
    });

    it('should try offline storage if beacon fails', () => {
      vi.spyOn(transport, 'sendBeacon').mockReturnValue(false);
      const storeSpy = vi
        .spyOn(storage, 'store')
        .mockResolvedValue(undefined);

      queue.enqueue(createEvent());
      queue.flushOnUnload();

      expect(storeSpy).toHaveBeenCalled();
    });
  });

  describe('start() and stop()', () => {
    it('should start and stop flush interval', async () => {
      const sendSpy = vi
        .spyOn(transport, 'send')
        .mockResolvedValue(true);
      vi.spyOn(storage, 'drain').mockResolvedValue([]);

      await queue.start();

      queue.enqueue(createEvent());

      // Advance past flush interval
      await vi.advanceTimersByTimeAsync(config.flushInterval + 100);

      expect(sendSpy).toHaveBeenCalled();

      queue.stop();
    });

    it('should drain offline events on start', async () => {
      const offlineEvent = createEvent({ event: 'offline_event' });
      vi.spyOn(storage, 'drain').mockResolvedValue([offlineEvent]);

      await queue.start();

      expect(queue.length).toBeGreaterThanOrEqual(1);

      queue.stop();
    });
  });

  describe('getQueue()', () => {
    it('should return a snapshot of the queue', () => {
      queue.enqueue(createEvent({ event: 'e1' }));
      queue.enqueue(createEvent({ event: 'e2' }));

      const snapshot = queue.getQueue();
      expect(snapshot).toHaveLength(2);
      expect(snapshot[0]).toMatchObject({ event: 'e1' });
      expect(snapshot[1]).toMatchObject({ event: 'e2' });

      // Should be a copy, not a reference
      snapshot.pop();
      expect(queue.length).toBe(2);
    });
  });
});
