import type {
  EvalContext,
  FlagCondition,
  FlagConfig,
  FlagEvaluationResult,
  FlagRule,
  RolloutConfig,
  VariantConfig,
} from './types';
import { FlagCache } from './cache';
import { percentageBucket } from './hash';
import {
  MAX_CONDITIONS_PER_RULE,
  MAX_RULES,
  NUMERIC_OPERATORS,
  PRESENCE_OPERATORS,
  SUPPORTED_OPERATORS,
  isBoundedString,
  isConditionValueValid,
  isIdentifier,
  isMembershipList,
  isScalar,
  parseNumeric,
  scalarEqual,
} from './targeting-contract';

type ResolvedAttribute =
  | { exists: true; value: unknown }
  | { exists: false; value: null };

interface RolloutResult {
  passed: boolean;
  rolloutBucket: number | null;
  percentage: number;
  bucketBy: string;
}

/**
 * Canonical local feature flag evaluator matching the config service contract.
 */
export class FlagEvaluator {
  private cache: FlagCache;

  constructor(cache: FlagCache) {
    this.cache = cache;
  }

  evaluate(key: string, context: EvalContext): FlagEvaluationResult {
    const flag = this.cache.get(key);

    if (!flag) {
      if (this.cache.isInvalid(key)) {
        return {
          key,
          variant: null,
          reason: 'invalid_config',
          rule_id: null,
          rollout_bucket: null,
          variant_bucket: null,
          rollout_percentage: null,
          bucket_by: null,
          config_version: null,
          source: null,
        };
      }

      return {
        key,
        variant: null,
        reason: 'not_found',
        rule_id: null,
        rollout_bucket: null,
        variant_bucket: null,
        rollout_percentage: null,
        bucket_by: null,
        config_version: null,
        source: null,
      };
    }

    const result = this.baseResult(flag);

    if (!flag.enabled) {
      return {
        ...result,
        reason: 'disabled',
      };
    }

    if (!this.rulesWithinLimits(flag.rules)) {
      return {
        ...result,
        reason: 'error',
      };
    }

    for (const rule of flag.rules) {
      if (!this.matchesRule(rule, context)) {
        continue;
      }

      const rollout = this.applyRollout(flag, rule.rollout, context);
      if (rollout.rolloutBucket === null) {
        return {
          ...result,
          reason: 'error',
          rule_id: rule.id,
          rollout_bucket: null,
          variant_bucket: null,
          rollout_percentage: rollout.percentage,
          bucket_by: rollout.bucketBy,
        };
      }

      if (!rollout.passed) {
        return {
          ...result,
          reason: 'rule_rollout',
          rule_id: rule.id,
          rollout_bucket: rollout.rolloutBucket,
          rollout_percentage: rollout.percentage,
          bucket_by: rollout.bucketBy,
        };
      }

      const assignment = this.assignWeightedVariant(flag, context, rollout.bucketBy);
      return {
        ...result,
        variant: assignment.variant,
        reason: 'rule_match',
        rule_id: rule.id,
        rollout_bucket: rollout.rolloutBucket,
        variant_bucket: assignment.variantBucket,
        rollout_percentage: rollout.percentage,
        bucket_by: rollout.bucketBy,
      };
    }

    const rollout = this.applyRollout(flag, flag.fallthrough.rollout, context);
    if (rollout.rolloutBucket === null) {
      return {
        ...result,
        reason: 'error',
        rollout_bucket: null,
        variant_bucket: null,
        rollout_percentage: rollout.percentage,
        bucket_by: rollout.bucketBy,
      };
    }

    if (!rollout.passed) {
      return {
        ...result,
        reason: 'fallthrough_rollout',
        rollout_bucket: rollout.rolloutBucket,
        rollout_percentage: rollout.percentage,
        bucket_by: rollout.bucketBy,
      };
    }

    const assignment = this.assignWeightedVariant(flag, context, rollout.bucketBy);
    return {
      ...result,
      variant: assignment.variant,
      reason: 'fallthrough',
      rollout_bucket: rollout.rolloutBucket,
      variant_bucket: assignment.variantBucket,
      rollout_percentage: rollout.percentage,
      bucket_by: rollout.bucketBy,
    };
  }

