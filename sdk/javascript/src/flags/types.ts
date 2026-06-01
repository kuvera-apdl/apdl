export interface GateConfig {
  key: string;
  enabled: boolean;
  default_value: boolean;
  salt: string;
  rules: GateRule[];
  fallthrough: FallthroughConfig;
  version: number;
}

export interface GateRule {
  id: string;
  name?: string;
  conditions: GateCondition[];
  rollout: RolloutConfig;
}

export interface GateCondition {
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
  | 'regex'
  | 'in'
  | 'not_in'
  | 'exists'
  | 'not_exists';

export interface RolloutConfig {
  percentage: number;
  bucket_by: string;
}

export interface FallthroughConfig {
  value: boolean;
  rollout: RolloutConfig;
}

export type GateConfigSource =
  | 'memory'
  | 'initial_fetch'
  | 'sse'
  | 'local_storage';

export type GateEvaluationReason =
  | 'not_found'
  | 'invalid_config'
  | 'disabled'
  | 'error'
  | 'rule_match'
  | 'rule_rollout'
  | 'fallthrough'
  | 'fallthrough_rollout';

export interface GateEvaluationResult {
  key: string;
  value: boolean;
  reason: GateEvaluationReason;
  rule_id: string;
  bucket: number | null;
  rollout_percentage: number | null;
  bucket_by: string;
  config_version: number;
  source: GateConfigSource | 'none';
}

export interface EvalContext {
  user_id?: string;
  anonymous_id: string;
  attributes?: Record<string, unknown>;
}

export type FlagConfig = GateConfig;
export type TargetingRule = GateRule;
export type Condition = GateCondition;
export type FlagResult = GateEvaluationResult;
