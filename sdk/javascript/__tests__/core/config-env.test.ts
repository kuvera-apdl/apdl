import { afterEach, describe, expect, it, vi } from 'vitest';
import { resolveConfig } from '../../src/core/config';
import { CLIENT_KEY, ENDPOINT } from '../helpers';

describe('resolveConfig env defaults and fail-soft validation', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('returns null in non-strict mode when credentials are absent', () => {
    expect(resolveConfig({}, { strict: false })).toBeNull();
    expect(resolveConfig({ endpoint: ENDPOINT }, { strict: false })).toBeNull();
    expect(resolveConfig({ auth: { clientKey: CLIENT_KEY } }, { strict: false })).toBeNull();
  });

  it('throws in strict mode when credentials are absent', () => {
    expect(() => resolveConfig({}, { strict: true })).toThrow('endpoint is required');
    expect(() => resolveConfig({ endpoint: ENDPOINT }, { strict: true })).toThrow(
      'auth'
    );
  });

  it('fills endpoint and clientKey from NEXT_PUBLIC_* env conventions', () => {
    vi.stubEnv('NEXT_PUBLIC_APDL_URL', ENDPOINT);
    vi.stubEnv('NEXT_PUBLIC_APDL_CLIENT_KEY', CLIENT_KEY);

    expect(resolveConfig({}, { strict: false })).toMatchObject({
      endpoint: ENDPOINT,
      auth: { clientKey: CLIENT_KEY },
      projectId: 'apdl',
    });
  });

  it('falls back to unprefixed APDL_* env conventions on the server', () => {
    vi.stubEnv('APDL_URL', ENDPOINT);
    vi.stubEnv('APDL_CLIENT_KEY', CLIENT_KEY);

    expect(resolveConfig({}, { strict: false })).toMatchObject({
      endpoint: ENDPOINT,
      auth: { clientKey: CLIENT_KEY },
    });
  });

  it('prefers explicit config over environment variables', () => {
    vi.stubEnv('NEXT_PUBLIC_APDL_URL', 'https://env.example.com');

    const resolved = resolveConfig(
      { endpoint: ENDPOINT, auth: { clientKey: CLIENT_KEY } },
      { strict: false }
    );

    expect(resolved?.endpoint).toBe(ENDPOINT);
  });

  it('rejects malformed explicit values instead of falling back to valid env values', () => {
    vi.stubEnv('NEXT_PUBLIC_APDL_URL', ENDPOINT);
    vi.stubEnv('NEXT_PUBLIC_APDL_CLIENT_KEY', CLIENT_KEY);

    expect(() => resolveConfig(
      { endpoint: '/relative', auth: { clientKey: CLIENT_KEY } },
      { strict: false }
    )).toThrow('endpoint must be an absolute HTTP(S) origin');
    expect(() => resolveConfig(
      { endpoint: ENDPOINT, auth: { clientKey: '' } },
      { strict: false }
    )).toThrow('auth.clientKey is required');
  });

  it('still throws on a malformed client key in non-strict mode', () => {
    expect(() =>
      resolveConfig(
        { endpoint: ENDPOINT, auth: { clientKey: 'bad_key' } },
        { strict: false }
      )
    ).toThrow('client_{project_id}_{token}');
  });

  it('rejects the legacy proj_ browser credential shape', () => {
    expect(() =>
      resolveConfig(
        {
          endpoint: ENDPOINT,
          auth: { clientKey: 'proj_apdl_0123456789abcdef' },
        },
        { strict: false }
      )
    ).toThrow('client_{project_id}_{token}');
  });

  it('still rejects removed config fields in non-strict mode', () => {
    expect(() =>
      resolveConfig(
        { endpoint: ENDPOINT, auth: { clientKey: CLIENT_KEY }, apiKey: 'x' } as never,
        { strict: false }
      )
    ).toThrow('apiKey is no longer supported');
  });
});
