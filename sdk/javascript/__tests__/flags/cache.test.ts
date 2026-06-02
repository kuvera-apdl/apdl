import { beforeEach, describe, expect, it } from 'vitest';
import { FlagCache } from '../../src/flags/cache';
import type { GateConfig } from '../../src/flags/types';

describe('FlagCache', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('persists and restores last-known-good flags when enabled', () => {
    const first = new FlagCache({
      persist: true,
      storageKey: 'apdl_test_flags',
    });

    first.set([makeGate()], 'initial_fetch');

    const restored = new FlagCache({
      persist: true,
      storageKey: 'apdl_test_flags',
    });

    expect(restored.get('checkout')).toMatchObject({
      key: 'checkout',
      enabled: true,
    });
    expect(restored.getSource('checkout')).toBe('local_storage');
  });

  it('uses memory only when persistence is disabled', () => {
    const first = new FlagCache({
      persist: false,
      storageKey: 'apdl_test_flags',
    });

    first.set([makeGate()], 'initial_fetch');

    const restored = new FlagCache({
      persist: true,
      storageKey: 'apdl_test_flags',
    });

    expect(restored.get('checkout')).toBeUndefined();
  });

  it('tracks invalid keyed configs without clearing unrelated flags', () => {
    const cache = new FlagCache();
    cache.set([makeGate()], 'initial_fetch');

    cache.markInvalid(['broken'], 'sse');

    expect(cache.get('checkout')).toBeDefined();
    expect(cache.isInvalid('broken')).toBe(true);
    expect(cache.getInvalidSource('broken')).toBe('sse');
  });
});

function makeGate(): GateConfig {
  return {
    key: 'checkout',
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
