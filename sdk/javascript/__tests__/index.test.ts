import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { APDL, APDLClient, type APDLConfig, type ExperimentContext } from '../src';
import {
  CLIENT_KEY,
  CONFIG_ENDPOINT,
  INGESTION_ENDPOINT,
  MockEventSource,
  createTestConfig,
  emptyFlagsResponse,
} from './helpers';

const fetchMock = vi.fn();

describe('public SDK entrypoint', () => {
  const clients: APDLClient[] = [];

  beforeEach(() => {
    vi.useFakeTimers();
    fetchMock.mockReset();
    fetchMock.mockResolvedValue(emptyFlagsResponse());
    MockEventSource.instances = [];
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('EventSource', MockEventSource);
  });

  afterEach(async () => {
    for (const client of clients.splice(0)) {
      await client.shutdown();
    }
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('exports the APDL namespace and APDLClient class', () => {
    expect(APDL).toBeDefined();
    expect(typeof APDL).toBe('object');
    expect(APDLClient).toBeTypeOf('function');
  });

  it('exposes APDL.init as the primary public entrypoint', () => {
    expect(APDL.init).toBeTypeOf('function');
  });

  it('returns an APDLClient from APDL.init', () => {
    const client = APDL.init(createTestConfig());
    clients.push(client);

    expect(client).toBeInstanceOf(APDLClient);
  });

  it('exports the ExperimentContext type from the public entrypoint', () => {
    const context: ExperimentContext = {
      attributes: {
        plan: 'pro',
      },
    };
    const client = APDL.init(createTestConfig());
    clients.push(client);

    client.experiments.setContext(context);

    expect(client.experiments.getContext()).toEqual(context);
  });

  it('applies the canonical endpoints and auth config contract', async () => {
    const client = APDL.init(createTestConfig({
      batchSize: 1,
    }));
    clients.push(client);

    expect(fetchMock).toHaveBeenCalledWith(`${CONFIG_ENDPOINT}/v1/flags`, {
      headers: {
        'X-API-Key': CLIENT_KEY,
        'X-APDL-SDK': 'js/0.1.0',
      },
    });

    const source = MockEventSource.instances.at(-1);
    expect(source).toBeDefined();
    const sseUrl = new URL(source!.url);
    expect(`${sseUrl.origin}${sseUrl.pathname}`).toBe(`${CONFIG_ENDPOINT}/v1/stream`);
    expect(sseUrl.searchParams.get('api_key')).toBe(CLIENT_KEY);

    client.track('public_api_event');
    await client.debug.flush();

    const eventPost = fetchMock.mock.calls.find(([url]) => {
      return url === `${INGESTION_ENDPOINT}/v1/events`;
    });
    expect(eventPost).toBeDefined();
    const [, init] = eventPost as [string, RequestInit];
    expect(init.headers).toMatchObject({
      'Content-Type': 'application/json',
      'X-API-Key': CLIENT_KEY,
      'X-APDL-SDK': 'js/0.1.0',
    });
  });

  it('rejects removed top-level config fields through APDL.init', () => {
    const config = {
      ...createTestConfig(),
      apiKey: CLIENT_KEY,
    };

    expect(() => APDL.init(config as unknown as APDLConfig))
      .toThrow('config.apiKey is no longer supported');
  });
});
