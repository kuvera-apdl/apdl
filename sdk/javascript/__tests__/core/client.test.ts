import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { APDLClient } from '../../src/core/client';
import { hashBucket } from '../../src/flags/hash';

// Mock fetch globally
const fetchMock = vi.fn().mockResolvedValue({
  ok: true,
  json: () => Promise.resolve({ flags: [] }),
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

  constructor(public url: string) {
    MockEventSource.instances.push(this);
  }

  addEventListener() {}
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

  describe('checkGate()', () => {
    it('should return false and details when gate not found', () => {
      expect(client.checkGate('nonexistent')).toBe(false);
      expect(client.checkGateDetails('nonexistent')).toMatchObject({
        key: 'nonexistent',
        value: false,
        reason: 'not_found',
        source: 'none',
      });
      expect(featureFlagExposures(client)).toHaveLength(0);
    });

    it('should evaluate gates from initial fetch', async () => {
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 1,
          flags: [{
            key: 'new-checkout-flow',
            enabled: true,
            default_value: false,
            salt: 'salt_123',
            rules: [],
            fallthrough: {
              value: true,
              rollout: { percentage: 100, bucket_by: 'user_id' },
            },
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

      expect(flaggedClient.checkGate('new-checkout-flow')).toBe(true);
      expect(flaggedClient.checkGateDetails('new-checkout-flow')).toMatchObject({
        value: true,
        reason: 'fallthrough',
        config_version: 3,
        source: 'initial_fetch',
      });
    });

    it('should not expose removed legacy gate APIs', () => {
      const api = client as unknown as Record<string, unknown>;

      expect(api.flag).toBeUndefined();
      expect(api.flagPayload).toBeUndefined();
      expect(api.experiment).toBeUndefined();
    });

    it('should restore cached gates in standard localStorage mode', () => {
      localStorage.setItem(flagStorageKey('test-key-123'), JSON.stringify({
        schema_version: 1,
        flags: [makeGate('cached-gate')],
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

      expect(cachedClient.checkGate('cached-gate')).toBe(true);
      expect(cachedClient.checkGateDetails('cached-gate').source).toBe('local_storage');
    });

    it('should preserve restored gates when initial fetch returns malformed config', async () => {
      localStorage.setItem(flagStorageKey('test-key-123'), JSON.stringify({
        schema_version: 1,
        flags: [makeGate('cached-gate')],
      }));
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 1,
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

      expect(cachedClient.checkGate('cached-gate')).toBe(true);
      expect(cachedClient.checkGateDetails('cached-gate').source).toBe('local_storage');
      expect(cachedClient.checkGate('legacy')).toBe(false);
    });

    it('should not restore cached gates in strict privacy mode', () => {
      localStorage.setItem(flagStorageKey('test-key-123'), JSON.stringify({
        schema_version: 1,
        flags: [makeGate('cached-gate')],
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

      expect(strictClient.checkGate('cached-gate')).toBe(false);
      expect(strictClient.checkGateDetails('cached-gate').reason).toBe('not_found');
    });

    it('should log one deduplicated exposure for repeated gate checks', async () => {
      window.history.pushState({}, '', '/checkout');
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 1,
          flags: [makeGate('new-checkout-flow')],
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

      expect(flaggedClient.checkGate('new-checkout-flow')).toBe(true);
      expect(flaggedClient.checkGateDetails('new-checkout-flow').value).toBe(true);
      expect(flaggedClient.checkGate('new-checkout-flow')).toBe(true);

      const exposures = featureFlagExposures(flaggedClient);
      expect(exposures).toHaveLength(1);
      expect(exposures[0].properties).toMatchObject({
        flag_key: 'new-checkout-flow',
        value: true,
        reason: 'fallthrough',
        rule_id: '',
        rollout_percentage: 100,
        bucket_by: 'user_id',
        config_version: 1,
        source: 'initial_fetch',
        page: '/checkout',
      });
      expect(exposures[0].properties?.bucket).toBeTypeOf('number');
    });

    it('should log a distinct exposure when the page changes', async () => {
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 1,
          flags: [makeGate('pricing-gate')],
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
      flaggedClient.checkGate('pricing-gate');
      window.history.pushState({}, '', '/pricing');
      flaggedClient.checkGate('pricing-gate');

      const exposurePages = featureFlagExposures(flaggedClient)
        .map((event) => event.properties?.page);
      expect(exposurePages).toEqual(['/checkout', '/pricing']);
    });

    it('should not mark exposures as deduped while analytics consent is denied', async () => {
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 1,
          flags: [makeGate('consent-gate')],
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
      flaggedClient.checkGate('consent-gate');
      expect(featureFlagExposures(flaggedClient)).toHaveLength(0);

      flaggedClient.consent.update({ analytics: true });
      flaggedClient.checkGate('consent-gate');
      expect(featureFlagExposures(flaggedClient)).toHaveLength(1);
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
  return `apdl_flags_${hashBucket('sdk_flag_cache', 'v1', apiKey).toString(16)}`;
}

function featureFlagExposures(
  client: APDLClient
): Array<{ properties?: Record<string, unknown> }> {
  return client.debug.getQueue()
    .map((event) => event as { event?: string; properties?: Record<string, unknown> })
    .filter((event) => event.event === '$feature_flag_exposure');
}

function makeGate(key: string): Record<string, unknown> {
  return {
    key,
    enabled: true,
    default_value: false,
    salt: 'salt_123',
    rules: [],
    fallthrough: {
      value: true,
      rollout: { percentage: 100, bucket_by: 'user_id' },
    },
    version: 1,
  };
}
