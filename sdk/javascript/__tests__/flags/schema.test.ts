import { describe, expect, it } from 'vitest';
import {
  extractFlagConfig,
  extractFlagConfigs,
  extractInvalidFlagKey,
  parseFlagConfigResult,
} from '../../src/flags/schema';

describe('gate config schema parsing', () => {
  it('extracts canonical gate configs from the SDK envelope', () => {
    const flags = extractFlagConfigs({
      schema_version: 1,
      project_id: 'apdl',
      flags: [makeGate()],
    });

    expect(flags).toHaveLength(1);
    expect(flags[0]).toMatchObject({
      key: 'checkout',
      default_value: false,
      fallthrough: {
        value: true,
        rollout: { percentage: 100, bucket_by: 'user_id' },
      },
    });
  });

  it('rejects legacy top-level fields', () => {
    for (const legacyField of [
      'variant_type',
      'variants',
      'rollout_percentage',
      'targeting_rules',
      'default_variant',
    ]) {
      expect(extractFlagConfig({
        ...makeGate(),
        [legacyField]: 'legacy',
      })).toBeNull();
      expect(extractInvalidFlagKey({
        ...makeGate(),
        [legacyField]: 'legacy',
      })).toBe('checkout');
    }
  });

  it('returns invalid keyed records in parse results', () => {
    const result = parseFlagConfigResult({
      schema_version: 1,
      flags: [
        makeGate({ key: 'valid' }),
        {
          ...makeGate({ key: 'invalid' }),
          default_value: 'false',
        },
      ],
    });

    expect(result).toEqual({
      flags: [makeGate({ key: 'valid' })],
      invalid_keys: ['invalid'],
    });
  });

  it('rejects non-canonical condition aliases', () => {
    expect(extractFlagConfig(makeGate({
      rules: [{
        id: 'rule_alias',
        conditions: [{ attribute: 'plan', operator: 'eq', value: 'pro' }],
        rollout: { percentage: 100, bucket_by: 'user_id' },
      }],
    }))).toBeNull();
  });

  it('rejects unknown nested fields', () => {
    expect(extractFlagConfig(makeGate({
      fallthrough: {
        value: true,
        rollout: {
          percentage: 100,
          bucket_by: 'user_id',
          seed: 'legacy',
        },
      },
    }))).toBeNull();
  });

  it('rejects malformed collection envelopes', () => {
    expect(extractFlagConfigs({
      schema_version: 2,
      flags: [makeGate()],
    })).toEqual([]);

    expect(extractFlagConfigs({
      schema_version: 1,
      flags: [makeGate()],
      results: [],
    })).toEqual([]);
  });
});

function makeGate(overrides: Record<string, unknown> = {}): Record<string, unknown> {
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
    ...overrides,
  };
}
