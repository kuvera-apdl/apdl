import { describe, expect, it } from 'vitest';
import { FlagCache } from '../../src/flags/cache';
import { SSEHandlers } from '../../src/sse/handlers';
import type { GateConfig } from '../../src/flags/types';

describe('SSEHandlers', () => {
  it('loads canonical flags from config events', () => {
    const cache = new FlagCache();
    const handlers = new SSEHandlers(cache, null);

    handlers.handle({
      type: 'config',
      data: JSON.stringify({
        schema_version: 2,
        project_id: 'apdl',
        flags: [makeGate('booking-flow', {
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

  it('merges full flag_update payloads', () => {
    const cache = new FlagCache();
    const handlers = new SSEHandlers(cache, null);

    handlers.handle({
      type: 'config',
      data: JSON.stringify({
        schema_version: 2,
        project_id: 'apdl',
        flags: [makeGate('existing')],
      }),
    });

    handlers.handle({
      type: 'flag_update',
      data: JSON.stringify({
        action: 'flag_created',
        flag: makeGate('created', {
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
    const handlers = new SSEHandlers(cache, null);

    handlers.handle({
      type: 'config',
      data: JSON.stringify({
        schema_version: 2,
        project_id: 'apdl',
        flags: [makeGate('delete-me')],
      }),
    });

    handlers.handle({
      type: 'flag_update',
      data: JSON.stringify({
        action: 'flag_removed',
        key: 'delete-me',
      }),
    });

    expect(cache.get('delete-me')).toBeUndefined();
  });

  it('rejects legacy flag payloads', () => {
    const cache = new FlagCache();
    const handlers = new SSEHandlers(cache, null);

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
    const handlers = new SSEHandlers(cache, null);

    handlers.handle({
      type: 'config',
      data: JSON.stringify({
        schema_version: 2,
        project_id: 'apdl',
        flags: [makeGate('existing')],
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
    const handlers = new SSEHandlers(cache, null);

    handlers.handle({
      type: 'config',
      data: JSON.stringify({
        schema_version: 2,
        project_id: 'apdl',
        flags: [makeGate('toggle-me')],
      }),
    });

    handlers.handle({
      type: 'flag_update',
      data: JSON.stringify({
        action: 'flag_updated',
        key: 'toggle-me',
        enabled: false,
      }),
    });

    expect(cache.get('toggle-me')?.enabled).toBe(true);
  });
});

function makeGate(key: string, overrides: Partial<GateConfig> = {}): GateConfig {
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
