import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { APDLClient } from '../../src/core/client';
import { resolveConfig, type APDLConfig } from '../../src/core/config';
import { SDK_IDENTIFIER } from '../../src/core/constants';
import type { TrackEvent } from '../../src/core/types';
import {
  CLIENT_KEY,
  ENDPOINT,
  MockEventSource,
  createTestConfig,
  mockApiFetch,
} from '../helpers';

// Mock fetch globally
const fetchMock = vi.fn(mockApiFetch);

vi.stubGlobal('fetch', fetchMock);

describe('APDLClient', () => {
  let client: APDLClient;

  beforeEach(() => {
    vi.useFakeTimers();
    fetchMock.mockClear();
    MockEventSource.instances = [];
    localStorage.clear();

    client = new APDLClient(createTestConfig());
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  describe('initialization', () => {
    it('should create a client with valid config', () => {
      expect(client).toBeInstanceOf(APDLClient);
    });

    it('should resolve canonical config and derive project ID from client key', () => {
      const resolved = resolveConfig(createTestConfig());

      expect(resolved).toMatchObject({
        projectId: 'apdl',
        endpoint: ENDPOINT,
        auth: {
          clientKey: CLIENT_KEY,
        },
      });
    });

    it('should default collection, personalization, experiments, and auto-capture off', () => {
      const resolved = resolveConfig({
        endpoint: ENDPOINT,
        auth: { clientKey: CLIENT_KEY },
      });

      expect(resolved.consent).toEqual({
        analytics: false,
        personalization: false,
        experiments: false,
      });
      expect(Object.values(resolved.autoCapture)).toEqual([
        false,
        false,
        false,
        false,
        false,
        false,
        false,
        false,
      ]);
    });

    it('should fetch initial flags from the config endpoint with client key auth', () => {
      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];

      expect(url).toBe(`${ENDPOINT}/v1/flags`);
      expect(init.headers).toMatchObject({
        'X-API-Key': CLIENT_KEY,
        'X-APDL-SDK': SDK_IDENTIFIER,
      });
    });

    it('should reject an initial flag envelope for another project', async () => {
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 2,
          project_id: 'foreign',
          flags: [makeFlag('foreign-flag')],
        }),
      });
      const foreignClient = new APDLClient(createTestConfig());

      await flushAsync();

      expect(foreignClient.getVariant('foreign-flag')).toBeNull();
      await foreignClient.shutdown();
    });

    it('should open the SSE stream with header auth and no credential in the URL', async () => {
      await flushAsync();
      const source = MockEventSource.instances.at(-1);
      expect(source).toBeDefined();

      const url = new URL(source!.url);
      expect(`${url.origin}${url.pathname}`).toBe(`${ENDPOINT}/v1/stream`);
      expect(url.search).toBe('');
      expect(source!.init.headers).toMatchObject({
        Accept: 'text/event-stream',
        'X-API-Key': CLIENT_KEY,
        'X-APDL-SDK': SDK_IDENTIFIER,
      });
    });

    it('should not queue sensitive DOM data with default auto-capture', async () => {
      const defaultConfig = createTestConfig();
      delete defaultConfig.autoCapture;
      const defaultClient = new APDLClient(defaultConfig);
      const input = document.createElement('input');
      input.type = 'password';
      input.value = 'default-client-password';
      document.body.append(input);

      try {
        for (let index = 0; index < 3; index += 1) {
          input.dispatchEvent(new MouseEvent('click', {
            bubbles: true,
            composed: true,
          }));
        }

        const queued = defaultClient.debug.getQueue() as TrackEvent[];
        const serialized = JSON.stringify(queued);
        expect(serialized).not.toContain('default-client-password');
        expect(queued.map((event) => event.event)).not.toContain('$click');
        expect(queued.map((event) => event.event)).not.toContain('$rage_click');
      } finally {
        input.remove();
        await defaultClient.shutdown();
      }
    });

    it('should throw on missing endpoint', () => {
      expect(() => resolveConfig(createTestConfig({
        endpoint: '',
      }))).toThrow('endpoint is required');
    });

    it('should throw on missing client key', () => {
      expect(() => new APDLClient(createTestConfig({
        auth: { clientKey: '' },
      }))).toThrow('auth.clientKey is required');
    });

    it('should throw when client key does not match the APDL format', () => {
      expect(() => resolveConfig(createTestConfig({
        auth: { clientKey: 'bad_key' },
      }))).toThrow('client_{project_id}_{token}');
    });

    it.each([
      'api.example.com',
      'ftp://api.example.com',
      'https://user:secret@api.example.com',
      'https://api.example.com/v1',
      'https://api.example.com?tenant=secret',
      'https://api.example.com/#fragment',
    ])('should reject endpoint values that are not an HTTP(S) origin: %s', (endpoint) => {
      expect(() => resolveConfig(createTestConfig({ endpoint })))
        .toThrow('endpoint must be an absolute HTTP(S) origin');
    });

    it.each([
      ['batchSize', 0, 'between 1 and 100'],
      ['batchSize', 101, 'between 1 and 100'],
      ['batchSize', 1.5, 'between 1 and 100'],
      ['batchSize', Number.NaN, 'between 1 and 100'],
      ['flushInterval', 99, 'between 100 and 3600000'],
      ['flushInterval', 3_600_001, 'between 100 and 3600000'],
      ['maxQueueSize', 0, 'between 1 and 100000'],
      ['maxQueueSize', Number.POSITIVE_INFINITY, 'between 1 and 100000'],
    ])('should reject invalid numeric config %s=%s', (field, value, message) => {
      const config = { ...createTestConfig(), [field]: value };
      expect(() => resolveConfig(config as unknown as APDLConfig)).toThrow(message);
    });

    it.each([
      ['privacyMode', 'strict', 'privacyMode must be one of: standard, cookieless'],
      ['persistence', 'cookie', 'persistence must be one of: localStorage, memory'],
      ['debug', 'true', 'debug is required and must be a boolean'],
    ])('should reject unsupported config %s=%s', (field, value, message) => {
      const config = { ...createTestConfig(), [field]: value };
      expect(() => resolveConfig(config as unknown as APDLConfig)).toThrow(message);
    });

    it.each([
      ['apiKey', CLIENT_KEY],
      ['host', ENDPOINT],
      ['configHost', ENDPOINT],
      ['projectId', 'apdl'],
    ])('should reject removed top-level config field %s', (field, value) => {
      const config = {
        ...createTestConfig(),
        [field]: value,
      };

      expect(() => resolveConfig(config as unknown as APDLConfig))
        .toThrow(`config.${field} is no longer supported`);
    });

    it('should reject unsupported top-level config fields', () => {
      const config = {
        ...createTestConfig(),
        apiBaseUrl: ENDPOINT,
      };

      expect(() => resolveConfig(config as unknown as APDLConfig))
        .toThrow('config.apiBaseUrl is not supported');
    });

    it('should reject the removed endpoints object', () => {
      const config = {
        ...createTestConfig(),
        endpoints: { ingestion: ENDPOINT, config: ENDPOINT },
      };

      expect(() => resolveConfig(config as unknown as APDLConfig))
        .toThrow('config.endpoints is no longer supported');
    });

    it('should reject unsupported auth fields', () => {
      const authConfig = {
        ...createTestConfig(),
        auth: {
          clientKey: CLIENT_KEY,
          projectId: 'apdl',
        },
      };

      expect(() => resolveConfig(authConfig as unknown as APDLConfig))
        .toThrow('auth.projectId is not supported');
    });

    it('should reject unsupported autoCapture and consent fields', () => {
      const autoCaptureConfig = {
        ...createTestConfig(),
        autoCapture: {
          pageViews: true,
          frontendErrors: true,
        },
      };
      const consentConfig = {
        ...createTestConfig(),
        consent: {
          analytics: true,
          personalization: true,
          experiments: true,
          marketing: true,
        },
      };

      expect(() => resolveConfig(autoCaptureConfig as unknown as APDLConfig))
        .toThrow('autoCapture.frontendErrors is not supported');
      expect(() => resolveConfig(consentConfig as unknown as APDLConfig))
        .toThrow('consent.marketing is not supported');
    });

    it('should reject invalid autoCapture and consent values', () => {
      const autoCaptureConfig = {
        ...createTestConfig(),
        autoCapture: {
          pageViews: 'yes',
        },
      };
      const consentConfig = {
        ...createTestConfig(),
        consent: {
          analytics: true,
          personalization: true,
        },
      };

      expect(() => resolveConfig(autoCaptureConfig as unknown as APDLConfig))
        .toThrow('autoCapture.pageViews is required and must be a boolean');
      expect(() => resolveConfig(consentConfig as unknown as APDLConfig))
        .toThrow('consent.experiments is required and must be a boolean');
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

      expect(client.experiments).toBeDefined();
      expect(client.experiments.setContext).toBeTypeOf('function');
      expect(client.experiments.getContext).toBeTypeOf('function');
      expect(client.experiments.clearContext).toBeTypeOf('function');

      expect(client.debug).toBeDefined();
      expect(client.debug.enable).toBeTypeOf('function');
      expect(client.debug.disable).toBeTypeOf('function');
      expect(client.debug.getQueue).toBeTypeOf('function');
      expect(client.debug.flush).toBeTypeOf('function');
    });

    it('should keep the SSE connection open while typed heartbeats arrive', async () => {
      await flushAsync();
      const source = MockEventSource.instances.at(-1);
      expect(source).toBeDefined();

      vi.advanceTimersByTime(30000);
      source?.emit('heartbeat', '{}');
      await flushAsync();
      vi.advanceTimersByTime(30000);
      source?.emit('heartbeat', '{}');
      await flushAsync();
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

    it('should apply built-in PII scrubbers in standard privacy mode', () => {
      client.track('pii_event', {
        email: 'contact alice@example.com',
        card: '4111 1111 1111 1111',
        ssn: '123-45-6789',
      });

      const event = client.debug.getQueue()[0] as TrackEvent;
      expect(event.properties).toEqual({
        email: 'contact [REDACTED]',
        card: '[REDACTED]',
        ssn: '[REDACTED]',
      });
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
      expect((queue[0] as TrackEvent).anonymousId).toBeTruthy();
    });

    it('should retain one anonymous ID across identify and subsequent events', () => {
      client.identify('user-42');
      client.track('page_loaded');

      const queue = client.debug.getQueue();
      expect(queue.length).toBe(2);
      const identifyEvent = queue[0] as TrackEvent;
      const subsequentEvent = queue[1] as TrackEvent;
      expect(subsequentEvent.userId).toBe('user-42');
      expect(subsequentEvent.anonymousId).toBe(identifyEvent.anonymousId);
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
        event: 'page',
        properties: { name: 'Home' },
      });
    });

    it('should include only query-free allowlisted page metadata', () => {
      window.history.pushState({}, '', '/account/reset?token=query-secret#fragment-secret');
      document.title = 'Password reset for Alice';
      client.page();

      const queue = client.debug.getQueue();
      const event = queue[0] as TrackEvent;
      expect(event.properties).toEqual({
        url: `${window.location.origin}/account/reset`,
        path: '/account/reset',
      });
      expect(event.context).toMatchObject({
        page: {
          url: `${window.location.origin}/account/reset`,
          title: '',
          path: '/account/reset',
          search: '',
        },
      });
      expect(event.context).not.toHaveProperty('referrer');
      expect(JSON.stringify(event)).not.toContain('query-secret');
      expect(JSON.stringify(event)).not.toContain('fragment-secret');
      expect(JSON.stringify(event)).not.toContain('Password reset for Alice');
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
      ) as TrackEvent | undefined;
      const identifyEvent = queue.find(
        (e: unknown) => (e as Record<string, unknown>).type === 'identify'
      ) as TrackEvent | undefined;
      expect(afterReset?.userId).toBeUndefined();
      expect(afterReset?.anonymousId).not.toBe(identifyEvent?.anonymousId);
    });
  });

  describe('experiments', () => {
    it('should set, return, and clear canonical experiment context', () => {
      expect(client.experiments.getContext()).toEqual({ attributes: {} });

      client.experiments.setContext({
        attributes: {
          plan: 'pro',
          region: 'us',
        },
      });

      const context = client.experiments.getContext();
      expect(context).toEqual({
        attributes: {
          plan: 'pro',
          region: 'us',
        },
      });

      context.attributes.plan = 'free';
      expect(client.experiments.getContext().attributes.plan).toBe('pro');

      client.experiments.clearContext();
      expect(client.experiments.getContext()).toEqual({ attributes: {} });
    });

    it('should defensively copy nested experiment context attributes', () => {
      const input = {
        profile: {
          plan: 'pro',
          team: { id: 'growth' },
        },
        segments: ['beta'],
      };

      client.experiments.setContext({ attributes: input });
      input.profile.team.id = 'sales';
      input.segments.push('internal');

      const stored = client.experiments.getContext().attributes as {
        profile: { team: { id: string } };
        segments: string[];
      };
      expect(stored.profile.team.id).toBe('growth');
      expect(stored.segments).toEqual(['beta']);

      stored.profile.team.id = 'support';
      stored.segments.push('enterprise');

      const reread = client.experiments.getContext().attributes as {
        profile: { team: { id: string } };
        segments: string[];
      };
      expect(reread.profile.team.id).toBe('growth');
      expect(reread.segments).toEqual(['beta']);
    });

    it('should reject non-canonical experiment context shapes', () => {
      const setContext = client.experiments.setContext as (context: unknown) => void;
      class Attributes {
        plan = 'pro';
      }

      expect(() => setContext({ attributes: {}, scope: 'checkout' }))
        .toThrow('APDL: experiments context.scope is not supported');
      expect(() => setContext({ attributes: [] }))
        .toThrow('APDL: experiments context.attributes is required and must be an object');
      expect(() => setContext({}))
        .toThrow('APDL: experiments context.attributes is required and must be an object');
      expect(() => setContext(new Date()))
        .toThrow('APDL: experiments context is required and must be an object');
      expect(() => setContext({ attributes: new Date() }))
        .toThrow('APDL: experiments context.attributes is required and must be an object');
      expect(() => setContext({ attributes: new Map([['plan', 'pro']]) }))
        .toThrow('APDL: experiments context.attributes is required and must be an object');
      expect(() => setContext({ attributes: new Attributes() }))
        .toThrow('APDL: experiments context.attributes is required and must be an object');
    });

    it('should merge experiment context attributes into flag evaluation attributes', async () => {
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 2,
          project_id: 'apdl',
          flags: [makeFlag('plan-flag', {
            rules: [{
              id: 'rule_plan',
              name: 'Plan targeting',
              conditions: [
                { attribute: 'plan', operator: 'equals', value: 'pro' },
              ],
              rollout: { percentage: 100, bucket_by: 'user_id' },
            }],
            fallthrough: {
              rollout: { percentage: 0, bucket_by: 'user_id' },
            },
          })],
        }),
        status: 200,
        headers: new Headers(),
      });

      const experimentClient = new APDLClient(createTestConfig());

      await flushAsync();
      experimentClient.identify('user_123');
      expect(experimentClient.getVariant('plan-flag')).toBe('control');

      experimentClient.experiments.setContext({
        attributes: { plan: 'pro' },
      });

      expect(experimentClient.getVariant('plan-flag')).toBe('treatment');
      await experimentClient.shutdown();
    });

    it('should not leak targeting context into exposure events', async () => {
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 2,
          project_id: 'apdl',
          flags: [makeFlag('context-exposure-flag')],
        }),
        status: 200,
        headers: new Headers(),
      });

      const experimentClient = new APDLClient(createTestConfig());

      await flushAsync();
      experimentClient.identify('user_123');
      experimentClient.experiments.setContext({
        attributes: {
          plan: 'pro',
          region: 'us',
        },
      });
      experimentClient.getVariant('context-exposure-flag', { component: 'PricingCTA' });

      const exposures = featureFlagExposures(experimentClient);
      expect(exposures).toHaveLength(1);
      expect(exposures[0].properties).toMatchObject({
        flag_key: 'context-exposure-flag',
      });
      expect(exposures[0].properties).not.toHaveProperty('experiment_context');

      await experimentClient.shutdown();
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

      const flaggedClient = new APDLClient(createTestConfig());

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

      const variantClient = new APDLClient(createTestConfig());
      await flushAsync();
      variantClient.identify('user_123');

      const callback = vi.fn();
      const unsubscribe = variantClient.onVariantChange('listener-flag', callback);

      const source = MockEventSource.instances.at(-1);
      source?.onmessage?.(new MessageEvent('message', {
        data: JSON.stringify({
          type: 'flag_update',
          action: 'flag_created',
          version: 1,
          flag: makeFlag('listener-flag'),
        }),
      }));
      await flushAsync();

      expect(callback).toHaveBeenCalledTimes(1);
      expect(callback).toHaveBeenCalledWith('treatment');

      unsubscribe();
      source?.onmessage?.(new MessageEvent('message', {
        data: JSON.stringify({
          type: 'flag_update',
          action: 'flag_updated',
          version: 2,
          flag: {
            ...makeFlag('listener-flag'),
            enabled: false,
            version: 2,
          },
        }),
      }));
      await flushAsync();

      expect(callback).toHaveBeenCalledTimes(1);
      await variantClient.shutdown();
    });

    it('should restore cached flags in standard localStorage mode', () => {
      localStorage.setItem(flagStorageKey('apdl'), JSON.stringify({
        schema_version: 3,
        project_id: 'local_storage',
        flags: [makeFlag('cached-flag')],
        versions: { 'cached-flag': 1 },
      }));
      fetchMock.mockRejectedValueOnce(new Error('offline'));

      const cachedClient = new APDLClient(createTestConfig({
        persistence: 'localStorage',
        privacyMode: 'standard',
      }));
      cachedClient.identify('user_123');

      expect(cachedClient.getVariant('cached-flag')).toBe('treatment');
      expect(cachedClient.getVariantDetails('cached-flag').source).toBe('local_storage');
    });

    it('should scope cached flags by project ID derived from the client key', () => {
      localStorage.setItem(flagStorageKey('apdl'), JSON.stringify({
        schema_version: 3,
        project_id: 'local_storage',
        flags: [makeFlag('apdl-only')],
        versions: { 'apdl-only': 1 },
      }));
      localStorage.setItem(flagStorageKey('projectb'), JSON.stringify({
        schema_version: 3,
        project_id: 'local_storage',
        flags: [makeFlag('projectb-only')],
        versions: { 'projectb-only': 1 },
      }));
      fetchMock.mockRejectedValueOnce(new Error('offline'));

      const projectClient = new APDLClient(createTestConfig({
        auth: { clientKey: 'client_projectb_0123456789abcdef' },
        persistence: 'localStorage',
        privacyMode: 'standard',
      }));
      projectClient.identify('user_123');

      expect(projectClient.getVariant('projectb-only')).toBe('treatment');

      const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
      expect(projectClient.getVariant('apdl-only')).toBeNull();
      expect(warn).toHaveBeenCalledTimes(1);
      warn.mockRestore();
    });

    it('should preserve restored flags when initial fetch returns malformed config', async () => {
      localStorage.setItem(flagStorageKey('apdl'), JSON.stringify({
        schema_version: 3,
        project_id: 'local_storage',
        flags: [makeFlag('cached-flag')],
        versions: { 'cached-flag': 1 },
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

      const cachedClient = new APDLClient(createTestConfig({
        persistence: 'localStorage',
        privacyMode: 'standard',
      }));
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

      const flaggedClient = new APDLClient(createTestConfig());

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

    it('should return assignments and retry exposure telemetry after queue pressure', async () => {
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 2,
          project_id: 'apdl',
          flags: [makeFlag('queue-pressure-flag')],
        }),
        status: 200,
        headers: new Headers(),
      });
      const flaggedClient = new APDLClient(createTestConfig({
        batchSize: 2,
        maxQueueSize: 1,
      }));
      await flushAsync();
      const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});

      flaggedClient.track('fills_queue');
      expect(() => flaggedClient.getVariant('queue-pressure-flag'))
        .not.toThrow();
      expect(flaggedClient.getVariantDetails('queue-pressure-flag').variant)
        .toBe('control');
      expect(featureFlagExposures(flaggedClient)).toHaveLength(0);
      expect(warn).toHaveBeenCalledWith(
        "APDL: Failed to enqueue exposure for feature flag 'queue-pressure-flag'",
        expect.objectContaining({ message: 'APDL: event queue is full' })
      );

      await flaggedClient.debug.flush();
      expect(flaggedClient.getVariant('queue-pressure-flag')).toBe('control');
      expect(featureFlagExposures(flaggedClient)).toHaveLength(1);

      warn.mockImplementation(() => {
        throw new Error('diagnostic logger failed');
      });
      expect(() => flaggedClient.getVariant('queue-pressure-flag', {
        component: 'logger-failure',
      })).not.toThrow();
      expect(featureFlagExposures(flaggedClient)).toHaveLength(1);

      warn.mockRestore();
      await flaggedClient.shutdown();
    });

    it('should preserve JSON attribute types for public flag evaluation', async () => {
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 2,
          project_id: 'apdl',
          flags: [makeFlag('typed-attributes', {
            rules: [{
              id: 'typed-rule',
              name: '',
              conditions: [
                { attribute: 'is_beta', operator: 'equals', value: false },
                { attribute: 'seat_count', operator: 'equals', value: 3 },
              ],
              rollout: { percentage: 100, bucket_by: 'user_id' },
            }],
            fallthrough: {
              rollout: { percentage: 0, bucket_by: 'user_id' },
            },
          })],
        }),
        status: 200,
        headers: new Headers(),
      });
      const typedClient = new APDLClient(createTestConfig());
      await flushAsync();

      typedClient.identify('user_123', { is_beta: false });
      typedClient.experiments.setContext({ attributes: { seat_count: 3 } });

      expect(typedClient.getVariantDetails('typed-attributes')).toMatchObject({
        reason: 'rule_match',
        rule_id: 'typed-rule',
        variant: 'treatment',
      });
      await typedClient.shutdown();
    });

    it('should send canonical exposure payloads to ingestion', async () => {
      window.history.pushState({}, '', '/checkout');
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 2,
          project_id: 'apdl',
          flags: [makeFlag('transport-flag')],
        }),
        status: 200,
        headers: new Headers(),
      });

      const flaggedClient = new APDLClient(createTestConfig());

      await flushAsync();
      flaggedClient.identify('user_123');
      flaggedClient.getVariant('transport-flag', { component: 'CheckoutPage' });
      await flaggedClient.debug.flush();

      const eventPost = fetchMock.mock.calls.find(([url]) => {
        return url === `${ENDPOINT}/v1/events`;
      });
      expect(eventPost).toBeDefined();

      const [, init] = eventPost as [string, RequestInit];
      const payload = JSON.parse(String(init.body)) as {
        events: Array<{ event?: string; properties?: Record<string, unknown> }>;
      };
      const exposure = payload.events.find(
        (event) => event.event === '$feature_flag_exposure'
      );

      expect(exposure?.properties).toMatchObject({
        flag_key: 'transport-flag',
        variant: 'treatment',
        reason: 'fallthrough',
        source: 'initial_fetch',
        page: '/checkout',
        component: 'CheckoutPage',
      });
      expect(exposure?.properties?.rollout_bucket).toBeTypeOf('number');
      expect(exposure?.properties?.variant_bucket).toBeTypeOf('number');
      expect(exposure?.properties).not.toHaveProperty('value');
      expect(exposure?.properties).not.toHaveProperty('bucket');
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

      const flaggedClient = new APDLClient(createTestConfig());

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

      const flaggedClient = new APDLClient(createTestConfig());

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

      const flaggedClient = new APDLClient(createTestConfig({
        consent: { analytics: false, personalization: true, experiments: true },
      }));

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

      const healthClient = new APDLClient(createTestConfig({
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
      }));

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

      const healthClient = new APDLClient(createTestConfig({
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
      }));

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

      const healthClient = new APDLClient(createTestConfig({
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
      }));

      await flushAsync();
      healthClient.identify('user_123');
      window.history.pushState({}, '', '/checkout');
      healthClient.getVariant('checkout-flag');

      const source = MockEventSource.instances.at(-1);
      source?.onmessage?.(new MessageEvent('message', {
        data: JSON.stringify({
          type: 'flag_update',
          action: 'flag_updated',
          version: 2,
          flag: {
            ...makeFlag('checkout-flag'),
            enabled: false,
            version: 2,
          },
        }),
      }));
      await flushAsync();

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
      const healthClient = new APDLClient(createTestConfig({
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
      }));
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

    it('should clear accepted events and stop auto-capture until consent is regranted', async () => {
      const captureClient = new APDLClient(createTestConfig({
        autoCapture: {
          pageViews: false,
          clicks: true,
          formSubmissions: false,
          inputChanges: false,
          scrollDepth: false,
          rage_clicks: false,
          frontend_errors: false,
          web_vitals: false,
        },
      }));
      const button = document.createElement('button');
      document.body.append(button);

      captureClient.track('accepted_before_revocation');
      captureClient.consent.update({ analytics: false });
      expect(captureClient.debug.getQueue()).toEqual([]);

      button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      expect(captureClient.debug.getQueue()).toEqual([]);

      captureClient.consent.update({ analytics: true });
      button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      expect((captureClient.debug.getQueue() as TrackEvent[]).map((event) => event.event))
        .toEqual(['$click']);

      button.remove();
      await captureClient.shutdown();
    });

    it('should fence flag assignment, exposure, fetch, cache, and SSE across experiment consent transitions', async () => {
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 2,
          project_id: 'apdl',
          flags: [makeFlag('consent-controlled-flag')],
        }),
        status: 200,
        headers: new Headers(),
      });
      const experimentClient = new APDLClient(createTestConfig({
        persistence: 'localStorage',
      }));
      await flushAsync();
      experimentClient.identify('user_123');

      expect(experimentClient.getVariant('consent-controlled-flag'))
        .toBe('treatment');
      expect(featureFlagExposures(experimentClient)).toHaveLength(1);
      expect(localStorage.getItem(flagStorageKey('apdl'))).not.toBeNull();
      const initialStream = MockEventSource.instances.at(-1);

      experimentClient.consent.update({ experiments: false });
      await flushAsync();

      expect(experimentClient.getVariantDetails('consent-controlled-flag'))
        .toMatchObject({ variant: null, reason: 'consent_denied', source: null });
      expect(featureFlagExposures(experimentClient)).toHaveLength(0);
      expect(experimentClient.experiments.getContext()).toEqual({ attributes: {} });
      expect(localStorage.getItem(flagStorageKey('apdl'))).toBeNull();
      expect(initialStream?.readyState).toBe(2);

      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 2,
          project_id: 'apdl',
          flags: [makeFlag('consent-controlled-flag', { version: 2 })],
        }),
        status: 200,
        headers: new Headers(),
      });
      experimentClient.consent.update({ experiments: true });
      await flushAsync();

      expect(experimentClient.getVariantDetails('consent-controlled-flag'))
        .toMatchObject({ variant: 'treatment', source: 'initial_fetch' });
      expect(MockEventSource.instances.at(-1)).not.toBe(initialStream);
      expect(localStorage.getItem(flagStorageKey('apdl'))).not.toBeNull();

      await experimentClient.shutdown();
    });

    it('should abort an in-flight flag fetch when experiment consent is revoked', async () => {
      let resolveRequest!: (value: unknown) => void;
      fetchMock.mockImplementationOnce(() => new Promise((resolve) => {
        resolveRequest = resolve;
      }));
      const experimentClient = new APDLClient(createTestConfig());
      const [, request] = fetchMock.mock.calls.at(-1) as [string, RequestInit];

      expect(request.signal?.aborted).toBe(false);
      experimentClient.consent.update({ experiments: false });
      expect(request.signal?.aborted).toBe(true);

      resolveRequest({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 2,
          project_id: 'apdl',
          flags: [makeFlag('late-flag')],
        }),
        status: 200,
        headers: new Headers(),
      });
      await flushAsync();

      expect(experimentClient.getVariantDetails('late-flag'))
        .toMatchObject({ variant: null, reason: 'consent_denied', source: null });
      await experimentClient.shutdown();
    });

    it('should not retain experiment context while experiment consent is denied', async () => {
      await flushAsync();
      const existingStreamCount = MockEventSource.instances.length;
      fetchMock.mockClear();
      localStorage.setItem(flagStorageKey('apdl'), JSON.stringify({
        schema_version: 3,
        project_id: 'local_storage',
        flags: [makeFlag('cached-flag')],
        versions: { 'cached-flag': 1 },
      }));
      const deniedClient = new APDLClient(createTestConfig({
        persistence: 'localStorage',
        consent: { analytics: true, personalization: true, experiments: false },
      }));

      deniedClient.experiments.setContext({ attributes: { plan: 'secret' } });
      expect(deniedClient.experiments.getContext()).toEqual({ attributes: {} });
      expect(deniedClient.getVariantDetails('cached-flag').reason)
        .toBe('consent_denied');
      expect(localStorage.getItem(flagStorageKey('apdl'))).toBeNull();
      expect(fetchMock).not.toHaveBeenCalledWith(
        `${ENDPOINT}/v1/flags`,
        expect.anything()
      );
      expect(MockEventSource.instances).toHaveLength(existingStreamCount);

      await deniedClient.shutdown();
    });

    it('should block and remove personalized UI until consent is granted again', async () => {
      const personalizedClient = new APDLClient(createTestConfig({
        consent: { analytics: true, personalization: false, experiments: true },
      }));
      const slot = document.createElement('div');
      slot.setAttribute('data-apdl-slot', 'consent-slot');
      document.body.append(slot);
      const onSlot = vi.fn();
      personalizedClient.ui.onSlotUpdate(onSlot);
      const uiConfig = {
        component: 'banner',
        props: { text: 'Consent controlled banner' },
        slotId: 'consent-slot',
      };

      expect(personalizedClient.ui.render(uiConfig, slot)).toBeNull();
      expect(slot.children).toHaveLength(0);
      expect(onSlot).not.toHaveBeenCalled();

      personalizedClient.consent.update({ personalization: true });
      expect(onSlot).toHaveBeenCalledWith('consent-slot', slot);
      expect(personalizedClient.ui.render(uiConfig, slot)).not.toBeNull();
      expect(slot.children).toHaveLength(1);

      personalizedClient.consent.update({ personalization: false });
      expect(slot.children).toHaveLength(0);
      expect(personalizedClient.ui.render(uiConfig, slot)).toBeNull();

      slot.remove();
      await personalizedClient.shutdown();
    });

    it('should namespace browser identity, session, consent, and flags by project', async () => {
      localStorage.setItem('apdl_anonymous_id', 'legacy-anonymous-id');
      localStorage.setItem('apdl_session', JSON.stringify({ id: 'legacy-session' }));
      localStorage.setItem('apdl_consent', JSON.stringify({
        analytics: false,
        personalization: false,
        experiments: false,
      }));
      localStorage.setItem('apdl_flags', JSON.stringify({ flags: ['legacy'] }));

      const projectA = new APDLClient(createTestConfig({
        persistence: 'localStorage',
      }));
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          schema_version: 2,
          project_id: 'projectb',
          flags: [],
        }),
      });
      const projectB = new APDLClient(createTestConfig({
        auth: { clientKey: 'client_projectb_0123456789abcdef' },
        persistence: 'localStorage',
      }));
      projectA.track('project_a');
      projectB.track('project_b');
      await flushAsync();

      for (const prefix of [
        'apdl_anonymous_id',
        'apdl_session',
        'apdl_consent',
        'apdl_flags',
      ]) {
        expect(localStorage.getItem(`${prefix}_apdl`)).not.toBeNull();
        expect(localStorage.getItem(`${prefix}_projectb`)).not.toBeNull();
      }
      expect(localStorage.getItem('apdl_anonymous_id_apdl'))
        .not.toBe('legacy-anonymous-id');
      expect(projectA.consent.get().analytics).toBe(true);
      expect(projectB.consent.get().analytics).toBe(true);

      await Promise.all([projectA.shutdown(), projectB.shutdown()]);
    });

    it('should keep identity, session, consent, flags, and offline storage in memory mode', async () => {
      localStorage.clear();
      const memoryClient = new APDLClient(createTestConfig({
        persistence: 'memory',
      }));
      memoryClient.identify('memory-user');
      memoryClient.consent.update({ analytics: false });
      memoryClient.reset();

      expect(Object.keys(localStorage).filter((key) => key.startsWith('apdl_')))
        .toEqual([]);

      await memoryClient.shutdown();
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
    it('should join callers, drain accepted events, and reject tracking after shutdown', async () => {
      client.track('before_shutdown');
      const first = client.shutdown();
      const second = client.shutdown();

      expect(second).toBe(first);
      expect(() => client.track('after_shutdown')).toThrow('client is shut down');
      expect(() => client.identify('after_shutdown')).toThrow('client is shut down');
      expect(() => client.group('after_shutdown')).toThrow('client is shut down');
      expect(() => client.page('after_shutdown')).toThrow('client is shut down');
      expect(() => client.reset()).toThrow('client is shut down');
      await expect(first).resolves.toMatchObject({ delivered: 1, pending: [] });
    });
  });
});

async function flushAsync(): Promise<void> {
  for (let index = 0; index < 5; index++) {
    await Promise.resolve();
  }
}

function flagStorageKey(projectId: string): string {
  return `apdl_flags_${projectId}`;
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

function makeFlag(
  key: string,
  overrides: Record<string, unknown> = {}
): Record<string, unknown> {
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
    ...overrides,
  };
}
