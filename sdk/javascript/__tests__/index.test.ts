import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  APDL,
  APDLClient,
  type APDLApi,
  type APDLConfig,
  type ExperimentContext,
} from '../src';
import { SDK_IDENTIFIER } from '../src/core/constants';
import {
  CLIENT_KEY,
  ENDPOINT,
  MockEventSource,
  createTestConfig,
  mockApiFetch,
} from './helpers';

const fetchMock = vi.fn();

describe('public SDK entrypoint', () => {
  const clients: APDLApi[] = [];

  beforeEach(() => {
    vi.useFakeTimers();
    fetchMock.mockReset();
    fetchMock.mockImplementation(mockApiFetch);
    MockEventSource.instances = [];
    vi.stubGlobal('fetch', fetchMock);
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

    expect(fetchMock).toHaveBeenCalledWith(`${ENDPOINT}/v1/flags`, {
      headers: {
        'X-API-Key': CLIENT_KEY,
        'X-APDL-SDK': SDK_IDENTIFIER,
      },
    });

    await flushAsync();
    const source = MockEventSource.instances.at(-1);
    expect(source).toBeDefined();
    const sseUrl = new URL(source!.url);
    expect(`${sseUrl.origin}${sseUrl.pathname}`).toBe(`${ENDPOINT}/v1/stream`);
    expect(sseUrl.search).toBe('');
    expect(source!.init.headers).toMatchObject({
      Accept: 'text/event-stream',
      'X-API-Key': CLIENT_KEY,
      'X-APDL-SDK': SDK_IDENTIFIER,
    });

    client.track('public_api_event');
    await client.debug.flush();

    const eventPost = fetchMock.mock.calls.find(([url]) => {
      return url === `${ENDPOINT}/v1/events`;
    });
    expect(eventPost).toBeDefined();
    const [, init] = eventPost as [string, RequestInit];
    expect(init.headers).toMatchObject({
      'Content-Type': 'application/json',
      'X-API-Key': CLIENT_KEY,
      'X-APDL-SDK': SDK_IDENTIFIER,
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

async function flushAsync(): Promise<void> {
  for (let index = 0; index < 10; index += 1) {
    await Promise.resolve();
  }
}
