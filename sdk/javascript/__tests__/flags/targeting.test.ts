import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { FlagCache } from '../../src/flags/cache';
import { FlagEvaluator } from '../../src/flags/evaluator';
import { extractFlagConfig } from '../../src/flags/schema';
import {
  MAX_CONDITIONS_PER_RULE,
  MAX_IDENTIFIER_LENGTH,
  MAX_MEMBERSHIP_VALUES,
  MAX_RULES,
  MAX_STRING_LENGTH,
  NUMERIC_PATTERN,
  SUPPORTED_OPERATORS,
} from '../../src/flags/targeting-contract';
import type {
  EvalContext,
  FlagCondition,
  FlagConfig,
} from '../../src/flags/types';

interface FixtureCondition {
  attribute: string;
  operator: string;
  value?: unknown;
}

interface ConditionCase {
  name: string;
  condition: FixtureCondition;
  context: EvalContext;
  expected_match: boolean;
}

interface UnitCase {
  name: string;
  bucket_by: string;
  context: EvalContext;
  expected_available: boolean;
}

interface TargetingFixture {
  fixture_schema_version: number;
  limits: Record<string, number>;
  numeric_pattern: string;
  condition_cases: ConditionCase[];
  invalid_condition_cases: Array<{
    name: string;
    condition: FixtureCondition;
  }>;
  unit_cases: UnitCase[];
}

const fixture = JSON.parse(
  readFileSync(resolve(process.cwd(), '../../fixtures/gates/targeting.json'), 'utf8')
) as TargetingFixture;

describe('strict shared targeting contract', () => {
  it('pins the fixture schema, limits, numeric grammar, and operator set', () => {
    expect(fixture.fixture_schema_version).toBe(1);
    expect(fixture.limits).toEqual({
      max_rules: MAX_RULES,
      max_conditions_per_rule: MAX_CONDITIONS_PER_RULE,
      max_identifier_length: MAX_IDENTIFIER_LENGTH,
      max_string_length: MAX_STRING_LENGTH,
      max_membership_values: MAX_MEMBERSHIP_VALUES,
    });
    expect(fixture.numeric_pattern).toBe(NUMERIC_PATTERN);
    expect(new Set(fixture.condition_cases.map(({ condition }) => condition.operator)))
      .toEqual(SUPPORTED_OPERATORS);
  });

  for (const testCase of fixture.condition_cases) {
    it(`matches shared condition case: ${testCase.name}`, () => {
      expect(evaluateCondition(testCase.condition, testCase.context))
        .toBe(testCase.expected_match);
      expect(extractFlagConfig(makeFlag(testCase.condition))).not.toBeNull();
    });
  }

  for (const testCase of fixture.invalid_condition_cases) {
    it(`fails closed for shared invalid condition: ${testCase.name}`, () => {
      expect(evaluateCondition(testCase.condition, {
        anonymous_id: 'fixture-unit',
        attributes: { value: 'pro' },
      })).toBe(false);
      expect(extractFlagConfig(makeFlag(testCase.condition))).toBeNull();
    });
  }

  for (const testCase of fixture.unit_cases) {
    it(`matches shared bucket unit case: ${testCase.name}`, () => {
      const cache = new FlagCache();
      const evaluator = new FlagEvaluator(cache);
      const flag = makeFlag(undefined, testCase.bucket_by);
      cache.set([flag]);

      const result = evaluator.evaluate(flag.key, testCase.context);
      expect(result.reason).toBe(
        testCase.expected_available ? 'fallthrough' : 'error'
      );
      expect(result.rollout_bucket === null).toBe(!testCase.expected_available);
      expect(result.variant_bucket === null).toBe(!testCase.expected_available);
    });
  }

  it('enforces rule, condition, identifier, string, and membership limits', () => {
    const tooManyRules = Array.from({ length: MAX_RULES + 1 }, (_, index) => ({
      id: `rule-${index}`,
      name: '',
      conditions: [],
      rollout: { percentage: 100, bucket_by: 'anonymous_id' },
    }));
    const tooManyConditions = Array.from(
      { length: MAX_CONDITIONS_PER_RULE + 1 },
      () => ({ attribute: 'value', operator: 'exists' })
    );

    expect(extractFlagConfig({ ...makeFlag(), rules: tooManyRules })).toBeNull();
    expect(extractFlagConfig({
      ...makeFlag(),
      rules: [{
        id: 'condition-limit',
        name: '',
        conditions: tooManyConditions,
        rollout: { percentage: 100, bucket_by: 'anonymous_id' },
      }],
    })).toBeNull();
    expect(extractFlagConfig(makeFlag({
      attribute: 'a'.repeat(MAX_IDENTIFIER_LENGTH + 1),
      operator: 'exists',
    }))).toBeNull();
    expect(extractFlagConfig(makeFlag({
      attribute: 'value',
      operator: 'equals',
      value: 'x'.repeat(MAX_STRING_LENGTH + 1),
    }))).toBeNull();
    expect(extractFlagConfig(makeFlag({
      attribute: 'value',
      operator: 'in',
      value: Array.from({ length: MAX_MEMBERSHIP_VALUES + 1 }, () => 'x'),
    }))).toBeNull();

    const cache = new FlagCache();
    const evaluator = new FlagEvaluator(cache);
    const invalidRuntimeFlag: FlagConfig = { ...makeFlag(), rules: tooManyRules };
    cache.set([invalidRuntimeFlag]);
    expect(evaluator.evaluate(invalidRuntimeFlag.key, {
      anonymous_id: 'fixture-unit',
      attributes: {},
    }).reason).toBe('error');
  });
});

function evaluateCondition(
  condition: FixtureCondition,
  context: EvalContext
): boolean {
  const cache = new FlagCache();
  const evaluator = new FlagEvaluator(cache);
  const flag = makeFlag(condition);
  cache.set([flag]);
  return evaluator.evaluate(flag.key, context).reason === 'rule_match';
}

function makeFlag(
  condition?: FixtureCondition,
  bucketBy = 'anonymous_id'
): FlagConfig {
  return {
    key: 'targeting-fixture',
    enabled: true,
    default_variant: 'control',
    variants: [{ key: 'control', weight: 1 }],
    salt: 'fixture-salt',
    rules: condition === undefined ? [] : [{
      id: 'fixture-rule',
      name: '',
      conditions: [condition as FlagCondition],
      rollout: { percentage: 100, bucket_by: 'anonymous_id' },
    }],
    fallthrough: {
      rollout: {
        percentage: condition === undefined ? 100 : 0,
        bucket_by: bucketBy,
      },
    },
    version: 1,
  };
}
