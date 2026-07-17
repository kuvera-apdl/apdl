import type {
  FallthroughConfig,
  FlagCondition,
  FlagConfig,
  FlagRule,
  RolloutConfig,
  VariantConfig,
} from './types';
import {
  MAX_CONDITIONS_PER_RULE,
  MAX_RULES,
  SUPPORTED_OPERATORS,
  isBoundedString,
  isConditionValueValid,
  isIdentifier,
} from './targeting-contract';

type RawRecord = Record<string, unknown>;

export interface FlagConfigParseResult {
  project_id: string;
  flags: FlagConfig[];
  invalid_keys: string[];
}

interface FlagConfigCandidates {
  project_id: string;
  flags: unknown[];
}

const FLAG_KEYS = new Set([
  'key',
  'enabled',
  'default_variant',
  'variants',
  'salt',
  'rules',
  'fallthrough',
  'version',
]);

const RULE_KEYS = new Set(['id', 'name', 'conditions', 'rollout']);
const CONDITION_KEYS = new Set(['attribute', 'operator', 'value']);
const ROLLOUT_KEYS = new Set(['percentage', 'bucket_by']);
const FALLTHROUGH_KEYS = new Set(['rollout']);
const VARIANT_KEYS = new Set(['key', 'weight']);
const COLLECTION_KEYS = new Set(['schema_version', 'project_id', 'flags']);
const REJECTED_FLAG_KEYS = new Set([
  'default_value',
  'defaultVariant',
  'variant_type',
  'variants_json',
  'rollout_percentage',
  'targeting_rules',
]);

export function extractFlagConfigs(input: unknown): FlagConfig[] {
  return parseFlagConfigs(input) ?? [];
}

export function parseFlagConfigs(input: unknown): FlagConfig[] | null {
  const result = parseFlagConfigResult(input);
  if (!result || result.invalid_keys.length > 0) {
    return null;
  }

  return result.flags;
}

export function parseFlagConfigResult(input: unknown): FlagConfigParseResult | null {
  const envelope = extractCandidates(input);
  if (!envelope) {
    return null;
  }

  const flags: FlagConfig[] = [];
  const invalidKeys: string[] = [];

  for (const candidate of envelope.flags) {
    if (isFlagConfig(candidate)) {
      flags.push(candidate);
      continue;
    }

    const invalidKey = extractInvalidFlagKey(candidate);
    if (!invalidKey) {
      return null;
    }
    invalidKeys.push(invalidKey);
  }

  return {
    project_id: envelope.project_id,
    flags,
    invalid_keys: invalidKeys,
  };
}

export function extractFlagConfig(input: unknown): FlagConfig | null {
  return isFlagConfig(input) ? input : null;
}

export function extractInvalidFlagKey(input: unknown): string | null {
  if (
    isRecord(input)
    && isIdentifier(input.key)
    && !isFlagConfig(input)
    && (hasAnyKey(input, REJECTED_FLAG_KEYS) || hasAllKeys(input, FLAG_KEYS))
  ) {
    return input.key;
  }

  return null;
}

export function isFlagConfig(input: unknown): input is FlagConfig {
  return isRecord(input)
    && hasOnlyKeys(input, FLAG_KEYS)
    && isIdentifier(input.key)
    && typeof input.enabled === 'boolean'
    && isIdentifier(input.default_variant)
    && Array.isArray(input.variants)
    && isVariantList(input.variants, input.default_variant)
    && isBoundedString(input.salt)
    && Array.isArray(input.rules)
    && input.rules.length <= MAX_RULES
    && input.rules.every(isFlagRule)
    && isFallthroughConfig(input.fallthrough)
    && typeof input.version === 'number'
    && Number.isInteger(input.version)
    && input.version >= 1;
}

function extractCandidates(input: unknown): FlagConfigCandidates | null {
  if (
    isRecord(input)
    && hasOnlyKeys(input, COLLECTION_KEYS)
    && Array.isArray(input.flags)
    && input.schema_version === 2
    && isIdentifier(input.project_id)
  ) {
    return {
      project_id: input.project_id,
      flags: input.flags,
    };
  }

  return null;
}

function isFlagRule(input: unknown): input is FlagRule {
  return isRecord(input)
    && hasOnlyKeys(input, RULE_KEYS)
    && isIdentifier(input.id)
    && isBoundedString(input.name)
    && Array.isArray(input.conditions)
    && input.conditions.length <= MAX_CONDITIONS_PER_RULE
    && input.conditions.every(isFlagCondition)
    && isRolloutConfig(input.rollout);
}

function isFlagCondition(input: unknown): input is FlagCondition {
  if (
    !isRecord(input)
    || !hasOnlyKeys(input, CONDITION_KEYS)
    || !isIdentifier(input.attribute)
    || typeof input.operator !== 'string'
    || !SUPPORTED_OPERATORS.has(input.operator)
  ) {
    return false;
  }

  const hasValue = Object.prototype.hasOwnProperty.call(input, 'value');
  if (input.operator === 'exists' || input.operator === 'not_exists') {
    return !hasValue;
  }

  return hasValue
    && input.value !== null
    && input.value !== undefined
    && isConditionValueValid(input.operator, input.value);
}

function isRolloutConfig(input: unknown): input is RolloutConfig {
  return isRecord(input)
    && hasOnlyKeys(input, ROLLOUT_KEYS)
    && typeof input.percentage === 'number'
    && Number.isFinite(input.percentage)
    && input.percentage >= 0.0
    && input.percentage <= 100.0
    && isIdentifier(input.bucket_by);
}

function isFallthroughConfig(input: unknown): input is FallthroughConfig {
  return isRecord(input)
    && hasOnlyKeys(input, FALLTHROUGH_KEYS)
    && isRolloutConfig(input.rollout);
}

function isVariantList(input: unknown[], defaultVariant: string): input is VariantConfig[] {
  if (input.length === 0) {
    return false;
  }

  const keys = new Set<string>();
  let totalWeight = 0;

  for (const variant of input) {
    const weight = isRecord(variant) ? variant.weight : undefined;
    if (
      !isRecord(variant)
      || !hasOnlyKeys(variant, VARIANT_KEYS)
      || !isIdentifier(variant.key)
      || !Number.isInteger(weight)
      || (weight as number) < 0
    ) {
      return false;
    }

    if (keys.has(variant.key)) {
      return false;
    }

    keys.add(variant.key);
    totalWeight += weight as number;
  }

  return totalWeight > 0 && keys.has(defaultVariant);
}

function hasOnlyKeys(input: RawRecord, allowed: Set<string>): boolean {
  return Object.keys(input).every((key) => allowed.has(key));
}

function hasAnyKey(input: RawRecord, keys: Set<string>): boolean {
  return Object.keys(input).some((key) => keys.has(key));
}

function hasAllKeys(input: RawRecord, keys: Set<string>): boolean {
  return Array.from(keys).every((key) => Object.prototype.hasOwnProperty.call(input, key));
}

function isRecord(input: unknown): input is RawRecord {
  return typeof input === 'object' && input !== null && !Array.isArray(input);
}
