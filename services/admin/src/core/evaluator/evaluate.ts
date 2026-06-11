// Local flag evaluator — an exact port of the SDK semantics
// (sdk/javascript/src/flags/evaluator.ts), with an additional trace layer the
// tester UI uses to explain *why* a user got a variant. The result path is
// parity-tested against fixtures/gates/parity.json; the trace is derived from
// the same single pass so it cannot drift from the result.
import type { FallthroughConfig, GateCondition, GateRule, RolloutConfig, VariantConfig } from '@/api/types/flags'

import { percentageBucket } from './hash'

export interface EvaluableFlag {
  key: string
  enabled: boolean
  default_variant: string
  variants: VariantConfig[]
  salt: string
  rules: GateRule[]
  fallthrough: FallthroughConfig
  version: number
}

export interface EvaluationContext {
  user_id?: string
  anonymous_id?: string
  attributes?: Record<string, unknown>
}

export type EvaluationReason =
  | 'not_found'
  | 'invalid_config'
  | 'disabled'
  | 'error'
  | 'rule_match'
  | 'rule_rollout'
  | 'fallthrough'
  | 'fallthrough_rollout'

export interface EvaluationResult {
  key: string
  variant: string | null
  reason: EvaluationReason
  rule_id: string | null
  rollout_bucket: number | null
  variant_bucket: number | null
  rollout_percentage: number | null
  bucket_by: string | null
  config_version: number | null
}

export interface AttributeResolution {
  exists: boolean
  value: unknown
}

export interface ConditionTrace {
  condition: GateCondition
  actual: AttributeResolution
  matched: boolean
}

export interface RolloutTrace {
  bucket: number | null
  passed: boolean
  percentage: number
  bucketBy: string
}

export type RuleOutcome = 'matched' | 'conditions_failed' | 'rollout_missed' | 'not_reached' | 'error'

export interface RuleTrace {
  rule: GateRule
  outcome: RuleOutcome
  conditions: ConditionTrace[]
  rollout: RolloutTrace | null
}

export interface FlagEvaluation {
  result: EvaluationResult
  rules: RuleTrace[]
  fallthrough: { reached: boolean; rollout: RolloutTrace | null }
}

export function resolveAttribute(attribute: string, context: EvaluationContext): AttributeResolution {
  if (attribute === 'user_id') {
    if (Object.prototype.hasOwnProperty.call(context, 'user_id')) {
      return { exists: true, value: context.user_id }
    }
    return { exists: false, value: null }
  }

  if (attribute === 'anonymous_id') {
    if (Object.prototype.hasOwnProperty.call(context, 'anonymous_id')) {
      return { exists: true, value: context.anonymous_id }
    }
    return { exists: false, value: null }
  }

  const attributes = context.attributes ?? {}
  if (Object.prototype.hasOwnProperty.call(attributes, attribute)) {
    return { exists: true, value: attributes[attribute] }
  }

  return { exists: false, value: null }
}

