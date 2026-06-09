import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { APDL, APDLClient, type APDLConfig } from '../src';

const CLIENT_KEY = 'proj_apdl_0123456789abcdef';
const INGESTION_ENDPOINT = 'https://ingest.public-api.test';
const CONFIG_ENDPOINT = 'https://config.public-api.test';

function createConfig(overrides: Partial<APDLConfig> = {}): APDLConfig {
  const { endpoints, auth, ...rest } = overrides;

  return {
    ...rest,
    endpoints: {
      ingestion: INGESTION_ENDPOINT,
      config: CONFIG_ENDPOINT,
      ...endpoints,
    },
    auth: {
      clientKey: CLIENT_KEY,
      ...auth,
    },
    autoCapture: false,
    persistence: 'memory',
  };
}

const fetchMock = vi.fn();

class MockEventSource {
  static instances: MockEventSource[] = [];
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;

  constructor(public url: string) {
    MockEventSource.instances.push(this);
  }

  addEventListener() {
    // The public API tests only assert connection construction.
  }

  close() {
    // No-op for test cleanup.
  }
}

describe('public SDK entrypoint', () => {
  const clients: APDLClient[] = [];

  beforeEach(() => {
    vi.useFakeTimers();
    fetchMock.mockReset();
    fetchMock.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        schema_version: 2,
        project_id: 'apdl',
        flags: [],
      }),
      status: 200,
      headers: new Headers(),
    });
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
    const client = APDL.init(createConfig());
    clients.push(client);

    expect(client).toBeInstanceOf(APDLClient);
  });

  it('applies the canonical endpoints and auth config contract', async () => {
    const client = APDL.init(createConfig({
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
      ...createConfig(),
      apiKey: CLIENT_KEY,
    };

    expect(() => APDL.init(config as unknown as APDLConfig))
      .toThrow('config.apiKey is no longer supported');
  });
});
