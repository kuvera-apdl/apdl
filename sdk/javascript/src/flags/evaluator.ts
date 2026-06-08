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
    return rule.conditions.every((condition) => this.matchesCondition(condition, context));
  }

  private matchesCondition(condition: FlagCondition, context: EvalContext): boolean {
    const { attribute, operator } = condition;
    const actual = this.resolveAttribute(attribute, context);

    if (operator === 'exists') {
      return actual.exists && actual.value !== null && actual.value !== undefined;
    }
    if (operator === 'not_exists') {
      return !actual.exists || actual.value === null || actual.value === undefined;
    }
    if (!actual.exists || !Object.prototype.hasOwnProperty.call(condition, 'value')) {
      return false;
    }

    const expected = condition.value;
    const actualValue = String(actual.value);

    if (operator === 'equals') {
      return actualValue === String(expected);
    }
    if (operator === 'not_equals') {
      return actualValue !== String(expected);
    }
    if (operator === 'contains') {
      return typeof expected === 'string' && actualValue.includes(expected);
    }
    if (operator === 'not_contains') {
      return typeof expected === 'string' && !actualValue.includes(expected);
    }
    if (operator === 'starts_with') {
      return typeof expected === 'string' && actualValue.startsWith(expected);
    }
    if (operator === 'ends_with') {
      return typeof expected === 'string' && actualValue.endsWith(expected);
    }
    if (operator === 'in') {
      return Array.isArray(expected) && expected.some((item) => Object.is(item, actual.value));
    }
    if (operator === 'not_in') {
      return !Array.isArray(expected) || !expected.some((item) => Object.is(item, actual.value));
    }
    if (operator === 'gt' || operator === 'gte' || operator === 'lt' || operator === 'lte') {
      const actualNumber = Number(actual.value);
      const expectedNumber = Number(expected);

      if (!Number.isFinite(actualNumber) || !Number.isFinite(expectedNumber)) {
        return false;
      }

      if (operator === 'gt') return actualNumber > expectedNumber;
      if (operator === 'gte') return actualNumber >= expectedNumber;
      if (operator === 'lt') return actualNumber < expectedNumber;
      return actualNumber <= expectedNumber;
    }
    if (operator === 'regex') {
      if (typeof expected !== 'string') {
        return false;
      }
      try {
        return new RegExp(expected).test(actualValue);
      } catch {
        return false;
      }
    }

    return false;
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
    const actual = this.resolveAttribute(bucketBy, context);
    if (actual.exists && actual.value !== null && actual.value !== undefined) {
      return String(actual.value);
    }
    return '';
  }

  private resolveAttribute(attribute: string, context: EvalContext): ResolvedAttribute {
    if (attribute === 'user_id') {
      if (Object.prototype.hasOwnProperty.call(context, 'user_id')) {
        return { exists: true, value: context.user_id };
      }
      return { exists: false, value: null };
    }

    if (attribute === 'anonymous_id') {
      if (Object.prototype.hasOwnProperty.call(context, 'anonymous_id')) {
        return { exists: true, value: context.anonymous_id };
      }
      return { exists: false, value: null };
    }

    const attributes = context.attributes ?? {};
    if (Object.prototype.hasOwnProperty.call(attributes, attribute)) {
      return { exists: true, value: attributes[attribute] };
    }

    return { exists: false, value: null };
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
