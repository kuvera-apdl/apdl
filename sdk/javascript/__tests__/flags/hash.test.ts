import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { hashBucket, isInRollout, percentageBucket } from '../../src/flags/hash';

interface HashFixture {
  flag_key: string;
  salt: string;
  unit_id: string;
  hash: number;
  bucket: number;
}

interface ParityFixture {
  hash_cases: HashFixture[];
}

const fixtures = JSON.parse(
  readFileSync(resolve(process.cwd(), '../../fixtures/gates/parity.json'), 'utf8')
) as ParityFixture;

describe('FNV-1a flag hashing', () => {
  it('matches config-service golden hash values', () => {
    for (const fixture of fixtures.hash_cases) {
      expect(hashBucket(fixture.flag_key, fixture.salt, fixture.unit_id)).toBe(fixture.hash);
      expect(
        percentageBucket(fixture.flag_key, fixture.salt, fixture.unit_id)
      ).toBeCloseTo(fixture.bucket, 10);
    }
  });

  it('returns unsigned 32-bit integers', () => {
    for (const [flagKey, salt, unitId] of [
      ['a', 'salt', 'user_1'],
      ['feature', 'salt', 'anon_1'],
      ['emoji', 'salt', '\u{1F600}'],
      ['long', 'salt', 'x'.repeat(10000)],
    ]) {
      const hash = hashBucket(flagKey, salt, unitId);
      expect(hash).toBeGreaterThanOrEqual(0);
      expect(hash).toBeLessThanOrEqual(0xffffffff);
      expect(Number.isInteger(hash)).toBe(true);
    }
  });

  it('handles rollout boundary values', () => {
    expect(isInRollout('feature', 'salt', 'user', 100)).toBe(true);
    expect(isInRollout('feature', 'salt', 'user', 0)).toBe(false);
  });

  it('distributes 50 percent rollout roughly evenly', () => {
    let enabled = 0;
    for (let index = 0; index < 10000; index++) {
      if (isInRollout('distribution_test', 'salt_123', `user_${index}`, 50)) {
        enabled++;
      }
    }

    const ratio = enabled / 10000;
    expect(ratio).toBeGreaterThan(0.45);
    expect(ratio).toBeLessThan(0.55);
  });
});
