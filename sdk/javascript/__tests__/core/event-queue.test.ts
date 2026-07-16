import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { EventQueue } from '../../src/core/event-queue';
import { Transport } from '../../src/core/transport';
import { OfflineStorage } from '../../src/core/storage';
import { Scrubber } from '../../src/privacy/scrubber';
import { ConsentManager } from '../../src/privacy/consent';
import { resolveConfig, type ResolvedConfig } from '../../src/core/config';
import type { TrackEvent } from '../../src/core/types';
import { MAX_SERIALIZED_REQUEST_BYTES } from '../../src/core/event-validation';
import { CLIENT_KEY, ENDPOINT } from '../helpers';

function createConfig(overrides?: Partial<ResolvedConfig>): ResolvedConfig {
  const base = resolveConfig({
    endpoint: ENDPOINT,
    auth: {
      clientKey: CLIENT_KEY,
    },
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
  });

  return {
    ...base,
    ...overrides,
    endpoint: overrides?.endpoint ?? base.endpoint,
    auth: {
      ...base.auth,
      ...(overrides?.auth ?? {}),
    },
    autoCapture: {
      ...base.autoCapture,
      ...(overrides?.autoCapture ?? {}),
    },
    consent: {
      ...base.consent,
      ...(overrides?.consent ?? {}),
    },
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
    transport = new Transport(CLIENT_KEY);
    storage = new OfflineStorage({ projectId: config.projectId });
    scrubber = new Scrubber(false); // No built-in scrubbers for testing
    consentManager = new ConsentManager(
      { analytics: true, personalization: true, experiments: true },
      'memory',
      config.projectId
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

    it.each([
      ['undefined', undefined],
      ['BigInt', BigInt(1)],
      ['NaN', Number.NaN],
      ['positive infinity', Number.POSITIVE_INFINITY],
      ['negative infinity', Number.NEGATIVE_INFINITY],
      ['function', () => undefined],
      ['symbol', Symbol('unsupported')],
      ['Date', new Date('2026-07-13T12:00:00.000Z')],
    ])('should synchronously reject unsupported %s property values', (_name, value) => {
      expect(() => queue.enqueue(createEvent({ properties: { value } }))).toThrow(
        'APDL: invalid event.properties.value'
      );
      expect(queue.length).toBe(0);
    });

    it('should synchronously reject cyclic values before the scrubber traverses them', () => {
      const properties: Record<string, unknown> = {};
      properties.self = properties;

      expect(() => queue.enqueue(createEvent({ properties }))).toThrow(
        'cyclic values are not supported'
      );
      expect(queue.length).toBe(0);
    });

    it('should reject explicit null instead of treating it as an omitted event name', () => {
      expect(() =>
        queue.enqueue(
          createEvent({ event: null as unknown as string })
        )
      ).toThrow('APDL: invalid event.event');
      expect(queue.length).toBe(0);
    });

    it.each([
      '2026-02-30T12:00:00.000Z',
      '2026-07-13T12:00:00.000-04:00',
      'not-a-timestamp',
    ])('should synchronously reject invalid timestamp %s', (timestamp) => {
      expect(() => queue.enqueue(createEvent({ timestamp }))).toThrow(
        'must be a valid RFC3339 UTC timestamp'
      );
      expect(queue.length).toBe(0);
    });

    it('should enforce the server depth, cardinality, node, and event-byte limits', () => {
      let atDepthLimit: unknown = 'leaf';
      for (let index = 0; index < 8; index += 1) {
        atDepthLimit = { child: atDepthLimit };
      }
      queue.enqueue(createEvent({ properties: { value: atDepthLimit } }));
      expect(queue.length).toBe(1);

      let pastDepthLimit: unknown = 'leaf';
      for (let index = 0; index < 9; index += 1) {
        pastDepthLimit = { child: pastDepthLimit };
      }
      expect(() => queue.enqueue(createEvent({ properties: { value: pastDepthLimit } })))
        .toThrow('maximum depth 10');

      expect(() => queue.enqueue(createEvent({
        properties: { value: Array.from({ length: 101 }, () => 1) },
      }))).toThrow('array exceeds 100 entries');

      expect(() => queue.enqueue(createEvent({
        properties: {
          value: Array.from(
            { length: 10 },
            () => Array.from({ length: 100 }, () => 1)
          ),
        },
      }))).toThrow('event exceeds 1000 JSON nodes');

      expect(() => queue.enqueue(createEvent({
        properties: { value: { payload: 'x'.repeat(65_536) } },
      }))).toThrow('exceeds 65536 bytes');
    });

    it('should detach canonical queued values from caller mutation', () => {
      const properties = { nested: { plan: 'free' } };
      queue.enqueue(createEvent({ properties }));

      properties.nested.plan = 'enterprise';

      expect(queue.getQueue()[0].properties).toEqual({
        nested: { plan: 'free' },
      });
    });

    it('should reject unknown context fields introduced by a custom scrubber', () => {
      scrubber.addScrubber((event) => ({
        ...event,
        context: {
          ...event.context,
          device_type: 'desktop',
        } as TrackEvent['context'],
      }));

      expect(() => queue.enqueue(createEvent())).toThrow(
        'event.context.device_type: unknown context field'
      );
      expect(queue.length).toBe(0);
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

    it.each(['$click', '$rage_click'])(
      'should remove sensitive data from reserved %s events without mutating the source',
      (eventName) => {
        const event = createEvent({
          event: eventName,
          properties: { text: 'live-password', tag: 'input' },
          context: {
            locale: 'en-CA',
            referrer: 'https://referrer.test/?token=referrer-secret',
            page: {
              url: 'https://example.test/account?token=query-secret#fragment-secret',
              title: 'Password reset secret',
              path: '/account/secret-path',
              search: '?token=query-secret',
            },
          },
        });

        queue.enqueue(event);

        expect(queue.getQueue()[0].properties).toEqual({ tag: 'input' });
        expect(queue.getQueue()[0].context).toEqual({ locale: 'en-CA' });
        expect(event.properties).toEqual({
          text: 'live-password',
          tag: 'input',
        });
        expect(event.context).toEqual({
          locale: 'en-CA',
          referrer: 'https://referrer.test/?token=referrer-secret',
          page: {
            url: 'https://example.test/account?token=query-secret#fragment-secret',
            title: 'Password reset secret',
            path: '/account/secret-path',
            search: '?token=query-secret',
          },
        });
      }
    );

    it('should enforce reserved-event safety before and after custom scrubbers', async () => {
      const sendSpy = vi.spyOn(transport, 'send').mockResolvedValue('accepted');
      const customScrubber = vi.fn((event: TrackEvent): TrackEvent => ({
        ...event,
        properties: {
          ...event.properties,
          text: 'reintroduced-secret',
          tag: 'input?token=reintroduced-secret',
          x: 'reintroduced-secret',
        },
        context: {
          ...event.context,
          referrer: 'https://referrer.test/?token=reintroduced-secret',
          page: {
            url: 'https://example.test/?token=reintroduced-secret#fragment',
            title: 'Reintroduced secret title',
            path: '/reintroduced-secret',
            search: '?token=reintroduced-secret',
          },
        },
      }));
      scrubber.addScrubber(customScrubber);

      queue.enqueue(createEvent({
        event: '$click',
        properties: { text: 'live-password', tag: 'input' },
        context: {
          locale: 'en-CA',
          referrer: 'https://referrer.test/?token=initial-secret',
          page: {
            url: 'https://example.test/?token=initial-secret#fragment',
            title: 'Initial secret title',
            path: '/initial-secret',
            search: '?token=initial-secret',
          },
        },
      }));

      expect(customScrubber.mock.calls[0][0].properties).toEqual({
        tag: 'input',
      });
      expect(customScrubber.mock.calls[0][0].context).toEqual({ locale: 'en-CA' });
      expect(queue.getQueue()[0]).toMatchObject({
        properties: {},
        context: { locale: 'en-CA' },
      });
      expect(JSON.stringify(queue.getQueue()[0])).not.toContain('reintroduced-secret');

      await queue.flush();

      const payload = sendSpy.mock.calls[0][1];
      expect(JSON.stringify(payload)).not.toContain('initial-secret');
      expect(JSON.stringify(payload)).not.toContain('reintroduced-secret');
    });

    it('should preserve text on non-reserved events', () => {
      queue.enqueue(createEvent({
        event: 'search_submitted',
        properties: { text: 'product query' },
      }));

      expect(queue.getQueue()[0].properties).toEqual({
        text: 'product query',
      });
    });

    it('should keep only canonical structural properties on reserved events', () => {
      queue.enqueue(createEvent({
        event: '$click',
        properties: {
          tag: 'a',
          href: 'https://example.test/reset?token=secret',
          id: 'secret-id',
          classes: 'secret-class',
          x: 10,
          y: 20,
        },
      }));

      expect(queue.getQueue()[0].properties).toEqual({
        tag: 'a',
        x: 10,
        y: 20,
      });
    });

    it('should preserve only canonical bounded values for reserved events', () => {
      queue.enqueue(createEvent({
        event: '$click',
        properties: {
          tag: 'checkout-button',
          x: 0,
          y: 100_000,
          clickCount: 3,
        },
      }));
      queue.enqueue(createEvent({
        event: '$rage_click',
        properties: {
          tag: `a${'b'.repeat(63)}`,
          x: 0.5,
          y: 99_999.5,
          clickCount: 3,
        },
      }));
      queue.enqueue(createEvent({
        event: '$rage_click',
        properties: {
          tag: 'button',
          x: 100_000,
          y: 0,
          clickCount: 100,
        },
      }));

      expect(queue.getQueue().map((event) => event.properties)).toEqual([
        { tag: 'checkout-button', x: 0, y: 100_000 },
        {
          tag: `a${'b'.repeat(63)}`,
          x: 0.5,
          y: 99_999.5,
          clickCount: 3,
        },
        {
          tag: 'button',
          x: 100_000,
          y: 0,
          clickCount: 100,
        },
      ]);
    });

    it('should drop invalid or secret-shaped values from canonical property keys', () => {
      queue.enqueue(createEvent({
        event: '$click',
        properties: {
          tag: 'input?token=query-secret#fragment-secret',
          x: 'live-password',
          y: false,
        },
      }));
      queue.enqueue(createEvent({
        event: '$rage_click',
        properties: {
          tag: `secret-${'x'.repeat(58)}`,
          x: -1,
          y: 100_000.01,
          clickCount: 2,
        },
      }));
      queue.enqueue(createEvent({
        event: '$rage_click',
        properties: {
          tag: 'INPUT',
          x: 'not-a-coordinate',
          y: 'live-password',
          clickCount: 101,
        },
      }));
      queue.enqueue(createEvent({
        event: '$rage_click',
        properties: {
          tag: '',
          x: null,
          y: false,
          clickCount: 3.14,
        },
      }));

      expect(queue.getQueue().map((event) => event.properties)).toEqual([
        {},
        {},
        {},
        {},
      ]);
      expect(JSON.stringify(queue.getQueue())).not.toContain('live-password');
      expect(JSON.stringify(queue.getQueue())).not.toContain('query-secret');
    });

    it('should reject a new event at maxQueueSize without evicting accepted events', () => {
      const smallConfig = createConfig({ maxQueueSize: 3, batchSize: 100 });
      const smallQueue = new EventQueue(
        smallConfig,
        transport,
        storage,
        scrubber,
        consentManager
      );

      for (let i = 0; i < 3; i++) {
        smallQueue.enqueue(createEvent({ event: `event_${i}` }));
      }

      expect(() => smallQueue.enqueue(createEvent({ event: 'event_3' })))
        .toThrow('APDL: event queue is full');
      expect(smallQueue.length).toBe(3);
      expect(smallQueue.getQueue().map((event) => event.event)).toEqual([
        'event_0',
        'event_1',
        'event_2',
      ]);
      smallQueue.stop();
    });
  });

  describe('auto-flush at batchSize', () => {
    it('should trigger flush when batch size is reached', async () => {
      const sendSpy = vi
        .spyOn(transport, 'send')
        .mockResolvedValue('accepted');

      for (let i = 0; i < 5; i++) {
        queue.enqueue(createEvent());
      }
      await Promise.resolve();

      // Flush is async, but should have been triggered
      expect(sendSpy).toHaveBeenCalled();
    });
  });

  describe('flush()', () => {
    it('should share one drain promise and drain events accepted during an in-flight batch', async () => {
      const drainingQueue = new EventQueue(
        createConfig({ batchSize: 2 }),
        transport,
        storage,
        scrubber,
        consentManager
      );
      let resolveFirstSend: ((outcome: 'accepted') => void) | undefined;
      const sendSpy = vi.spyOn(transport, 'send')
        .mockImplementationOnce(() => new Promise((resolve) => {
          resolveFirstSend = resolve;
        }))
        .mockResolvedValue('accepted');

      drainingQueue.enqueue(createEvent({ event: 'event_1' }));
      drainingQueue.enqueue(createEvent({ event: 'event_2' }));
      drainingQueue.enqueue(createEvent({ event: 'event_3' }));
      drainingQueue.enqueue(createEvent({ event: 'event_4' }));

      const first = drainingQueue.flush();
      const second = drainingQueue.flush();
      expect(second).toBe(first);

      await Promise.resolve();
      resolveFirstSend?.('accepted');
      const report = await first;

      expect(sendSpy).toHaveBeenCalledTimes(2);
      expect(report).toMatchObject({
        delivered: 4,
        persisted: 0,
        discardedForConsent: 0,
        pending: [],
      });
      expect(drainingQueue.length).toBe(0);
      drainingQueue.stop();
    });

    it('should requeue once and report immutable pending events if persistence fails', async () => {
      const boundedQueue = new EventQueue(
        createConfig({ batchSize: 1, maxQueueSize: 2 }),
        transport,
        storage,
        scrubber,
        consentManager
      );
      let resolveSend: ((outcome: 'retryable') => void) | undefined;
      const sendSpy = vi.spyOn(transport, 'send').mockImplementation(
        () => new Promise((resolve) => {
          resolveSend = resolve;
        })
      );
      vi.spyOn(storage, 'store').mockRejectedValue(new Error('IndexedDB failed'));

      boundedQueue.enqueue(createEvent({ event: 'first', messageId: 'first' }));
      boundedQueue.enqueue(createEvent({ event: 'second', messageId: 'second' }));
      expect(() => boundedQueue.enqueue(createEvent({ event: 'third' })))
        .toThrow('APDL: event queue is full');

      const drain = boundedQueue.flush();
      await Promise.resolve();
      resolveSend?.('retryable');
      const report = await drain;

      expect(sendSpy).toHaveBeenCalledTimes(1);
      expect(boundedQueue.getQueue().map((event) => event.event)).toEqual([
        'first',
        'second',
      ]);
      expect(report.pending.map((event) => event.event)).toEqual([
        'first',
        'second',
      ]);
      expect(Object.isFrozen(report)).toBe(true);
      expect(Object.isFrozen(report.pending)).toBe(true);
      expect(Object.isFrozen(report.pending[0])).toBe(true);
      boundedQueue.stop();
    });

    it('should send batched events via transport', async () => {
      const sendSpy = vi
        .spyOn(transport, 'send')
        .mockResolvedValue('accepted');

      queue.enqueue(createEvent());
      queue.enqueue(createEvent());
      await queue.flush();

      expect(sendSpy).toHaveBeenCalledTimes(1);
      const call = sendSpy.mock.calls[0];
      expect(call[0]).toBe(`${ENDPOINT}/v1/events`);
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

    it('should re-apply reserved-event safety before transport and offline storage', async () => {
      const sendSpy = vi.spyOn(transport, 'send').mockResolvedValue('retryable');
      const storeSpy = vi.spyOn(storage, 'store').mockResolvedValue();

      queue.enqueue(createEvent({
        event: '$click',
        properties: { tag: 'input', x: 1, y: 2 },
        context: {
          locale: 'en-CA',
          library: { name: 'apdl-js', version: 'test' },
        },
      }));
      const queued = queue.getQueue()[0];
      queued.properties = {
        ...queued.properties,
        text: 'reintroduced-password',
        href: 'https://example.test/reset?token=secret',
        tag: 'input?token=reintroduced-secret#fragment',
        x: 'reintroduced-password',
        y: Number.NaN,
      };
      queued.context = {
        ...queued.context,
        referrer: 'https://referrer.test/?token=reintroduced-secret',
        page: {
          url: 'https://example.test/reset?token=reintroduced-secret#fragment',
          title: 'Reintroduced password',
          path: '/reset/reintroduced-secret',
          search: '?token=reintroduced-secret',
        },
      };

      await queue.flush();

      const payload = sendSpy.mock.calls[0][1] as {
        events: Array<{
          properties?: Record<string, unknown>;
          context: Record<string, unknown>;
        }>;
      };
      expect(payload.events[0]).toMatchObject({
        properties: {},
        context: {
          locale: 'en-CA',
          library: { name: 'apdl-js', version: 'test' },
        },
      });
      expect(payload.events[0].context).not.toHaveProperty('page');
      expect(payload.events[0].context).not.toHaveProperty('referrer');
      expect(storeSpy.mock.calls[0][0][0]).toMatchObject({
        properties: {},
        context: {
          locale: 'en-CA',
          library: { name: 'apdl-js', version: 'test' },
        },
      });
      expect(JSON.stringify(payload)).not.toContain('reintroduced');
      expect(JSON.stringify(storeSpy.mock.calls[0][0])).not.toContain('reintroduced');
    });

    it('should normalize SDK camelCase event fields for ingestion', async () => {
      const sendSpy = vi
        .spyOn(transport, 'send')
        .mockResolvedValue('accepted');

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
          browser: { name: 'Chrome', version: '123' },
          device: { type: 'desktop' },
        },
      });
    });

    it('should clear the queue after successful flush', async () => {
      vi.spyOn(transport, 'send').mockResolvedValue('accepted');

      queue.enqueue(createEvent());
      await queue.flush();

      expect(queue.length).toBe(0);
    });

    it('should drop a permanent rejection and continue with neighboring events', async () => {
      const sendSpy = vi.spyOn(transport, 'send')
        .mockResolvedValueOnce('permanent_rejection')
        .mockResolvedValueOnce('accepted');
      const storeSpy = vi.spyOn(storage, 'store').mockResolvedValue();

      queue.enqueue(createEvent({ event: 'permanently_rejected', messageId: 'msg-rejected' }));
      const firstFlush = queue.flush();
      await Promise.resolve();
      queue.enqueue(createEvent({ event: 'valid_neighbor', messageId: 'msg-neighbor' }));

      const report = await firstFlush;
      await Promise.resolve();
      await Promise.resolve();

      expect(sendSpy).toHaveBeenCalledTimes(2);
      expect(storeSpy).not.toHaveBeenCalled();
      expect(report.permanentRejections.map((event) => event.messageId)).toEqual([
        'msg-rejected',
      ]);
      expect(Object.isFrozen(report.permanentRejections[0])).toBe(true);
      const neighborPayload = sendSpy.mock.calls[1][1] as {
        events: Array<{ event: string; message_id: string }>;
      };
      expect(neighborPayload.events).toEqual([
        expect.objectContaining({
          event: 'valid_neighbor',
          message_id: 'msg-neighbor',
        }),
      ]);
    });

    it('should quarantine a queued record mutated into a poison value', async () => {
      const sendSpy = vi.spyOn(transport, 'send').mockResolvedValue('accepted');
      queue.enqueue(createEvent({ event: 'mutated', messageId: 'msg-mutated' }));
      queue.enqueue(createEvent({ event: 'valid', messageId: 'msg-valid' }));
      queue.getQueue()[0].properties = { value: BigInt(1) };

      await queue.flush();

      expect(sendSpy).toHaveBeenCalledTimes(1);
      const payload = sendSpy.mock.calls[0][1] as {
        events: Array<{ event: string; message_id: string }>;
      };
      expect(payload.events).toEqual([
        expect.objectContaining({ event: 'valid', message_id: 'msg-valid' }),
      ]);
    });

    it('should form requests below the server byte limit without changing message IDs', async () => {
      const boundedQueue = new EventQueue(
        createConfig({ batchSize: 100 }),
        transport,
        storage,
        scrubber,
        consentManager
      );
      const sendSpy = vi.spyOn(transport, 'send').mockResolvedValue('accepted');
      const messageIds = Array.from({ length: 10 }, (_, index) => `large-msg-${index}`);

      for (const [index, messageId] of messageIds.entries()) {
        boundedQueue.enqueue(createEvent({
          event: `large_${index}`,
          messageId,
          properties: { value: { payload: 'x'.repeat(60_000) } },
        }));
      }

      await boundedQueue.flush();
      await Promise.resolve();
      await Promise.resolve();

      expect(sendSpy.mock.calls.length).toBeGreaterThan(1);
      const sentMessageIds: string[] = [];
      for (const call of sendSpy.mock.calls) {
        const payload = call[1] as { events: Array<{ message_id: string }> };
        expect(new TextEncoder().encode(JSON.stringify(payload)).byteLength)
          .toBeLessThanOrEqual(MAX_SERIALIZED_REQUEST_BYTES);
        sentMessageIds.push(...payload.events.map((event) => event.message_id));
      }
      expect(sentMessageIds).toEqual(messageIds);
      boundedQueue.stop();
    });

    it('should not send if queue is empty', async () => {
      const sendSpy = vi
        .spyOn(transport, 'send')
        .mockResolvedValue('accepted');

      await queue.flush();

      expect(sendSpy).not.toHaveBeenCalled();
    });
  });

  describe('offline fallback', () => {
    it('should persist events to offline storage on send failure', async () => {
      vi.spyOn(transport, 'send').mockResolvedValue('retryable');
      const storeSpy = vi
        .spyOn(storage, 'store')
        .mockResolvedValue(undefined);

      queue.enqueue(createEvent());
      queue.enqueue(createEvent({ messageId: 'retry-message-2' }));
      await queue.flush();

      expect(storeSpy).toHaveBeenCalledTimes(1);
      const storedEvents = storeSpy.mock.calls[0][0] as TrackEvent[];
      expect(storedEvents).toHaveLength(2);
      expect(storedEvents[1].messageId).toBe('retry-message-2');
    });
  });

  describe('analytics consent fencing', () => {
    it('should re-check consent immediately before every send', async () => {
      const sendSpy = vi.spyOn(transport, 'send').mockResolvedValue('accepted');
      queue.enqueue(createEvent({ event: 'accepted_before_revocation' }));

      consentManager.update({ analytics: false });
      const report = await queue.flush();

      expect(sendSpy).not.toHaveBeenCalled();
      expect(report.discardedForConsent).toBe(1);
      expect(queue.length).toBe(0);
    });

    it('should abort an in-flight send and clear both memory and offline storage', async () => {
      let observedSignal: AbortSignal | undefined;
      const sendSpy = vi.spyOn(transport, 'send').mockImplementation(
        (_url, _payload, signal) => new Promise((resolve) => {
          observedSignal = signal;
          signal?.addEventListener('abort', () => resolve('retryable'), {
            once: true,
          });
        })
      );
      const storeSpy = vi.spyOn(storage, 'store');
      const clearSpy = vi.spyOn(storage, 'clear');
      queue.enqueue(createEvent({ event: 'in_flight' }));

      const drain = queue.flush();
      await Promise.resolve();
      consentManager.update({ analytics: false });
      await queue.revokeAnalyticsConsent();
      const report = await drain;

      expect(sendSpy).toHaveBeenCalledTimes(1);
      expect(observedSignal?.aborted).toBe(true);
      expect(storeSpy).not.toHaveBeenCalled();
      expect(clearSpy).toHaveBeenCalled();
      expect(report.discardedForConsent).toBe(1);
      expect(report.pending).toEqual([]);
    });
  });

  describe('flushOnUnload()', () => {
    it('should use a keepalive request', async () => {
      const keepaliveSpy = vi
        .spyOn(transport, 'sendKeepalive')
        .mockResolvedValue('accepted');

      queue.enqueue(createEvent());
      await queue.flushOnUnload();

      expect(keepaliveSpy).toHaveBeenCalledTimes(1);
      expect(queue.length).toBe(0);
    });

    it('should try offline storage if the keepalive request fails', async () => {
      vi.spyOn(transport, 'sendKeepalive').mockResolvedValue('retryable');
      const storeSpy = vi
        .spyOn(storage, 'store')
        .mockResolvedValue(undefined);

      queue.enqueue(createEvent());
      await queue.flushOnUnload();

      expect(storeSpy).toHaveBeenCalled();
    });
  });

  describe('start() and stop()', () => {
    it('should start and stop flush interval', async () => {
      const sendSpy = vi
        .spyOn(transport, 'send')
        .mockResolvedValue('accepted');
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

    it('should remove text from legacy auto-capture events before direct requeue', async () => {
      vi.spyOn(storage, 'drain').mockResolvedValue([
        createEvent({
          event: '$click',
          properties: { text: 'stored-password', tag: 'input' },
        }),
        createEvent({
          event: '$rage_click',
          properties: {
            text: 'stored-password',
            tag: 'input',
            clickCount: 3,
          },
        }),
      ]);

      await queue.start();

      expect(queue.getQueue().map((event) => event.properties)).toEqual([
        { tag: 'input' },
        { tag: 'input', clickCount: 3 },
      ]);
    });

    it('should discard restored events when analytics consent was revoked', async () => {
      const offlineEvent = createEvent({ event: 'revoked_consent_event' });
      await storage.store([offlineEvent]);
      consentManager.update({ analytics: false });

      await queue.start();

      expect(queue.length).toBe(0);
      expect(await storage.count()).toBe(0);
    });

    it('should reject an offline restore that crossed revoke and regrant', async () => {
      const offlineEvent = createEvent({ event: 'consent_race_event' });
      let resolveDrain: ((events: TrackEvent[]) => void) | undefined;
      vi.spyOn(storage, 'drain').mockImplementation(
        () => new Promise((resolve) => {
          resolveDrain = resolve;
        })
      );

      const startPromise = queue.start();
      await Promise.resolve();
      consentManager.update({ analytics: false });
      const cleared = queue.revokeAnalyticsConsent();
      consentManager.update({ analytics: true });
      resolveDrain?.([offlineEvent]);
      await Promise.all([startPromise, cleared]);

      expect(queue.length).toBe(0);
      expect((await queue.flush()).discardedForConsent).toBe(1);
    });
  });

  describe('shutdown()', () => {
    it('should join concurrent callers, fully drain, and reject later enqueues', async () => {
      vi.spyOn(transport, 'send').mockResolvedValue('accepted');
      queue.enqueue(createEvent({ event: 'before_shutdown' }));

      const first = queue.shutdown();
      const second = queue.shutdown();

      expect(second).toBe(first);
      expect(() => queue.enqueue(createEvent({ event: 'after_shutdown' })))
        .toThrow('APDL: client is shut down');
      await expect(first).resolves.toMatchObject({
        delivered: 1,
        pending: [],
      });
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