function matchesCondition(condition: GateCondition, actual: AttributeResolution): boolean {
  const { operator } = condition

  if (operator === 'exists') {
    return actual.exists && actual.value !== null && actual.value !== undefined
  }
  if (operator === 'not_exists') {
    return !actual.exists || actual.value === null || actual.value === undefined
  }
  if (!actual.exists || !Object.prototype.hasOwnProperty.call(condition, 'value')) {
    return false
  }

  const expected = condition.value
  const actualValue = String(actual.value)

  if (operator === 'equals') {
    return actualValue === String(expected)
  }
  if (operator === 'not_equals') {
    return actualValue !== String(expected)
  }
  if (operator === 'contains') {
    return typeof expected === 'string' && actualValue.includes(expected)
  }
  if (operator === 'not_contains') {
    return typeof expected === 'string' && !actualValue.includes(expected)
  }
  if (operator === 'starts_with') {
    return typeof expected === 'string' && actualValue.startsWith(expected)
  }
  if (operator === 'ends_with') {
    return typeof expected === 'string' && actualValue.endsWith(expected)
  }
  if (operator === 'in') {
    return Array.isArray(expected) && expected.some((item) => Object.is(item, actual.value))
  }
  if (operator === 'not_in') {
    return !Array.isArray(expected) || !expected.some((item) => Object.is(item, actual.value))
  }
  if (operator === 'gt' || operator === 'gte' || operator === 'lt' || operator === 'lte') {
    const actualNumber = Number(actual.value)
    const expectedNumber = Number(expected)

    if (!Number.isFinite(actualNumber) || !Number.isFinite(expectedNumber)) {
      return false
    }

    if (operator === 'gt') return actualNumber > expectedNumber
    if (operator === 'gte') return actualNumber >= expectedNumber
    if (operator === 'lt') return actualNumber < expectedNumber
    return actualNumber <= expectedNumber
  }
  if (operator === 'regex') {
    if (typeof expected !== 'string') {
      return false
    }
    try {
      return new RegExp(expected).test(actualValue)
    } catch {
      return false
    }
  }

  return false
}

function unitId(context: EvaluationContext, bucketBy: string): string {
  const actual = resolveAttribute(bucketBy, context)
  if (actual.exists && actual.value !== null && actual.value !== undefined) {
    return String(actual.value)
  }
  return ''
}

function applyRollout(flag: EvaluableFlag, rollout: RolloutConfig, context: EvaluationContext): RolloutTrace {
  const unit = unitId(context, rollout.bucket_by)
  if (!unit) {
    return { passed: false, bucket: null, percentage: rollout.percentage, bucketBy: rollout.bucket_by }
  }

  const bucket = percentageBucket(flag.key, `${flag.salt}:rollout`, unit)
  return {
    passed: bucket < rollout.percentage,
    bucket,
    percentage: rollout.percentage,
    bucketBy: rollout.bucket_by,
  }
}

export function pickWeightedVariant(variants: VariantConfig[], variantBucket: number): string | null {
  const totalWeight = variants.reduce((total, variant) => total + variant.weight, 0)
  if (totalWeight <= 0) {
    return null
  }

  const target = (variantBucket / 100) * totalWeight
  let cumulative = 0
  let lastPositiveVariant: string | null = null

  for (const variant of variants) {
    if (variant.weight <= 0) {
      continue
    }

    lastPositiveVariant = variant.key
    cumulative += variant.weight
    if (target < cumulative) {
      return variant.key
    }
  }

  return lastPositiveVariant
}

function assignVariant(
  flag: EvaluableFlag,
  context: EvaluationContext,
  bucketBy: string,
): { variant: string; variantBucket: number | null } {
  const unit = unitId(context, bucketBy)
  if (!unit) {
    return { variant: flag.default_variant, variantBucket: null }
  }

  const variantBucket = percentageBucket(flag.key, `${flag.salt}:variant`, unit)
  const assigned = pickWeightedVariant(flag.variants, variantBucket) ?? flag.default_variant
  return { variant: assigned, variantBucket }
}

const NOT_FOUND_RESULT = (key: string): EvaluationResult => ({
  key,
  variant: null,
  reason: 'not_found',
  rule_id: null,
  rollout_bucket: null,
  variant_bucket: null,
  rollout_percentage: null,
  bucket_by: null,
  config_version: null,
})

