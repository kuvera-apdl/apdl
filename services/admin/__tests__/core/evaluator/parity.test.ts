// Evaluator parity (plan §10.1, highest value): every hash, bucket, and
// end-to-end evaluation case in fixtures/gates/parity.json must match
// byte-for-byte — the same fixture the JS SDK, Python SDK, and config service
// pin. Any drift fails CI.
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it, test } from 'vitest'

import {
  clientFlagConfigSchema,
  gateConditionSchema,
  gateRuleSchema,
} from '../../../src/api/schemas/flags'
import type { GateCondition } from '../../../src/api/types/flags'
import {
  evaluateFlag,
  evaluateFlagDetailed,
  matchesCondition,
  pickWeightedVariant,
  resolveAttribute,
  type EvaluableFlag,
  type EvaluationContext,
  type EvaluationResult,
} from '../../../src/core/evaluator/evaluate'
import { hashBucket, percentageBucket } from '../../../src/core/evaluator/hash'
import {
  MAX_CONDITIONS_PER_RULE,
  MAX_IDENTIFIER_LENGTH,
  MAX_MEMBERSHIP_VALUES,
  MAX_RULES,
  MAX_STRING_LENGTH,
  NUMERIC_PATTERN,
  SUPPORTED_OPERATORS,
} from '../../../src/core/evaluator/targetingContract'

interface HashFixture {
  flag_key: string
  salt: string
  unit_id: string
  hash: number
  bucket: number
}

interface EvaluationFixture {
  name: string
  flag: EvaluableFlag
  context: EvaluationContext
  result: EvaluationResult
}

interface ParityFixtures {
  hash_cases: HashFixture[]
  evaluation_cases: EvaluationFixture[]
}

const fixtures: ParityFixtures = JSON.parse(
  readFileSync(resolve(process.cwd(), '../../fixtures/gates/parity.json'), 'utf8'),
)

interface TargetingCondition {
  attribute: string
  operator: string
  value?: unknown
}

interface TargetingFixture {
  fixture_schema_version: number
  limits: Record<string, number>
  numeric_pattern: string
  condition_cases: Array<{
    name: string
    condition: TargetingCondition
    context: EvaluationContext
    expected_match: boolean
  }>
  invalid_condition_cases: Array<{
    name: string
    condition: TargetingCondition
  }>
  unit_cases: Array<{
    name: string
    bucket_by: string
    context: EvaluationContext
    expected_available: boolean
  }>
}

const targetingFixtures: TargetingFixture = JSON.parse(
  readFileSync(resolve(process.cwd(), '../../fixtures/gates/targeting.json'), 'utf8'),
)

describe('hash parity', () => {
  test('fixture file covers cases', () => {
    expect(fixtures.hash_cases.length).toBeGreaterThan(0)
    expect(fixtures.evaluation_cases.length).toBeGreaterThan(0)
  })

  for (const fixture of fixtures.hash_cases) {
    it(`hash("${fixture.flag_key}:${fixture.salt}:${fixture.unit_id}")`, () => {
      expect(hashBucket(fixture.flag_key, fixture.salt, fixture.unit_id)).toBe(fixture.hash)
      expect(percentageBucket(fixture.flag_key, fixture.salt, fixture.unit_id)).toBe(fixture.bucket)
    })
  }
})

describe('evaluation parity', () => {
  for (const fixture of fixtures.evaluation_cases) {
    it(fixture.name, () => {
      expect(evaluateFlag(fixture.flag, fixture.context)).toEqual(fixture.result)
    })
  }
})

