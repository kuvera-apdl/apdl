import type { FlagConfig } from './types';

type RawRecord = Record<string, unknown>;

export function extractFlagConfigs(input: unknown): FlagConfig[] {
  const candidates = Array.isArray(input)
    ? input
    : isRecord(input) && Array.isArray(input.flags)
      ? input.flags
      : [];

  return candidates.filter(isFlagConfig);
}

export function extractFlagConfig(input: unknown): FlagConfig | null {
  return isFlagConfig(input) ? input : null;
}

export function isFlagConfig(input: unknown): input is FlagConfig {
  return isRecord(input)
    && typeof input.key === 'string'
    && typeof input.enabled === 'boolean'
    && typeof input.variant_type === 'string'
    && typeof input.default_value === 'string'
    && typeof input.rollout_percentage === 'number'
    && Array.isArray(input.rules)
    && Array.isArray(input.variants);
}

function isRecord(input: unknown): input is RawRecord {
  return typeof input === 'object' && input !== null;
}
