import type {
  ConditionOperator,
  FallthroughConfig,
  GateCondition,
  GateConfig,
  GateRule,
  RolloutConfig,
  VariantConfig,
} from './types';

type RawRecord = Record<string, unknown>;

export interface FlagConfigParseResult {
  flags: GateConfig[];
  invalid_keys: string[];
}

const CONDITION_OPERATORS = new Set<ConditionOperator>([
  'equals',
  'not_equals',
  'gt',
  'gte',
  'lt',
  'lte',
  'contains',
  'not_contains',
  'starts_with',
  'ends_with',
  'regex',
  'in',
  'not_in',
  'exists',
  'not_exists',
]);

const GATE_KEYS = new Set([
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
const REJECTED_GATE_KEYS = new Set([
  'default_value',
  'defaultVariant',
  'variant_type',
  'variants_json',
  'rollout_percentage',
  'targeting_rules',
]);

export function extractFlagConfigs(input: unknown): GateConfig[] {
  return parseFlagConfigs(input) ?? [];
}

export function parseFlagConfigs(input: unknown): GateConfig[] | null {
  const result = parseFlagConfigResult(input);
  if (!result || result.invalid_keys.length > 0) {
    return null;
  }

  return result.flags;
}

export function parseFlagConfigResult(input: unknown): FlagConfigParseResult | null {
  const candidates = extractCandidates(input);
  if (!candidates) {
    return null;
  }

  const flags: GateConfig[] = [];
  const invalidKeys: string[] = [];

  for (const candidate of candidates) {
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
    flags,
    invalid_keys: invalidKeys,
  };
}

export function extractFlagConfig(input: unknown): GateConfig | null {
  return isFlagConfig(input) ? input : null;
}

export function extractInvalidFlagKey(input: unknown): string | null {
  if (
    isRecord(input)
    && typeof input.key === 'string'
    && input.key.length > 0
    && !isFlagConfig(input)
    && (hasAnyKey(input, REJECTED_GATE_KEYS) || hasAllKeys(input, GATE_KEYS))
  ) {
    return input.key;
  }

  return null;
}

export function isFlagConfig(input: unknown): input is GateConfig {
  return isRecord(input)
    && hasOnlyKeys(input, GATE_KEYS)
    && typeof input.key === 'string'
    && input.key.length > 0
    && typeof input.enabled === 'boolean'
    && typeof input.default_variant === 'string'
    && input.default_variant.length > 0
    && Array.isArray(input.variants)
    && isVariantList(input.variants, input.default_variant)
    && typeof input.salt === 'string'
    && Array.isArray(input.rules)
    && input.rules.every(isGateRule)
    && isFallthroughConfig(input.fallthrough)
    && typeof input.version === 'number'
    && Number.isInteger(input.version)
    && input.version >= 1;
}

function extractCandidates(input: unknown): unknown[] | null {
  if (
    isRecord(input)
    && hasOnlyKeys(input, COLLECTION_KEYS)
    && Array.isArray(input.flags)
    && input.schema_version === 2
    && typeof input.project_id === 'string'
    && input.project_id.length > 0
  ) {
    return input.flags;
  }

  return null;
}

function isGateRule(input: unknown): input is GateRule {
  return isRecord(input)
    && hasOnlyKeys(input, RULE_KEYS)
    && typeof input.id === 'string'
    && input.id.length > 0
    && typeof input.name === 'string'
    && Array.isArray(input.conditions)
    && input.conditions.every(isGateCondition)
    && isRolloutConfig(input.rollout);
}

function isGateCondition(input: unknown): input is GateCondition {
  if (
    !isRecord(input)
    || !hasOnlyKeys(input, CONDITION_KEYS)
    || typeof input.attribute !== 'string'
    || input.attribute.length === 0
    || typeof input.operator !== 'string'
    || !CONDITION_OPERATORS.has(input.operator as ConditionOperator)
  ) {
    return false;
  }

  const hasValue = Object.prototype.hasOwnProperty.call(input, 'value');
  if (input.operator === 'exists' || input.operator === 'not_exists') {
    return !hasValue;
  }

  return hasValue && input.value !== null && input.value !== undefined;
}

function isRolloutConfig(input: unknown): input is RolloutConfig {
  return isRecord(input)
    && hasOnlyKeys(input, ROLLOUT_KEYS)
    && typeof input.percentage === 'number'
    && Number.isFinite(input.percentage)
    && input.percentage >= 0.0
    && input.percentage <= 100.0
    && typeof input.bucket_by === 'string'
    && input.bucket_by.length > 0;
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
      || typeof variant.key !== 'string'
      || variant.key.length === 0
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
