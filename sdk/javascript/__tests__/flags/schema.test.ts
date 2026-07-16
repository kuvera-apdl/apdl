import { describe, expect, it } from 'vitest';
import {
  extractFlagConfig,
  extractFlagConfigs,
  extractInvalidFlagKey,
  parseFlagConfigResult,
} from '../../src/flags/schema';

describe('flag config schema parsing', () => {
  it('extracts canonical flag configs from the SDK envelope', () => {
    const flags = extractFlagConfigs({
      schema_version: 2,
      project_id: 'apdl',
      flags: [makeFlag()],
    });

    expect(flags).toHaveLength(1);
    expect(flags[0]).toMatchObject({
      key: 'checkout',
      default_variant: 'control',
      variants: [
        { key: 'control', weight: 1 },
        { key: 'treatment', weight: 1 },
      ],
      fallthrough: {
        rollout: { percentage: 100, bucket_by: 'user_id' },
      },
    });
  });

  it('rejects old and ambiguous top-level fields', () => {
    for (const rejectedField of [
      'default_value',
      'defaultVariant',
      'variant_type',
      'variants_json',
      'rollout_percentage',
      'targeting_rules',
    ]) {
      expect(extractFlagConfig({
        ...makeFlag(),
        [rejectedField]: 'legacy',
      })).toBeNull();
      expect(extractInvalidFlagKey({
        ...makeFlag(),
        [rejectedField]: 'legacy',
      })).toBe('checkout');
    }
  });

  it('returns invalid keyed records in parse results', () => {
    const result = parseFlagConfigResult({
      schema_version: 2,
      project_id: 'apdl',
      flags: [
        makeFlag({ key: 'valid' }),
        {
          ...makeFlag({ key: 'invalid' }),
          default_variant: 'missing',
        },
      ],
    });

    expect(result).toEqual({
      project_id: 'apdl',
      flags: [makeFlag({ key: 'valid' })],
      invalid_keys: ['invalid'],
    });
  });

  it('rejects non-canonical condition aliases', () => {
    expect(extractFlagConfig(makeFlag({
      rules: [{
        id: 'rule_alias',
        conditions: [{ attribute: 'plan', operator: 'eq', value: 'pro' }],
        rollout: { percentage: 100, bucket_by: 'user_id' },
        name: '',
      }],
    }))).toBeNull();
  });

  it('rejects unknown nested fields', () => {
    expect(extractFlagConfig(makeFlag({
      fallthrough: {
        rollout: {
          percentage: 100,
          bucket_by: 'user_id',
          seed: 'legacy',
        },
      },
    }))).toBeNull();
  });

  it('rejects malformed collection envelopes', () => {
    expect(extractFlagConfigs([makeFlag()])).toEqual([]);

    expect(extractFlagConfigs({
      schema_version: 1,
      project_id: 'apdl',
      flags: [makeFlag()],
    })).toEqual([]);

    expect(extractFlagConfigs({
      schema_version: 2,
      flags: [makeFlag()],
      results: [],
    })).toEqual([]);
  });

  it('rejects invalid variant definitions', () => {
    const invalidOverrides = [
      { default_variant: undefined },
      { default_variant: 'missing' },
      {
        variants: [
          { key: 'control', weight: 1 },
          { key: 'control', weight: 1 },
        ],
      },
      {
        variants: [
          { key: 'control', weight: 0.5 },
          { key: 'treatment', weight: 0.5 },
        ],
      },
      {
        variants: [
          { key: 'control', weight: -1 },
          { key: 'treatment', weight: 1 },
        ],
      },
      {
        variants: [
          { key: 'control', weight: '1' },
          { key: 'treatment', weight: 1 },
        ],
      },
      {
        variants: [
          { key: 'control', weight: 0 },
          { key: 'treatment', weight: 0 },
        ],
      },
    ];

    for (const overrides of invalidOverrides) {
      expect(extractFlagConfig(makeFlag(overrides))).toBeNull();
    }
  });

  it('rejects zero flag config versions', () => {
    expect(extractFlagConfig(makeFlag({ version: 0 }))).toBeNull();
    expect(extractInvalidFlagKey(makeFlag({ version: 0 }))).toBe('checkout');
  });
});

function makeFlag(overrides: Record<string, unknown> = {}): Record<string, unknown> {
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
