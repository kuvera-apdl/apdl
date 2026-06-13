import { beforeEach, describe, expect, it } from 'vitest';
import { FlagCache } from '../../src/flags/cache';
import type { FlagConfig } from '../../src/flags/types';

describe('FlagCache', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('persists and restores last-known-good flags when enabled', () => {
    const first = new FlagCache({
      persist: true,
      storageKey: 'apdl_test_flags',
    });

    first.set([makeFlag()], 'initial_fetch');

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

    first.set([makeFlag()], 'initial_fetch');

    const restored = new FlagCache({
      persist: true,
      storageKey: 'apdl_test_flags',
    });

    expect(restored.get('checkout')).toBeUndefined();
  });

  it('tracks invalid keyed configs without clearing unrelated flags', () => {
    const cache = new FlagCache();
    cache.set([makeFlag()], 'initial_fetch');

    cache.markInvalid(['broken'], 'sse');

    expect(cache.get('checkout')).toBeDefined();
    expect(cache.isInvalid('broken')).toBe(true);
    expect(cache.getInvalidSource('broken')).toBe('sse');
  });
});

function makeFlag(): FlagConfig {
  return {
    key: 'checkout',
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
  };
}
