import type {
  Condition,
  EvalContext,
  FlagResult,
  TargetingRule,
  Variant,
} from './types';
import { FlagCache } from './cache';
import { hashBucket, isInRollout } from './hash';

/**
 * Feature flag evaluation engine matching the config service contract.
 */
export class FlagEvaluator {
  private cache: FlagCache;

  constructor(cache: FlagCache) {
    this.cache = cache;
  }

  evaluate(key: string, context: EvalContext): FlagResult {
    const flag = this.cache.get(key);

    if (!flag) {
      return {
        key,
        enabled: false,
        value: '',
        variant: '',
        reason: 'not_found',
      };
    }

    const defaultValue = flag.default_value ?? 'false';

    if (!flag.enabled) {
      return {
        key: flag.key,
        enabled: false,
        value: defaultValue,
        variant: '',
        reason: 'disabled',
      };
    }

    const userKey = context.user_id || context.anonymous_id || '';
    if (!userKey) {
      return {
        key: flag.key,
        enabled: false,
        value: defaultValue,
        variant: '',
        reason: 'error',
      };
    }

    if (!this.matchesRules(flag.rules, context)) {
      return {
        key: flag.key,
        enabled: false,
        value: defaultValue,
        variant: '',
        reason: 'rule_no_match',
      };
    }

    if (!isInRollout(flag.key, userKey, flag.rollout_percentage ?? 100.0)) {
      return {
        key: flag.key,
        enabled: false,
        value: defaultValue,
        variant: '',
        reason: 'rollout',
      };
    }

    if (flag.variant_type === 'boolean') {
      return {
        key: flag.key,
        enabled: true,
        value: 'true',
        variant: '',
        payload: flag.payload,
        reason: 'rule_match',
      };
    }

    if (flag.variants.length > 0) {
      const variant = this.selectVariant(flag.key, userKey, flag.variants);
      if (variant) {
        return {
          key: flag.key,
          enabled: true,
          value: variant.value,
          variant: variant.value,
          payload: variant.payload ?? flag.payload,
          reason: 'rule_match',
        };
      }
    }

    return {
      key: flag.key,
      enabled: true,
      value: defaultValue,
      variant: '',
      payload: flag.payload,
      reason: 'default',
    };
  }

  private matchesRules(rules: TargetingRule[] | undefined, context: EvalContext): boolean {
    if (!Array.isArray(rules) || rules.length === 0) {
      return true;
    }

    for (const rule of rules) {
      if (!rule || typeof rule !== 'object') {
        continue;
      }

      if (Array.isArray(rule.conditions)) {
        const allMatch = rule.conditions.every((condition) =>
          this.matchesCondition(condition, context)
        );
        if (allMatch) {
          return true;
        }
      } else if (typeof rule.attribute === 'string' && typeof rule.operator === 'string') {
        if (this.matchesCondition(rule as Condition, context)) {
          return true;
        }
      }
    }

    return false;
  }

