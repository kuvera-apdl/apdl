import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CLIENT_KEY, ENDPOINT } from './helpers';

describe('package import safety', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.stubEnv('NEXT_PUBLIC_APDL_URL', ENDPOINT);
    vi.stubEnv('NEXT_PUBLIC_APDL_CLIENT_KEY', CLIENT_KEY);
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it('does not initialize, capture, fetch, or open a stream on import', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    await import('../src/index');
    await Promise.resolve();

    expect(fetchMock).not.toHaveBeenCalled();
    expect((globalThis as Record<string, unknown>).__APDL_SINGLETONS__).toBeUndefined();
  });
});