describe('strict shared targeting contract', () => {
  test('pins fixture metadata and the supported operator set', () => {
    expect(targetingFixtures.fixture_schema_version).toBe(1)
    expect(targetingFixtures.limits).toEqual({
      max_rules: MAX_RULES,
      max_conditions_per_rule: MAX_CONDITIONS_PER_RULE,
      max_identifier_length: MAX_IDENTIFIER_LENGTH,
      max_string_length: MAX_STRING_LENGTH,
      max_membership_values: MAX_MEMBERSHIP_VALUES,
    })
    expect(targetingFixtures.numeric_pattern).toBe(NUMERIC_PATTERN)
    expect(
      new Set(targetingFixtures.condition_cases.map(({ condition }) => condition.operator)),
    ).toEqual(SUPPORTED_OPERATORS)
  })

  for (const fixture of targetingFixtures.condition_cases) {
    it(`matches shared condition case: ${fixture.name}`, () => {
      expect(gateConditionSchema.safeParse(fixture.condition).success).toBe(true)
      expect(
        matchesCondition(
          fixture.condition as GateCondition,
          resolveAttribute(fixture.condition.attribute, fixture.context),
        ),
      ).toBe(fixture.expected_match)
    })
  }

  for (const fixture of targetingFixtures.invalid_condition_cases) {
    it(`fails closed for shared invalid condition: ${fixture.name}`, () => {
      expect(gateConditionSchema.safeParse(fixture.condition).success).toBe(false)
      expect(
        matchesCondition(
          fixture.condition as GateCondition,
          resolveAttribute(fixture.condition.attribute, {
            anonymous_id: 'fixture-unit',
            attributes: { value: 'pro' },
          }),
        ),
      ).toBe(false)
    })
  }

  for (const fixture of targetingFixtures.unit_cases) {
    it(`matches shared bucket unit case: ${fixture.name}`, () => {
      const result = evaluateFlag(targetingFlag(undefined, fixture.bucket_by), fixture.context)
      expect(result.reason).toBe(fixture.expected_available ? 'fallthrough' : 'error')
      expect(result.rollout_bucket === null).toBe(!fixture.expected_available)
      expect(result.variant_bucket === null).toBe(!fixture.expected_available)
    })
  }

  test('enforces structural limits in API schemas and runtime evaluation', () => {
    const tooManyRules = Array.from({ length: MAX_RULES + 1 }, (_, index) => ({
      id: `rule-${index}`,
      name: '',
      conditions: [],
      rollout: { percentage: 100, bucket_by: 'anonymous_id' },
    }))
    const tooManyConditions = Array.from(
      { length: MAX_CONDITIONS_PER_RULE + 1 },
      () => ({ attribute: 'value', operator: 'exists' }),
    )

    expect(
      clientFlagConfigSchema.safeParse({ ...targetingFlag(), rules: tooManyRules }).success,
    ).toBe(false)
    expect(
      gateRuleSchema.safeParse({
        id: 'condition-limit',
        name: '',
        conditions: tooManyConditions,
        rollout: { percentage: 100, bucket_by: 'anonymous_id' },
      }).success,
    ).toBe(false)
    expect(
      gateConditionSchema.safeParse({
        attribute: 'a'.repeat(MAX_IDENTIFIER_LENGTH + 1),
        operator: 'exists',
      }).success,
    ).toBe(false)
    expect(
      gateConditionSchema.safeParse({
        attribute: 'value',
        operator: 'equals',
        value: 'x'.repeat(MAX_STRING_LENGTH + 1),
      }).success,
    ).toBe(false)
    expect(
      gateConditionSchema.safeParse({
        attribute: 'value',
        operator: 'in',
        value: Array.from({ length: MAX_MEMBERSHIP_VALUES + 1 }, () => 'x'),
      }).success,
    ).toBe(false)
    expect(evaluateFlag({ ...targetingFlag(), rules: tooManyRules }, {
      anonymous_id: 'fixture-unit',
      attributes: {},
    }).reason).toBe('error')
  })
})

describe('trace layer', () => {
  const flag = fixtures.evaluation_cases.find((entry) => entry.name === 'ordered rules stop after first match')

  it('annotates rule outcomes without changing the result', () => {
    expect(flag).toBeDefined()
    const evaluation = evaluateFlagDetailed(flag!.flag, flag!.context)
    expect(evaluation.result).toEqual(flag!.result)
    // First rule matched conditions but missed its 0% rollout; the second was
    // never reached even though its conditions would match.
    expect(evaluation.rules[0]?.outcome).toBe('rollout_missed')
    expect(evaluation.rules[1]?.outcome).toBe('not_reached')
    expect(evaluation.fallthrough.reached).toBe(false)
  })

  it('marks failing conditions in non-matching rules', () => {
    const caseWithMiss = fixtures.evaluation_cases.find(
      (entry) => entry.name === 'no rule match uses fallthrough',
    )
    const evaluation = evaluateFlagDetailed(caseWithMiss!.flag, caseWithMiss!.context)
    expect(evaluation.rules[0]?.outcome).toBe('conditions_failed')
    expect(evaluation.rules[0]?.conditions.some((condition) => !condition.matched)).toBe(true)
    expect(evaluation.fallthrough.reached).toBe(true)
  })
})

describe('pickWeightedVariant', () => {
  it('returns null for zero total weight and respects cumulative order', () => {
    expect(pickWeightedVariant([{ key: 'a', weight: 0 }], 50)).toBeNull()
    expect(
      pickWeightedVariant(
        [
          { key: 'a', weight: 1 },
          { key: 'b', weight: 1 },
        ],
        25,
      ),
    ).toBe('a')
    expect(
      pickWeightedVariant(
        [
          { key: 'a', weight: 1 },
          { key: 'b', weight: 1 },
        ],
        75,
      ),
    ).toBe('b')
  })
})

function targetingFlag(
  condition?: TargetingCondition,
  bucketBy = 'anonymous_id',
): EvaluableFlag {
  return {
    key: 'targeting-fixture',
    enabled: true,
    default_variant: 'control',
    variants: [{ key: 'control', weight: 1 }],
    salt: 'fixture-salt',
    rules: condition === undefined
      ? []
      : [
          {
            id: 'fixture-rule',
            name: '',
            conditions: [condition as GateCondition],
            rollout: { percentage: 100, bucket_by: 'anonymous_id' },
          },
        ],
    fallthrough: {
      rollout: {
        percentage: condition === undefined ? 100 : 0,
        bucket_by: bucketBy,
      },
    },
    version: 1,
  }
}