  private baseResult(flag: FlagConfig): FlagEvaluationResult {
    return {
      key: flag.key,
      variant: flag.default_variant,
      reason: 'error',
      rule_id: null,
      rollout_bucket: null,
      variant_bucket: null,
      rollout_percentage: null,
      bucket_by: null,
      config_version: flag.version,
      source: this.cache.getSource(flag.key),
    };
  }

  private matchesRule(rule: FlagRule, context: EvalContext): boolean {
    if (
      !Array.isArray(rule.conditions)
      || rule.conditions.length > MAX_CONDITIONS_PER_RULE
    ) {
      return false;
    }
    return rule.conditions.every((condition) => this.matchesCondition(condition, context));
  }

  private matchesCondition(condition: FlagCondition, context: EvalContext): boolean {
    if (condition === null || typeof condition !== 'object') return false;
    const { attribute, operator } = condition;
    if (
      !isIdentifier(attribute)
      || typeof operator !== 'string'
      || !SUPPORTED_OPERATORS.has(operator)
    ) {
      return false;
    }

    const hasValue = Object.prototype.hasOwnProperty.call(condition, 'value');
    if (PRESENCE_OPERATORS.has(operator)) {
      if (hasValue) return false;
    } else if (!hasValue || !isConditionValueValid(operator, condition.value)) {
      return false;
    }

    if (operator === 'exists') {
      return this.resolveAttribute(attribute, context).exists;
    }
    if (operator === 'not_exists') {
      return !this.resolveAttribute(attribute, context).exists;
    }

    const actual = this.resolveAttribute(attribute, context);
    if (!actual.exists) return false;
    const expected = condition.value;

    if (operator === 'equals') {
      return scalarEqual(actual.value, expected);
    }
    if (operator === 'not_equals') {
      return isScalar(actual.value) && !scalarEqual(actual.value, expected);
    }
    if (operator === 'contains') {
      return isBoundedString(actual.value)
        && isBoundedString(expected)
        && actual.value.includes(expected);
    }
    if (operator === 'not_contains') {
      return isBoundedString(actual.value)
        && isBoundedString(expected)
        && !actual.value.includes(expected);
    }
    if (operator === 'starts_with') {
      return isBoundedString(actual.value)
        && isBoundedString(expected)
        && actual.value.startsWith(expected);
    }
    if (operator === 'ends_with') {
      return isBoundedString(actual.value)
        && isBoundedString(expected)
        && actual.value.endsWith(expected);
    }
    if (operator === 'in') {
      return isScalar(actual.value)
        && isMembershipList(expected)
        && expected.some((item) => scalarEqual(actual.value, item));
    }
    if (operator === 'not_in') {
      return isScalar(actual.value)
        && isMembershipList(expected)
        && !expected.some((item) => scalarEqual(actual.value, item));
    }
    if (NUMERIC_OPERATORS.has(operator)) {
      const actualNumber = parseNumeric(actual.value);
      const expectedNumber = parseNumeric(expected);

      if (actualNumber === null || expectedNumber === null) {
        return false;
      }

      if (operator === 'gt') return actualNumber > expectedNumber;
      if (operator === 'gte') return actualNumber >= expectedNumber;
      if (operator === 'lt') return actualNumber < expectedNumber;
      return actualNumber <= expectedNumber;
    }

    return false;
  }

  private rulesWithinLimits(rules: FlagRule[]): boolean {
    if (!Array.isArray(rules) || rules.length > MAX_RULES) return false;
    return rules.every((rule) => (
      rule !== null
      && typeof rule === 'object'
      && isIdentifier(rule.id)
      && Array.isArray(rule.conditions)
      && rule.conditions.length <= MAX_CONDITIONS_PER_RULE
    ));
  }

