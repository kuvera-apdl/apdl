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

  it('does not let an older snapshot resurrect a deletion tombstone', () => {
    const cache = new FlagCache();
    cache.set([makeFlag({ version: 3 })], 'initial_fetch');

    expect(cache.removeIfNewer('checkout', 4)).toBe(true);
    cache.set([makeFlag({ version: 3 })], 'initial_fetch');

    expect(cache.get('checkout')).toBeUndefined();
    expect(cache.getAuthoritativeVersion('checkout')).toBe(4);

    cache.set([makeFlag({ enabled: false, version: 5 })], 'initial_fetch');

    expect(cache.get('checkout')).toMatchObject({ enabled: false, version: 5 });
    expect(cache.getAuthoritativeVersion('checkout')).toBe(5);
  });

  it('persists deletion versions so stale updates stay rejected after reload', () => {
    const first = new FlagCache({
      persist: true,
      storageKey: 'apdl_test_flags',
    });
    first.set([makeFlag({ version: 3 })], 'initial_fetch');
    first.removeIfNewer('checkout', 4);

    const restored = new FlagCache({
      persist: true,
      storageKey: 'apdl_test_flags',
    });

    expect(restored.get('checkout')).toBeUndefined();
    expect(restored.getAuthoritativeVersion('checkout')).toBe(4);
    expect(restored.upsertIfNewer(makeFlag({ version: 4 }), 4)).toBe(false);
    expect(restored.upsertIfNewer(makeFlag({ version: 5 }), 5)).toBe(true);
    expect(restored.get('checkout')).toMatchObject({ version: 5 });
  });

  it('rejects and clears obsolete version 2 persisted collections', () => {
    localStorage.setItem('apdl_test_flags', JSON.stringify({
      schema_version: 2,
      project_id: 'local_storage',
      flags: [makeFlag({ version: 3 })],
    }));

    const restored = new FlagCache({
      persist: true,
      storageKey: 'apdl_test_flags',
    });

    expect(restored.get('checkout')).toBeUndefined();
    expect(restored.getAuthoritativeVersion('checkout')).toBeNull();
    expect(localStorage.getItem('apdl_test_flags')).toBeNull();
  });

  it.each([
    '',
    'x'.repeat(129),
  ])('rejects non-canonical persisted version key %j', (key) => {
    localStorage.setItem('apdl_test_flags', JSON.stringify({
      schema_version: 3,
      project_id: 'local_storage',
      flags: [],
      versions: { [key]: 4 },
    }));

    const restored = new FlagCache({
      persist: true,
      storageKey: 'apdl_test_flags',
    });

    expect(restored.getAll()).toEqual([]);
    expect(localStorage.getItem('apdl_test_flags')).toBeNull();
  });
});

function makeFlag(overrides: Partial<FlagConfig> = {}): FlagConfig {
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
    ...overrides,
  };
}
