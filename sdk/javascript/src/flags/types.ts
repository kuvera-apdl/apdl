export interface FlagConfig {
  key: string;
  enabled: boolean;
  variant_type: 'boolean' | 'string' | string;
  default_value: string;
  rollout_percentage: number; // 0-100
  rules: TargetingRule[];
  variants: Variant[];
  description?: string;
  updated_at?: string;
  payload?: unknown;
}

export interface TargetingRule {
  conditions?: Condition[]; // AND logic within a rule
  attribute?: string;
  operator?: ConditionOperator;
  value?: unknown;
}

export interface Condition {
  attribute: string;
  operator: ConditionOperator;
  value?: unknown;
}

export type ConditionOperator =
  | 'equals'
  | 'eq'
  | 'is'
  | 'not_equals'
  | 'neq'
  | 'is_not'
  | 'gt'
  | 'gte'
  | 'lt'
  | 'lte'
  | 'contains'
  | 'not_contains'
  | 'starts_with'
  | 'ends_with'
  | 'regex'
  | 'matches'
  | 'in'
  | 'not_in'
  | 'exists'
  | 'is_set'
  | 'not_exists'
  | 'is_not_set';

export interface Variant {
  key?: string;
  value?: unknown;
  weight?: number; // percentage-style weight; all weights are relative
  payload?: unknown;
}

export interface FlagResult {
  key: string;
  enabled: boolean;
  value: string;
  variant: string;
  payload?: unknown;
  reason:
    | 'not_found'
    | 'disabled'
    | 'error'
    | 'rule_no_match'
    | 'rule_match'
    | 'rollout'
    | 'default';
}

export interface EvalContext {
  user_id?: string;
  anonymous_id: string;
  attributes?: Record<string, unknown>;
  groups?: Record<string, string>;
}
