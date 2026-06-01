import type {
  ConditionOperator,
  FallthroughConfig,
  GateCondition,
  GateConfig,
  GateRule,
  RolloutConfig,
} from './types';

type RawRecord = Record<string, unknown>;

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
  'default_value',
  'salt',
  'rules',
  'fallthrough',
  'version',
]);

const RULE_KEYS = new Set(['id', 'name', 'conditions', 'rollout']);
const CONDITION_KEYS = new Set(['attribute', 'operator', 'value']);
const ROLLOUT_KEYS = new Set(['percentage', 'bucket_by']);
const FALLTHROUGH_KEYS = new Set(['value', 'rollout']);
const COLLECTION_KEYS = new Set(['schema_version', 'project_id', 'flags']);

export function extractFlagConfigs(input: unknown): GateConfig[] {
  return parseFlagConfigs(input) ?? [];
}

export function parseFlagConfigs(input: unknown): GateConfig[] | null {
  const candidates = extractCandidates(input);
  if (!candidates) {
    return null;
  }

  if (!candidates.every(isFlagConfig)) {
    return null;
  }

  return candidates;
}

export function extractFlagConfig(input: unknown): GateConfig | null {
  return isFlagConfig(input) ? input : null;
}

export function isFlagConfig(input: unknown): input is GateConfig {
  return isRecord(input)
    && hasOnlyKeys(input, GATE_KEYS)
    && typeof input.key === 'string'
    && input.key.length > 0
    && typeof input.enabled === 'boolean'
    && typeof input.default_value === 'boolean'
    && typeof input.salt === 'string'
    && Array.isArray(input.rules)
    && input.rules.every(isGateRule)
    && isFallthroughConfig(input.fallthrough)
    && typeof input.version === 'number'
    && Number.isInteger(input.version)
    && input.version >= 0;
}

function extractCandidates(input: unknown): unknown[] | null {
  if (Array.isArray(input)) {
    return input;
  }

  if (
    isRecord(input)
    && hasOnlyKeys(input, COLLECTION_KEYS)
    && Array.isArray(input.flags)
    && (!Object.prototype.hasOwnProperty.call(input, 'schema_version')
      || input.schema_version === 1)
    && (!Object.prototype.hasOwnProperty.call(input, 'project_id')
      || typeof input.project_id === 'string')
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
    && (!Object.prototype.hasOwnProperty.call(input, 'name') || typeof input.name === 'string')
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
    && typeof input.value === 'boolean'
    && isRolloutConfig(input.rollout);
}

function hasOnlyKeys(input: RawRecord, allowed: Set<string>): boolean {
  return Object.keys(input).every((key) => allowed.has(key));
}

function isRecord(input: unknown): input is RawRecord {
  return typeof input === 'object' && input !== null && !Array.isArray(input);
}