export function evaluateFlagDetailed(
  flag: EvaluableFlag | null,
  context: EvaluationContext,
): FlagEvaluation {
  if (!flag) {
    return {
      result: NOT_FOUND_RESULT(''),
      rules: [],
      fallthrough: { reached: false, rollout: null },
    }
  }

  const base: EvaluationResult = {
    key: flag.key,
    variant: flag.default_variant,
    reason: 'error',
    rule_id: null,
    rollout_bucket: null,
    variant_bucket: null,
    rollout_percentage: null,
    bucket_by: null,
    config_version: flag.version,
  }

  const ruleTraces: RuleTrace[] = []

  if (!flag.enabled) {
    for (const rule of flag.rules) {
      ruleTraces.push({ rule, outcome: 'not_reached', conditions: [], rollout: null })
    }
    return {
      result: { ...base, reason: 'disabled' },
      rules: ruleTraces,
      fallthrough: { reached: false, rollout: null },
    }
  }

  let decided: EvaluationResult | null = null

  for (const rule of flag.rules) {
    // Condition outcomes are computed for every rule so the tester can render
    // the full picture; only the first fully-matching rule decides the result.
    const conditions: ConditionTrace[] = rule.conditions.map((condition) => {
      const actual = resolveAttribute(condition.attribute, context)
      return { condition, actual, matched: matchesCondition(condition, actual) }
    })
    const allMatch = conditions.every((trace) => trace.matched)

    if (decided) {
      ruleTraces.push({ rule, outcome: 'not_reached', conditions, rollout: null })
      continue
    }

    if (!allMatch) {
      ruleTraces.push({ rule, outcome: 'conditions_failed', conditions, rollout: null })
      continue
    }

    const rollout = applyRollout(flag, rule.rollout, context)

    if (rollout.bucket === null) {
      decided = {
        ...base,
        reason: 'error',
        rule_id: rule.id,
        rollout_percentage: rollout.percentage,
        bucket_by: rollout.bucketBy,
      }
      ruleTraces.push({ rule, outcome: 'error', conditions, rollout })
      continue
    }

    if (!rollout.passed) {
      decided = {
        ...base,
        reason: 'rule_rollout',
        rule_id: rule.id,
        rollout_bucket: rollout.bucket,
        rollout_percentage: rollout.percentage,
        bucket_by: rollout.bucketBy,
      }
      ruleTraces.push({ rule, outcome: 'rollout_missed', conditions, rollout })
      continue
    }

    const assignment = assignVariant(flag, context, rollout.bucketBy)
    decided = {
      ...base,
      variant: assignment.variant,
      reason: 'rule_match',
      rule_id: rule.id,
      rollout_bucket: rollout.bucket,
      variant_bucket: assignment.variantBucket,
      rollout_percentage: rollout.percentage,
      bucket_by: rollout.bucketBy,
    }
    ruleTraces.push({ rule, outcome: 'matched', conditions, rollout })
  }

  if (decided) {
    return { result: decided, rules: ruleTraces, fallthrough: { reached: false, rollout: null } }
  }

  const rollout = applyRollout(flag, flag.fallthrough.rollout, context)

  if (rollout.bucket === null) {
    return {
      result: {
        ...base,
        reason: 'error',
        rollout_percentage: rollout.percentage,
        bucket_by: rollout.bucketBy,
      },
      rules: ruleTraces,
      fallthrough: { reached: true, rollout },
    }
  }

  if (!rollout.passed) {
    return {
      result: {
        ...base,
        reason: 'fallthrough_rollout',
        rollout_bucket: rollout.bucket,
        rollout_percentage: rollout.percentage,
        bucket_by: rollout.bucketBy,
      },
      rules: ruleTraces,
      fallthrough: { reached: true, rollout },
    }
  }

  const assignment = assignVariant(flag, context, rollout.bucketBy)
  return {
    result: {
      ...base,
      variant: assignment.variant,
      reason: 'fallthrough',
      rollout_bucket: rollout.bucket,
      variant_bucket: assignment.variantBucket,
      rollout_percentage: rollout.percentage,
      bucket_by: rollout.bucketBy,
    },
    rules: ruleTraces,
    fallthrough: { reached: true, rollout },
  }
}

export function evaluateFlag(flag: EvaluableFlag | null, context: EvaluationContext): EvaluationResult {
  return evaluateFlagDetailed(flag, context).result
}