  private matchesCondition(condition: Condition, context: EvalContext): boolean {
    if (
      !condition ||
      typeof condition.attribute !== 'string' ||
      typeof condition.operator !== 'string'
    ) {
      return false;
    }

    const actual = this.resolveAttribute(condition.attribute, condition.operator, context);
    if (!actual.exists) {
      return actual.value === true;
    }

    const op = condition.operator;

    if (!Object.prototype.hasOwnProperty.call(condition, 'value')) {
      if (op === 'exists' || op === 'is_set') {
        return Boolean(actual.value);
      }
      if (op === 'not_exists' || op === 'is_not_set') {
        return !actual.value;
      }
      return false;
    }

    const actualValue = String(actual.value);
    const expected = condition.value;

    if (op === 'equals' || op === 'eq' || op === 'is') {
      if (typeof expected === 'string') {
        return actualValue === expected;
      }
      if (typeof expected === 'boolean') {
        return actualValue === (expected ? 'true' : 'false');
      }
      if (typeof expected === 'number') {
        return actualValue === String(expected);
      }
      return false;
    }

    if (op === 'not_equals' || op === 'neq' || op === 'is_not') {
      if (typeof expected === 'string') {
        return actualValue !== expected;
      }
      if (typeof expected === 'boolean') {
        return actualValue !== (expected ? 'true' : 'false');
      }
      return true;
    }

    if (op === 'contains') {
      return typeof expected === 'string' && actualValue.includes(expected);
    }

    if (op === 'not_contains') {
      return typeof expected === 'string' ? !actualValue.includes(expected) : true;
    }

    if (op === 'starts_with') {
      return typeof expected === 'string' && actualValue.startsWith(expected);
    }

    if (op === 'ends_with') {
      return typeof expected === 'string' && actualValue.endsWith(expected);
    }

    if (op === 'in') {
      return Array.isArray(expected)
        && expected.some((item) => typeof item === 'string' && actualValue === item);
    }

    if (op === 'not_in') {
      if (!Array.isArray(expected)) {
        return true;
      }
      return !expected.some((item) => typeof item === 'string' && actualValue === item);
    }

    if (op === 'gt' || op === 'gte' || op === 'lt' || op === 'lte') {
      const actualNumber = Number(actualValue);
      const expectedNumber = typeof expected === 'number'
        ? expected
        : typeof expected === 'string'
          ? Number(expected)
          : Number.NaN;

      if (!Number.isFinite(actualNumber) || !Number.isFinite(expectedNumber)) {
        return false;
      }

      if (op === 'gt') return actualNumber > expectedNumber;
      if (op === 'gte') return actualNumber >= expectedNumber;
      if (op === 'lt') return actualNumber < expectedNumber;
      return actualNumber <= expectedNumber;
    }

    if (op === 'regex' || op === 'matches') {
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

  private resolveAttribute(
    attribute: string,
    operator: string,
    context: EvalContext
  ): { exists: true; value: unknown } | { exists: false; value: boolean } {
    if (attribute === 'user_id' || attribute === 'userId') {
      return { exists: true, value: context.user_id ?? '' };
    }

    if (attribute === 'anonymous_id' || attribute === 'anonymousId') {
      return { exists: true, value: context.anonymous_id ?? '' };
    }

    const attributes = context.attributes ?? {};
    if (Object.prototype.hasOwnProperty.call(attributes, attribute)) {
      return { exists: true, value: attributes[attribute] };
    }

    if (operator === 'not_exists' || operator === 'is_not_set') {
      return { exists: false, value: true };
    }

    return { exists: false, value: false };
  }

  private selectVariant(
    flagKey: string,
    userKey: string,
    variants: Variant[]
  ): { value: string; payload?: unknown } | null {
    if (variants.length === 0) {
      return null;
    }

    let totalWeight = 0.0;
    for (const variant of variants) {
      if (!this.isVariantRecord(variant)) {
        continue;
      }
      if (typeof variant.weight === 'number') {
        totalWeight += variant.weight;
      }
    }

    if (totalWeight <= 0.0) {
      totalWeight = variants.length;
    }

    const bucket = (hashBucket(`${flagKey}:variant`, userKey) / 0xffffffff) * totalWeight;
    let cumulative = 0.0;

    for (let index = 0; index < variants.length; index++) {
      const variant = variants[index];
      if (!this.isVariantRecord(variant)) {
        continue;
      }

      const weight = typeof variant.weight === 'number' ? variant.weight : 1.0;
      cumulative += weight;

      if (bucket < cumulative) {
        return {
          value: this.variantValue(variant, index),
          payload: variant.payload,
        };
      }
    }

    return null;
  }

  private isVariantRecord(variant: unknown): variant is Variant {
    return typeof variant === 'object' && variant !== null && !Array.isArray(variant);
  }

  private variantValue(variant: Variant, index: number): string {
    if (Object.prototype.hasOwnProperty.call(variant, 'value')) {
      if (typeof variant.value === 'string') {
        return variant.value;
      }
      return JSON.stringify(variant.value) ?? '';
    }

    if (typeof variant.key === 'string') {
      return variant.key;
    }

    return String(index);
  }
}
