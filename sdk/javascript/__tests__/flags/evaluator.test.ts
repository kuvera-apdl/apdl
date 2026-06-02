import { describe, it, expect, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { FlagEvaluator } from '../../src/flags/evaluator';
import { FlagCache } from '../../src/flags/cache';
import type { EvalContext, GateConfig, GateEvaluationResult } from '../../src/flags/types';

interface EvaluationFixture {
  name: string;
  flag: GateConfig;
  context: EvalContext;
  result: Omit<GateEvaluationResult, 'source'>;
}

interface ParityFixture {
  evaluation_cases: EvaluationFixture[];
}

const fixtures = JSON.parse(
  readFileSync(resolve(process.cwd(), '../../fixtures/gates/parity.json'), 'utf8')
) as ParityFixture;

describe('FlagEvaluator', () => {
  let cache: FlagCache;
  let evaluator: FlagEvaluator;

  beforeEach(() => {
    cache = new FlagCache();
    evaluator = new FlagEvaluator(cache);
  });

  it('returns not_found for unknown gates', () => {
    expect(evaluator.evaluate('missing', {
      user_id: 'user_123',
      anonymous_id: '',
      attributes: {},
    })).toEqual({
      key: 'missing',
      value: false,
      reason: 'not_found',
      rule_id: '',
      bucket: null,
      rollout_percentage: null,
      bucket_by: '',
      config_version: 0,
      source: 'none',
    });
  });

  it('returns invalid_config for malformed keyed gate configs', () => {
    cache.markInvalid(['broken-gate'], 'initial_fetch');

    expect(evaluator.evaluate('broken-gate', {
      user_id: 'user_123',
      anonymous_id: '',
      attributes: {},
    })).toEqual({
      key: 'broken-gate',
      value: false,
      reason: 'invalid_config',
      rule_id: '',
      bucket: null,
      rollout_percentage: null,
      bucket_by: '',
      config_version: 0,
      source: 'initial_fetch',
    });
  });

  for (const fixture of fixtures.evaluation_cases) {
    it(`matches config-service parity fixture: ${fixture.name}`, () => {
      cache.set([fixture.flag]);

      expectResult(evaluator.evaluate(fixture.flag.key, fixture.context), {
        ...fixture.result,
        source: 'memory',
      });
    });
  }

  it('evaluates canonical condition operators', () => {
    const conditions = [
      { attribute: 'plan', operator: 'not_equals', value: 'free' },
      { attribute: 'country', operator: 'in', value: ['US', 'CA'] },
      { attribute: 'age', operator: 'gte', value: 18 },
      { attribute: 'missing', operator: 'not_exists' },
      { attribute: 'email', operator: 'contains', value: '@company.com' },
      { attribute: 'email', operator: 'starts_with', value: 'alice' },
      { attribute: 'email', operator: 'ends_with', value: '.com' },
      { attribute: 'email', operator: 'regex', value: '^alice@' },
    ] as const;

    for (const [index, condition] of conditions.entries()) {
      const key = `operator_${index}`;
      cache.set([makeGate(key, {
        rules: [{
          id: `rule_${index}`,
          conditions: [condition],
          rollout: { percentage: 100, bucket_by: 'user_id' },
        }],
      })]);

      expect(evaluator.evaluate(key, {
        user_id: 'user_123',
        anonymous_id: '',
        attributes: {
          plan: 'pro',
          country: 'US',
          age: '30',
          email: 'alice@company.com',
        },
      }).value).toBe(true);
    }
  });

  it('uses anonymous_id only when bucket_by explicitly selects it', () => {
    cache.set([makeGate('anonymous_gate', {
      fallthrough: {
        value: true,
        rollout: { percentage: 100, bucket_by: 'anonymous_id' },
      },
    })]);

    expect(evaluator.evaluate('anonymous_gate', {
      anonymous_id: 'anon_123',
      attributes: {},
    })).toMatchObject({
      value: true,
      reason: 'fallthrough',
      bucket_by: 'anonymous_id',
    });
  });

  it('returns source details from the cache', () => {
    cache.set([makeGate('source_gate')], 'sse');

    expect(evaluator.evaluate('source_gate', {
      user_id: 'user_123',
      anonymous_id: '',
      attributes: {},
    }).source).toBe('sse');
  });
});

function makeGate(key: string, overrides: Partial<GateConfig> = {}): GateConfig {
  return {
    key,
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

function expectResult(actual: GateEvaluationResult, expected: GateEvaluationResult): void {
  expect(actual).toMatchObject({
    ...expected,
    bucket: actual.bucket,
  });

  if (expected.bucket === null) {
    expect(actual.bucket).toBeNull();
  } else {
    expect(actual.bucket).toBeCloseTo(expected.bucket, 10);
  }
}
