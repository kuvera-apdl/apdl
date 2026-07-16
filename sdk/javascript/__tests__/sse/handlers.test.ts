import { describe, expect, it, vi } from 'vitest';
import { FlagCache } from '../../src/flags/cache';
import { SSEHandlers } from '../../src/sse/handlers';
import type { FlagConfig } from '../../src/flags/types';

describe('SSEHandlers', () => {
  it('loads canonical flags from config events', () => {
    const cache = new FlagCache();
    const handlers = new SSEHandlers(cache, 'apdl');

    handlers.handle({
      type: 'config',
      data: JSON.stringify({
        schema_version: 2,
        project_id: 'apdl',
        flags: [makeFlag('booking-flow', {
          fallthrough: {
            rollout: { percentage: 50, bucket_by: 'user_id' },
          },
        })],
      }),
    });

    expect(cache.get('booking-flow')).toMatchObject({
      key: 'booking-flow',
      enabled: true,
      fallthrough: {
        rollout: { percentage: 50, bucket_by: 'user_id' },
      },
    });
    expect(cache.getSource('booking-flow')).toBe('sse');
  });

  it('rejects config events for another project', () => {
    const cache = new FlagCache();
    const handlers = new SSEHandlers(cache, 'apdl');

    handlers.handle({
      type: 'config',
      data: JSON.stringify({
        schema_version: 2,
        project_id: 'foreign',
        flags: [makeFlag('foreign')],
      }),
    });

    expect(cache.get('foreign')).toBeUndefined();
  });

  it('merges full flag_update payloads', () => {
    const cache = new FlagCache();
    const handlers = new SSEHandlers(cache, 'apdl');

    handlers.handle({
      type: 'config',
      data: JSON.stringify({
        schema_version: 2,
        project_id: 'apdl',
        flags: [makeFlag('existing')],
      }),
    });

    handlers.handle({
      type: 'flag_update',
      data: JSON.stringify({
        action: 'flag_created',
        version: 1,
        flag: makeFlag('created', {
          fallthrough: {
            rollout: { percentage: 20, bucket_by: 'user_id' },
          },
        }),
      }),
    });

    expect(cache.get('existing')).toBeDefined();
    expect(cache.get('created')).toMatchObject({
      key: 'created',
      fallthrough: {
        rollout: { percentage: 20 },
      },
    });
  });

  it('removes cached flags on canonical removal events', () => {
    const cache = new FlagCache();
    const handlers = new SSEHandlers(cache, 'apdl');

    handlers.handle({
      type: 'config',
      data: JSON.stringify({
        schema_version: 2,
        project_id: 'apdl',
        flags: [makeFlag('delete-me')],
      }),
    });

    handlers.handle({
      type: 'flag_update',
      data: JSON.stringify({
        action: 'flag_removed',
        key: 'delete-me',
        version: 2,
      }),
    });

    expect(cache.get('delete-me')).toBeUndefined();
  });

  it('rejects legacy flag payloads', () => {
    const cache = new FlagCache();
    const handlers = new SSEHandlers(cache, 'apdl');

    handlers.handle({
      type: 'config',
      data: JSON.stringify({
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
    });

    expect(cache.get('legacy')).toBeUndefined();
  });

  it('does not clear existing flags when a malformed config payload arrives', () => {
    const cache = new FlagCache();
    const handlers = new SSEHandlers(cache, 'apdl');

    handlers.handle({
      type: 'config',
      data: JSON.stringify({
        schema_version: 2,
        project_id: 'apdl',
        flags: [makeFlag('existing')],
      }),
    });

    handlers.handle({
      type: 'config',
      data: JSON.stringify({
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
    });

    expect(cache.get('existing')).toBeDefined();
    expect(cache.get('legacy')).toBeUndefined();
    expect(cache.isInvalid('legacy')).toBe(true);
    expect(cache.getInvalidSource('legacy')).toBe('sse');
  });

  it('ignores enabled-only compatibility deltas', () => {
    const cache = new FlagCache();
    const handlers = new SSEHandlers(cache, 'apdl');

    handlers.handle({
      type: 'config',
      data: JSON.stringify({
        schema_version: 2,
        project_id: 'apdl',
        flags: [makeFlag('toggle-me')],
      }),
    });

    handlers.handle({
      type: 'flag_update',
      data: JSON.stringify({
        action: 'flag_updated',
        key: 'toggle-me',
        enabled: false,
        version: 2,
      }),
    });

    expect(cache.get('toggle-me')?.enabled).toBe(true);
  });

  it('ignores stale and duplicate updates without notifying listeners', () => {
    const cache = new FlagCache();
    const handlers = new SSEHandlers(cache, 'apdl');

    handlers.handle({
      type: 'config',
      data: JSON.stringify({
        schema_version: 2,
        project_id: 'apdl',
        flags: [makeFlag('checkout', { version: 3 })],
      }),
    });

    const listener = vi.fn();
    cache.onChange(listener);
    const cacheVersion = cache.getVersion();

    for (const version of [2, 3]) {
      handlers.handle({
        type: 'flag_update',
        data: JSON.stringify({
          action: 'flag_updated',
          version,
          flag: makeFlag('checkout', { enabled: false, version }),
        }),
      });
    }

    expect(cache.get('checkout')).toMatchObject({ enabled: true, version: 3 });
    expect(cache.getAuthoritativeVersion('checkout')).toBe(3);
    expect(cache.getVersion()).toBe(cacheVersion);
    expect(listener).not.toHaveBeenCalled();

    handlers.handle({
      type: 'flag_update',
      data: JSON.stringify({
        action: 'flag_updated',
        version: 4,
        flag: makeFlag('checkout', { enabled: false, version: 4 }),
      }),
    });

    expect(cache.get('checkout')).toMatchObject({ enabled: false, version: 4 });
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it('ignores stale removals and prevents old creates from resurrecting tombstones', () => {
    const cache = new FlagCache();
    const handlers = new SSEHandlers(cache, 'apdl');

    handlers.handle({
      type: 'config',
      data: JSON.stringify({
        schema_version: 2,
        project_id: 'apdl',
        flags: [makeFlag('checkout', { version: 3 })],
      }),
    });

    for (const version of [2, 3]) {
      handlers.handle({
        type: 'flag_update',
        data: JSON.stringify({
          action: 'flag_removed',
          key: 'checkout',
          version,
        }),
      });
    }

    expect(cache.get('checkout')).toBeDefined();
    expect(cache.getAuthoritativeVersion('checkout')).toBe(3);

    handlers.handle({
      type: 'flag_update',
      data: JSON.stringify({
        action: 'flag_removed',
        key: 'checkout',
        version: 4,
      }),
    });

    expect(cache.get('checkout')).toBeUndefined();
    expect(cache.getAuthoritativeVersion('checkout')).toBe(4);

    for (const version of [3, 4]) {
      handlers.handle({
        type: 'flag_update',
        data: JSON.stringify({
          action: 'flag_created',
          version,
          flag: makeFlag('checkout', { version }),
        }),
      });
    }

    expect(cache.get('checkout')).toBeUndefined();
    expect(cache.getAuthoritativeVersion('checkout')).toBe(4);
  });

  it('records removals for unknown keys before any older create arrives', () => {
    const cache = new FlagCache();
    const handlers = new SSEHandlers(cache, 'apdl');

    handlers.handle({
      type: 'flag_update',
      data: JSON.stringify({
        action: 'flag_removed',
        key: 'ghost',
        version: 5,
      }),
    });
    handlers.handle({
      type: 'flag_update',
      data: JSON.stringify({
        action: 'flag_created',
        version: 4,
        flag: makeFlag('ghost', { version: 4 }),
      }),
    });

    expect(cache.get('ghost')).toBeUndefined();
    expect(cache.getAuthoritativeVersion('ghost')).toBe(5);
  });

  it('rejects update envelopes whose version disagrees with the flag', () => {
    const cache = new FlagCache();
    const handlers = new SSEHandlers(cache, 'apdl');

    handlers.handle({
      type: 'flag_update',
      data: JSON.stringify({
        action: 'flag_created',
        version: 4,
        flag: makeFlag('mismatch', { version: 5 }),
      }),
    });

    expect(cache.get('mismatch')).toBeUndefined();
    expect(cache.getAuthoritativeVersion('mismatch')).toBeNull();
  });
});

function makeFlag(key: string, overrides: Partial<FlagConfig> = {}): FlagConfig {
  return {
    key,
    enabled: true,
    default_variant: 'control',
    variants: [
      { key: 'control', weight: 1 },
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
