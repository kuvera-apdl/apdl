import type {
  EvalContext,
  GateCondition,
  GateConfig,
  GateEvaluationResult,
  GateRule,
  RolloutConfig,
} from './types';
import { FlagCache } from './cache';
import { percentageBucket } from './hash';

type ResolvedAttribute =
  | { exists: true; value: unknown }
  | { exists: false; value: null };

interface RolloutResult {
  passed: boolean;
  bucket: number | null;
  percentage: number;
  bucketBy: string;
}

/**
 * Canonical local feature gate evaluator matching the config service contract.
 */
export class FlagEvaluator {
  private cache: FlagCache;

  constructor(cache: FlagCache) {
    this.cache = cache;
  }

  evaluate(key: string, context: EvalContext): GateEvaluationResult {
    const flag = this.cache.get(key);

    if (!flag) {
      return {
        key,
        value: false,
        reason: 'not_found',
        rule_id: '',
        bucket: null,
        rollout_percentage: null,
        bucket_by: '',
        config_version: 0,
        source: 'none',
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
      if (rollout.bucket === null) {
        return {
          ...result,
          reason: 'error',
          rule_id: rule.id,
          bucket: null,
          rollout_percentage: rollout.percentage,
          bucket_by: rollout.bucketBy,
        };
      }

      return {
        ...result,
        value: rollout.passed ? true : flag.default_value,
        reason: rollout.passed ? 'rule_match' : 'rule_rollout',
        rule_id: rule.id,
        bucket: rollout.bucket,
        rollout_percentage: rollout.percentage,
        bucket_by: rollout.bucketBy,
      };
    }

    const rollout = this.applyRollout(flag, flag.fallthrough.rollout, context);
    if (rollout.bucket === null) {
      return {
        ...result,
        reason: 'error',
        bucket: null,
        rollout_percentage: rollout.percentage,
        bucket_by: rollout.bucketBy,
      };
    }

    return {
      ...result,
      value: rollout.passed ? flag.fallthrough.value : flag.default_value,
      reason: rollout.passed ? 'fallthrough' : 'fallthrough_rollout',
      bucket: rollout.bucket,
      rollout_percentage: rollout.percentage,
      bucket_by: rollout.bucketBy,
    };
  }

  private baseResult(flag: GateConfig): GateEvaluationResult {
    return {
      key: flag.key,
      value: flag.default_value,
      reason: 'error',
      rule_id: '',
      bucket: null,
      rollout_percentage: null,
      bucket_by: '',
      config_version: flag.version,
      source: this.cache.getSource(flag.key),
    };
  }

  private matchesRule(rule: GateRule, context: EvalContext): boolean {
    return rule.conditions.every((condition) => this.matchesCondition(condition, context));
  }

  private matchesCondition(condition: GateCondition, context: EvalContext): boolean {
    const { attribute, operator } = condition;
    const actual = this.resolveAttribute(attribute, context);

    if (operator === 'exists') {
      return actual.exists && Boolean(actual.value);
    }
    if (operator === 'not_exists') {
      return !actual.exists || !actual.value;
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
    flag: GateConfig,
    rollout: RolloutConfig,
    context: EvalContext
  ): RolloutResult {
    const unitId = this.unitId(context, rollout.bucket_by);
    if (!unitId) {
      return {
        passed: false,
        bucket: null,
        percentage: rollout.percentage,
        bucketBy: rollout.bucket_by,
      };
    }

    const bucket = percentageBucket(flag.key, flag.salt, unitId);
    return {
      passed: bucket < rollout.percentage,
      bucket,
      percentage: rollout.percentage,
      bucketBy: rollout.bucket_by,
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
      return { exists: true, value: context.user_id ?? '' };
    }

    if (attribute === 'anonymous_id') {
      return { exists: true, value: context.anonymous_id ?? '' };
    }

    const attributes = context.attributes ?? {};
    if (Object.prototype.hasOwnProperty.call(attributes, attribute)) {
      return { exists: true, value: attributes[attribute] };
    }

    return { exists: false, value: null };
  }
}
