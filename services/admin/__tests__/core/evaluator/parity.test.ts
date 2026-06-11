// Evaluator parity (plan §10.1, highest value): every hash, bucket, and
// end-to-end evaluation case in fixtures/gates/parity.json must match
// byte-for-byte — the same fixture the JS SDK, Python SDK, and config service
// pin. Any drift fails CI.
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it, test } from 'vitest'

import {
  evaluateFlag,
  evaluateFlagDetailed,
  pickWeightedVariant,
  type EvaluableFlag,
  type EvaluationContext,
  type EvaluationResult,
} from '../../../src/core/evaluator/evaluate'
import { hashBucket, percentageBucket } from '../../../src/core/evaluator/hash'

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
