import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { APDL, apdl, init } from '../../src/core/init';
import { APDLClient } from '../../src/core/client';
import { NoopClient } from '../../src/core/noop-client';
import {
  MockEventSource,
  createTestConfig,
  emptyFlagsResponse,
} from '../helpers';

const SECOND_CLIENT_KEY = 'proj_other_0123456789abcdef';
const fetchMock = vi.fn();

function clearRegistry(): void {
  delete (globalThis as Record<string, unknown>).__APDL_SINGLETONS__;
}

describe('init() singleton and SSR safety', () => {
  const clients: Array<{ shutdown: () => Promise<void> }> = [];

  beforeEach(() => {
    vi.useFakeTimers();
    fetchMock.mockReset();
    fetchMock.mockResolvedValue(emptyFlagsResponse());
    MockEventSource.instances = [];
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('EventSource', MockEventSource);
    clearRegistry();
  });

  afterEach(async () => {
    for (const client of clients.splice(0)) {
      await client.shutdown();
    }
    clearRegistry();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('returns the same instance for repeated calls with the same client key', () => {
    const a = init(createTestConfig());
    const b = init(createTestConfig());
    clients.push(a);

    expect(a).toBeInstanceOf(APDLClient);
    expect(b).toBe(a);
  });

  it('returns distinct instances for different client keys', () => {
    const a = init(createTestConfig());
    const b = init(createTestConfig({ auth: { clientKey: SECOND_CLIENT_KEY } }));
    clients.push(a, b);

    expect(a).not.toBe(b);
  });

  it('evicts the instance on shutdown so a later init() starts fresh', async () => {
    const a = init(createTestConfig());
    await a.shutdown();

    const b = init(createTestConfig());
    clients.push(b);

    expect(b).not.toBe(a);
    expect(b).toBeInstanceOf(APDLClient);
  });

  it('exposes init through the APDL namespace', () => {
    expect(APDL.init).toBe(init);
  });

  it('returns an inert no-op client during SSR (no window)', () => {
    vi.stubGlobal('window', undefined);
    vi.stubGlobal('document', undefined);

    const client = init(createTestConfig());

    expect(client).toBeInstanceOf(NoopClient);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(MockEventSource.instances).toHaveLength(0);
  });

  it('returns a no-op client and warns when configuration is absent', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});

    const client = init();

    expect(client).toBeInstanceOf(NoopClient);
    expect(client.getVariant('any-flag')).toBeNull();
    expect(() => client.track('noop_event')).not.toThrow();
    expect(fetchMock).not.toHaveBeenCalled();

    warn.mockRestore();
  });

  it('lazy `apdl` no-ops without configuration', () => {
    expect(apdl.getVariant('any-flag')).toBeNull();
    expect(() => apdl.track('lazy_event')).not.toThrow();
  });
});
