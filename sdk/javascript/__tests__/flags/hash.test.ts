import { describe, expect, it } from 'vitest';
import { hashBucket, isInRollout, percentageBucket } from '../../src/flags/hash';

describe('FNV-1a flag hashing', () => {
  it('should match config-service golden hash values', () => {
    expect(hashBucket('', '')).toBe(1057798253);
    expect(hashBucket('feature_y', 'user_123')).toBe(3351610489);
    expect(hashBucket('rollout_test', 'consistent_user')).toBe(1371409931);
    expect(hashBucket('distribution_test', 'user_1')).toBe(2379438105);
    expect(hashBucket('multivar:variant', 'user_123')).toBe(1166540398);
  });

  it('should return unsigned 32-bit integers', () => {
    for (const [key, user] of [
      ['a', 'user_1'],
      ['feature', 'anon_1'],
      ['emoji', '\u{1F600}'],
      ['long', 'x'.repeat(10000)],
    ]) {
      const hash = hashBucket(key, user);
      expect(hash).toBeGreaterThanOrEqual(0);
      expect(hash).toBeLessThanOrEqual(0xffffffff);
      expect(Number.isInteger(hash)).toBe(true);
    }
  });

  it('should convert hash values to 0-100 percentage buckets', () => {
    const bucket = percentageBucket('feature_y', 'user_123');

    expect(bucket).toBeGreaterThanOrEqual(0);
    expect(bucket).toBeLessThanOrEqual(100);
    expect(bucket).toBeCloseTo(78.0358, 4);
  });

  it('should handle rollout boundary values', () => {
    expect(isInRollout('feature', 'user', 100)).toBe(true);
    expect(isInRollout('feature', 'user', 0)).toBe(false);
  });

  it('should distribute 50 percent rollout roughly evenly', () => {
    let enabled = 0;
    for (let index = 0; index < 10000; index++) {
      if (isInRollout('distribution_test', `user_${index}`, 50)) {
        enabled++;
      }
    }

    const ratio = enabled / 10000;
    expect(ratio).toBeGreaterThan(0.45);
    expect(ratio).toBeLessThan(0.55);
  });
});
