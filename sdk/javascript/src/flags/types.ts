export interface FlagConfig {
  key: string;
  enabled: boolean;
  default_variant: string;
  variants: VariantConfig[];
  salt: string;
  rules: FlagRule[];
  fallthrough: FallthroughConfig;
  version: number;
}

export interface VariantConfig {
  key: string;
  weight: number;
}

export interface FlagRule {
  id: string;
  name: string;
  conditions: FlagCondition[];
  rollout: RolloutConfig;
}

export interface FlagCondition {
  attribute: string;
  operator: ConditionOperator;
  value?: unknown;
}

export type ConditionOperator =
  | 'equals'
  | 'not_equals'
  | 'gt'
  | 'gte'
  | 'lt'
  | 'lte'
  | 'contains'
  | 'not_contains'
  | 'starts_with'
  | 'ends_with'
  | 'in'
  | 'not_in'
  | 'exists'
  | 'not_exists';

export interface RolloutConfig {
  percentage: number;
  bucket_by: string;
}

export interface FallthroughConfig {
  rollout: RolloutConfig;
}

export type FlagConfigSource =
  | 'memory'
  | 'initial_fetch'
  | 'sse'
  | 'local_storage'
  | 'server';

export type FlagEvaluationReason =
  | 'not_found'
  | 'invalid_config'
  | 'consent_denied'
  | 'disabled'
  | 'error'
  | 'rule_match'
  | 'rule_rollout'
  | 'fallthrough'
  | 'fallthrough_rollout';

export interface FlagEvaluationResult {
  key: string;
  variant: string | null;
  reason: FlagEvaluationReason;
  rule_id: string | null;
  rollout_bucket: number | null;
  variant_bucket: number | null;
  rollout_percentage: number | null;
  bucket_by: string | null;
  config_version: number | null;
  source: FlagConfigSource | null;
}

export interface FlagEvaluationOptions {
  page?: string;
  component?: string;
}

export interface EvalContext {
  user_id?: string | null;
  anonymous_id?: string | null;
  attributes?: Record<string, unknown>;
}
