import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { IDBFactory } from 'fake-indexeddb';
import { EventQueue } from '../../src/core/event-queue';
import { Transport } from '../../src/core/transport';
import {
  MAX_OFFLINE_EVENTS_PER_PROJECT,
  OfflineStorage,
  type OfflineStoreResult,
} from '../../src/core/storage';
import { Scrubber } from '../../src/privacy/scrubber';
import { ConsentManager } from '../../src/privacy/consent';
import { resolveConfig, type ResolvedConfig } from '../../src/core/config';
import type { TrackEvent } from '../../src/core/types';
import {
  MAX_KEEPALIVE_REQUEST_BYTES,
  MAX_SERIALIZED_REQUEST_BYTES,
} from '../../src/core/event-validation';
import { CLIENT_KEY, ENDPOINT } from '../helpers';

const FIXED_NOW = new Date('2026-07-13T12:00:00.000Z');

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

function durableStoreResult(events: TrackEvent[]): OfflineStoreResult {
  return {
    stored: events.map((event) => ({ event, durability: 'durable' })),
    evicted: [],
    rejected: [],
  };
}

function prependPendingEvent(
  eventQueue: EventQueue,
  event: TrackEvent,
  durableRecordId?: number
): void {
  const internals = eventQueue as unknown as {
    queue: Array<{ event: TrackEvent; durableRecordId?: number }>;
  };
  internals.queue.unshift({
    event,
    ...(durableRecordId === undefined ? {} : { durableRecordId }),
  });
}