  private applyRollout(
    flag: FlagConfig,
    rollout: RolloutConfig,
    context: EvalContext
  ): RolloutResult {
    const unitId = this.unitId(context, rollout.bucket_by);
    if (!unitId) {
      return {
        passed: false,
        rolloutBucket: null,
        percentage: rollout.percentage,
        bucketBy: rollout.bucket_by,
      };
    }

    const bucket = percentageBucket(flag.key, `${flag.salt}:rollout`, unitId);
    return {
      passed: bucket < rollout.percentage,
      rolloutBucket: bucket,
      percentage: rollout.percentage,
      bucketBy: rollout.bucket_by,
    };
  }

  private assignWeightedVariant(
    flag: FlagConfig,
    context: EvalContext,
    bucketBy: string
  ): { variant: string; variantBucket: number | null } {
    const unitId = this.unitId(context, bucketBy);
    if (!unitId) {
      return {
        variant: flag.default_variant,
        variantBucket: null,
      };
    }

    const variantBucket = percentageBucket(flag.key, `${flag.salt}:variant`, unitId);
    const assigned = assignWeightedVariant(flag.variants, variantBucket) ?? flag.default_variant;
    return {
      variant: assigned,
      variantBucket,
    };
  }

  private unitId(context: EvalContext, bucketBy: string): string {
    if (!isIdentifier(bucketBy)) return '';
    const actual = this.resolveAttribute(bucketBy, context);
    return actual.exists && isIdentifier(actual.value) ? actual.value : '';
  }

  private resolveAttribute(attribute: string, context: EvalContext): ResolvedAttribute {
    // Presence contract — canonical, must match services/config/app/flags/
    // evaluator.py and sdk/python/apdl/flags/evaluator.py byte-for-byte: an
    // attribute is *present* only when its value is non-null. A null/undefined
    // value (an explicit `user_id: null` identity or a `null` trait) is ABSENT,
    // like a missing key — it is never stringified into a value comparison, so
    // the three evaluators stay in lockstep (a null would otherwise compare
    // against `String(null)` = "null" here vs `str(None)` = "None" in Python).
    // Falsy non-null values (`''`, `0`, `false`) stay present. See parity.json.
    if (attribute === 'user_id') {
      if (Object.prototype.hasOwnProperty.call(context, 'user_id')) {
        const value = context.user_id;
        return value != null
          ? { exists: true, value }
          : { exists: false, value: null };
      }
      return { exists: false, value: null };
    }

    if (attribute === 'anonymous_id') {
      if (Object.prototype.hasOwnProperty.call(context, 'anonymous_id')) {
        const value = context.anonymous_id;
        return value != null
          ? { exists: true, value }
          : { exists: false, value: null };
      }
      return { exists: false, value: null };
    }

    const attributes = context.attributes;
    if (
      attributes === null
      || typeof attributes !== 'object'
      || Array.isArray(attributes)
      || !Object.prototype.hasOwnProperty.call(attributes, attribute)
    ) {
      return { exists: false, value: null };
    }
    const value = attributes[attribute];
    return value != null
      ? { exists: true, value }
      : { exists: false, value: null };
  }
}

export function assignWeightedVariant(
  variants: VariantConfig[],
  variantBucket: number
): string | null {
  const totalWeight = variants.reduce((total, variant) => total + variant.weight, 0);
  if (totalWeight <= 0) {
    return null;
  }

  const target = (variantBucket / 100) * totalWeight;
  let cumulative = 0;
  let lastPositiveVariant: string | null = null;

  for (const variant of variants) {
    if (variant.weight <= 0) {
      continue;
    }

    lastPositiveVariant = variant.key;
    cumulative += variant.weight;
    if (target < cumulative) {
      return variant.key;
    }
  }

  return lastPositiveVariant;
}
