import { describe, it, expect, beforeEach } from 'vitest';
import { FlagEvaluator } from '../../src/flags/evaluator';
import { FlagCache } from '../../src/flags/cache';
import type { EvalContext, FlagConfig } from '../../src/flags/types';

describe('FlagEvaluator', () => {
  let cache: FlagCache;
  let evaluator: FlagEvaluator;

  const baseContext: EvalContext = {
    user_id: 'user_123',
    anonymous_id: '',
    attributes: {
      plan: 'pro',
      country: 'US',
      age: '30',
      email: 'alice@company.com',
    },
  };

  const makeFlag = (
    key: string,
    enabled = true,
    rollout = 100.0,
    overrides: Partial<FlagConfig> = {}
  ): FlagConfig => ({
    key,
    enabled,
    variant_type: 'boolean',
    default_value: 'false',
    rollout_percentage: rollout,
    rules: [],
    variants: [],
    ...overrides,
  });

  beforeEach(() => {
    cache = new FlagCache();
    evaluator = new FlagEvaluator(cache);
  });

  it('should return not_found for unknown flags', () => {
    expect(evaluator.evaluate('missing', baseContext)).toEqual({
      key: 'missing',
      enabled: false,
      value: '',
      variant: '',
      reason: 'not_found',
    });
  });

  it('should return the default value for disabled flags', () => {
    cache.set([makeFlag('feature_x', false)]);

    expect(evaluator.evaluate('feature_x', baseContext)).toMatchObject({
      key: 'feature_x',
      enabled: false,
      value: 'false',
      reason: 'disabled',
    });
  });

  it('should evaluate full and zero rollout using 0-100 percentages', () => {
    cache.set([
      makeFlag('feature_y', true, 100.0),
      makeFlag('feature_z', true, 0.0),
    ]);

    expect(evaluator.evaluate('feature_y', baseContext)).toMatchObject({
      enabled: true,
      value: 'true',
      reason: 'rule_match',
    });
    expect(evaluator.evaluate('feature_z', baseContext)).toMatchObject({
      enabled: false,
      value: 'false',
      reason: 'rollout',
    });
  });

  it('should return error when no user identifier is available', () => {
    cache.set([makeFlag('feature_a')]);

    expect(evaluator.evaluate('feature_a', {
      anonymous_id: '',
      attributes: {},
    })).toMatchObject({
      enabled: false,
      reason: 'error',
    });
  });

  it('should fall back to anonymous_id', () => {
    cache.set([makeFlag('feature_b')]);

    expect(evaluator.evaluate('feature_b', {
      anonymous_id: 'anon_456',
      attributes: {},
    }).enabled).toBe(true);
  });

  it('should produce consistent rollout decisions', () => {
    cache.set([makeFlag('rollout_test', true, 50.0)]);

    const first = evaluator.evaluate('rollout_test', {
      ...baseContext,
      user_id: 'consistent_user',
    });
    const second = evaluator.evaluate('rollout_test', {
      ...baseContext,
      user_id: 'consistent_user',
    });

    expect(first.enabled).toBe(second.enabled);
  });

  it('should distribute 50 percent rollout roughly evenly', () => {
    cache.set([makeFlag('distribution_test', true, 50.0)]);

    let enabled = 0;
    for (let index = 0; index < 1000; index++) {
      if (evaluator.evaluate('distribution_test', {
        user_id: `user_${index}`,
        anonymous_id: '',
        attributes: {},
      }).enabled) {
        enabled++;
      }
    }

    const ratio = enabled / 1000;
    expect(ratio).toBeGreaterThan(0.35);
    expect(ratio).toBeLessThan(0.65);
  });

  it('should match flat targeting rules', () => {
    cache.set([makeFlag('rule_test', true, 100.0, {
      rules: [{ attribute: 'plan', operator: 'equals', value: 'pro' }],
    })]);

    expect(evaluator.evaluate('rule_test', baseContext)).toMatchObject({
      enabled: true,
      reason: 'rule_match',
    });
  });

  it('should reject users that do not match targeting rules', () => {
    cache.set([makeFlag('rule_test', true, 100.0, {
      rules: [{ attribute: 'plan', operator: 'equals', value: 'enterprise' }],
    })]);

    expect(evaluator.evaluate('rule_test', baseContext)).toMatchObject({
      enabled: false,
      reason: 'rule_no_match',
    });
  });

  it('should apply AND within a rule and OR across rules', () => {
    cache.set([makeFlag('compound', true, 100.0, {
      rules: [
        {
          conditions: [
            { attribute: 'plan', operator: 'equals', value: 'enterprise' },
          ],
        },
        {
          conditions: [
            { attribute: 'plan', operator: 'equals', value: 'pro' },
            { attribute: 'country', operator: 'equals', value: 'US' },
          ],
        },
      ],
    })]);

    expect(evaluator.evaluate('compound', baseContext).enabled).toBe(true);
  });

  it('should support backend condition operators', () => {
    const operators: Array<[string, FlagConfig]> = [
      ['contains', makeFlag('contains', true, 100.0, {
        rules: [{ attribute: 'email', operator: 'contains', value: '@company.com' }],
      })],
      ['starts_with', makeFlag('starts_with', true, 100.0, {
        rules: [{ attribute: 'email', operator: 'starts_with', value: 'alice' }],
      })],
      ['in', makeFlag('in', true, 100.0, {
        rules: [{ attribute: 'country', operator: 'in', value: ['US', 'CA'] }],
      })],
      ['gt', makeFlag('gt', true, 100.0, {
        rules: [{ attribute: 'age', operator: 'gt', value: 18 }],
      })],
      ['regex', makeFlag('regex', true, 100.0, {
        rules: [{ attribute: 'email', operator: 'regex', value: '^alice@' }],
      })],
      ['not_exists', makeFlag('not_exists', true, 100.0, {
        rules: [{ attribute: 'missing', operator: 'not_exists' }],
      })],
    ];

    for (const [key, flag] of operators) {
      cache.set([flag]);
      expect(evaluator.evaluate(key, baseContext).enabled).toBe(true);
    }
  });

  it('should select variants from backend-shaped variant definitions', () => {
    cache.set([makeFlag('multivar', true, 100.0, {
      variant_type: 'string',
      variants: [
        { key: 'control', value: 'control', weight: 50 },
        { key: 'variant_a', value: 'variant_a', weight: 50 },
      ],
    })]);

    const result = evaluator.evaluate('multivar', baseContext);

    expect(result.enabled).toBe(true);
    expect(['control', 'variant_a']).toContain(result.value);
    expect(result.variant).toBe(result.value);
  });

  it('should select variants consistently for the same user', () => {
    cache.set([makeFlag('multivar_consistent', true, 100.0, {
      variant_type: 'string',
      variants: [
        { key: 'a', value: 'a', weight: 33 },
        { key: 'b', value: 'b', weight: 33 },
        { key: 'c', value: 'c', weight: 34 },
      ],
    })]);

    const first = evaluator.evaluate('multivar_consistent', baseContext);
    const second = evaluator.evaluate('multivar_consistent', baseContext);

    expect(first.variant).toBe(second.variant);
  });

  it('should skip malformed variant entries without throwing', () => {
    cache.set([makeFlag('malformed-variants', true, 100.0, {
      variant_type: 'string',
      default_value: 'control',
      variants: [null] as never,
    })]);

    expect(() => evaluator.evaluate('malformed-variants', baseContext)).not.toThrow();
    expect(evaluator.evaluate('malformed-variants', baseContext)).toMatchObject({
      enabled: true,
      value: 'control',
      variant: '',
      reason: 'default',
    });
  });

  it('should expose optional variant payloads as an SDK extension', () => {
    cache.set([makeFlag('payload-test', true, 100.0, {
      variant_type: 'string',
      variants: [{
        key: 'only',
        value: 'only',
        weight: 100,
        payload: { color: 'blue' },
      }],
    })]);

    expect(evaluator.evaluate('payload-test', baseContext).payload).toEqual({
      color: 'blue',
    });
  });
});