function installConsentFence(
  eventQueue: EventQueue,
  promise: Promise<void>
): void {
  const internals = eventQueue as unknown as {
    consentFence: Promise<void>;
    consentFencePending: boolean;
  };
  internals.consentFencePending = true;
  internals.consentFence = promise.finally(() => {
    internals.consentFencePending = false;
  });
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
    vi.setSystemTime(FIXED_NOW);
    config = createConfig();
    transport = new Transport(CLIENT_KEY);
    const storageScope = {
      deploymentOrigin: config.endpoint,
      projectId: config.projectId,
    };
    storage = new OfflineStorage(storageScope);
    scrubber = new Scrubber(false); // No built-in scrubbers for testing
    consentManager = new ConsentManager(
      { analytics: true, personalization: true, experiments: true },
      'memory',
      storageScope,
      true
    );
    queue = new EventQueue(config, transport, storage, scrubber, consentManager);
  });

  afterEach(() => {
    queue.stop();
    vi.useRealTimers();
    vi.unstubAllGlobals();
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

    it.each([
      '2026-07-06T12:00:00.000Z',
      '2026-07-13T12:05:00.000Z',
    ])('should accept timestamp window boundary %s', (timestamp) => {
      queue.enqueue(createEvent({ timestamp }));
      expect(queue.length).toBe(1);
    });

    it.each([
      [
        '2026-07-06T11:59:59.999999Z',
        'must be no more than 7 days old',
      ],
      [
        '2026-07-13T12:05:00.000001Z',
        'must not be more than 5 minutes in the future',
      ],
      ['1000-01-01T00:00:00Z', 'must be no more than 7 days old'],
      [
        '9999-12-31T23:59:59Z',
        'must not be more than 5 minutes in the future',
      ],
    ])('should reject timestamp %s outside the canonical window', (timestamp, message) => {
      expect(() => queue.enqueue(createEvent({ timestamp }))).toThrow(message);
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

    it.each([
      [
        'page',
        {
          url: 'https://example.test/search?q=private#fragment',
          title: 'Private search title',
          path: '/search?q=private#fragment',
          search: '?q=private',
          referrer: 'https://referrer.test/private',
          extra: 'private',
        },
        { url: 'https://example.test/search', path: '/search' },
      ],
      [
        '$form_submit',
        {
          formId: 'private-id',
          formName: 'private-name',
          formAction: 'https://example.test/reset?token=private',
          formMethod: 'POST',
        },
        { formMethod: 'post' },
      ],
      [
        '$input_change',
        {
          tag: 'input',
          inputType: 'email',
          inputName: 'private-name',
          inputId: 'private-id',
          hasValue: true,
        },
        { tag: 'input', inputType: 'email', hasValue: true },
      ],
      [
        '$scroll_depth',
        { threshold: 50, percent: 67, url: 'https://example.test/?secret=1' },
        { threshold: 50, percent: 67 },
      ],
    ])('should enforce the property allowlist for %s auto-capture', (
      eventName,
      properties,
      expected
    ) => {
      queue.enqueue(createEvent({
        event: eventName,
        type: eventName === 'page' ? 'page' : 'track',
        properties,
      }));

      expect(queue.getQueue()[0].properties).toEqual(expected);
      expect(JSON.stringify(queue.getQueue()[0].properties)).not.toContain('private');
      expect(JSON.stringify(queue.getQueue()[0].properties)).not.toContain('secret');
    });

    it('should strip sensitive context before and after custom scrubbers for every event', () => {
      scrubber.addScrubber((event) => ({
        ...event,
        context: {
          ...event.context,
          referrer: 'https://referrer.test/reset?token=reintroduced',
          page: {
            url: 'https://example.test/account?token=reintroduced#fragment',
            title: 'Reintroduced private title',
            path: '/account?token=reintroduced#fragment',
            search: '?token=reintroduced',
          },
        },
      }));

      queue.enqueue(createEvent({
        event: 'custom_event',
        context: {
          referrer: 'https://referrer.test/?token=initial',
          page: {
            url: 'https://example.test/account?token=initial#fragment',
            title: 'Initial private title',
            path: '/account?token=initial#fragment',
            search: '?token=initial',
          },
        },
      }));

      expect(queue.getQueue()[0].context).toEqual({
        page: {
          url: 'https://example.test/account',
          title: '',
          path: '/account',
          search: '',
        },
      });
      expect(JSON.stringify(queue.getQueue()[0].context)).not.toContain('token');
      expect(JSON.stringify(queue.getQueue()[0].context)).not.toContain('private');
      expect(queue.getQueue()[0].context).not.toHaveProperty('referrer');
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
        createConfig({
          batchSize: 1,
          maxQueueSize: 2,
          persistence: 'localStorage',
        }),
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
      const persistentQueue = new EventQueue(
        createConfig({ persistence: 'localStorage' }),
        transport,
        storage,
        scrubber,
        consentManager
      );
      const sendSpy = vi.spyOn(transport, 'send').mockResolvedValue('retryable');
      const storeSpy = vi
        .spyOn(storage, 'store')
        .mockImplementation(async (events) => durableStoreResult(events));

      persistentQueue.enqueue(createEvent({
        event: '$click',
        properties: { tag: 'input', x: 1, y: 2 },
        context: {
          locale: 'en-CA',
          library: { name: 'apdl-js', version: 'test' },
        },
      }));
      const queued = persistentQueue.getQueue()[0];
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

      await persistentQueue.flush();

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
      persistentQueue.stop();
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
      let rejectFirstBatch!: (outcome: 'permanent_rejection') => void;
      const firstOutcome = new Promise<'permanent_rejection'>((resolve) => {
        rejectFirstBatch = resolve;
      });
      const sendSpy = vi.spyOn(transport, 'send')
        .mockReturnValueOnce(firstOutcome)
        .mockResolvedValueOnce('accepted');
      const storeSpy = vi
        .spyOn(storage, 'store')
        .mockResolvedValue(durableStoreResult([]));

      queue.enqueue(createEvent({ event: 'permanently_rejected', messageId: 'msg-rejected' }));
      const firstFlush = queue.flush();
      await vi.waitFor(() => expect(sendSpy).toHaveBeenCalledTimes(1));
      queue.enqueue(createEvent({ event: 'valid_neighbor', messageId: 'msg-neighbor' }));
      rejectFirstBatch('permanent_rejection');

      const report = await firstFlush;

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

    it('should bisect payload rejections and deliver valid neighboring events in order', async () => {
      const sendSpy = vi.spyOn(transport, 'send').mockImplementation(
        async (_url, payload) => {
          const events = (payload as {
            events: Array<{ event: string }>;
          }).events;
          return events.some((item) => item.event === 'invalid')
            ? 'payload_rejected'
            : 'accepted';
        }
      );

      queue.enqueue(createEvent({
        event: 'valid_before',
        messageId: 'msg-before',
      }));
      queue.enqueue(createEvent({ event: 'invalid', messageId: 'msg-invalid' }));
      queue.enqueue(createEvent({ event: 'valid_after', messageId: 'msg-after' }));

      const report = await queue.flush();

      expect(report.delivered).toBe(2);
      expect(report.permanentRejections.map((item) => item.messageId)).toEqual([
        'msg-invalid',
      ]);
      const attemptedEvents = sendSpy.mock.calls.map((call) => {
        const payload = call[1] as { events: Array<{ event: string }> };
        return payload.events.map((item) => item.event);
      });
      expect(attemptedEvents).toEqual([
        ['valid_before', 'invalid', 'valid_after'],
        ['valid_before'],
        ['invalid', 'valid_after'],
        ['invalid'],
        ['valid_after'],
      ]);
    });

    it('should not bisect a permanent request rejection', async () => {
      const sendSpy = vi
        .spyOn(transport, 'send')
        .mockResolvedValue('permanent_rejection');
      queue.enqueue(createEvent({ event: 'first', messageId: 'msg-first' }));
      queue.enqueue(createEvent({ event: 'second', messageId: 'msg-second' }));

      const report = await queue.flush();

      expect(sendSpy).toHaveBeenCalledTimes(1);
      expect(report.delivered).toBe(0);
      expect(report.permanentRejections.map((item) => item.messageId)).toEqual([
        'msg-first',
        'msg-second',
      ]);
    });

    it('should retain a retryable subset and unattempted neighbors during bisection', async () => {
      const sendSpy = vi.spyOn(transport, 'send')
        .mockResolvedValueOnce('payload_rejected')
        .mockResolvedValueOnce('retryable');
      const messageIds = ['msg-first', 'msg-second', 'msg-third'];
      messageIds.forEach((messageId, index) => {
        queue.enqueue(createEvent({ event: `event_${index}`, messageId }));
      });

      const report = await queue.flush();

      expect(sendSpy).toHaveBeenCalledTimes(2);
      expect(report.delivered).toBe(0);
      expect(report.permanentRejections).toEqual([]);
      expect(report.pending.map((item) => item.messageId)).toEqual(messageIds);
      expect(queue.getQueue().map((item) => item.messageId)).toEqual(messageIds);
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
    it('acknowledges a claimed durable record only after server success', async () => {
      const persistentQueue = new EventQueue(
        createConfig({ persistence: 'localStorage' }),
        transport,
        storage,
        scrubber,
        consentManager
      );
      const offlineEvent = createEvent({
        event: 'durable_success_order',
        messageId: 'durable-success-order',
      });
      await storage.store([offlineEvent]);
      let resolveSend: ((outcome: 'accepted') => void) | undefined;
      const sendSpy = vi.spyOn(transport, 'send').mockImplementation(
        () => new Promise((resolve) => {
          resolveSend = resolve;
        })
      );
      await persistentQueue.start();

      const flush = persistentQueue.flush();
      await vi.waitFor(() => expect(sendSpy).toHaveBeenCalledTimes(1));

      expect(await storage.count()).toBe(1);
      resolveSend?.('accepted');
      await expect(flush).resolves.toMatchObject({ delivered: 1 });
      expect(await storage.count()).toBe(0);
      persistentQueue.stop();
    });

    it('acknowledges a claimed durable record after permanent server disposition', async () => {
      const persistentQueue = new EventQueue(
        createConfig({ persistence: 'localStorage' }),
        transport,
        storage,
        scrubber,
        consentManager
      );
      const offlineEvent = createEvent({
        event: 'durable_permanent_rejection',
        messageId: 'durable-permanent-rejection',
      });
      await storage.store([offlineEvent]);
      vi.spyOn(transport, 'send').mockResolvedValue('permanent_rejection');

      await persistentQueue.start();
      const report = await persistentQueue.flush();

      expect(report.permanentRejections.map((event) => event.messageId)).toEqual([
        'durable-permanent-rejection',
      ]);
      expect(await storage.count()).toBe(0);
      persistentQueue.stop();
    });

    it('should persist events to offline storage on send failure', async () => {
      const persistentQueue = new EventQueue(
        createConfig({ persistence: 'localStorage' }),
        transport,
        storage,
        scrubber,
        consentManager
      );
      vi.spyOn(transport, 'send').mockResolvedValue('retryable');
      const storeSpy = vi
        .spyOn(storage, 'store')
        .mockImplementation(async (events) => durableStoreResult(events));

      persistentQueue.enqueue(createEvent());
      persistentQueue.enqueue(createEvent({ messageId: 'retry-message-2' }));
      await persistentQueue.flush();

      expect(storeSpy).toHaveBeenCalledTimes(1);
      const storedEvents = storeSpy.mock.calls[0][0] as TrackEvent[];
      expect(storedEvents).toHaveLength(2);
      expect(storedEvents[1].messageId).toBe('retry-message-2');
      persistentQueue.stop();
    });

    it('reports exact stored, evicted, and rejected offline dispositions', async () => {
      const persistentQueue = new EventQueue(
        createConfig({ persistence: 'localStorage' }),
        transport,
        storage,
        scrubber,
        consentManager
      );
      const countEvicted = createEvent({
        event: 'count_evicted',
        messageId: 'count-evicted',
      });
      const invalidRejected = createEvent({
        event: 'invalid_rejected',
        messageId: 'invalid-rejected',
      });
      const durable = createEvent({
        event: 'durable_survivor',
        messageId: 'durable-survivor',
      });
      vi.spyOn(transport, 'send').mockResolvedValue('retryable');
      vi.spyOn(storage, 'store').mockResolvedValue({
        stored: [{ event: durable, durability: 'durable' }],
        evicted: [
          { event: countEvicted, reason: 'offline_count_limit' },
        ],
        rejected: [
          { event: invalidRejected, reason: 'offline_invalid_event' },
        ],
      });
      persistentQueue.enqueue(countEvicted);
      persistentQueue.enqueue(invalidRejected);
      persistentQueue.enqueue(durable);

      const report = await persistentQueue.flush();

      expect(report.persisted).toBe(1);
      expect(report.pending).toEqual([]);
      expect(report.dropped).toEqual([
        {
          category: 'evicted',
          reason: 'offline_count_limit',
          event: countEvicted,
        },
        {
          category: 'rejected',
          reason: 'offline_invalid_event',
          event: invalidRejected,
        },
      ]);
      expect(Object.isFrozen(report.dropped)).toBe(true);
      expect(Object.isFrozen(report.dropped[0])).toBe(true);
      expect(Object.isFrozen(report.dropped[0].event)).toBe(true);
      persistentQueue.stop();
    });

    it('keeps storage failures pending instead of reporting them as persisted or dropped', async () => {
      const persistentQueue = new EventQueue(
        createConfig({ persistence: 'localStorage' }),
        transport,
        storage,
        scrubber,
        consentManager
      );
      const event = createEvent({
        event: 'storage_failure_pending',
        messageId: 'storage-failure-pending',
      });
      vi.spyOn(transport, 'send').mockResolvedValue('retryable');
      vi.spyOn(storage, 'store').mockResolvedValue({
        stored: [],
        evicted: [],
        rejected: [
          { event, reason: 'offline_storage_failure' },
        ],
      });
      persistentQueue.enqueue(event);

      const report = await persistentQueue.flush();

      expect(report.persisted).toBe(0);
      expect(report.dropped).toEqual([]);
      expect(report.pending).toEqual([event]);
      expect(persistentQueue.getQueue()).toEqual([event]);
      persistentQueue.stop();
    });

    it('reports degraded memory storage as pending and retries it on the next flush', async () => {
      const persistentQueue = new EventQueue(
        createConfig({ persistence: 'localStorage' }),
        transport,
        storage,
        scrubber,
        consentManager
      );
      const event = createEvent({
        event: 'memory_fallback_pending',
        messageId: 'memory-fallback-pending',
      });
      const sendSpy = vi.spyOn(transport, 'send')
        .mockResolvedValueOnce('retryable')
        .mockResolvedValue('accepted');
      persistentQueue.enqueue(event);

      const firstReport = await persistentQueue.flush();

      expect(firstReport.persisted).toBe(0);
      expect(firstReport.pending).toEqual([event]);
      expect(firstReport.dropped).toEqual([]);
      expect(await storage.count()).toBe(1);

      const secondReport = await persistentQueue.flush();

      expect(secondReport.delivered).toBe(1);
      expect(secondReport.pending).toEqual([]);
      expect(await storage.count()).toBe(0);
      expect(sendSpy).toHaveBeenCalledTimes(2);
      persistentQueue.stop();
    });

    it('should report retryable deliveries as pending in memory mode', async () => {
      const storeSpy = vi.spyOn(storage, 'store');
      vi.spyOn(transport, 'send').mockResolvedValue('retryable');
      queue.enqueue(createEvent({ event: 'memory_pending' }));

      const report = await queue.flush();

      expect(storeSpy).not.toHaveBeenCalled();
      expect(report.persisted).toBe(0);
      expect(report.pending.map((event) => event.event)).toEqual([
        'memory_pending',
      ]);
      expect(queue.getQueue().map((event) => event.event)).toEqual([
        'memory_pending',
      ]);
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

    it('should send at most one conservatively bounded keepalive batch', async () => {
      const boundedQueue = new EventQueue(
        createConfig({ batchSize: 100 }),
        transport,
        storage,
        scrubber,
        consentManager
      );
      const keepaliveSpy = vi
        .spyOn(transport, 'sendKeepalive')
        .mockResolvedValue('accepted');
      for (let index = 0; index < 3; index += 1) {
        boundedQueue.enqueue(createEvent({
          event: `keepalive_${index}`,
          messageId: `keepalive-msg-${index}`,
          properties: { value: { payload: 'x'.repeat(20_000) } },
        }));
      }

      const report = await boundedQueue.flushOnUnload();

      expect(keepaliveSpy).toHaveBeenCalledTimes(1);
      const payload = keepaliveSpy.mock.calls[0][1];
      expect(new TextEncoder().encode(JSON.stringify(payload)).byteLength)
        .toBeLessThanOrEqual(MAX_KEEPALIVE_REQUEST_BYTES);
      expect(report.delivered).toBe(2);
      expect(report.pending.map((event) => event.messageId)).toEqual([
        'keepalive-msg-2',
      ]);
      boundedQueue.stop();
    });

    it('should latch sequential lifecycle signals to one keepalive attempt', async () => {
      const boundedQueue = new EventQueue(
        createConfig({ batchSize: 100 }),
        transport,
        storage,
        scrubber,
        consentManager
      );
      vi.spyOn(storage, 'claim').mockResolvedValue([]);
      const keepaliveSpy = vi
        .spyOn(transport, 'sendKeepalive')
        .mockResolvedValue('accepted');
      const normalSpy = vi
        .spyOn(transport, 'send')
        .mockResolvedValue('accepted');
      const visibilityStateSpy = vi
        .spyOn(document, 'visibilityState', 'get')
        .mockReturnValue('hidden');
      await boundedQueue.start();
      for (let index = 0; index < 3; index += 1) {
        boundedQueue.enqueue(createEvent({
          event: `sequential_lifecycle_${index}`,
          messageId: `sequential-lifecycle-msg-${index}`,
          properties: { value: { payload: 'x'.repeat(20_000) } },
        }));
      }

      document.dispatchEvent(new Event('visibilitychange'));
      const firstLifecycleReport = await boundedQueue.flushOnUnload();

      expect(firstLifecycleReport.delivered).toBe(2);
      expect(boundedQueue.length).toBe(1);
      window.dispatchEvent(new Event('pagehide'));
      window.dispatchEvent(new Event('beforeunload'));
      await Promise.resolve();
      expect(keepaliveSpy).toHaveBeenCalledTimes(1);

      const hiddenReport = await boundedQueue.flush();

      expect(normalSpy).not.toHaveBeenCalled();
      expect(hiddenReport.delivered).toBe(0);
      expect(hiddenReport.pending).toHaveLength(1);
      visibilityStateSpy.mockReturnValue('visible');
      document.dispatchEvent(new Event('visibilitychange'));
      const normalReport = await boundedQueue.flush();
      expect(normalSpy).toHaveBeenCalledTimes(1);
      expect(normalReport.delivered).toBe(1);
      expect(normalReport.pending).toEqual([]);
      expect(keepaliveSpy).toHaveBeenCalledTimes(1);
      visibilityStateSpy.mockRestore();
      boundedQueue.stop();
    });

    it('should stay lifecycle-armed until a BFCache resume and then re-arm', async () => {
      const boundedQueue = new EventQueue(
        createConfig({ batchSize: 100 }),
        transport,
        storage,
        scrubber,
        consentManager
      );
      vi.spyOn(storage, 'claim').mockResolvedValue([]);
      const keepaliveSpy = vi
        .spyOn(transport, 'sendKeepalive')
        .mockResolvedValue('accepted');
      const normalSpy = vi
        .spyOn(transport, 'send')
        .mockResolvedValue('accepted');
      await boundedQueue.start();
      for (let index = 0; index < 3; index += 1) {
        boundedQueue.enqueue(createEvent({
          event: `bfcache_cycle_${index}`,
          messageId: `bfcache-cycle-msg-${index}`,
          properties: { value: { payload: 'x'.repeat(20_000) } },
        }));
      }

      window.dispatchEvent(new Event('pagehide'));
      const firstLifecycleReport = await boundedQueue.flushOnUnload();

      expect(firstLifecycleReport.delivered).toBe(2);
      await expect(boundedQueue.flush()).resolves.toMatchObject({
        delivered: 0,
      });
      expect(normalSpy).not.toHaveBeenCalled();
      expect(() => boundedQueue.enqueue(createEvent({
        event: 'rejected_while_suspended',
      }))).toThrow(
        'APDL: cannot enqueue while the document lifecycle is suspended'
      );

      const pageShow = new Event('pageshow');
      Object.defineProperty(pageShow, 'persisted', { value: true });
      window.dispatchEvent(pageShow);

      const resumedReport = await boundedQueue.flush();
      expect(resumedReport.delivered).toBe(1);
      boundedQueue.enqueue(createEvent({
        event: 'accepted_after_bfcache_resume',
        messageId: 'accepted-after-bfcache-resume',
      }));
      window.dispatchEvent(new Event('pagehide'));
      const secondLifecycleReport = await boundedQueue.flushOnUnload();

      expect(secondLifecycleReport.delivered).toBe(1);
      expect(keepaliveSpy).toHaveBeenCalledTimes(2);
      boundedQueue.stop();
    });

    it('should synchronously reject a later pagehide listener enqueue', async () => {
      vi.spyOn(storage, 'claim').mockResolvedValue([]);
      await queue.start();
      let observedError: unknown;
      const laterPagehideListener = () => {
        try {
          queue.enqueue(createEvent({
            event: 'too_late_for_lifecycle_snapshot',
            messageId: 'too-late-for-lifecycle-snapshot',
          }));
        } catch (error) {
          observedError = error;
        }
      };
      window.addEventListener('pagehide', laterPagehideListener);

      window.dispatchEvent(new Event('pagehide'));
      await queue.flushOnUnload();

      expect(observedError).toBeInstanceOf(Error);
      expect((observedError as Error).message).toBe(
        'APDL: cannot enqueue while the document lifecycle is suspended'
      );
      expect(queue.length).toBe(0);
      window.removeEventListener('pagehide', laterPagehideListener);
    });

    it('should retain a valid event that exceeds only the keepalive budget', async () => {
      const boundedQueue = new EventQueue(
        createConfig({ batchSize: 100 }),
        transport,
        storage,
        scrubber,
        consentManager
      );
      const keepaliveSpy = vi
        .spyOn(transport, 'sendKeepalive')
        .mockResolvedValue('accepted');
      boundedQueue.enqueue(createEvent({
        event: 'larger_than_keepalive',
        messageId: 'larger-than-keepalive',
        properties: { value: { payload: 'x'.repeat(50_000) } },
      }));

      const report = await boundedQueue.flushOnUnload();

      expect(keepaliveSpy).not.toHaveBeenCalled();
      expect(report.delivered).toBe(0);
      expect(report.dropped).toEqual([]);
      expect(report.pending.map((event) => event.messageId)).toEqual([
        'larger-than-keepalive',
      ]);
      boundedQueue.stop();
    });

    it('should start durable batch and overflow ownership before awaiting keepalive', async () => {
      const persistentQueue = new EventQueue(
        createConfig({ batchSize: 100, persistence: 'localStorage' }),
        transport,
        storage,
        scrubber,
        consentManager
      );
      const callOrder: string[] = [];
      const acknowledgeStoredSpy = vi
        .spyOn(storage, 'acknowledgeStored')
        .mockResolvedValue(2);
      const storeSpy = vi.spyOn(storage, 'store').mockImplementation(
        async (events) => {
          callOrder.push('store');
          return durableStoreResult(events);
        }
      );
      const keepaliveSpy = vi
        .spyOn(transport, 'sendKeepalive')
        .mockImplementation(async () => {
          callOrder.push('keepalive');
          return 'accepted';
        });
      for (let index = 0; index < 3; index += 1) {
        persistentQueue.enqueue(createEvent({
          event: `durable_overflow_${index}`,
          messageId: `durable-overflow-msg-${index}`,
          properties: { value: { payload: 'x'.repeat(20_000) } },
        }));
      }

      const drain = persistentQueue.flushOnUnload();

      expect(callOrder).toEqual(['store', 'keepalive']);
      const report = await drain;
      expect(storeSpy).toHaveBeenCalledTimes(1);
      expect(storeSpy.mock.calls[0][0].map((event) => event.messageId)).toEqual([
        'durable-overflow-msg-0',
        'durable-overflow-msg-1',
        'durable-overflow-msg-2',
      ]);
      expect(keepaliveSpy).toHaveBeenCalledTimes(1);
      expect(acknowledgeStoredSpy).toHaveBeenCalledWith([
        'durable-overflow-msg-0',
        'durable-overflow-msg-1',
      ]);
      expect(report).toMatchObject({
        delivered: 2,
        persisted: 1,
        pending: [],
        dropped: [],
      });
      persistentQueue.stop();
    });

    it('should report exact unload-overflow storage dispositions', async () => {
      const persistentQueue = new EventQueue(
        createConfig({ batchSize: 100, persistence: 'localStorage' }),
        transport,
        storage,
        scrubber,
        consentManager
      );
      vi.spyOn(transport, 'sendKeepalive').mockResolvedValue('accepted');
      vi.spyOn(storage, 'store').mockImplementation(async (events) => ({
        stored: [
          { event: events[0], durability: 'durable' },
          { event: events[1], durability: 'durable' },
        ],
        evicted: [{
          event: events[2],
          reason: 'offline_count_limit',
        }],
        rejected: [{
          event: events[3],
          reason: 'offline_storage_failure',
        }],
      }));
      for (let index = 0; index < 4; index += 1) {
        persistentQueue.enqueue(createEvent({
          event: `overflow_disposition_${index}`,
          messageId: `overflow-disposition-msg-${index}`,
          properties: { value: { payload: 'x'.repeat(20_000) } },
        }));
      }

      const report = await persistentQueue.flushOnUnload();

      expect(report.delivered).toBe(2);
      expect(report.persisted).toBe(0);
      expect(report.pending.map((event) => event.messageId)).toEqual([
        'overflow-disposition-msg-3',
      ]);
      expect(report.dropped).toEqual([
        {
          category: 'evicted',
          reason: 'offline_count_limit',
          event: expect.objectContaining({
            messageId: 'overflow-disposition-msg-2',
          }),
        },
      ]);
      persistentQueue.stop();
    });

    it('should take over an active normal request in the same lifecycle task', async () => {
      const takeoverQueue = new EventQueue(
        createConfig({ batchSize: 100 }),
        transport,
        storage,
        scrubber,
        consentManager
      );
      let normalSignal: AbortSignal | undefined;
      const sendSpy = vi.spyOn(transport, 'send').mockImplementation(
        (_url, _payload, signal) => new Promise((resolve) => {
          normalSignal = signal;
          signal?.addEventListener('abort', () => resolve('retryable'), {
            once: true,
          });
        })
      );
      const keepaliveSpy = vi
        .spyOn(transport, 'sendKeepalive')
        .mockResolvedValue('accepted');
      takeoverQueue.enqueue(createEvent({
        event: 'navigation_takeover',
        messageId: 'navigation-takeover',
      }));
      const normalDrain = takeoverQueue.flush();
      await vi.waitFor(() => expect(sendSpy).toHaveBeenCalledTimes(1));

      const unloadDrain = takeoverQueue.flushOnUnload();

      expect(normalSignal?.aborted).toBe(true);
      expect(keepaliveSpy).toHaveBeenCalledTimes(1);
      const unloadReport = await unloadDrain;
      const normalReport = await normalDrain;
      expect(unloadReport).toMatchObject({
        delivered: 1,
        persisted: 0,
        pending: [],
      });
      expect(normalReport.delivered).toBe(0);
      const payload = keepaliveSpy.mock.calls[0][1] as {
        events: Array<{ message_id: string }>;
      };
      expect(payload.events.map((event) => event.message_id)).toEqual([
        'navigation-takeover',
      ]);
      expect(takeoverQueue.length).toBe(0);
      takeoverQueue.stop();
    });

    it('should let pagehide win while a normal drain awaits the consent fence', async () => {
      vi.spyOn(storage, 'claim').mockResolvedValue([]);
      await queue.start();
      queue.enqueue(createEvent({
        event: 'blocked_consent_fence',
        messageId: 'blocked-consent-fence',
      }));
      let releaseFence!: () => void;
      installConsentFence(queue, new Promise<void>((resolve) => {
        releaseFence = resolve;
      }));
      const normalSpy = vi
        .spyOn(transport, 'send')
        .mockResolvedValue('accepted');
      const keepaliveSpy = vi
        .spyOn(transport, 'sendKeepalive')
        .mockResolvedValue('accepted');

      const normalDrain = queue.flush();
      await Promise.resolve();
      window.dispatchEvent(new Event('pagehide'));
      const lifecycleDrain = queue.flushOnUnload();

      expect(normalSpy).not.toHaveBeenCalled();
      expect(keepaliveSpy).not.toHaveBeenCalled();
      releaseFence();
      await vi.waitFor(() => expect(keepaliveSpy).toHaveBeenCalledTimes(1));
      const [normalReport, lifecycleReport] = await Promise.all([
        normalDrain,
        lifecycleDrain,
      ]);
      expect(normalSpy).not.toHaveBeenCalled();
      expect(normalReport.delivered).toBe(0);
      expect(lifecycleReport.delivered).toBe(1);
    });

    it('should expose a prepared batch to pagehide while local ACK is blocked', async () => {
      vi.spyOn(storage, 'claim').mockResolvedValue([]);
      await queue.start();
      queue.enqueue(createEvent({
        event: 'valid_after_local_ack',
        messageId: 'valid-after-local-ack',
      }));
      const invalidDurable = createEvent({
        event: 'invalid_durable_before_ack',
        messageId: 'invalid-durable-before-ack',
      });
      invalidDurable.properties = { poison: BigInt(1) };
      prependPendingEvent(queue, invalidDurable, 41);

      let releaseAcknowledge!: () => void;
      const acknowledgeSpy = vi.spyOn(storage, 'acknowledge')
        .mockImplementationOnce(
          () => new Promise<number>((resolve) => {
            releaseAcknowledge = () => resolve(1);
          })
        )
        .mockResolvedValue(1);
      const normalSpy = vi
        .spyOn(transport, 'send')
        .mockResolvedValue('accepted');
      const keepaliveSpy = vi
        .spyOn(transport, 'sendKeepalive')
        .mockResolvedValue('accepted');

      const normalDrain = queue.flush();
      await vi.waitFor(() => expect(acknowledgeSpy).toHaveBeenCalledWith([41]));
      window.dispatchEvent(new Event('pagehide'));
      const lifecycleDrain = queue.flushOnUnload();

      expect(normalSpy).not.toHaveBeenCalled();
      expect(keepaliveSpy).toHaveBeenCalledTimes(1);
      releaseAcknowledge();
      const [normalReport, lifecycleReport] = await Promise.all([
        normalDrain,
        lifecycleDrain,
      ]);
      expect(normalReport.delivered).toBe(0);
      expect(lifecycleReport.delivered).toBe(1);
      expect(queue.length).toBe(0);
    });

    it('should preserve mixed retryables for pagehide while release is blocked', async () => {
      vi.spyOn(storage, 'claim').mockResolvedValue([]);
      await queue.start();
      const durable = createEvent({
        event: 'durable_retryable',
        messageId: 'durable-retryable',
      });
      prependPendingEvent(queue, durable, 73);
      queue.enqueue(createEvent({
        event: 'volatile_retryable',
        messageId: 'volatile-retryable',
      }));

      vi.spyOn(transport, 'send').mockResolvedValue('retryable');
      let releaseDurable!: () => void;
      const releaseSpy = vi.spyOn(storage, 'release').mockImplementation(
        () => new Promise<number>((resolve) => {
          releaseDurable = () => resolve(1);
        })
      );
      vi.spyOn(storage, 'acknowledge').mockResolvedValue(1);
      const keepaliveSpy = vi
        .spyOn(transport, 'sendKeepalive')
        .mockResolvedValue('accepted');

      const normalDrain = queue.flush();
      await vi.waitFor(() => expect(releaseSpy).toHaveBeenCalledWith([73]));
      expect(queue.getQueue().map((event) => event.messageId)).toEqual([
        'durable-retryable',
        'volatile-retryable',
      ]);

      window.dispatchEvent(new Event('pagehide'));
      const lifecycleDrain = queue.flushOnUnload();

      expect(keepaliveSpy).toHaveBeenCalledTimes(1);
      const payload = keepaliveSpy.mock.calls[0][1] as {
        events: Array<{ message_id: string }>;
      };
      expect(payload.events.map((event) => event.message_id)).toEqual([
        'durable-retryable',
        'volatile-retryable',
      ]);
      releaseDurable();
      const [normalReport, lifecycleReport] = await Promise.all([
        normalDrain,
        lifecycleDrain,
      ]);
      expect(normalReport.delivered).toBe(0);
      expect(lifecycleReport.delivered).toBe(2);
      expect(queue.length).toBe(0);
    });

    it('should remove an accepted durable record after a pending release', async () => {
      vi.useRealTimers();
      vi.stubGlobal('indexedDB', new IDBFactory());
      const raceConfig = createConfig({
        endpoint: 'https://h04-release-race.test',
        persistence: 'localStorage',
      });
      const raceStorage = new OfflineStorage({
        deploymentOrigin: raceConfig.endpoint,
        projectId: raceConfig.projectId,
        persistence: 'localStorage',
      });
      const raceQueue = new EventQueue(
        raceConfig,
        transport,
        raceStorage,
        scrubber,
        consentManager
      );
      const durable = createEvent({
        event: 'accepted_during_pending_release',
        messageId: 'accepted-during-pending-release',
      });
      await raceStorage.store([durable]);
      const [claim] = await raceStorage.claim(1);
      if (claim === undefined) throw new Error('expected one durable claim');
      prependPendingEvent(raceQueue, durable, claim.id);

      vi.spyOn(transport, 'send').mockResolvedValue('retryable');
      vi.spyOn(transport, 'sendKeepalive').mockResolvedValue('accepted');
      const originalRelease = raceStorage.release.bind(raceStorage);
      let releaseGate!: () => void;
      const waitForRelease = new Promise<void>((resolve) => {
        releaseGate = resolve;
      });
      const releaseSpy = vi.spyOn(raceStorage, 'release').mockImplementation(
        (ids) => {
          const release = originalRelease(ids);
          return release.then(async (count) => {
            await waitForRelease;
            return count;
          });
        }
      );

      const normalDrain = raceQueue.flush();
      await vi.waitFor(() => expect(releaseSpy).toHaveBeenCalledWith([claim.id]));
      const lifecycleDrain = raceQueue.flushOnUnload();
      const lifecycleReport = await lifecycleDrain;

      expect(lifecycleReport.delivered).toBe(1);
      await vi.waitFor(async () => {
        expect(await raceStorage.count()).toBe(0);
      });
      releaseGate();
      await normalDrain;
      expect(await raceStorage.claim(1)).toEqual([]);
      raceQueue.stop();
    });

    it('should inherit a pending store and report real bound eviction exactly once', async () => {
      vi.useRealTimers();
      vi.stubGlobal('indexedDB', new IDBFactory());
      const boundConfig = createConfig({
        endpoint: 'https://h04-pending-store-bound.test',
        persistence: 'localStorage',
      });
      const boundStorage = new OfflineStorage({
        deploymentOrigin: boundConfig.endpoint,
        projectId: boundConfig.projectId,
        persistence: 'localStorage',
      });
      const boundQueue = new EventQueue(
        boundConfig,
        transport,
        boundStorage,
        scrubber,
        consentManager
      );
      const backlog = Array.from(
        { length: MAX_OFFLINE_EVENTS_PER_PROJECT },
        (_, index) => createEvent({
          event: `pending_store_backlog_${index}`,
          messageId: `pending-store-backlog-${index}`,
        })
      );
      await boundStorage.store(backlog);
      const [oldestClaim] = await boundStorage.claim(1);
      if (oldestClaim === undefined) {
        throw new Error('expected the oldest bounded durable claim');
      }
      expect(oldestClaim.event.messageId).toBe('pending-store-backlog-0');
      prependPendingEvent(
        boundQueue,
        oldestClaim.event,
        oldestClaim.id
      );
      const volatile = createEvent({
        event: 'pending_store_volatile',
        messageId: 'pending-store-volatile',
      });
      boundQueue.enqueue(volatile);

      vi.spyOn(transport, 'send').mockResolvedValue('retryable');
      vi.spyOn(transport, 'sendKeepalive').mockResolvedValue('retryable');
      const originalStore = boundStorage.store.bind(boundStorage);
      let storeGate!: () => void;
      const waitForStore = new Promise<void>((resolve) => {
        storeGate = resolve;
      });
      const storeSpy = vi.spyOn(boundStorage, 'store').mockImplementation(
        (events) => {
          const store = originalStore(events);
          return store.then(async (result) => {
            await waitForStore;
            return result;
          });
        }
      );

      const normalDrain = boundQueue.flush();
      await vi.waitFor(() => expect(storeSpy).toHaveBeenCalledTimes(1));
      const lifecycleDrain = boundQueue.flushOnUnload();

      expect(storeSpy).toHaveBeenCalledTimes(1);
      storeGate();
      const [normalReport, lifecycleReport] = await Promise.all([
        normalDrain,
        lifecycleDrain,
      ]);

      expect(normalReport.delivered).toBe(0);
      expect(storeSpy).toHaveBeenCalledTimes(1);
      expect(lifecycleReport.persisted).toBe(1);
      expect(lifecycleReport.dropped).toEqual([
        {
          category: 'evicted',
          reason: 'offline_count_limit',
          event: expect.objectContaining({
            messageId: 'pending-store-backlog-0',
          }),
        },
      ]);
      expect(lifecycleReport.pending).toEqual([]);
      expect(await boundStorage.count()).toBe(
        MAX_OFFLINE_EVENTS_PER_PROJECT
      );
      const retained = await boundStorage.claim(
        MAX_OFFLINE_EVENTS_PER_PROJECT
      );
      expect(
        retained.filter(
          ({ event }) => event.messageId === volatile.messageId
        )
      ).toHaveLength(1);
      expect(
        retained.some(
          ({ event }) => event.messageId === oldestClaim.event.messageId
        )
      ).toBe(false);
      await boundStorage.acknowledge(retained.map(({ id }) => id));
      boundQueue.stop();
    });

    it('should not await an unfinished offline restore before keepalive starts', async () => {
      vi.spyOn(storage, 'claim').mockImplementation(
        () => new Promise(() => undefined)
      );
      const keepaliveSpy = vi
        .spyOn(transport, 'sendKeepalive')
        .mockResolvedValue('accepted');
      void queue.start();
      queue.enqueue(createEvent({
        event: 'restore_independent',
        messageId: 'restore-independent',
      }));

      const drain = queue.flushOnUnload();

      expect(keepaliveSpy).toHaveBeenCalledTimes(1);
      await expect(drain).resolves.toMatchObject({
        delivered: 1,
        pending: [],
      });
    });

    it('should try offline storage if the keepalive request fails', async () => {
      const persistentQueue = new EventQueue(
        createConfig({ persistence: 'localStorage' }),
        transport,
        storage,
        scrubber,
        consentManager
      );
      vi.spyOn(transport, 'sendKeepalive').mockResolvedValue('retryable');
      const storeSpy = vi
        .spyOn(storage, 'store')
        .mockImplementation(async (events) => durableStoreResult(events));

      persistentQueue.enqueue(createEvent());
      await persistentQueue.flushOnUnload();

      expect(storeSpy).toHaveBeenCalled();
      persistentQueue.stop();
    });

    it('should preserve a lifecycle payload rejection for one later normal drain', async () => {
      vi.spyOn(storage, 'claim').mockResolvedValue([]);
      await queue.start();
      const keepaliveSpy = vi
        .spyOn(transport, 'sendKeepalive')
        .mockResolvedValue('payload_rejected');
      queue.enqueue(createEvent({ event: 'valid', messageId: 'msg-valid' }));
      queue.enqueue(createEvent({ event: 'invalid', messageId: 'msg-invalid' }));

      const report = await queue.flushOnUnload();

      expect(keepaliveSpy).toHaveBeenCalledTimes(1);
      expect(report.delivered).toBe(0);
      expect(report.permanentRejections).toEqual([]);
      expect(report.pending.map((item) => item.messageId)).toEqual([
        'msg-valid',
        'msg-invalid',
      ]);

      window.dispatchEvent(new Event('pageshow'));
      const normalSpy = vi
        .spyOn(transport, 'send')
        .mockResolvedValue('accepted');
      const normalReport = await queue.flush();
      expect(normalSpy).toHaveBeenCalledTimes(1);
      expect(normalReport.delivered).toBe(2);
      expect(normalReport.pending).toEqual([]);
    });

    it('should flush synchronously when a real pagehide event is dispatched', async () => {
      const keepaliveSpy = vi
        .spyOn(transport, 'sendKeepalive')
        .mockResolvedValue('accepted');
      vi.spyOn(storage, 'claim').mockResolvedValue([]);
      await queue.start();
      queue.enqueue(createEvent({
        event: 'pagehide_event',
        messageId: 'pagehide-event',
      }));

      window.dispatchEvent(new Event('pagehide'));

      expect(keepaliveSpy).toHaveBeenCalledTimes(1);
      await vi.waitFor(() => expect(queue.length).toBe(0));
    });
  });

  describe('start() and stop()', () => {
    it('should start and stop flush interval', async () => {
      const sendSpy = vi
        .spyOn(transport, 'send')
        .mockResolvedValue('accepted');
      vi.spyOn(storage, 'claim').mockResolvedValue([]);

      await queue.start();

      queue.enqueue(createEvent());

      // Advance past flush interval
      await vi.advanceTimersByTimeAsync(config.flushInterval + 100);

      expect(sendSpy).toHaveBeenCalled();

      queue.stop();
    });

    it('should claim offline events without deleting them on start', async () => {
      const offlineEvent = createEvent({ event: 'offline_event' });
      const claimSpy = vi.spyOn(storage, 'claim')
        .mockResolvedValueOnce([{ id: 1, event: offlineEvent }])
        .mockResolvedValue([]);
      const sendSpy = vi.spyOn(transport, 'send').mockResolvedValue('accepted');

      await queue.start();
      await vi.waitFor(() => expect(sendSpy).toHaveBeenCalledTimes(1));

      expect(claimSpy).toHaveBeenCalled();
      const payload = sendSpy.mock.calls[0][1] as {
        events: Array<{ event: string }>;
      };
      expect(payload.events).toEqual([
        expect.objectContaining({ event: 'offline_event' }),
      ]);
      await queue.flush();

      queue.stop();
    });

    it('should remove text from legacy auto-capture events before direct requeue', async () => {
      vi.spyOn(storage, 'claim')
        .mockResolvedValueOnce([
          {
            id: 1,
            event: createEvent({
              event: '$click',
              properties: { text: 'stored-password', tag: 'input' },
            }),
          },
          {
            id: 2,
            event: createEvent({
              event: '$rage_click',
              properties: {
                text: 'stored-password',
                tag: 'input',
                clickCount: 3,
              },
            }),
          },
        ])
        .mockResolvedValue([]);
      const sendSpy = vi.spyOn(transport, 'send').mockResolvedValue('accepted');

      await queue.start();
      await vi.waitFor(() => expect(sendSpy).toHaveBeenCalledTimes(1));

      const payload = sendSpy.mock.calls[0][1] as {
        events: Array<{ properties?: Record<string, unknown> }>;
      };
      expect(payload.events.map((event) => event.properties)).toEqual([
        { tag: 'input' },
        { tag: 'input', clickCount: 3 },
      ]);
      await queue.flush();
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
      let resolveClaim:
        ((events: Array<{ id: number; event: TrackEvent }>) => void) | undefined;
      vi.spyOn(storage, 'claim')
        .mockImplementationOnce(
          () => new Promise((resolve) => {
            resolveClaim = resolve;
          })
        )
        .mockResolvedValue([]);

      const startPromise = queue.start();
      await Promise.resolve();
      consentManager.update({ analytics: false });
      const cleared = queue.revokeAnalyticsConsent();
      consentManager.update({ analytics: true });
      resolveClaim?.([{ id: 1, event: offlineEvent }]);
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
