import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { APDLClient } from '../../src/core/client';
import { hashBucket } from '../../src/flags/hash';

// Mock fetch globally
const fetchMock = vi.fn().mockResolvedValue({
  ok: true,
  json: () => Promise.resolve({
    schema_version: 2,
    project_id: 'apdl',
    flags: [],
  }),
  status: 200,
  headers: new Headers(),
});

vi.stubGlobal('fetch', fetchMock);

// Mock EventSource
class MockEventSource {
  static instances: MockEventSource[] = [];
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  readyState = 0;
  private listeners: Map<string, Set<(ev: MessageEvent) => void>> = new Map();

  constructor(public url: string) {
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: EventListener) {
    if (!this.listeners.has(type)) {
      this.listeners.set(type, new Set());
    }
    this.listeners.get(type)!.add(listener as (ev: MessageEvent) => void);
  }

  emit(type: string, data: string) {
    const event = new MessageEvent(type, { data });
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
    }
  }

  close() {
    this.readyState = 2;
  }
}

vi.stubGlobal('EventSource', MockEventSource);

describe('APDLClient', () => {
  let client: APDLClient;

  beforeEach(() => {
    vi.useFakeTimers();
    fetchMock.mockClear();
    MockEventSource.instances = [];
    localStorage.clear();

    client = new APDLClient({
      apiKey: 'test-key-123',
      host: 'https://ingest.test.dev',
      configHost: 'https://config.test.dev',
      autoCapture: false,
      persistence: 'memory',
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  describe('initialization', () => {
    it('should create a client with valid config', () => {
      expect(client).toBeInstanceOf(APDLClient);
    });

    it('should throw on missing apiKey', () => {
      expect(() => new APDLClient({ apiKey: '' })).toThrow('apiKey is required');
    });

    it('should expose public namespaces', () => {
      expect(client.ui).toBeDefined();
      expect(client.ui.register).toBeTypeOf('function');
      expect(client.ui.render).toBeTypeOf('function');
      expect(client.ui.onSlotUpdate).toBeTypeOf('function');

      expect(client.consent).toBeDefined();
      expect(client.consent.get).toBeTypeOf('function');
      expect(client.consent.update).toBeTypeOf('function');
      expect(client.consent.onUpdate).toBeTypeOf('function');

      expect(client.privacy).toBeDefined();
      expect(client.privacy.addScrubber).toBeTypeOf('function');
      expect(client.privacy.removeScrubber).toBeTypeOf('function');

      expect(client.debug).toBeDefined();
      expect(client.debug.enable).toBeTypeOf('function');
      expect(client.debug.disable).toBeTypeOf('function');
      expect(client.debug.getQueue).toBeTypeOf('function');
      expect(client.debug.flush).toBeTypeOf('function');
    });

    it('should keep the SSE connection open while typed heartbeats arrive', () => {
      const source = MockEventSource.instances.at(-1);
      expect(source).toBeDefined();

      source?.onopen?.(new Event('open'));
      vi.advanceTimersByTime(30000);
      source?.emit('heartbeat', '{}');
      vi.advanceTimersByTime(30000);
      source?.emit('heartbeat', '{}');
      vi.advanceTimersByTime(30000);

      expect(MockEventSource.instances).toHaveLength(1);
    });
  });

  describe('track()', () => {
    it('should enqueue a track event', () => {
      client.track('button_clicked', { buttonId: 'signup' });

      const queue = client.debug.getQueue();
      expect(queue.length).toBe(1);
      expect(queue[0]).toMatchObject({
        type: 'track',
        event: 'button_clicked',
        properties: { buttonId: 'signup' },
      });
    });

    it('should include a timestamp and messageId', () => {
      client.track('test_event');

      const queue = client.debug.getQueue();
      expect(queue[0]).toHaveProperty('timestamp');
      expect(queue[0]).toHaveProperty('messageId');
      expect(typeof (queue[0] as Record<string, unknown>).timestamp).toBe('string');
      expect(typeof (queue[0] as Record<string, unknown>).messageId).toBe('string');
    });

    it('should include a sessionId', () => {
      client.track('test_event');

      const queue = client.debug.getQueue();
      expect(queue[0]).toHaveProperty('sessionId');
      expect(typeof (queue[0] as Record<string, unknown>).sessionId).toBe('string');
    });
  });

  describe('identify()', () => {
    it('should enqueue an identify event', () => {
      client.identify('user-42', { plan: 'pro', name: 'Alice' });

      const queue = client.debug.getQueue();
      expect(queue.length).toBe(1);
      expect(queue[0]).toMatchObject({
        type: 'identify',
        userId: 'user-42',
        traits: { plan: 'pro', name: 'Alice' },
      });
    });

    it('should set userId on subsequent events', () => {
      client.identify('user-42');
      client.track('page_loaded');

      const queue = client.debug.getQueue();
      expect(queue.length).toBe(2);
      expect((queue[1] as Record<string, unknown>).userId).toBe('user-42');
    });
  });

  describe('group()', () => {
    it('should enqueue a group event', () => {
      client.group('company-99', { industry: 'tech' });

      const queue = client.debug.getQueue();
      expect(queue.length).toBe(1);
      expect(queue[0]).toMatchObject({
        type: 'group',
        groupId: 'company-99',
        traits: { industry: 'tech' },
      });
    });
  });

  describe('page()', () => {
    it('should enqueue a page event', () => {
      client.page('Home');

      const queue = client.debug.getQueue();
      expect(queue.length).toBe(1);
      expect(queue[0]).toMatchObject({
        type: 'page',
        event: 'Home',
      });
    });

    it('should include page URL context', () => {
      client.page();

      const queue = client.debug.getQueue();
      const event = queue[0] as Record<string, unknown>;
      const props = event.properties as Record<string, unknown>;
      expect(props).toHaveProperty('url');
      expect(props).toHaveProperty('title');
    });
  });

  describe('reset()', () => {
    it('should clear userId after reset', () => {
      client.identify('user-42');
      client.reset();
      client.track('after_reset');

      const queue = client.debug.getQueue();
      // The identify and after_reset events (queue was flushed/cleared partially)
      const afterReset = queue.find(
        (e: unknown) => (e as Record<string, unknown>).event === 'after_reset'
      ) as Record<string, unknown> | undefined;
      expect(afterReset?.userId).toBeUndefined();
    });
  });

  describe('getVariant()', () => {
    it('should return null and details when flag not found', () => {
      const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});

      expect(client.getVariant('nonexistent')).toBeNull();
      expect(client.getVariantDetails('nonexistent')).toMatchObject({
        key: 'nonexistent',
        variant: null,
        reason: 'not_found',
        source: null,
      });
      expect(featureFlagExposures(client)).toHaveLength(0);
      expect(warn).toHaveBeenCalledTimes(1);
      expect(warn).toHaveBeenCalledWith(
        "APDL: Feature flag 'nonexistent' is missing or archived; returning null variant."
      );

      warn.mockRestore();
    });

    it('should evaluate flags from initial fetch', async () => {
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 2,
          project_id: 'apdl',
          flags: [{
            ...makeFlag('new-checkout-flow'),
            version: 3,
          }],
        }),
        status: 200,
        headers: new Headers(),
      });

      const flaggedClient = new APDLClient({
        apiKey: 'test-key-123',
        host: 'https://ingest.test.dev',
        configHost: 'https://config.test.dev',
        autoCapture: false,
        persistence: 'memory',
      });

      await flushAsync();
      flaggedClient.identify('user_123');

      expect(flaggedClient.getVariant('new-checkout-flow')).toBe('treatment');
      expect(flaggedClient.getVariantDetails('new-checkout-flow')).toMatchObject({
        variant: 'treatment',
        reason: 'fallthrough',
        config_version: 3,
        source: 'initial_fetch',
      });
    });

    it('should not expose removed legacy flag APIs', () => {
      const api = client as unknown as Record<string, unknown>;

      expect(api.checkGate).toBeUndefined();
      expect(api.checkGateDetails).toBeUndefined();
      expect(api.onFlagChange).toBeUndefined();
      expect(api.flag).toBeUndefined();
      expect(api.flagPayload).toBeUndefined();
      expect(api.experiment).toBeUndefined();
    });

    it('should notify variant listeners with variant strings after flag updates', async () => {
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 2,
          project_id: 'apdl',
          flags: [],
        }),
        status: 200,
        headers: new Headers(),
      });

      const variantClient = new APDLClient({
        apiKey: 'test-key-123',
        host: 'https://ingest.test.dev',
        configHost: 'https://config.test.dev',
        autoCapture: false,
        persistence: 'memory',
      });
      await flushAsync();
      variantClient.identify('user_123');

      const callback = vi.fn();
      const unsubscribe = variantClient.onVariantChange('listener-flag', callback);

      const source = MockEventSource.instances.at(-1);
      source?.onmessage?.(new MessageEvent('message', {
        data: JSON.stringify({
          type: 'flag_update',
          action: 'flag_created',
          flag: makeFlag('listener-flag'),
        }),
      }));

      expect(callback).toHaveBeenCalledTimes(1);
      expect(callback).toHaveBeenCalledWith('treatment');

      unsubscribe();
      source?.onmessage?.(new MessageEvent('message', {
        data: JSON.stringify({
          type: 'flag_update',
          action: 'flag_updated',
          flag: {
            ...makeFlag('listener-flag'),
            enabled: false,
            version: 2,
          },
        }),
      }));

      expect(callback).toHaveBeenCalledTimes(1);
      await variantClient.shutdown();
    });

    it('should restore cached flags in standard localStorage mode', () => {
      localStorage.setItem(flagStorageKey('test-key-123'), JSON.stringify({
        schema_version: 2,
        project_id: 'apdl',
        flags: [makeFlag('cached-flag')],
      }));
      fetchMock.mockRejectedValueOnce(new Error('offline'));

      const cachedClient = new APDLClient({
        apiKey: 'test-key-123',
        host: 'https://ingest.test.dev',
        configHost: 'https://config.test.dev',
        autoCapture: false,
        persistence: 'localStorage',
        privacyMode: 'standard',
      });
      cachedClient.identify('user_123');

      expect(cachedClient.getVariant('cached-flag')).toBe('treatment');
      expect(cachedClient.getVariantDetails('cached-flag').source).toBe('local_storage');
    });

    it('should preserve restored flags when initial fetch returns malformed config', async () => {
      localStorage.setItem(flagStorageKey('test-key-123'), JSON.stringify({
        schema_version: 2,
        project_id: 'apdl',
        flags: [makeFlag('cached-flag')],
      }));
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 2,
          project_id: 'apdl',
          flags: [{
            key: 'legacy',
            enabled: true,
            variant_type: 'boolean',
            default_value: 'false',
            rollout_percentage: 100,
            rules: [],
            variants: [],
          }],
        }),
        status: 200,
        headers: new Headers(),
      });

      const cachedClient = new APDLClient({
        apiKey: 'test-key-123',
        host: 'https://ingest.test.dev',
        configHost: 'https://config.test.dev',
        autoCapture: false,
        persistence: 'localStorage',
        privacyMode: 'standard',
      });
      await flushAsync();
      cachedClient.identify('user_123');

      const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
      expect(cachedClient.getVariant('cached-flag')).toBe('treatment');
      expect(cachedClient.getVariantDetails('cached-flag').source).toBe('local_storage');
      expect(cachedClient.getVariant('legacy')).toBeNull();
      expect(cachedClient.getVariantDetails('legacy')).toMatchObject({
        reason: 'invalid_config',
        source: null,
      });
      expect(warn).not.toHaveBeenCalled();
      warn.mockRestore();
    });

    it('should not restore cached flags in strict privacy mode', () => {
      localStorage.setItem(flagStorageKey('test-key-123'), JSON.stringify({
        schema_version: 2,
        project_id: 'apdl',
        flags: [makeFlag('cached-flag')],
      }));
      fetchMock.mockRejectedValueOnce(new Error('offline'));

      const strictClient = new APDLClient({
        apiKey: 'test-key-123',
        host: 'https://ingest.test.dev',
        configHost: 'https://config.test.dev',
        autoCapture: false,
        persistence: 'localStorage',
        privacyMode: 'strict',
      });
      strictClient.identify('user_123');

      const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
      expect(strictClient.getVariant('cached-flag')).toBeNull();
      expect(strictClient.getVariantDetails('cached-flag').reason).toBe('not_found');
      expect(warn).toHaveBeenCalledTimes(1);
      warn.mockRestore();
    });

    it('should log one deduplicated exposure for repeated variant checks', async () => {
      window.history.pushState({}, '', '/checkout');
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 2,
          project_id: 'apdl',
          flags: [makeFlag('new-checkout-flow')],
        }),
        status: 200,
        headers: new Headers(),
      });

      const flaggedClient = new APDLClient({
        apiKey: 'test-key-123',
        host: 'https://ingest.test.dev',
        configHost: 'https://config.test.dev',
        autoCapture: false,
        persistence: 'memory',
      });

      await flushAsync();
      flaggedClient.identify('user_123');

      const flagOptions = { component: 'CheckoutPage' };
      expect(flaggedClient.getVariant('new-checkout-flow', flagOptions)).toBe('treatment');
      expect(flaggedClient.getVariantDetails('new-checkout-flow', flagOptions).variant).toBe('treatment');
      expect(flaggedClient.getVariant('new-checkout-flow', flagOptions)).toBe('treatment');

      const exposures = featureFlagExposures(flaggedClient);
      expect(exposures).toHaveLength(1);
      expect(exposures[0].properties).toMatchObject({
        flag_key: 'new-checkout-flow',
        variant: 'treatment',
        reason: 'fallthrough',
        rule_id: null,
        rollout_percentage: 100,
        bucket_by: 'user_id',
        config_version: 1,
        source: 'initial_fetch',
        page: '/checkout',
        component: 'CheckoutPage',
      });
      expect(exposures[0].properties?.rollout_bucket).toBeTypeOf('number');
      expect(exposures[0].properties?.variant_bucket).toBeTypeOf('number');
    });

    it('should log distinct exposures for different components', async () => {
      window.history.pushState({}, '', '/checkout');
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 2,
          project_id: 'apdl',
          flags: [makeFlag('component-flag')],
        }),
        status: 200,
        headers: new Headers(),
      });

      const flaggedClient = new APDLClient({
        apiKey: 'test-key-123',
        host: 'https://ingest.test.dev',
        configHost: 'https://config.test.dev',
        autoCapture: false,
        persistence: 'memory',
      });

      await flushAsync();
      flaggedClient.identify('user_123');

      flaggedClient.getVariant('component-flag', { component: 'HeaderCTA' });
      flaggedClient.getVariant('component-flag', { component: 'FooterCTA' });

      const exposureComponents = featureFlagExposures(flaggedClient)
        .map((event) => event.properties?.component);
      expect(exposureComponents).toEqual(['HeaderCTA', 'FooterCTA']);
    });

    it('should log a distinct exposure when the page changes', async () => {
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 2,
          project_id: 'apdl',
          flags: [makeFlag('pricing-flag')],
        }),
        status: 200,
        headers: new Headers(),
      });

      const flaggedClient = new APDLClient({
        apiKey: 'test-key-123',
        host: 'https://ingest.test.dev',
        configHost: 'https://config.test.dev',
        autoCapture: false,
        persistence: 'memory',
      });

      await flushAsync();
      flaggedClient.identify('user_123');

      window.history.pushState({}, '', '/checkout');
      flaggedClient.getVariant('pricing-flag');
      window.history.pushState({}, '', '/pricing');
      flaggedClient.getVariant('pricing-flag');

      const exposurePages = featureFlagExposures(flaggedClient)
        .map((event) => event.properties?.page);
      expect(exposurePages).toEqual(['/checkout', '/pricing']);
    });

    it('should not mark exposures as deduped while analytics consent is denied', async () => {
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 2,
          project_id: 'apdl',
          flags: [makeFlag('consent-flag')],
        }),
        status: 200,
        headers: new Headers(),
      });

      const flaggedClient = new APDLClient({
        apiKey: 'test-key-123',
        host: 'https://ingest.test.dev',
        configHost: 'https://config.test.dev',
        autoCapture: false,
        persistence: 'memory',
        consent: { analytics: false, personalization: true, experiments: true },
      });

      await flushAsync();
      flaggedClient.identify('user_123');
      flaggedClient.getVariant('consent-flag');
      expect(featureFlagExposures(flaggedClient)).toHaveLength(0);

      flaggedClient.consent.update({ analytics: true });
      flaggedClient.getVariant('consent-flag');
      expect(featureFlagExposures(flaggedClient)).toHaveLength(1);
    });
  });

  describe('frontend health capture', () => {
    it('should include active flag state in frontend error events', async () => {
      window.history.pushState({}, '', '/checkout');
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 2,
          project_id: 'apdl',
          flags: [makeFlag('checkout-flag')],
        }),
        status: 200,
        headers: new Headers(),
      });

      const healthClient = new APDLClient({
        apiKey: 'test-key-123',
        host: 'https://ingest.test.dev',
        configHost: 'https://config.test.dev',
        autoCapture: {
          pageViews: false,
          clicks: false,
          formSubmissions: false,
          inputChanges: false,
          scrollDepth: false,
          rage_clicks: false,
          frontend_errors: true,
          web_vitals: false,
        },
        persistence: 'memory',
      });

      await flushAsync();
      healthClient.identify('user_123');
      healthClient.getVariant('checkout-flag');

      window.dispatchEvent(new ErrorEvent('error', {
        message: 'Checkout exploded',
        filename: 'checkout.js',
        lineno: 12,
        colno: 4,
        error: new Error('Checkout exploded'),
      }));

      const errors = frontendErrors(healthClient);
      expect(errors).toHaveLength(1);
      expect(errors[0].properties).toMatchObject({
        error_type: 'javascript_error',
        message: 'Checkout exploded',
        page: '/checkout',
        component: '',
        slot_id: '',
        source: 'checkout.js',
        line: 12,
        column: 4,
        active_flags: { 'checkout-flag': 'treatment' },
        active_flag_versions: { 'checkout-flag': 1 },
      });

      await healthClient.shutdown();
    });

    it('should not include flags evaluated on a different page', async () => {
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 2,
          project_id: 'apdl',
          flags: [makeFlag('checkout-flag')],
        }),
        status: 200,
        headers: new Headers(),
      });

      const healthClient = new APDLClient({
        apiKey: 'test-key-123',
        host: 'https://ingest.test.dev',
        configHost: 'https://config.test.dev',
        autoCapture: {
          pageViews: false,
          clicks: false,
          formSubmissions: false,
          inputChanges: false,
          scrollDepth: false,
          rage_clicks: false,
          frontend_errors: true,
          web_vitals: false,
        },
        persistence: 'memory',
      });

      await flushAsync();
      healthClient.identify('user_123');
      window.history.pushState({}, '', '/checkout');
      healthClient.getVariant('checkout-flag');
      window.history.pushState({}, '', '/pricing');

      window.dispatchEvent(new ErrorEvent('error', {
        message: 'Pricing exploded',
        error: new Error('Pricing exploded'),
      }));

      const errors = frontendErrors(healthClient);
      expect(errors).toHaveLength(1);
      expect(errors[0].properties).toMatchObject({
        page: '/pricing',
        active_flags: {},
        active_flag_versions: {},
      });

      await healthClient.shutdown();
    });

    it('should refresh page-scoped active flag states after config updates', async () => {
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 2,
          project_id: 'apdl',
          flags: [makeFlag('checkout-flag')],
        }),
        status: 200,
        headers: new Headers(),
      });

      const healthClient = new APDLClient({
        apiKey: 'test-key-123',
        host: 'https://ingest.test.dev',
        configHost: 'https://config.test.dev',
        autoCapture: {
          pageViews: false,
          clicks: false,
          formSubmissions: false,
          inputChanges: false,
          scrollDepth: false,
          rage_clicks: false,
          frontend_errors: true,
          web_vitals: false,
        },
        persistence: 'memory',
      });

      await flushAsync();
      healthClient.identify('user_123');
      window.history.pushState({}, '', '/checkout');
      healthClient.getVariant('checkout-flag');

      const source = MockEventSource.instances.at(-1);
      source?.onmessage?.(new MessageEvent('message', {
        data: JSON.stringify({
          type: 'flag_update',
          action: 'flag_updated',
          flag: {
            ...makeFlag('checkout-flag'),
            enabled: false,
            version: 2,
          },
        }),
      }));

      window.dispatchEvent(new ErrorEvent('error', {
        message: 'Checkout exploded',
        error: new Error('Checkout exploded'),
      }));

      const errors = frontendErrors(healthClient);
      expect(errors).toHaveLength(1);
      expect(errors[0].properties).toMatchObject({
        active_flags: { 'checkout-flag': 'control' },
        active_flag_versions: { 'checkout-flag': 2 },
      });

      await healthClient.shutdown();
    });

    it('should capture component render failures as frontend errors', async () => {
      const healthClient = new APDLClient({
        apiKey: 'test-key-123',
        host: 'https://ingest.test.dev',
        configHost: 'https://config.test.dev',
        autoCapture: {
          pageViews: false,
          clicks: false,
          formSubmissions: false,
          inputChanges: false,
          scrollDepth: false,
          rage_clicks: false,
          frontend_errors: true,
          web_vitals: false,
        },
        persistence: 'memory',
      });
      healthClient.ui.register({
        name: 'broken-card',
        schema: { type: 'object', properties: {} },
        render: () => {
          throw new Error('Render failed');
        },
      });

      const target = document.createElement('div');
      target.setAttribute('data-apdl-slot', 'checkout-slot');
      expect(healthClient.ui.render({
        component: 'broken-card',
        props: {},
      }, target)).toBeNull();

      const errors = frontendErrors(healthClient);
      expect(errors).toHaveLength(1);
      expect(errors[0].properties).toMatchObject({
        error_type: 'component_render_error',
        message: 'Render failed',
        component: 'broken-card',
        slot_id: 'checkout-slot',
      });

      await healthClient.shutdown();
    });
  });

  describe('consent', () => {
    it('should return default consent state', () => {
      const state = client.consent.get();
      expect(state.analytics).toBe(true);
      expect(state.personalization).toBe(true);
      expect(state.experiments).toBe(true);
    });

    it('should update consent state', () => {
      client.consent.update({ analytics: false });

      const state = client.consent.get();
      expect(state.analytics).toBe(false);
      expect(state.personalization).toBe(true);
    });

    it('should notify on consent change', () => {
      const callback = vi.fn();
      client.consent.onUpdate(callback);

      client.consent.update({ analytics: false });
      expect(callback).toHaveBeenCalledWith(
        expect.objectContaining({ analytics: false })
      );
    });

    it('should drop events when analytics consent is denied', () => {
      client.consent.update({ analytics: false });
      client.track('should_be_dropped');

      const queue = client.debug.getQueue();
      expect(queue.length).toBe(0);
    });
  });

  describe('debug namespace', () => {
    it('should return current queue', () => {
      client.track('e1');
      client.track('e2');

      const queue = client.debug.getQueue();
      expect(queue.length).toBe(2);
    });

    it('should flush the queue', async () => {
      client.track('flush_test');
      await client.debug.flush();

      // After flush, queue should be empty (events sent or stored)
      const queue = client.debug.getQueue();
      expect(queue.length).toBe(0);
    });
  });

  describe('shutdown()', () => {
    it('should gracefully shut down', async () => {
      client.track('before_shutdown');
      await client.shutdown();
      // No errors thrown
    });
  });
});

async function flushAsync(): Promise<void> {
  for (let index = 0; index < 5; index++) {
    await Promise.resolve();
  }
}

function flagStorageKey(apiKey: string): string {
  return `apdl_flags_${hashBucket('sdk_flag_cache', 'v2', apiKey).toString(16)}`;
}

function featureFlagExposures(
  client: APDLClient
): Array<{ properties?: Record<string, unknown> }> {
  return client.debug.getQueue()
    .map((event) => event as { event?: string; properties?: Record<string, unknown> })
    .filter((event) => event.event === '$feature_flag_exposure');
}

function frontendErrors(
  client: APDLClient
): Array<{ properties?: Record<string, unknown> }> {
  return client.debug.getQueue()
    .map((event) => event as { event?: string; properties?: Record<string, unknown> })
    .filter((event) => event.event === '$frontend_error');
}

function makeFlag(key: string): Record<string, unknown> {
  return {
    key,
    enabled: true,
    default_variant: 'control',
    variants: [
      { key: 'control', weight: 0 },
      { key: 'treatment', weight: 1 },
    ],
    salt: 'salt_123',
    rules: [],
    fallthrough: {
      rollout: { percentage: 100, bucket_by: 'user_id' },
    },
    version: 1,
  };
}
